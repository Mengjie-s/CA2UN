import math
import warnings

import numbers

import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.nn.init import _calculate_fan_in_and_fan_out

from einops import rearrange
from torch import einsum

from box import Box
from fvcore.nn import FlopCountAnalysis

from csi.data import shift_batch, shift_back_batch, gen_meas_torch_batch
from timm.models.layers import DropPath, trunc_normal_, drop_path

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    def norm_cdf(x):
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)
    with torch.no_grad():
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)
        tensor.uniform_(2 * l - 1, 2 * u - 1)
        tensor.erfinv_()
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)



class GELU(nn.Module):
    def forward(self, x):
        return F.gelu(x)


class FeedForward(nn.Module):
    def __init__(self, dim, mult=4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(dim, dim * mult, 1, 1, bias=False),
            GELU(),
            nn.Conv2d(dim * mult, dim * mult, 3, 1, 1, bias=False, groups=dim * mult),
            GELU(),
            nn.Conv2d(dim * mult, dim, 1, 1, bias=False),
        )

    def forward(self, x):
        """
        x: [b, h, w, c]
        return out: [b, h, w, c]
        """
        out = self.net(x)
        return out
    

## Gated-Dconv Feed-Forward Network (GDFN)
class Gated_Dconv_FeedForward(nn.Module):
    def __init__(self, 
                 dim, 
                 ffn_expansion_factor = 2.66
    ):
        super(Gated_Dconv_FeedForward, self).__init__()

        hidden_features = int(dim*ffn_expansion_factor)

        self.project_in = nn.Conv2d(dim, hidden_features*2, kernel_size=1, bias=False)

        self.dwconv = nn.Conv2d(hidden_features*2, hidden_features*2, kernel_size=3, stride=1, padding=1, groups=hidden_features*2, bias=True)

        self.act_fn = nn.GELU()

        self.project_out = nn.Conv2d(hidden_features, dim, kernel_size=1, bias=False)

    def forward(self, x):
        """
        x: [b, c, h, w]
        return out: [b, c, h, w]
        """
        x = self.project_in(x)
        x1, x2 = self.dwconv(x).chunk(2, dim=1)
        x = self.act_fn(x1) * x2
        x = self.project_out(x)
        return x
    

def FFN_FN(
    cfg,
    ffn_name,
    dim
):
    if ffn_name == "Gated_Dconv_FeedForward":
        return Gated_Dconv_FeedForward(
                dim, 
                ffn_expansion_factor=cfg.MODEL.DENOISER.CAUN.FFN_EXPAND, 
            )
    elif ffn_name == "FeedForward":
        return FeedForward(dim = dim)


def to_3d(x):
    return rearrange(x, 'b c h w -> b (h w) c')

def to_4d(x,h,w):
    return rearrange(x, 'b (h w) c -> b c h w',h=h,w=w)


