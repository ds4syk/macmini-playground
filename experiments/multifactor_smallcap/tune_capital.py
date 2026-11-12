"""Capital-aware sweep for the small-cap account, focused on 30k.

Tests portfolio size, lot-aware selection and the residual-cash sweep on the
2022-2023 validation window, then reports the test window for reference.
Selection is by validation metrics only.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
PANEL = ROOT / "panel" / "panel.npz"
FACTORS = ROOT / "factors" / "factors.npz"
SCORES = ROOT / "results" / "scores.npz"
OUT = ROOT / "results"

VALIDATION_START = "2022-01-04"
VALIDATION_END = "2023-12-29"
TEST_START = "2024-01-02"
TEST_END = "2026-11-12"


def masked_scores(monthly, month_indices, n_dates, keep_days):
    keep = set(int(day) for day in keep_days)
    result = np.full((monthly.shape[0], n_dates), np.nan, dtype=np.float32)
    for month, day in enumerate(month_indices):
        if int(day) in keep:
            result[:, int(day)] = monthly[:, month]
    return result


def signal_days_in(month_dates, month_indices, start_date, end_date, stride):
    selected = [
        int(day) for date, day in zip(month_dates, month_indices) if start_date <= date <= end_date
    ]
    return selected[::stride]


def evaluate(panel, dates, stocks, scores, start_signal, start_date, end_day, capital, params):
    from . import account

    rows, units, cost_basis, trades, _ = account.run(
        dates,
        stocks,
        panel["close"],
        panel["pct_chg"],
        panel["float_mcap"],
        panel["is_st"],
        panel["tradable"],
        scores,
        capital,
        start_signal,
        end_day,
        params,
    )
    trimmed_rows = [row for row in rows if row["date"] >= start_date]
    trimmed_trades = [trade for trade in trades if trade["date"] >= start_date]
    metrics = account.metrics(trimmed_rows, trimmed_trades, capital)
    final_equity = trimmed_rows[-1]["equity"]
    cash_pct = trimmed_rows[-1]["cash"] / final_equity * 100 if final_equity else 0
    return metrics, int((units > 0).sum()), round(cash_pct, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=30000)
    parser.add_argument("--models", default="mf_lgbm")
    parser.add_argument("--sizes", default="6,8,10,12,15,20,30,50")
    parser.add_argument("--lot-aware", default="0,1")
    parser.add_argument("--sweep-modes", default="top")
    parser.add_argument("--max-weights", default="2.0")
    args = parser.parse_args()

    panel = dict(np.load(PANEL))
    factors = np.load(FACTORS)
    scores = np.load(SCORES)
    dates = [str(item) for item in panel["dates"]]
    stocks = [str(item) for item in panel["stocks"]]
    month_dates = [str(item) for item in factors["month_dates"]]
    month_indices = factors["month_indices"].astype(int)
    validation_start_signal = max(
        day for date, day in zip(month_dates, month_indices) if date <= "2021-12-31"
    )
    test_start_signal = max(
        day for date, day in zip(month_dates, month_indices) if date <= "2023-12-29"
    )
    validation_end_day = dates.index(VALIDATION_END)
    test_end_day = dates.index(TEST_END)

    sizes = [int(item) for item in args.sizes.split(",") if item]
    models = [item for item in args.models.split(",") if item]
    lot_flags = [bool(int(item)) for item in args.lot_aware.split(",")]
    sweep_modes = [item for item in args.sweep_modes.split(",") if item]
    max_weights = [float(item) for item in args.max_weights.split(",") if item]
    results = []
    for model, size, lot_aware in itertools.product(models, sizes, lot_flags):
        modes = sweep_modes if lot_aware else ["top"]
        weights = max_weights if lot_aware else [2.0]
        for sweep_mode, max_weight in itertools.product(modes, weights):
            params = {
                "portfolio_size": size,
                "buffer_size": size,
                "max_replace": size,
                "rebalance_stride": 3,
                "lot_aware": lot_aware,
                "sweep_mode": sweep_mode,
                "max_weight_mult": max_weight,
                "afford_mult": 1.5,
            }
            row = {"model": model, "config": params}
            for split, signal, start_date, end_day in (
                ("validation", validation_start_signal, VALIDATION_START, validation_end_day),
                ("test", test_start_signal, TEST_START, test_end_day),
            ):
                signal_start = "2021-12-31" if split == "validation" else "2023-12-29"
                keep = signal_days_in(month_dates, month_indices, signal_start, dates[end_day], 1)
                masked = masked_scores(scores[model], month_indices, len(dates), keep)
                metrics, positions, cash_pct = evaluate(
                    panel,
                    dates,
                    stocks,
                    masked,
                    signal,
                    start_date,
                    end_day,
                    args.capital,
                    params,
                )
                row[split] = {
                    "ret_pct": round(metrics["ret_pct"], 2),
                    "max_drawdown": round(metrics["max_drawdown"], 2),
                    "sharpe": round(metrics["sharpe"], 2),
                    "trades": metrics["trades"],
                    "positions": positions,
                    "cash_pct": cash_pct,
                }
            results.append(row)
            print(
                f"{model} {int(args.capital)} N={size} lot={int(lot_aware)} {sweep_mode} w={max_weight} | "
                f"val {row['validation']['ret_pct']:+.1f}% Sh {row['validation']['sharpe']:.2f} "
                f"cash {row['validation']['cash_pct']:.0f}% | "
                f"test {row['test']['ret_pct']:+.1f}% Sh {row['test']['sharpe']:.2f} "
                f"DD {row['test']['max_drawdown']:.1f} cash {row['test']['cash_pct']:.0f}%",
                flush=True,
            )
    ranked = sorted(
        results,
        key=lambda row: (row["validation"]["sharpe"], row["validation"]["ret_pct"]),
        reverse=True,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"tuning_{int(args.capital)}.json").write_text(
        json.dumps({"capital": args.capital, "results": results, "ranked": ranked}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("\nTop by validation Sharpe:")
    for row in ranked[:6]:
        print(row["model"], row["config"]["portfolio_size"], row["config"]["lot_aware"], row["validation"], row["test"])


if __name__ == "__main__":
    main()
