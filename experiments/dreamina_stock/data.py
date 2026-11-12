"""Stock panel -> "video" clips for a Dreamina/Seedream-style diffusion transformer.

The mapping used in this experiment:

    image/video pixel grid              stock panel
    ------------------------------      -----------------------------------
    frame (H x W x C)                   one trading day (N stocks x F factors + valid flag)
    video (T x H x W x C)               L context days + H future days
    16x16 patch                         16 consecutive stocks (fixed order)
    text prompt / VLM embedding         market-regime vector + encoded context frames
    VAE latent                          per-patch latent token produced by StockVAE
    DiT denoiser                        conditional flow-matching transformer over future tokens

The panel is the one already used by Share v2:
  ~/labs/VLA/Share/v2/local_data/{panel.npz,metadata.json}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np

DEFAULT_PANEL_DIR = Path(
    os.environ.get("VLA_PANEL_DIR", Path.home() / "labs" / "VLA" / "Share" / "v2" / "local_data")
).expanduser()


@dataclass
class Panel:
    features: np.ndarray      # [T, N, F] standardized, NaN filled with 0
    valid: np.ndarray         # [T, N] float32 1 = eligible
    returns: np.ndarray       # [T, N] float32 5-day open-to-open return
    dates: List[str]
    stocks: List[str]
    benchmark_close: np.ndarray
    regime: np.ndarray        # [T, R] standardized market-regime vector
    feature_count: int
    stock_count: int
    date_count: int

    @property
    def channels(self) -> int:
        return self.feature_count + 1  # + valid flag


def to_index(date: str) -> int:
    """Signal date -> integer day index; accepts YYYY-MM-DD."""
    return int(date[:4]) * 10000 + int(date[5:7]) * 100 + int(date[8:10])


def _standardize(values: np.ndarray, center: np.ndarray, scale: np.ndarray) -> np.ndarray:
    out = (values - center) / np.where(np.abs(scale) < 1e-8, 1.0, scale)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _regime(benchmark_close: np.ndarray, returns: np.ndarray, valid: np.ndarray) -> np.ndarray:
    close = benchmark_close.astype(np.float64)
    log = np.diff(np.log(np.maximum(close, 1e-9)), prepend=np.log(max(close[0], 1e-9)))

    def rolling_ret(span: int) -> np.ndarray:
        out = np.zeros_like(close)
        out[span:] = close[span:] / np.maximum(close[:-span], 1e-9) - 1.0
        return out

    def rolling_vol(span: int) -> np.ndarray:
        series = np.zeros_like(close)
        for t in range(len(close)):
            window = log[max(0, t - span + 1): t + 1]
            series[t] = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
        return series

    ret = returns.astype(np.float64)
    breadth = np.zeros(len(close))
    dispersion = np.zeros(len(close))
    for t in range(len(close)):
        mask = valid[t] > 0
        values = ret[t][mask]
        values = values[np.isfinite(values)]
        if len(values) > 1:
            breadth[t] = float((values > 0).mean())
            dispersion[t] = float(np.std(values, ddof=1))
    columns = [
        rolling_ret(1), rolling_ret(5), rolling_ret(20), rolling_ret(60),
        rolling_vol(20), rolling_vol(60), breadth, dispersion,
    ]
    matrix = np.stack(columns, axis=1)
    matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)
    mean = matrix.mean(axis=0, keepdims=True)
    std = matrix.std(axis=0, keepdims=True)
    return ((matrix - mean) / np.where(std < 1e-8, 1.0, std)).astype(np.float32)


def load_panel(panel_dir: Path | str = DEFAULT_PANEL_DIR) -> Panel:
    root = Path(panel_dir).expanduser()
    raw = np.load(root / "panel.npz", allow_pickle=False)
    meta = json.loads((root / "metadata.json").read_text())

    features = raw["features"]                       # [N, T, F]
    valid = raw["eligible"].astype(np.float32)       # [N, T]
    returns = raw["returns"].astype(np.float32)      # [N, T]
    center, scale = raw["center"], raw["scale"]

    # [T, N, F]
    features = np.transpose(features, (1, 0, 2))
    valid = np.transpose(valid, (1, 0)).copy()
    returns = np.transpose(returns, (1, 0)).copy()

    normalized = _standardize(features, center, scale)
    normalized = np.where(valid[:, :, None] > 0, normalized, 0.0).astype(np.float32)

    benchmark_close = raw["benchmark_close"].astype(np.float32)
    regime = _regime(benchmark_close, returns, valid)

    return Panel(
        features=normalized,
        valid=valid,
        returns=returns,
        dates=[str(d) for d in meta["dates"]],
        stocks=[str(s) for s in meta["stocks"]],
        benchmark_close=benchmark_close,
        regime=regime,
        feature_count=int(features.shape[2]),
        stock_count=int(features.shape[1]),
        date_count=int(features.shape[0]),
    )


def rank_target(returns: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Cross-sectional rank in [-1, 1] with average ranks (same as Share v2)."""
    out = np.full(returns.shape, np.nan, dtype=np.float32)
    for t in range(returns.shape[0]):
        stocks = np.flatnonzero((valid[t] > 0) & np.isfinite(returns[t]))
        count = len(stocks)
        if count == 0:
            continue
        if count == 1:
            out[t, stocks] = 0.0
            continue
        from scipy.stats import rankdata

        ranks = rankdata(returns[t, stocks], method="average")
        out[t, stocks] = (2.0 * (ranks - 1.0) / (count - 1.0) - 1.0).astype(np.float32)
    return out


