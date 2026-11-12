"""Turn a training run's metrics into a summary table and two figures."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(os.environ.get(
    "VLA_CODE_DIR", Path.home() / "labs" / "VLA" / "Share" / "v2"
)).expanduser()))

from experiments.dreamina_stock.data import load_panel  # noqa: E402


def equity_curve(scores, panel, dates, config):
    import compute

    panel_dir = Path(config["panel_dir"]).expanduser()
    panel_dict = dict(np.load(panel_dir / "panel.npz", allow_pickle=False))
    metadata = json.loads((panel_dir / "metadata.json").read_text())
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
    return ledger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", default=str(HERE / "reports"))
    args = parser.parse_args()
    reports = Path(args.reports)
    metrics = json.loads((reports / "metrics.json").read_text())
    config = metrics["config"]
    config["panel_dir"] = os.environ.get("VLA_PANEL_DIR", config["panel_dir"])
    panel = load_panel(config["panel_dir"])
    from experiments.dreamina_stock.data import signal_dates

    test_dates = signal_dates(
        panel, config["test_start"], config["test_end"], config["lookback"], config["horizon"]
    )

    # training curves
    history = metrics["history"]
    epochs = [row["epoch"] for row in history]
    warmup = int(config.get("vae_warmup_epochs", 0))
    figure, axes = plt.subplots(1, 2, figsize=(10, 3.6), dpi=150)
    trained = [row for row in history if row["epoch"] > warmup]
    axes[0].plot([row["epoch"] for row in trained], [row["train_fm"] for row in trained], label="train FM")
    axes[0].plot([row["epoch"] for row in trained], [row["val_fm"] for row in trained], label="val FM")
    axes[0].set_title("flow-matching loss (after %d VAE warmup epochs)" % warmup)
    axes[0].set_xlabel("epoch")
    axes[0].legend()
    axes[1].plot(epochs, [row["train_recon"] for row in history], label="train recon")
    axes[1].plot(epochs, [row["val_recon"] for row in history], label="val recon")
    axes[1].set_title("VAE reconstruction")
    axes[1].set_xlabel("epoch")
    axes[1].legend()
    figure.tight_layout()
    figure.savefig(reports / "training.png")
    plt.close(figure)

    # equity curves
    curves = {}
    for mode in ("generated", "context"):
        path = reports / f"scores_{mode}.npy"
        if path.exists():
            curves[mode] = equity_curve(np.load(path), panel, list(test_dates), config)
    reference_path = Path(config["panel_dir"]).expanduser() / "scores" / "attn_gru_ensemble.npz"
    if reference_path.exists():
        reference = np.load(reference_path)["score"].T
        curves["gru_reference"] = equity_curve(reference, panel, list(test_dates), config)

    figure, axis = plt.subplots(figsize=(8, 4), dpi=150)
    for name, ledger in curves.items():
        axis.plot(pd.to_datetime(ledger.index), ledger.equity.values, label=name, linewidth=1.3)
    axis.set_title("low-turnover backtest, test 2024-01 .. 2026-11")
    axis.set_ylabel("equity (start = 1)")
    axis.legend()
    axis.xaxis.set_major_locator(mdates.YearLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(reports / "equity.png")
    plt.close(figure)

    # summary csv
    rows = []
    for mode in ("generated", "context", "generated_only", "oracle", "reference_gru"):
        item = metrics.get(mode)
        if not item:
            continue
        backtest = item.get("backtest", {})
        rows.append(
            {
                "probe": mode,
                "val_ic": item.get("val_ic_mean", float("nan")),
                "test_ic": item.get("test_ic_mean", float("nan")),
                "test_ic_std": item.get("test_ic_std", float("nan")),
                "total_return": backtest.get("total_return", float("nan")),
                "annualized_return": backtest.get("annualized_return", float("nan")),
                "max_drawdown": backtest.get("max_drawdown", float("nan")),
                "sharpe": backtest.get("sharpe_zero_cash_rate", float("nan")),
                "annualized_cost": backtest.get("annualized_cost_fraction", float("nan")),
            }
        )
    with open(reports / "summary.csv", "w") as handle:
        header = list(rows[0]) if rows else []
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(str(row[key]) for key in header) + "\n")
    print(json.dumps({"parameters": metrics["parameters"], "rows": rows}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