class BiasFree_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(BiasFree_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return x / torch.sqrt(sigma+1e-5) * self.weight
    

class WithBias_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(WithBias_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.bias = nn.Parameter(torch.zeros(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        mu = x.mean(-1, keepdim=True)
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return (x - mu) / torch.sqrt(sigma+1e-5) * self.weight + self.bias


class LayerNorm(nn.Module):
    def __init__(self, dim, LayerNorm_type):
        super(LayerNorm, self).__init__()
        if LayerNorm_type =='BiasFree':
            self.body = BiasFree_LayerNorm(dim)
        else:
            self.body = WithBias_LayerNorm(dim)

    def forward(self, x):
        # x: (b, c, h, w)
        h, w = x.shape[-2:]
        return to_4d(self.body(to_3d(x)), h, w)
    

class PreNorm(nn.Module):
    def __init__(self, dim, fn, layernorm_type='WithBias'):
        super().__init__()
        self.fn = fn
        self.layernorm_type = layernorm_type
        if layernorm_type == 'BiasFree' or layernorm_type == 'WithBias':
            self.norm = LayerNorm(dim, layernorm_type)
        else:
            self.norm = nn.LayerNorm(dim)

    def forward(self, x, *args, **kwargs):
        if self.layernorm_type == 'BiasFree' or self.layernorm_type == 'WithBias':
            x = self.norm(x)
        else:
            h, w = x.shape[-2:]
            x = to_4d(self.norm(to_3d(x)), h, w)
        return self.fn(x, *args, **kwargs)
    


class LocalNonLocalBlock(nn.Module):

    def __init__(self, 
                 cfg,
                 dim, 
                 num_heads,
                 window_size:tuple,
                 window_num:tuple,
                 layernorm_type,
                 num_blocks,
                 ):
        super().__init__()
        self.cfg = cfg
        self.window_size = window_size
        self.window_num = window_num
#    def __init__(self, dim, num_heads=2, num_tokens=1, window_size=8, qkv_bias=False, drop=0., attn_drop=0.)
        self.blocks = nn.ModuleList([])
        for _ in range(num_blocks):
            self.blocks.append(nn.ModuleList([
                PreNorm(dim, SGLB(
                        dim = dim, 
                        num_heads = num_heads,
                        num_tokens=1,
                        window_size = 8,
                      qkv_bias=False, drop=0., attn_drop=0.),
                    layernorm_type = layernorm_type) if self.cfg.MODEL.DENOISER.CAUN.NON_LOCAL else nn.Identity(),
                PreNorm(dim, FFN_FN(
                    cfg,
                    ffn_name = cfg.MODEL.DENOISER.CAUN.FFN_NAME,
                    dim = dim
                ),
                layernorm_type = layernorm_type)
            ]))

    def forward(self, x):
        for (Attention, ffn) in self.blocks:
            x = x + Attention(x) 
            x = x + ffn(x)

        return x
    

class DownSample(nn.Module):
    def __init__(self, in_channels, bias=False):
        super(DownSample, self).__init__()
        self.down = nn.Sequential(
            nn.Conv2d(in_channels, in_channels * 2, 4, 2, 1, bias=False)
        )

    def forward(self, x):
        x = self.down(x)
        return x

class UpSample(nn.Module):
    def __init__(self, in_channels, bias=False):
        super(UpSample, self).__init__()
        self.up = nn.Sequential(
            nn.ConvTranspose2d(in_channels, in_channels // 2, stride=2, kernel_size=2, padding=0, output_padding=0)
        )

    def forward(self, x):
        x = self.up(x)
        return x
    

class LNLT(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.embedding = nn.Conv2d(cfg.MODEL.DENOISER.CAUN.IN_DIM, cfg.MODEL.DENOISER.CAUN.DIM, kernel_size=3, stride=1, padding=1, bias=False)


        self.Encoder = nn.ModuleList([
            LocalNonLocalBlock(
                cfg = cfg, 
                dim = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 0, 
                num_heads = 2 ** 0, 
                window_size = cfg.MODEL.DENOISER.CAUN.WINDOW_SIZE,
                window_num = cfg.MODEL.DENOISER.CAUN.WINDOW_NUM,
                layernorm_type = cfg.MODEL.DENOISER.CAUN.LAYERNORM_TYPE,
                num_blocks = cfg.MODEL.DENOISER.CAUN.NUM_BLOCKS[0],
            ),
            LocalNonLocalBlock(
                cfg = cfg, 
                dim = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1, 
                num_heads = 2 ** 1, 
                window_size = cfg.MODEL.DENOISER.CAUN.WINDOW_SIZE,
                window_num = cfg.MODEL.DENOISER.CAUN.WINDOW_NUM,
                layernorm_type = cfg.MODEL.DENOISER.CAUN.LAYERNORM_TYPE,
                num_blocks = cfg.MODEL.DENOISER.CAUN.NUM_BLOCKS[1],
            ),
        ])

        self.BottleNeck = LocalNonLocalBlock(
                cfg = cfg, 
                dim = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 2, 
                num_heads = 2 ** 2, 
                window_size = cfg.MODEL.DENOISER.CAUN.WINDOW_SIZE,
                window_num = cfg.MODEL.DENOISER.CAUN.WINDOW_NUM,
                layernorm_type = cfg.MODEL.DENOISER.CAUN.LAYERNORM_TYPE,
                num_blocks = cfg.MODEL.DENOISER.CAUN.NUM_BLOCKS[2],
            )

        self.Decoder = nn.ModuleList([
            LocalNonLocalBlock(
                cfg = cfg, 
                dim = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1, 
                num_heads = 2 ** 1, 
                window_size = cfg.MODEL.DENOISER.CAUN.WINDOW_SIZE,
                window_num = cfg.MODEL.DENOISER.CAUN.WINDOW_NUM,
                layernorm_type = cfg.MODEL.DENOISER.CAUN.LAYERNORM_TYPE,
                num_blocks = cfg.MODEL.DENOISER.CAUN.NUM_BLOCKS[3],
            ),
            LocalNonLocalBlock(
                cfg = cfg, 
                dim = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 0, 
                num_heads = 2 ** 0, 
                window_size = cfg.MODEL.DENOISER.CAUN.WINDOW_SIZE,
                window_num = cfg.MODEL.DENOISER.CAUN.WINDOW_NUM,
                layernorm_type = cfg.MODEL.DENOISER.CAUN.LAYERNORM_TYPE,
                num_blocks = cfg.MODEL.DENOISER.CAUN.NUM_BLOCKS[4],
            )
        ])

        self.Downs = nn.ModuleList([
            DownSample(cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 0),
            DownSample(cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1)
        ])

        self.Ups = nn.ModuleList([
            UpSample(cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 2),
            UpSample(cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1)
        ])

        self.fusions = nn.ModuleList([
            nn.Conv2d(
                in_channels = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 2,
                out_channels = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1,
                kernel_size = 1,
                stride = 1,
                padding = 0,
                bias = False
            ),
            nn.Conv2d(
                in_channels = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 1,
                out_channels = cfg.MODEL.DENOISER.CAUN.DIM * 2 ** 0,
                kernel_size = 1,
                stride = 1,
                padding = 0,
                bias = False
            )
        ])

        self.mapping = nn.Conv2d(cfg.MODEL.DENOISER.CAUN.DIM, cfg.MODEL.DENOISER.CAUN.OUT_DIM, kernel_size=3, stride=1, padding=1, bias=False)


    def forward(self, x):
        b, c, h_inp, w_inp = x.shape
        hb, wb = 16, 16
        pad_h = (hb - h_inp % hb) % hb
        pad_w = (wb - w_inp % wb) % wb
        x = F.pad(x, [0, pad_w, 0, pad_h], mode='reflect')


        x1 = self.embedding(x)
        res1 = self.Encoder[0](x1)

        x2 = self.Downs[0](res1)
        res2 = self.Encoder[1](x2)

        x4 = self.Downs[1](res2)
        res4 = self.BottleNeck(x4)

        dec_res2 = self.Ups[0](res4) # dim * 2 ** 2 -> dim * 2 ** 1
        dec_res2 = torch.cat([dec_res2, res2], dim=1) # dim * 2 ** 2
        dec_res2 = self.fusions[0](dec_res2) # dim * 2 ** 2 -> dim * 2 ** 1
        dec_res2 = self.Decoder[0](dec_res2)

        dec_res1 = self.Ups[1](dec_res2) # dim * 2 ** 1 -> dim * 2 ** 0
        dec_res1 = torch.cat([dec_res1, res1], dim=1) # dim * 2 ** 1 
        dec_res1 = self.fusions[1](dec_res1) # dim * 2 ** 1 -> dim * 2 ** 0        
        dec_res1 = self.Decoder[1](dec_res1)

        if self.cfg.MODEL.DENOISER.CAUN.WITH_NOISE_LEVEL:
            out = self.mapping(dec_res1) + x[:, 1:, :, :]
        else:
            out = self.mapping(dec_res1) + x
            

        return out[:, :, :h_inp, :w_inp]
    

def PWDWPWConv(in_channels, out_channels):
    return nn.Sequential(
        nn.Conv2d(in_channels, 64, 1, 1, 0, bias=True),
        nn.GELU(),
        nn.Conv2d(64, 64, 3, 1, 1, bias=True, groups=64),
        nn.GELU(),
        nn.Conv2d(64, out_channels, 1, 1, 0, bias=False)
    )

def A(x, Phi):
    B, nC, H, W = x.shape
    temp = x * Phi
    y = torch.sum(temp, 1)
    return y

def At(y, Phi):
    temp = torch.unsqueeze(y, 1).repeat(1, Phi.shape[1], 1, 1)
    x = temp * Phi
    return x


def shift_3d(inputs, step=2):
    [B, C, H, W] = inputs.shape
    temp = torch.zeros((B, C, H, W+(C-1)*step)).to(inputs.device)
    temp[:, :, :, :W] = inputs
    for i in range(C):
        temp[:,i,:,:] = torch.roll(temp[:,i,:,:], shifts=step*i, dims=2)
    return temp

def shift_back_3d(inputs,step=2):
    [bs, nC, row, col] = inputs.shape
    for i in range(nC):
        inputs[:,i,:,:] = torch.roll(inputs[:,i,:,:], shifts=(-1)*step*i, dims=2)
    return inputs


class DADN(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.DL = nn.Sequential(
            PWDWPWConv(self.cfg.DATASETS.WAVE_LENS*2, self.cfg.DATASETS.WAVE_LENS*2),
            PWDWPWConv(self.cfg.DATASETS.WAVE_LENS*2, self.cfg.DATASETS.WAVE_LENS),
        )
        self.down_sample = nn.Conv2d(self.cfg.DATASETS.WAVE_LENS, self.cfg.DATASETS.WAVE_LENS*2, 3, 2, 1, bias=True) # (B, 64, H, W) -> (B, 64, H//2, W//2)
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.mlp = nn.Sequential(
                nn.Conv2d(self.cfg.DATASETS.WAVE_LENS*2, self.cfg.DATASETS.WAVE_LENS*2, 1, padding=0, bias=True),
                nn.ReLU(inplace=True),
                nn.Conv2d(self.cfg.DATASETS.WAVE_LENS*2, self.cfg.DATASETS.WAVE_LENS*2, 1, padding=0, bias=True),
                nn.ReLU(inplace=True),
                nn.Conv2d(self.cfg.DATASETS.WAVE_LENS*2, 2, 1, padding=0, bias=True),
                nn.Softplus())
        self.relu = nn.ReLU(inplace=True)


    def forward(self, y, phi):

        inp = torch.cat([phi, y], dim=1)
        phi_r = self.DL(inp)

        phi = phi + phi_r

        x = self.down_sample(self.relu(phi_r))
        x = self.avg_pool(x)
        x = self.mlp(x) + 1e-6
        mu = x[:, 0, :, :]
        noise_level = x[:, 1, :, :]

        return phi, mu, noise_level[:, None, :, :]
    
class SGLB(nn.Module):
    def __init__(self, dim, num_heads=2, num_tokens=1, window_size=8, qkv_bias=False, drop=0., attn_drop=0.):
        super(SGLB, self).__init__()

        self.attn = Attention(dim, num_heads=num_heads, num_tokens=num_tokens, window_size=window_size, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop)
        self.global_token = nn.Parameter(torch.zeros(1, num_tokens, dim))
        self.dim= dim

    def forward(self, x):
        _, _, H, W = x.shape
        x = x.flatten(2).transpose(1, 2)
        global_token = self.global_token.expand(x.shape[0], -1, -1)
        x = torch.cat((global_token, x), dim=1)

        x = self.attn(x,H, W)
        x = x[:, -H*W:]
        out = x.view(-1, H, W, self.dim).permute(0, 3, 1, 2).contiguous()      
        return out


class Attention(nn.Module):
    def __init__(self, dim, num_tokens=1, num_heads=2, window_size=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.num_tokens = num_tokens
        self.window_size = window_size
        self.attn_area = window_size * window_size
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.kv_global = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop) if attn_drop > 0 else nn.Identity()
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop) if proj_drop > 0 else nn.Identity()

        # positional embedding
        # Define a parameter table of relative position bias, shape: 2*Wh-1 * 2*Ww-1, nH
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size - 1) * (2 * window_size - 1), num_heads))

        # Get pair-wise relative position index for each token inside the window
        self.register_buffer("relative_position_index", get_relative_position_index(window_size,
                                                                                    window_size).view(-1))
        # Init relative positional bias
        trunc_normal_(self.relative_position_bias_table, std=.02)

    def _get_relative_positional_bias(
            self
    ) -> torch.Tensor:
        """ Returns the relative positional bias.
        Returns:
            relative_position_bias (torch.Tensor): Relative positional bias.
        """
        relative_position_bias = self.relative_position_bias_table[
            self.relative_position_index].view(self.attn_area, self.attn_area, -1)
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()
        return relative_position_bias.unsqueeze(0)

    def forward_global_aggregation(self, q, k, v):
        """
        q: global tokens
        k: image tokens
        v: image tokens
        """
        B, _, N, _ = q.shape
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        return x

    def forward_local(self, q, k, v, H, W):
        """
        q: image tokens
        k: image tokens
        v: image tokens
        """
        B, num_heads, N, C = q.shape
        ws = self.window_size
        h_group, w_group = H // ws, W // ws

        # partition to windows
        q = q.view(B, num_heads, h_group, ws, w_group, ws, -1).permute(0, 2, 4, 1, 3, 5, 6).contiguous()
        q = q.view(-1, num_heads, ws*ws, C)
        k = k.view(B, num_heads, h_group, ws, w_group, ws, -1).permute(0, 2, 4, 1, 3, 5, 6).contiguous()
        k = k.view(-1, num_heads, ws*ws, C)
        v = v.view(B, num_heads, h_group, ws, w_group, ws, -1).permute(0, 2, 4, 1, 3, 5, 6).contiguous()
        v = v.view(-1, num_heads, ws*ws, v.shape[-1])

        attn = (q @ k.transpose(-2, -1)) * self.scale
        pos_bias = self._get_relative_positional_bias()
        attn = (attn + pos_bias).softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(v.shape[0], ws*ws, -1)

        # reverse
        x = window_reverse(x, (H, W), (ws, ws))
        return x

    def forward_global_broadcast(self, q, k, v):
        """
        q: image tokens
        k: global tokens
        v: global tokens
        """
        B, num_heads, N, _ = q.shape
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        return x

    def forward(self, x, H, W):
        B, N, C = x.shape# 4,65537,28
        NC = self.num_tokens#1
        # pad
        x_img, x_global = x[:, NC:], x[:, :NC]
        x_img = x_img.view(B, H, W, C)
        pad_l = pad_t = 0
        ws = self.window_size
        pad_r = (ws - W % ws) % ws
        pad_b = (ws - H % ws) % ws
        x_img = F.pad(x_img, (0, 0, pad_l, pad_r, pad_t, pad_b))
        Hp, Wp = x_img.shape[1], x_img.shape[2]
        x_img = x_img.view(B, -1, C)
        x = torch.cat([x_global, x_img], dim=1)

        # qkv
        qkv = self.qkv(x)
        q, k, v = qkv.view(B, -1, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4).unbind(0)

        # split img tokens & global tokens
        q_img, k_img, v_img = q[:, :, NC:], k[:, :, NC:], v[:, :, NC:]
        q_cls, _, _ = q[:, :, :NC], k[:, :, :NC], v[:, :, :NC]

        # local window attention
        x_img = self.forward_local(q_img, k_img, v_img, Hp, Wp)
        # restore to the original size
        x_img = x_img.view(B, Hp, Wp, -1)[:, :H, :W].reshape(B, H*W, -1)
        q_img = q_img.reshape(B, self.num_heads, Hp, Wp, -1)[:, :, :H, :W].reshape(B, self.num_heads, H*W, -1)
        k_img = k_img.reshape(B, self.num_heads, Hp, Wp, -1)[:, :, :H, :W].reshape(B, self.num_heads, H*W, -1)
        v_img = v_img.reshape(B, self.num_heads, Hp, Wp, -1)[:, :, :H, :W].reshape(B, self.num_heads, H*W, -1)

        # global aggregation
        x_cls = self.forward_global_aggregation(q_cls, k_img, v_img)
        k_cls, v_cls = self.kv_global(x_cls).view(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4).unbind(0)

        # gloal broadcast
        x_img = x_img + self.forward_global_broadcast(q_img, k_cls, v_cls)

        x = torch.cat([x_cls, x_img], dim=1)
        x = self.proj(x)
        return x


def get_relative_position_index(
        win_h: int,
        win_w: int
) -> torch.Tensor:

    coords = torch.stack(torch.meshgrid([torch.arange(win_h), torch.arange(win_w)]))
    coords_flatten = torch.flatten(coords, 1)
    relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
    relative_coords = relative_coords.permute(1, 2, 0).contiguous()
    relative_coords[:, :, 0] += win_h - 1
    relative_coords[:, :, 1] += win_w - 1
    relative_coords[:, :, 0] *= 2 * win_w - 1
    return relative_coords.sum(-1)

def window_reverse(
        windows: torch.Tensor,
        original_size,
        window_size=(7, 7)
) -> torch.Tensor:

    # Get height and width
    H, W = original_size
    # Compute original batch size
    B = int(windows.shape[0] / (H * W / window_size[0] / window_size[1]))
    # Fold grid tensor
    output = windows.view(B, H // window_size[0], W // window_size[1], window_size[0], window_size[1], -1)
    output = output.permute(0, 1, 3, 2, 4, 5).reshape(B, H * W, -1)
    return output

class CAUN(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

        self.fusion = nn.Conv2d(cfg.DATASETS.WAVE_LENS*2, cfg.DATASETS.WAVE_LENS, 1, padding=0, bias=True)

        self.DP = nn.ModuleList([
           DADN(cfg) for _ in range(cfg.MODEL.DENOISER.CAUN.STAGE)
        ]) if not cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else DADN(cfg)
        self.PP = nn.ModuleList([
            LNLT(cfg) for _ in range(cfg.MODEL.DENOISER.CAUN.STAGE)
        ]) if not cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else LNLT(cfg)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Conv2d) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def initial(self, y, Phi):

        nC = self.cfg.DATASETS.WAVE_LENS
        step = self.cfg.DATASETS.STEP
        bs, nC, row, col = Phi.shape
        y_shift = torch.zeros(bs, nC, row, col).to(y.device).float()
        for i in range(nC):
            y_shift[:, i, :, step * i:step * i + col - (nC - 1) * step] = y[:, :, step * i:step * i + col - (nC - 1) * step]
        z = self.fusion(torch.cat([y_shift, Phi], dim=1))
        return z

    def prepare_input(self, data):
        hsi = data['hsi']
        mask = data['mask']

        YH = gen_meas_torch_batch(hsi, mask, step=self.cfg.DATASETS.STEP, wave_len=self.cfg.DATASETS.WAVE_LENS, mask_type=self.cfg.DATASETS.MASK_TYPE, with_noise=self.cfg.DATASETS.TRAIN.WITH_NOISE)

        data['Y'] = YH['Y']
        data['H'] = YH['H']

        return data
    

    def forward_train(self, data):
        y = data['Y']
        phi = data['mask']
        x0 = data['H']

        z = self.initial(y, phi)
        
        B, C, H, W = phi.shape
        B, C, H_, W_ = x0.shape      

        z_hat = z
        z_list=[]
        z_list.append(z)
        beta=0.5* torch.ones((W, 1)).to(y.device)

        for i in range(self.cfg.MODEL.DENOISER.CAUN.STAGE):
            Phi, mu, noise_level = self.DP[i](z, phi) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.DP(z, phi)

            if not self.cfg.MODEL.DENOISER.CAUN.WITH_DL:
                Phi = phi
            if not self.cfg.MODEL.DENOISER.CAUN.WITH_MU:
                mu = torch.FloatTensor([1e-6]).to(y.device)

            Phi_s = torch.sum(Phi**2,1)
            Phi_s[Phi_s==0] = 1
            Phi_z = A(z_hat, Phi)
            x = z + At(torch.div(y-Phi_z,mu+Phi_s), Phi)  
            x = shift_back_3d(x)[:, :, :, :W_]
            noise_level_repeat = noise_level.repeat(1,1,x.shape[2], x.shape[3])
            if not self.cfg.MODEL.DENOISER.CAUN.WITH_NOISE_LEVEL:
                z = self.PP[i](x) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.PP(x)
            else:
                z = self.PP[i](torch.cat([noise_level_repeat, x],dim=1)) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.PP(torch.cat([noise_level_repeat, x],dim=1))
            z = shift_3d(z)
            z_list.append(z)
            z_hat = z + beta[i]*(z_list[-1]-z_list[-2])


        z = shift_back_3d(z)[:, :, :, :W_]

        out = z[:, :, :, :W_]

        return out
    
    def forward_test(self, data):
        y = data['Y']
        phi = data['mask']
        x0 = data['H']

        z = self.initial(y, phi)


        B, C, H, W = phi.shape
        B, C, H_, W_ = x0.shape        

        z_hat = z
        z_list=[]
        z_list.append(z)
        beta=0.5* torch.ones((W, 1)).to(y.device)

        for i in range(self.cfg.MODEL.DENOISER.CAUN.STAGE):
            Phi, mu, noise_level = self.DP[i](z, phi) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.DP(z, phi)

            if not self.cfg.MODEL.DENOISER.CAUN.WITH_DL:
                Phi = phi
            if not self.cfg.MODEL.DENOISER.CAUN.WITH_MU:
                mu = torch.FloatTensor([1e-6]).to(y.device)

            Phi_s = torch.sum(Phi**2,1)
            Phi_s[Phi_s==0] = 1
            Phi_z = A(z_hat, Phi)
            x = z + At(torch.div(y-Phi_z,mu+Phi_s), Phi)
            x = shift_back_3d(x)[:, :, :, :W_]
            noise_level_repeat = noise_level.repeat(1,1,x.shape[2], x.shape[3])
            if not self.cfg.MODEL.DENOISER.CAUN.WITH_NOISE_LEVEL:
                z = self.PP[i](x) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.PP(x)
            else:
                z = self.PP[i](torch.cat([noise_level_repeat, x],dim=1)) if not self.cfg.MODEL.DENOISER.CAUN.SHARE_PARAMS else self.PP(torch.cat([noise_level_repeat, x],dim=1))
            z = shift_3d(z)
            z_list.append(z)
            z_hat = z + beta[i]*(z_list[-1]-z_list[-2])


        z = shift_back_3d(z)[:, :, :, :W_]

        out = z[:, :, :, :W_]

        return out
    
    def forward(self, data):
        if self.training:
            data = self.prepare_input(data)
            x = self.forward_train(data)

        else:
            x = self.forward_test(data)

        return x
    
