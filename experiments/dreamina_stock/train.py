"""Pretrain a Dreamina-style stock video diffusion model, probe it, and backtest.

Run:
    ~/.venvs/dreamina/bin/python -m experiments.dreamina_stock.train --config experiments/dreamina_stock/config.json

Smoke:
    ... train.py --smoke
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch
from scipy.stats import spearmanr
from torch.nn import functional as F

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(os.environ.get(
    "VLA_CODE_DIR", Path.home() / "labs" / "VLA" / "Share" / "v2"
)).expanduser()))

from experiments.dreamina_stock.data import (  # noqa: E402
    ClipBuilder,
    load_panel,
    rank_target,
    signal_dates,
)
from experiments.dreamina_stock.model import (  # noqa: E402
    StockDiT,
    flow_matching_loss,
    sample_future,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output", default=str(HERE / "reports"))
    return parser.parse_args()


def load_config(path: str, smoke: bool) -> dict:
    config = json.loads(Path(path).read_text())
    config["smoke"] = smoke
    if smoke:
        config["epochs"] = 2
        config["vae_warmup_epochs"] = 1
        config["probe_epochs"] = 3
        config["depth"] = 2
        config["batch_dates"] = 2
        config["gen_steps"] = 2
        config["max_train_dates"] = 24
        config["max_eval_dates"] = 12
        config["dim"] = 96
    return config


def seed_everything(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def device_of() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def to_device(batch: dict, device: torch.device) -> dict:
    return {key: torch.as_tensor(value, device=device) for key, value in batch.items()}


def batches(dates: np.ndarray, size: int):
    for start in range(0, len(dates), size):
        yield dates[start:start + size]


def build_model(config: dict, panel, device: torch.device) -> StockDiT:
    builder = ClipBuilder(panel, config["group_size"])
    return StockDiT(
        channels=panel.channels,
        feature_count=panel.feature_count,
        group_size=config["group_size"],
        groups=builder.groups,
        horizon=config["horizon"],
        regime_dim=panel.regime.shape[1],
        dim=config["dim"],
        depth=config["depth"],
        heads=config["heads"],
        ratio=config["mlp_ratio"],
    ).to(device)


# ---------------------------------------------------------------------------
# stage 1: conditional flow-matching pretraining
# ---------------------------------------------------------------------------


def pretrain(model, builder, dates, val_dates, config, device):
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config["learning_rate"], weight_decay=config["weight_decay"]
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(config["epochs"], 1)
    )
    history = []
    best = float("inf")
    best_state = None
    warmup = config.get("vae_warmup_epochs", 0)
    for epoch in range(config["epochs"]):
        model.train()
        started = time.time()
        total = total_recon = total_fm = 0.0
        count = 0
        for batch_dates in batches(dates, config["batch_dates"]):
            batch = to_device(builder.clip(batch_dates, config["lookback"], config["horizon"]), device)
            fm_loss, recon_loss, _ = flow_matching_loss(
                model, batch["context"], batch["target"], batch["regime"]
            )
            if epoch < warmup:
                loss = recon_loss
                fm_loss = torch.zeros_like(recon_loss)
            else:
                loss = fm_loss + config["recon_weight"] * recon_loss
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), config["grad_clip"])
            optimizer.step()
            total += float(loss.detach())
            total_recon += float(recon_loss.detach())
            total_fm += float(fm_loss.detach())
            count += 1
        scheduler.step()

        model.eval()
        val_total = val_recon = val_fm = 0.0
        val_count = 0
        with torch.no_grad():
            for batch_dates in batches(val_dates[: config["max_eval_dates"]], config["batch_dates"]):
                batch = to_device(builder.clip(batch_dates, config["lookback"], config["horizon"]), device)
                fm_loss, recon_loss, _ = flow_matching_loss(
                    model, batch["context"], batch["target"], batch["regime"]
                )
                val_total += float((fm_loss + config["recon_weight"] * recon_loss).detach())
                val_recon += float(recon_loss.detach())
                val_fm += float(fm_loss.detach())
                val_count += 1
        record = {
            "epoch": epoch + 1,
            "train_loss": total / max(count, 1),
            "train_fm": total_fm / max(count, 1),
            "train_recon": total_recon / max(count, 1),
            "val_loss": val_total / max(val_count, 1) if val_count else float("nan"),
            "val_fm": val_fm / max(val_count, 1) if val_count else float("nan"),
            "val_recon": val_recon / max(val_count, 1) if val_count else float("nan"),
            "seconds": round(time.time() - started, 2),
        }
        history.append(record)
        print(f"epoch {record['epoch']:02d} train {record['train_loss']:.3f} "
              f"(fm {record['train_fm']:.3f} recon {record['train_recon']:.2f}) "
              f"val {record['val_loss']:.3f} (fm {record['val_fm']:.3f} "
              f"recon {record['val_recon']:.2f}) {record['seconds']}s", flush=True)
        score = record["val_recon"] if val_count else record["train_recon"]
        if np.isfinite(score) and score < best:
            best = score
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
    if best_state is not None:
        model.load_state_dict(best_state)
    return history


# ---------------------------------------------------------------------------
# stage 2: probes on generated / raw context / oracle future features
# ---------------------------------------------------------------------------


class Probe(torch.nn.Module):
    def __init__(self, inputs: int, hidden: int = 32):
        super().__init__()
        self.net = torch.nn.Sequential(
            torch.nn.LayerNorm(inputs),
            torch.nn.Linear(inputs, hidden),
            torch.nn.GELU(),
            torch.nn.Linear(hidden, 1),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


def collect_features(model, builder, dates, config, device, mode: str, progress: str = ""):
    """Per-stock features for the probe.

    Returns dict with:
      future  [M, H*C]  generated future / true future / last context (mode dependent)
      context [M, H*C]  last H context frames
      labels  [M]       cross-sectional rank of the 5-day forward return
      dates   [M], stocks [M]
    """
    model.eval()
    future_parts, context_parts, label_parts, date_parts, stock_parts = [], [], [], [], []
    for batch_dates in batches(dates, config["batch_dates"]):
        batch = to_device(builder.clip(batch_dates, config["lookback"], config["horizon"]), device)
        if mode == "generated":
            future = sample_future(
                model, batch["context"], batch["regime"],
                steps=config["gen_steps"], seed=config["seed"],
            )
        elif mode == "oracle":
            future = batch["target"]
        elif mode == "context":
            future = batch["context"][:, -config["horizon"]:]
        else:
            raise ValueError(mode)

        count = builder.panel.stock_count
        future = future[:, :, :count]
        context = batch["context"][:, -config["horizon"]:, :count]

        def flat(values):
            return values.permute(0, 2, 1, 3).reshape(
                values.shape[0], count, config["horizon"] * builder.panel.channels
            )

        future_flat, context_flat = flat(future), flat(context)
        valid = batch["label_valid"].detach().cpu().numpy() > 0
        for row, t in enumerate(batch_dates):
            stocks = np.flatnonzero(valid[row])
            if len(stocks) == 0:
                continue
            future_parts.append(future_flat[row, stocks].detach().cpu().to(torch.float16).numpy())
            context_parts.append(context_flat[row, stocks].detach().cpu().to(torch.float16).numpy())
            ranks = rank_target(
                builder.panel.returns[int(t): int(t) + 1][:, stocks],
                np.ones((1, len(stocks)), dtype=np.float32),
            )[0]
            label_parts.append(ranks)
            date_parts.append(np.full(len(stocks), int(t), dtype=np.int64))
            stock_parts.append(stocks.astype(np.int64))
    total = sum(len(x) for x in future_parts)
    print(f"  collected {mode}: {total} samples {progress}", flush=True)
    empty = np.empty((0, 1), dtype=np.float16)
    return {
        "future": np.concatenate(future_parts) if future_parts else empty,
        "context": np.concatenate(context_parts) if context_parts else empty,
        "labels": np.concatenate(label_parts) if label_parts else np.empty((0,), np.float32),
        "dates": np.concatenate(date_parts) if date_parts else np.empty((0,), np.int64),
        "stocks": np.concatenate(stock_parts) if stock_parts else np.empty((0,), np.int64),
    }


def train_probe(features, labels, config, device):
    probe = Probe(features.shape[1]).to(device)
    x = torch.as_tensor(features.astype(np.float32))
    y = torch.as_tensor(labels)
    loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(x, y), batch_size=8192, shuffle=True
    )
    optimizer = torch.optim.AdamW(probe.parameters(), lr=2e-3, weight_decay=1e-3)
    for epoch in range(config["probe_epochs"]):
        total = 0.0
        count = 0
        for xb, yb in loader:
            xb, yb = xb.to(device), yb.to(device)
            output = probe(xb)
            loss = F.huber_loss(output, yb, delta=1.0)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(xb)
            count += len(xb)
        print(f"  probe epoch {epoch + 1:02d} huber {total / max(count, 1):.4f}", flush=True)
    probe.eval()
    return probe


@torch.no_grad()
def probe_scores(probe, features, dates, stocks, panel, device):
    values = []
    for start in range(0, len(features), 65536):
        chunk = torch.as_tensor(features[start:start + 65536].astype(np.float32), device=device)
        values.append(probe(chunk).cpu().numpy())
    values = np.concatenate(values) if values else np.empty((0,))
    scores = np.full((panel.date_count, panel.stock_count), np.nan, dtype=np.float32)
    for value, t, stock in zip(values, dates, stocks):
        scores[int(t), int(stock)] = value
    return scores


def rank_ic(scores, panel, dates):
    values = []
    for t in dates:
        stocks = np.flatnonzero(
            np.isfinite(scores[t]) & (panel.valid[t] > 0) & np.isfinite(panel.returns[t])
        )
        if len(stocks) < 10 or np.ptp(scores[t, stocks]) == 0:
            continue
        values.append(spearmanr(scores[t, stocks], panel.returns[t, stocks]).statistic)
    return values


# ---------------------------------------------------------------------------
# stage 3: portfolio backtest (Share v2 accounting)
# ---------------------------------------------------------------------------


def backtest_scores(scores, panel, dates, config):
    import compute  # Share v2 single-file compute entry point

    panel_dir = Path(config["panel_dir"]).expanduser()
    panel_dict = dict(np.load(panel_dir / "panel.npz", allow_pickle=False))
    metadata = json.loads((panel_dir / "metadata.json").read_text())
    metadata["dates"] = list(metadata["dates"])
    bt_config = {
        "horizon": config["horizon"],
        "portfolio_size": config["portfolio_size"],
        "buffer_size": config["buffer_size"],
        "max_replace": config["max_replace"],
        "rebalance_days": config["rebalance_days"],
        "buy_cost": config["buy_cost"],
        "sell_cost": config["sell_cost"],
    }
    score_dict = {}
    for t in dates:
        stocks = np.flatnonzero(np.isfinite(scores[t]) & (panel.valid[t] > 0))
        if len(stocks):
            score_dict[int(t)] = (stocks, scores[t, stocks].astype(np.float64))
    ledger = compute.backtest(panel_dict, metadata, score_dict, "low_turnover", bt_config)
    metrics = compute.portfolio_metrics(ledger, bt_config)
    return {key: float(value) for key, value in metrics.items()}


def load_reference_scores(config):
    path = Path(config["panel_dir"]).expanduser() / "scores" / "attn_gru_ensemble.npz"
    if not path.exists():
        return None
    return np.load(path)["score"].T  # [N, T] -> [T, N]


def probe_inputs(collected, mode: str, split: str) -> tuple[np.ndarray, dict]:
    if mode == "generated":
        data = collected[split]["generated"]
        return np.concatenate([data["future"], data["context"]], axis=1), data
    if mode == "generated_only":
        data = collected[split]["generated"]
        return data["future"], data
    if mode == "oracle":
        data = collected[split]["oracle"]
        return np.concatenate([data["future"], collected[split]["context"]["context"]], axis=1), data
    data = collected[split]["context"]
    return data["context"], data


def main():
    args = parse_args()
    config = load_config(args.config, args.smoke)
    device = device_of()
    print("device:", device, flush=True)
    seed_everything(config["seed"])

    panel = load_panel(config["panel_dir"])
    builder = ClipBuilder(panel, config["group_size"])
    print(f"panel: {panel.date_count} days x {panel.stock_count} stocks "
          f"x {panel.feature_count} features", flush=True)

    train_dates = signal_dates(panel, config["train_start"], config["train_end"],
                               config["lookback"], config["horizon"])
    val_dates = signal_dates(panel, config["validation_start"], config["validation_end"],
                             config["lookback"], config["horizon"])
    test_dates = signal_dates(panel, config["test_start"], config["test_end"],
                              config["lookback"], config["horizon"])
    if config["smoke"]:
        train_dates = train_dates[: config["max_train_dates"]]
        val_dates = val_dates[: config["max_eval_dates"]]
        test_dates = test_dates[: config["max_eval_dates"]]
    print(f"dates: train {len(train_dates)} val {len(val_dates)} test {len(test_dates)}", flush=True)

    model = build_model(config, panel, device)
    params = model.parameter_count()
    print(f"parameters: {params:,} ({params * 4 / 2**20:.2f} MiB fp32)", flush=True)

    started = time.time()
    history = pretrain(model, builder, train_dates, val_dates, config, device)
    train_seconds = time.time() - started

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"config": config, "state_dict": model.state_dict(), "parameters": params},
        output_dir / "model.pt",
    )

    result = {
        "parameters": params,
        "parameter_mib": params * 4 / 2**20,
        "train_seconds": round(train_seconds, 1),
        "history": history,
        "dates": {"train": int(len(train_dates)), "validation": int(len(val_dates)),
                  "test": int(len(test_dates))},
        "config": config,
    }

    print("collecting probe features", flush=True)
    splits = {"train": train_dates, "validation": val_dates, "test": test_dates}
    collected = {}
    for name, dates in splits.items():
        collected[name] = {
            "generated": collect_features(model, builder, dates, config, device, "generated", name),
            "context": collect_features(model, builder, dates, config, device, "context", name),
        }
        if not config["smoke"]:
            collected[name]["oracle"] = collect_features(
                model, builder, dates, config, device, "oracle", name
            )

    probes = ["generated", "context", "generated_only"] + ([] if config["smoke"] else ["oracle"])
    for mode in probes:
        train_x, train_meta = probe_inputs(collected, mode, "train")
        probe = train_probe(train_x, train_meta["labels"], config, device)
        scores_by_split = {}
        for name in ("validation", "test"):
            x, meta = probe_inputs(collected, mode, name)
            scores_by_split[name] = probe_scores(
                probe, x, meta["dates"], meta["stocks"], panel, device
            )

        val_ic = rank_ic(scores_by_split["validation"], panel, val_dates)
        test_ic = rank_ic(scores_by_split["test"], panel, test_dates)
        result[mode] = {
            "val_ic_mean": float(np.mean(val_ic)) if val_ic else 0.0,
            "val_ic_std": float(np.std(val_ic)) if val_ic else 0.0,
            "test_ic_mean": float(np.mean(test_ic)) if test_ic else 0.0,
            "test_ic_std": float(np.std(test_ic)) if test_ic else 0.0,
            "test_ic_days": int(len(test_ic)),
        }
        try:
            result[mode]["backtest"] = backtest_scores(
                scores_by_split["test"], panel, list(test_dates), config
            )
        except Exception as exc:  # noqa: BLE001
            result[mode]["backtest_error"] = repr(exc)
            print("  backtest failed:", exc, flush=True)
        np.save(output_dir / f"scores_{mode}.npy", scores_by_split["test"])
        print(f"  {mode}: val IC {result[mode]['val_ic_mean']:.4f} "
              f"test IC {result[mode]['test_ic_mean']:.4f}", flush=True)

    reference_scores = load_reference_scores(config)
    if reference_scores is not None:
        test_ic = rank_ic(reference_scores, panel, test_dates)
        result["reference_gru"] = {
            "test_ic_mean": float(np.mean(test_ic)) if test_ic else 0.0,
            "test_ic_std": float(np.std(test_ic)) if test_ic else 0.0,
            "source": "Share v2 attn_gru_ensemble scores",
        }
        try:
            result["reference_gru"]["backtest"] = backtest_scores(
                reference_scores, panel, list(test_dates), config
            )
        except Exception as exc:  # noqa: BLE001
            result["reference_gru"]["backtest_error"] = repr(exc)

    (output_dir / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("history", "config")}, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
