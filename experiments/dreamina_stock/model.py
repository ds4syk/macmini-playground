"""Dreamina/Seedream-4.0-style latent diffusion transformer, adapted to stock panels.

Architecture mapping (see README):

    Seedream 4.0                     this experiment
    ---------------------------      -----------------------------------------
    high-compression VAE             StockVAE: 16 stocks x 26 channels -> 1 latent token
    diffusion transformer (DiT)      StockDiT: 3 blocks, d=96, self-attn + cross-attn + MLP
    adaLN-Zero timestep conditioning shared 6-way modulation driven by timestep + regime
    VLM text-prompt conditioning     cross-attention to 82 encoded context tokens
    flow matching / few-step sample  rectified-flow velocity field, Euler sampler
"""
from __future__ import annotations

import math

import torch
from torch import nn
from torch.nn import functional as F


def sinusoidal_embedding(timesteps: torch.Tensor, dim: int) -> torch.Tensor:
    half = dim // 2
    freqs = torch.exp(
        -math.log(10000.0) * torch.arange(half, device=timesteps.device, dtype=torch.float32) / half
    )
    args = timesteps.float()[:, None] * freqs[None, :]
    return torch.cat([torch.cos(args), torch.sin(args)], dim=1)


class Attention(nn.Module):
    def __init__(self, dim: int, heads: int):
        super().__init__()
        self.heads = heads
        self.head_dim = dim // heads
        self.qkv = nn.Linear(dim, 3 * dim)
        self.out = nn.Linear(dim, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, dim = x.shape
        q, k, v = self.qkv(x).chunk(3, dim=-1)
        shape = (batch, length, self.heads, self.head_dim)
        q, k, v = (t.view(*shape).transpose(1, 2) for t in (q, k, v))
        out = F.scaled_dot_product_attention(q, k, v)
        return self.out(out.transpose(1, 2).reshape(batch, length, dim))


class CrossAttention(nn.Module):
    def __init__(self, dim: int, heads: int):
        super().__init__()
        self.heads = heads
        self.head_dim = dim // heads
        self.query = nn.Linear(dim, dim)
        self.key_value = nn.Linear(dim, 2 * dim)
        self.out = nn.Linear(dim, dim)

    def forward(self, x: torch.Tensor, context: torch.Tensor) -> torch.Tensor:
        batch, length, dim = x.shape
        q = self.query(x)
        k, v = self.key_value(context).chunk(2, dim=-1)
        q = q.view(batch, length, self.heads, self.head_dim).transpose(1, 2)
        k = k.view(batch, context.shape[1], self.heads, self.head_dim).transpose(1, 2)
        v = v.view(batch, context.shape[1], self.heads, self.head_dim).transpose(1, 2)
        out = F.scaled_dot_product_attention(q, k, v)
        return self.out(out.transpose(1, 2).reshape(batch, length, dim))


class FeedForward(nn.Module):
    def __init__(self, dim: int, ratio: int = 2):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * ratio), nn.GELU(), nn.Linear(dim * ratio, dim)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class DiTBlock(nn.Module):
    """Pre-norm block with shared adaLN modulation and its own residual gates."""

    def __init__(self, dim: int, heads: int, ratio: int = 2):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        self.self_attn = Attention(dim, heads)
        self.norm2 = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        self.cross_attn = CrossAttention(dim, heads)
        self.norm3 = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        self.mlp = FeedForward(dim, ratio)
        self.gate_cross = nn.Parameter(torch.zeros(1))

    def forward(self, x, context, modulation):
        shift_a, scale_a, gate_a, shift_m, scale_m, gate_m = modulation
        x = x + gate_a[:, None, :] * self.self_attn(
            self.norm1(x) * (1 + scale_a[:, None, :]) + shift_a[:, None, :]
        )
        x = x + self.gate_cross * self.cross_attn(self.norm2(x), context)
        x = x + gate_m[:, None, :] * self.mlp(
            self.norm3(x) * (1 + scale_m[:, None, :]) + shift_m[:, None, :]
        )
        return x


class StockVAE(nn.Module):
    """Compress one 16-stock x 26-channel patch into a latent token and back."""

    def __init__(self, channels: int, group_size: int, dim: int):
        super().__init__()
        patch = channels * group_size
        self.encoder = nn.Sequential(
            nn.Linear(patch, dim), nn.GELU(), nn.Linear(dim, dim)
        )
        self.decoder = nn.Sequential(
            nn.Linear(dim, dim), nn.GELU(), nn.Linear(dim, patch)
        )
        self.post_norm = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        self.channels = channels
        self.group_size = group_size

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        return self.post_norm(self.encoder(x))

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        return self.decoder(z)


