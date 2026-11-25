"""Train the five A-share model routes on the Hong Kong panel.

The architectures, target, splits and hyper-parameters are copied from the
A-share v2 line (``~/labs/VLA/Share/v2/compute.py`` and ``gdn_ntp.py``).
Only the device selection and the data root differ; the same 60-day / 25-feature
inputs and the same per-date cross-sectional rank target are used.

Run on this Mac with the Anaconda/MPS environment::

    ~/labs/VLA/Share/v2/.venv/bin/python -m experiments.hk_models.train
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from scipy.stats import rankdata, spearmanr
from torch import nn
from torch.nn import functional as F

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
DATA = Path(os.environ.get("HK_DATA_DIR", ROOT / "data" / "hk"))
DEVICE = torch.device(
    "mps" if torch.backends.mps.is_available() else
    "cuda" if torch.cuda.is_available() else "cpu"
)


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def load_panel():
    panel = dict(np.load(DATA / "panel.npz", allow_pickle=False))
    metadata = json.loads((DATA / "metadata.json").read_text())
    return panel, metadata


def rank_targets(returns, valid):
    target = np.full(returns.shape, np.nan, dtype=np.float32)
    for t in range(returns.shape[1]):
        stocks = np.flatnonzero(valid[:, t])
        count = len(stocks)
        if count == 0:
            continue
        if count == 1:
            target[stocks, t] = 0.0
            continue
        ranks = rankdata(returns[stocks, t], method="average")
        target[stocks, t] = 2.0 * (ranks - 1.0) / (count - 1.0) - 1.0
    return target


def token_targets(returns, valid, bins=10):
    target = np.full(returns.shape, -1, dtype=np.int64)
    for t in range(returns.shape[1]):
        stocks = np.flatnonzero(valid[:, t])
        count = len(stocks)
        if count < bins:
            continue
        ranks = rankdata(returns[stocks, t], method="average")
        index = np.floor((ranks - 1) / count * bins).astype(np.int64)
        target[stocks, t] = np.clip(index, 0, bins - 1)
    return target


def split_dates(dates, valid, config):
    horizon = config["horizon"]
    last_signal = len(dates) - horizon - 2
    windows = {
        "train": (config["sample_start"], config["train_end"]),
        "validation": (config["validation_start"], config["validation_end"]),
        "test": (config["test_start"], config["data_end"]),
        "all": (config["sample_start"], config["data_end"]),
    }
    result = {}
    for name, (start, end) in windows.items():
        first = int(np.searchsorted(dates, start))
        selected = []
        for t in range(first, last_signal + 1):
            if dates[t] > end:
                break
            if not valid[:, t].any():
                continue
            if dates[t + horizon + 1] <= end:
                selected.append(t)
        result[name] = np.asarray(selected, dtype=np.int64)
    return result


def stocks_on(valid, t):
    return np.flatnonzero(valid[:, int(t)])


# ---------------------------------------------------------------------------
# architectures (identical to Share v2)
# ---------------------------------------------------------------------------


class AttnGRU(nn.Module):
    def __init__(self, features, hidden=64, layers=2, dropout=0.1):
        super().__init__()
        self.gru = nn.GRU(features, hidden, layers, batch_first=True,
                          dropout=dropout if layers > 1 else 0.0)
        self.attn = nn.Linear(hidden, 1)
        self.head = nn.Sequential(nn.LayerNorm(hidden * 2), nn.Linear(hidden * 2, 32),
                                  nn.GELU(), nn.Dropout(dropout), nn.Linear(32, 1))

    def forward(self, inputs):
        values, _ = self.gru(inputs)
        weights = torch.softmax(self.attn(values).squeeze(-1), dim=1)
        pooled = (values * weights.unsqueeze(-1)).sum(dim=1)
        return self.head(torch.cat([values[:, -1], pooled], dim=-1)).squeeze(-1)


class CausalBlock(nn.Module):
    def __init__(self, in_channels, out_channels, dilation, kernel=3, dropout=0.1):
        super().__init__()
        self.padding = (kernel - 1) * dilation
        self.conv1 = nn.Conv1d(in_channels, out_channels, kernel, dilation=dilation)
        self.conv2 = nn.Conv1d(out_channels, out_channels, kernel, dilation=dilation)
        self.norm1 = nn.BatchNorm1d(out_channels)
        self.norm2 = nn.BatchNorm1d(out_channels)
        self.drop = nn.Dropout(dropout)
        self.residual = nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels else nn.Identity()

    def forward(self, values):
        residual = self.residual(values)
        values = self.conv1(F.pad(values, (self.padding, 0)))
        values = self.drop(F.gelu(self.norm1(values)))
        values = self.conv2(F.pad(values, (self.padding, 0)))
        values = self.drop(F.gelu(self.norm2(values)))
        return F.gelu(values + residual)


class TCN(nn.Module):
    def __init__(self, features, channels=64, dilations=(1, 2, 4, 8), dropout=0.1):
        super().__init__()
        blocks = []
        for position, dilation in enumerate(dilations):
            blocks.append(CausalBlock(features if position == 0 else channels,
                                      channels, dilation, dropout=dropout))
        self.blocks = nn.Sequential(*blocks)
        self.head = nn.Sequential(nn.LayerNorm(channels), nn.Linear(channels, 32),
                                  nn.GELU(), nn.Dropout(dropout), nn.Linear(32, 1))

    def forward(self, inputs):
        values = self.blocks(inputs.transpose(1, 2))
        return self.head(values[:, :, -1]).squeeze(-1)


class Transformer(nn.Module):
    def __init__(self, features, size=64, heads=4, layers=2, feedforward=128,
                 dropout=0.1, lookback=60):
        super().__init__()
        self.project = nn.Linear(features, size)
        self.position = nn.Parameter(torch.zeros(1, lookback, size))
        layer = nn.TransformerEncoderLayer(size, heads, dim_feedforward=feedforward,
                                           dropout=dropout, batch_first=True,
                                           norm_first=True, activation="gelu")
        self.encoder = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)
        self.head = nn.Sequential(nn.LayerNorm(size), nn.Linear(size, 32),
                                  nn.GELU(), nn.Dropout(dropout), nn.Linear(32, 1))

    def forward(self, inputs):
        values = self.project(inputs) + self.position[:, :inputs.shape[1]]
        values = self.encoder(values)
        return self.head(values.mean(dim=1)).squeeze(-1)


class CNN(nn.Module):
    def __init__(self, features, channels=64, dropout=0.1):
        super().__init__()
        self.network = nn.Sequential(
            nn.Conv1d(features, channels, 3, padding=1), nn.BatchNorm1d(channels), nn.GELU(),
            nn.Conv1d(channels, channels, 3, padding=1), nn.BatchNorm1d(channels), nn.GELU(),
            nn.MaxPool1d(2),
            nn.Conv1d(channels, channels, 3, padding=1), nn.BatchNorm1d(channels), nn.GELU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.head = nn.Sequential(nn.LayerNorm(channels), nn.Linear(channels, 32),
                                  nn.GELU(), nn.Dropout(dropout), nn.Linear(32, 1))

    def forward(self, inputs):
        values = self.network(inputs.transpose(1, 2)).squeeze(-1)
        return self.head(values).squeeze(-1)


class GatedDeltaBlock(nn.Module):
    def __init__(self, width, dropout=0.1):
        super().__init__()
        self.q = nn.Linear(width, width)
        self.k = nn.Linear(width, width)
        self.v = nn.Linear(width, width)
        self.alpha = nn.Linear(width, width)
        self.beta = nn.Linear(width, 1)
        self.out = nn.Linear(width, width)
        self.norm = nn.LayerNorm(width)
        self.dropout = nn.Dropout(dropout)

    def forward(self, inputs):
        batch, length, width = inputs.shape
        query = self.q(inputs)
        key = F.normalize(self.k(inputs), dim=-1)
        value = self.v(inputs)
        alpha = torch.sigmoid(self.alpha(inputs))
        beta = torch.sigmoid(self.beta(inputs))
        state = inputs.new_zeros(batch, width, width)
        outputs = []
        for step in range(length):
            key_t = key[:, step]
            value_t = value[:, step]
            predicted = torch.einsum("bd,bde->be", key_t, state)
            delta = beta[:, step] * (value_t - predicted)
            state = alpha[:, step].unsqueeze(-1) * state + delta.unsqueeze(1) * key_t.unsqueeze(-1)
            outputs.append(torch.einsum("bd,bde->be", query[:, step], state))
        result = torch.stack(outputs, dim=1)
        return self.norm(inputs + self.dropout(self.out(result)))


class GDNToken(nn.Module):
    def __init__(self, features, width=64, blocks=2, bins=10, dropout=0.1):
        super().__init__()
        self.input = nn.Sequential(nn.Linear(features, width), nn.LayerNorm(width))
        self.blocks = nn.ModuleList([GatedDeltaBlock(width, dropout) for _ in range(blocks)])
        self.head = nn.Sequential(nn.LayerNorm(width), nn.Linear(width, width),
                                  nn.GELU(), nn.Dropout(dropout), nn.Linear(width, bins))

    def forward(self, inputs):
        values = self.input(inputs)
        for block in self.blocks:
            values = block(values)
        return self.head(values[:, -1])


def build_model(name, feature_count, config):
    if name == "attn_gru":
        return AttnGRU(feature_count, config["hidden_size"], config["layers"], config["dropout"])
    if name == "tcn":
        return TCN(feature_count, config["tcn_channels"], dropout=config["dropout"])
    if name == "transformer":
        return Transformer(feature_count, config["transformer_size"],
                           config["transformer_heads"], config["transformer_layers"],
                           config["transformer_ff"], config["dropout"], config["lookback"])
    if name == "cnn":
        return CNN(feature_count, config["cnn_channels"], config["dropout"])
    if name == "gdn_ntp":
        return GDNToken(feature_count, config["gdn_width"], config["gdn_blocks"],
                        config["token_bins"], config["dropout"])
    raise ValueError(f"unknown model: {name}")


class Muon(torch.optim.Optimizer):
    def __init__(self, params, lr=0.01, momentum=0.95, nesterov=True):
        super().__init__(params, dict(lr=lr, momentum=momentum, nesterov=nesterov))

    @torch.no_grad()
    def step(self, closure=None):
        for group in self.param_groups:
            for parameter in group["params"]:
                if parameter.grad is None:
                    continue
                gradient = parameter.grad
                state = self.state[parameter]
                if "buffer" not in state:
                    state["buffer"] = torch.zeros_like(gradient)
                buffer = state["buffer"]
                buffer.mul_(group["momentum"]).add_(gradient)
                update = gradient.add(buffer, alpha=group["momentum"]) if group["nesterov"] else buffer
                update = _zeropower(update)
                update *= max(1.0, gradient.size(0) / gradient.size(1)) ** 0.5
                parameter.add_(update, alpha=-group["lr"])


def _zeropower(matrix, steps=5):
    a, b, c = 3.4445, -4.7750, 2.0315
    values = matrix.to(torch.float32)
    transposed = values.size(0) > values.size(1)
    if transposed:
        values = values.T
    values = values / (values.norm() + 1e-7)
    for _ in range(steps):
        gram = values @ values.T
        poly = b * gram + c * (gram @ gram)
        values = a * values + poly @ values
    if transposed:
        values = values.T
    return values.to(matrix.dtype)


# ---------------------------------------------------------------------------
# training
# ---------------------------------------------------------------------------


class Batcher:
    def __init__(self, features, target, valid, lookback):
        self.features = features
        self.target = target
        self.valid = valid
        self.lookback = lookback

    def stack(self, dates):
        inputs, targets, sizes = [], [], []
        for t in dates:
            stocks = stocks_on(self.valid, t)
            window = self.features[torch.as_tensor(stocks, device=self.features.device),
                                   int(t) - self.lookback + 1:int(t) + 1]
            inputs.append(window)
            targets.append(self.target[torch.as_tensor(stocks, device=self.target.device), int(t)])
            sizes.append(len(stocks))
        return torch.cat(inputs), torch.cat(targets), sizes


def chunks(sequence, size):
    for start in range(0, len(sequence), size):
        yield sequence[start:start + size]


def evaluate_batches(model, batcher, dates, batch_dates, bins=None):
    model.eval()
    total = 0.0
    count = 0
    predictions = {}
    with torch.no_grad():
        for batch in chunks(list(dates), batch_dates):
            inputs, targets, sizes = batcher.stack(batch)
            output = model(inputs)
            if bins is None:
                total += F.huber_loss(output, targets, delta=1.0, reduction="sum").item()
            else:
                total += F.cross_entropy(output, targets, reduction="sum").item()
            count += len(targets)
            offset = 0
            for t, size in zip(batch, sizes):
                values = output[offset:offset + size].cpu().numpy()
                if bins is not None:
                    shifted = np.exp(values - values.max(axis=1, keepdims=True))
                    probabilities = shifted / shifted.sum(axis=1, keepdims=True)
                    values = (probabilities * np.arange(bins)).sum(axis=1)
                predictions[int(t)] = values
                offset += size
    ic_values = []
    for t in dates:
        stocks = stocks_on(batcher.valid, t)
        values = predictions[int(t)]
        target = batcher.returns[stocks, int(t)]
        if len(stocks) < 10 or np.ptp(values) == 0 or np.ptp(target) == 0:
            continue
        ic_values.append(spearmanr(values, target).statistic)
    return total / max(count, 1), ic_values, predictions


def train_one(name, seed, config, features, target, returns, valid, dates,
              train_dates, validation_dates, feature_count):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if DEVICE.type == "mps" and hasattr(torch, "mps"):
        torch.mps.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    bins = config["token_bins"] if name == "gdn_ntp" else None
    model = build_model(name, feature_count, config).to(DEVICE)
    parameters = sum(parameter.numel() for parameter in model.parameters())
    if name == "gdn_ntp":
        muon = Muon([parameter for parameter in model.parameters() if parameter.ndim >= 2],
                    lr=config["muon_lr"], momentum=config["muon_momentum"])
        adam = torch.optim.AdamW([parameter for parameter in model.parameters()
                                  if parameter.ndim < 2],
                                 lr=config["learning_rate"],
                                 weight_decay=config["weight_decay"])
    else:
        optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"],
                                      weight_decay=config["weight_decay"])
    batcher = Batcher(features, target, valid, config["lookback"])
    batcher.returns = returns
    best_ic = float("-inf")
    best_loss = float("inf")
    best_epoch = 0
    best_state = None
    history = []
    started = time.monotonic()
    steps_per_epoch = math.ceil(len(train_dates) / config["batch_dates"])
    seen_per_epoch = sum(int(valid[:, int(t)].sum()) for t in train_dates)
    for epoch in range(1, config["max_epochs"] + 1):
        epoch_started = time.monotonic()
        model.train()
        rng = np.random.default_rng(seed * 1000 + epoch)
        order = rng.permutation(len(train_dates))
        total = 0.0
        count = 0
        for batch in chunks(order, config["batch_dates"]):
            selected = train_dates[batch]
            inputs, targets, _ = batcher.stack(selected)
            output = model(inputs)
            loss = (F.cross_entropy(output, targets) if name == "gdn_ntp"
                    else F.huber_loss(output, targets, delta=config["huber_delta"], reduction="mean"))
            if not torch.isfinite(loss):
                raise RuntimeError("non-finite training loss")
            if name == "gdn_ntp":
                muon.zero_grad(set_to_none=True)
                adam.zero_grad(set_to_none=True)
            else:
                optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            if name == "gdn_ntp":
                muon.step()
                adam.step()
            else:
                optimizer.step()
            total += loss.item() * len(targets)
            count += len(targets)
        train_loss = total / max(count, 1)
        validation_loss, validation_ic, _ = evaluate_batches(
            model, batcher, validation_dates, config["batch_dates"], bins=bins)
        row = {
            "model": name, "seed": seed, "epoch": epoch,
            "train_loss": train_loss, "validation_loss": validation_loss,
            "validation_ic": float(np.mean(validation_ic)) if validation_ic else float("nan"),
            "seconds": time.monotonic() - epoch_started,
        }
        history.append(row)
        log(json.dumps(row))
        # The HK universe is much smaller than the A-share one, so validation
        # Huber loss is nearly flat across the first epochs.  Select and stop
        # on validation Rank IC instead; every other hyper-parameter is shared
        # with the A-share line.
        validation_ic_mean = float(np.mean(validation_ic)) if validation_ic else float("-inf")
        if validation_ic_mean > best_ic + 1e-6:
            best_ic = validation_ic_mean
            best_loss = validation_loss
            best_epoch = epoch
            best_state = {key: value.detach().clone()
                          for key, value in model.state_dict().items()}
        if epoch - best_epoch >= config["patience"]:
            break
    train_seconds = time.monotonic() - started
    if best_state is not None:
        model.load_state_dict(best_state)
    else:
        best_epoch = len(history)
        best_loss = float(history[-1]["validation_loss"]) if history else float("nan")
    return {
        "model": model, "history": history, "best_epoch": best_epoch,
        "best_loss": best_loss, "parameters": parameters,
        "train_seconds": train_seconds, "epochs_run": len(history),
        "steps_per_epoch": steps_per_epoch, "seen_per_epoch": seen_per_epoch,
        "bins": bins,
    }


def score_matrix(model, batcher, dates, bins=None):
    matrix = np.full(batcher.valid.shape, np.nan, dtype=np.float32)
    _, _, predictions = evaluate_batches(model, batcher, dates, 8, bins=bins)
    for t, values in predictions.items():
        matrix[stocks_on(batcher.valid, t), t] = values
    return matrix


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def ic_frame(score, returns, valid, dates, signal_dates):
    rows = []
    for t in signal_dates:
        stocks = stocks_on(valid, t)
        values = score[stocks, int(t)]
        target = returns[stocks, int(t)]
        if len(stocks) < 10 or np.ptp(values) == 0 or np.ptp(target) == 0:
            continue
        rows.append({"date": dates[int(t)], "ic": float(spearmanr(values, target).statistic),
                     "stocks": int(len(stocks))})
    return pd.DataFrame(rows)


def ic_summary(frame):
    if frame.empty:
        return {"mean_ic": None, "std_ic": None, "icir": None,
                "positive_fraction": None, "dates": 0}
    values = frame.ic.to_numpy()
    std = float(values.std(ddof=1))
    return {
        "mean_ic": float(values.mean()),
        "std_ic": std,
        "icir": float(values.mean() / std) if std > 0 else None,
        "positive_fraction": float((values > 0).mean()),
        "dates": int(len(values)),
    }


def decile_spread(score, returns, valid, signal_dates):
    spreads = []
    for t in signal_dates:
        stocks = stocks_on(valid, t)
        values = score[stocks, int(t)]
        target = returns[stocks, int(t)]
        if len(stocks) < 20 or np.ptp(values) == 0:
            continue
        order = np.argsort(values, kind="stable")
        bucket = np.array_split(order, 10)
        spreads.append(float(target[bucket[-1]].mean() - target[bucket[0]].mean()))
    if not spreads:
        return {"mean": None, "t": None}
    values = np.asarray(spreads)
    return {
        "mean": float(values.mean()),
        "t": float(values.mean() / values.std(ddof=1) * np.sqrt(len(values)))
        if values.std(ddof=1) > 0 else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="train HK model routes")
    parser.add_argument("--models", default=None, help="comma-separated subset")
    parser.add_argument("--seeds", default=None)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    config = dict(CONFIG)
    if args.models:
        config["models"] = [item.strip() for item in args.models.split(",") if item.strip()]
    if args.seeds:
        config["seeds"] = [int(item) for item in args.seeds.split(",") if item.strip()]
    if args.smoke:
        config["models"] = config["models"][:2]
        config["seeds"] = [42]
        config["max_epochs"] = 1
        config["patience"] = 1
    output = Path(args.output) if args.output else DATA / "artifacts"
    (output / "scores").mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(4)
    log(json.dumps({"event": "start", "device": str(DEVICE), "host": platform.node(),
                    "config": {key: config[key] for key in ("models", "seeds", "max_epochs", "patience")}}))
    panel, metadata = load_panel()
    dates = np.array(metadata["dates"])
    returns = panel["returns"]
    valid = panel["eligible"].astype(bool) & np.isfinite(returns)
    splits = split_dates(dates, valid, config)
    log(json.dumps({name: {"days": len(value),
                           "samples": int(sum(int(valid[:, int(t)].sum()) for t in value))}
                    for name, value in splits.items()}, indent=2))
    target = rank_targets(returns, valid)
    tokens = token_targets(returns, valid, config["token_bins"]) if "gdn_ntp" in config["models"] else None
    normalized = np.nan_to_num((panel["features"] - panel["center"]) / panel["scale"],
                               nan=0.0, posinf=8.0, neginf=-8.0).clip(-8.0, 8.0)
    features = torch.from_numpy(normalized.astype(np.float32)).to(DEVICE)
    target_tensor = torch.from_numpy(target).to(DEVICE)
    tokens_tensor = torch.from_numpy(tokens).to(DEVICE) if tokens is not None else None

    results = {}
    histories = []
    for name in config["models"]:
        seed_results = {}
        matrices = []
        for seed in config["seeds"]:
            log(f"training {name} seed {seed}")
            target_for_model = tokens_tensor if name == "gdn_ntp" else target_tensor
            trained = train_one(name, seed, config, features, target_for_model, returns, valid,
                                dates, splits["train"], splits["validation"],
                                len(metadata["feature_names"]))
            batcher = Batcher(features, target_for_model, valid, config["lookback"])
            batcher.returns = returns
            matrix = score_matrix(trained["model"], batcher, splits["test"],
                                  bins=config["token_bins"] if name == "gdn_ntp" else None)
            frame = ic_frame(matrix, returns, valid, dates, splits["test"])
            summary = ic_summary(frame)
            np.savez_compressed(output / "scores" / f"{name}_{seed}.npz", score=matrix)
            seed_results[seed] = {
                "parameters": trained["parameters"],
                "train_seconds": trained["train_seconds"],
                "best_epoch": trained["best_epoch"],
                "epochs_run": trained["epochs_run"],
                "steps_per_epoch": trained["steps_per_epoch"],
                "seen_samples_per_epoch": trained["seen_per_epoch"],
                "validation_loss": trained["best_loss"],
                "ic": summary,
            }
            histories.extend(trained["history"])
            matrices.append(matrix)
            log(json.dumps({"event": "seed_done", "model": name, "seed": seed,
                            "best_epoch": trained["best_epoch"],
                            "seconds": round(trained["train_seconds"], 1),
                            "mean_ic": summary["mean_ic"]}))
        ensemble = np.nanmean(np.stack(matrices), axis=0)
        np.savez_compressed(output / "scores" / f"{name}_ensemble.npz", score=ensemble)
        ensemble_frame = ic_frame(ensemble, returns, valid, dates, splits["test"])
        results[name] = {
            "seeds": seed_results,
            "ensemble": {
                "ic": ic_summary(ensemble_frame),
                "decile_spread": decile_spread(ensemble, returns, valid, splits["test"]),
            },
            "parameters": seed_results[config["seeds"][0]]["parameters"],
            "train_seconds": float(np.mean([seed_results[s]["train_seconds"] for s in config["seeds"]])),
        }
        pd.DataFrame(histories).to_csv(output / "training_history.csv", index=False)
        log(json.dumps({"event": "model_done", "model": name,
                        "mean_ic": results[name]["ensemble"]["ic"]["mean_ic"]}))
    (output / "metrics.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    log("done")


if __name__ == "__main__":
    main()
