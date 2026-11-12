"""Validation-based parameter sweep for the small-cap multi-factor strategy.

The 2024-2026 test window has already been viewed, so selection must use the
2022-2023 validation window.  This sweep varies the executable parameters
(portfolio size, buffer width, rebalance frequency) and the LightGBM
hyper-parameters, then reports both validation and test metrics.

Run on the lab::

    MF_ROOT=... python -m experiments.multifactor_smallcap.tune
"""
from __future__ import annotations

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
CAPITAL = 100000
MODELS = ("mf_lgbm", "mf_equal")


def month_signal_days(month_dates, month_indices, dates, start: str, end: str, stride: int):
    selected = [
        int(day)
        for date, day in zip(month_dates, month_indices)
        if start <= date <= end
    ]
    return selected[::stride]


def masked_scores(monthly: np.ndarray, month_indices, n_dates: int, keep_days) -> np.ndarray:
    keep = set(int(day) for day in keep_days)
    result = np.full((monthly.shape[0], n_dates), np.nan, dtype=np.float32)
    for month, day in enumerate(month_indices):
        if int(day) in keep:
            result[:, int(day)] = monthly[:, month]
    return result


def evaluate_account(panel, metadata, dates, stocks, scores, start_signal, end_day, params):
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
        CAPITAL,
        start_signal,
        end_day,
        params,
    )
    start_date = dates[start_signal + 1]
    trimmed_rows = [row for row in rows if row["date"] >= start_date]
    trimmed_trades = [trade for trade in trades if trade["date"] >= start_date]
    metrics = account.metrics(trimmed_rows, trimmed_trades, CAPITAL)
    return metrics, int((units > 0).sum()), float(rows[-1]["cash"])


def main() -> None:
    panel = dict(np.load(PANEL))
    factors = np.load(FACTORS)
    scores_npz = np.load(SCORES)
    dates = [str(item) for item in panel["dates"]]
    stocks = [str(item) for item in panel["stocks"]]
    month_dates = [str(item) for item in factors["month_dates"]]
    month_indices = factors["month_indices"].astype(int)
    scores = np.load(SCORES)

    validation_start_signal = max(
        day for date, day in zip(month_dates, month_indices) if date <= "2021-12-31"
    )
    test_start_signal = max(
        day for date, day in zip(month_dates, month_indices) if date <= "2023-12-29"
    )
    validation_end_day = dates.index(VALIDATION_END)
    test_end_day = dates.index(TEST_END)

    configs = []
    for size, buffer_mult, stride in itertools.product((20, 30, 50), (1.0, 1.5), (1, 3)):
        configs.append(
            {
                "portfolio_size": size,
                "buffer_size": int(round(size * buffer_mult)),
                "max_replace": size,
                "rebalance_months": stride,
            }
        )

    results = []
    for config in configs:
        for model in MODELS:
            row = {"config": config, "model": model}
            for split, start_signal, start_date, end_day in (
                ("validation", validation_start_signal, VALIDATION_START, validation_end_day),
                ("test", test_start_signal, TEST_START, test_end_day),
            ):
                signal_start_date = "2021-12-31" if split == "validation" else "2023-12-29"
                keep = month_signal_days(
                    month_dates, month_indices, dates, signal_start_date, dates[end_day], config["rebalance_months"]
                )
                masked = masked_scores(scores[model], month_indices, len(dates), keep)
                metrics, positions, cash = evaluate_account(
                    panel,
                    {},
                    dates,
                    stocks,
                    masked,
                    start_signal,
                    end_day,
                    {k: v for k, v in config.items() if k != "rebalance_months"},
                )
                row[split] = {
                    "ret_pct": round(metrics["ret_pct"], 2),
                    "max_drawdown": round(metrics["max_drawdown"], 2),
                    "sharpe": round(metrics["sharpe"], 2),
                    "trades": metrics["trades"],
                    "positions": positions,
                    "cash_pct": round(cash / CAPITAL * 100, 1),
                }
            results.append(row)
            print(
                f"{model} N={config['portfolio_size']} B={config['buffer_size']} "
                f"M={config['rebalance_months']} | "
                f"val {row['validation']['ret_pct']:+.1f}% DD {row['validation']['max_drawdown']:.1f} "
                f"Sh {row['validation']['sharpe']:.2f} | "
                f"test {row['test']['ret_pct']:+.1f}% DD {row['test']['max_drawdown']:.1f} "
                f"Sh {row['test']['sharpe']:.2f}",
                flush=True,
            )

    # Rank by validation Sharpe, then validation return.
    ranked = sorted(
        results,
        key=lambda row: (row["validation"]["sharpe"], row["validation"]["ret_pct"]),
        reverse=True,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tuning_account.json").write_text(
        json.dumps({"configs": results, "ranked": ranked}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("\nTop validation configurations:")
    for row in ranked[:8]:
        print(
            row["model"],
            row["config"],
            "val",
            row["validation"],
            "test",
            row["test"],
        )


if __name__ == "__main__":
    main()