class StockDiT(nn.Module):
    def __init__(
        self,
        channels: int,
        feature_count: int,
        group_size: int,
        groups: int,
        horizon: int,
        regime_dim: int,
        dim: int = 96,
        depth: int = 3,
        heads: int = 6,
        ratio: int = 2,
        time_dim: int = 128,
    ):
        super().__init__()
        self.channels = channels
        self.feature_count = feature_count
        self.group_size = group_size
        self.groups = groups
        self.horizon = horizon
        self.dim = dim

        self.vae = StockVAE(channels, group_size, dim)
        self.time_mlp = nn.Sequential(
            nn.Linear(time_dim, dim), nn.SiLU(), nn.Linear(dim, dim)
        )
        self.regime_mlp = nn.Sequential(
            nn.Linear(regime_dim, dim), nn.SiLU(), nn.Linear(dim, dim)
        )
        self.ada = nn.Sequential(nn.SiLU(), nn.Linear(dim, 6 * dim))
        nn.init.zeros_(self.ada[-1].weight)
        nn.init.zeros_(self.ada[-1].bias)

        self.pos_frame = nn.Parameter(torch.zeros(horizon, dim))
        self.pos_group = nn.Parameter(torch.zeros(groups, dim))
        nn.init.normal_(self.pos_frame, std=0.02)
        nn.init.normal_(self.pos_group, std=0.02)

        self.blocks = nn.ModuleList([DiTBlock(dim, heads, ratio) for _ in range(depth)])
        self.final_norm = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)

        frame_index = torch.arange(horizon).repeat_interleave(groups)
        group_index = torch.arange(groups).repeat(horizon)
        self.register_buffer("frame_index", frame_index, persistent=False)
        self.register_buffer("group_index", group_index, persistent=False)

    # -- tokenisation -------------------------------------------------------
    def _flatten_patches(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, T, Np, C] -> [B, T, G, K*C]
        batch, length, _ = x.shape[0], x.shape[1], x.shape[2]
        return x.reshape(batch, length, self.groups, self.group_size * self.channels)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        return self.vae.encode(self._flatten_patches(x))

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        # z: [B, T, G, d] -> [B, T, Np, C]
        batch, length, groups, dim = z.shape
        out = self.vae.decode(z)
        return out.reshape(batch, length, groups * self.group_size, self.channels)

    # -- conditioning -------------------------------------------------------
    def condition(self, context: torch.Tensor, regime: torch.Tensor):
        context_latent = self.encode(context)                     # [B, L, G, d]
        kv = torch.cat(
            [context_latent.mean(dim=1), context_latent[:, -1]], dim=1
        )                                                          # [B, 2G, d]
        regime_embedding = self.regime_mlp(regime)
        return kv, regime_embedding

    def forward(self, target_latent: torch.Tensor, timesteps, kv, regime_embedding):
        # target_latent: [B, H, G, d] -> [B, H*G, d]
        batch, horizon, groups, dim = target_latent.shape
        x = target_latent.reshape(batch, horizon * groups, dim)
        x = x + self.pos_frame[self.frame_index][None] + self.pos_group[self.group_index][None]

        time_embedding = self.time_mlp(sinusoidal_embedding(timesteps, 128))
        modulation = self.ada(time_embedding + regime_embedding).chunk(6, dim=-1)

        for block in self.blocks:
            x = block(x, kv, modulation)
        return self.final_norm(x)

    # -- parameter accounting ----------------------------------------------
    def parameter_count(self) -> int:
        return sum(p.numel() for p in self.parameters())


def flow_matching_loss(model: StockDiT, context, target, regime, generator=None):
    """Rectified-flow velocity matching on the future clip latent."""
    batch = target.shape[0]
    device = target.device
    with torch.no_grad():
        target_latent = model.encode(target)
    noise = torch.randn(target_latent.shape, device=device, generator=generator)
    t = torch.rand(batch, device=device, generator=generator)
    x_t = (1 - t)[:, None, None, None] * noise + t[:, None, None, None] * target_latent
    velocity = target_latent - noise

    kv, regime_embedding = model.condition(context, regime)
    predicted = model(x_t, t, kv, regime_embedding)
    loss = F.mse_loss(predicted, velocity.reshape_as(predicted))

    recon = model.decode(target_latent)
    mask = target[:, :, :, -1:].float()
    recon_loss = (F.mse_loss(recon, target, reduction="none") * mask).sum() / mask.sum().clamp_min(1.0)
    return loss, recon_loss, target_latent


@torch.no_grad()
def sample_future(model: StockDiT, context, regime, steps: int = 6, seed: int = 0) -> torch.Tensor:
    """Euler integration from noise to the future clip; returns decoded frames [B,H,Np,C]."""
    batch, horizon = context.shape[0], model.horizon
    device = context.device
    generator = torch.Generator(device=device).manual_seed(seed)
    shape = (batch, horizon, model.groups, model.dim)
    x = torch.randn(shape, device=device, generator=generator)

    kv, regime_embedding = model.condition(context, regime)
    dt = 1.0 / steps
    for step in range(steps):
        t = torch.full((batch,), step * dt, device=device)
        velocity = model(x, t, kv, regime_embedding)
        x = x + dt * velocity.reshape(batch, horizon, model.groups, model.dim)
    return model.decode(x)