def signal_dates(panel: Panel, start: str, end: str, lookback: int, horizon: int) -> np.ndarray:
    """Days t with a full lookback and a full future horizon, inside [start, end]."""
    dates = panel.dates
    first = int(np.searchsorted(dates, start))
    out = []
    last_signal = panel.date_count - horizon - 2
    for t in range(max(first, lookback - 1), last_signal + 1):
        if dates[t] > end:
            break
        if dates[t + horizon + 1] <= end:
            out.append(t)
    return np.asarray(out, dtype=np.int64)


class ClipBuilder:
    """Turn integer day indices into padded (G, K, C) token arrays."""

    def __init__(self, panel: Panel, group_size: int = 16):
        self.panel = panel
        self.group_size = group_size
        self.n_pad = int(np.ceil(panel.stock_count / group_size)) * group_size
        self.groups = self.n_pad // group_size

    def _frames(self, indices: np.ndarray) -> np.ndarray:
        """[B, T, Np, C] float32."""
        panel = self.panel
        n_pad = self.n_pad
        features = np.zeros((len(indices), n_pad, panel.feature_count), dtype=np.float32)
        validflags = np.zeros((len(indices), n_pad), dtype=np.float32)
        for row, t in enumerate(indices):
            features[row, : panel.stock_count] = panel.features[t]
            validflags[row, : panel.stock_count] = panel.valid[t]
        return np.concatenate([features, validflags[:, :, None]], axis=2)

    def clip(self, dates: np.ndarray, lookback: int, horizon: int) -> Dict[str, np.ndarray]:
        dates = np.asarray(dates, dtype=np.int64)
        batch = len(dates)
        context_idx = np.stack([np.arange(t - lookback + 1, t + 1) for t in dates])
        target_idx = np.stack([np.arange(t + 1, t + horizon + 1) for t in dates])

        context = self._frames(context_idx.reshape(-1)).reshape(
            batch, lookback, self.n_pad, self.panel.channels
        )
        target = self._frames(target_idx.reshape(-1)).reshape(
            batch, horizon, self.n_pad, self.panel.channels
        )
        context_mask = np.stack([self.panel.valid[context_idx[i]] for i in range(batch)])
        target_mask = np.stack([self.panel.valid[target_idx[i]] for i in range(batch)])
        labels = np.stack([self.panel.returns[t] for t in dates])
        label_valid = np.stack(
            [(self.panel.valid[t] > 0) & np.isfinite(self.panel.returns[t]) for t in dates]
        )
        regime = self.panel.regime[dates]

        return {
            "context": context,
            "target": target,
            "context_mask": context_mask.astype(np.float32),
            "target_mask": target_mask.astype(np.float32),
            "label": labels.astype(np.float32),
            "label_valid": label_valid.astype(np.float32),
            "regime": regime.astype(np.float32),
        }


def label_rank(panel: Panel, dates: np.ndarray) -> np.ndarray:
    """[B, N] rank score for the given signal days."""
    return np.stack([rank_target(panel.returns[t:t + 1], panel.valid[t:t + 1])[0] for t in dates])
