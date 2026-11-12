"""Train the monthly small-cap multi-factor model and backtest it.

Three variants are compared on the identical pool, labels and account rules:

* ``mf_lgbm``  LightGBM regression on the 16 factor z-scores, 3-seed average;
* ``mf_equal`` classic equal-weight composite of signed factor z-scores;
* ``mf_size``  pure small-cap baseline (negative size z-score only).

Time split (valuation history starts 2018-01-02):

    train      2018-01 .. 2021-12
    validation 2022-01 .. 2023-12
    test       2024-01 .. 2026-10 (last month with an observable 20-day label)

The test years 2024-2026 have already been viewed in earlier Share work, so
these are historical comparisons, not clean forward evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
PANEL = ROOT / "panel" / "panel.npz"
FACTORS = ROOT / "factors" / "factors.npz"
RESULTS = ROOT / "results"

TRAIN = ("2018-01-01", "2021-12-31")
VALIDATION = ("2022-01-01", "2023-12-31")
TEST = ("2024-01-01", "2026-12-31")
CAPITALS = [10000, 30000, 50000, 100000]
MODELS = ("mf_lgbm", "mf_equal", "mf_size")


def rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values))
    return ranks


def spearman(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 20:
        return float("nan")
    a, b = rank(left), rank(right)
    a = a - a.mean()
    b = b - b.mean()
    denominator = np.sqrt((a * a).sum() * (b * b).sum())
    if denominator <= 0:
        return float("nan")
    return float((a * b).sum() / denominator)


def evaluate(score: np.ndarray, fwd: np.ndarray, pool: np.ndarray, label: np.ndarray, months: list[str]):
    ics = []
    deciles = [[] for _ in range(10)]
    monthly = []
    for index, date in enumerate(months):
        if date < TEST[0]:
            continue
        mask = pool[:, index] & np.isfinite(score[:, index]) & np.isfinite(fwd[:, index])
        if mask.sum() < 30:
            continue
        values = score[mask, index]
        returns = fwd[mask, index]
        ics.append(spearman(values, returns))
        order = np.argsort(-values, kind="stable")
        buckets = np.array_split(order, 10)
        for bucket_index, bucket in enumerate(buckets):
            deciles[bucket_index].append(float(np.mean(returns[bucket])))
        monthly.append((date, float(np.mean(returns)), float(np.mean(values))))
    ics = np.array([value for value in ics if np.isfinite(value)])
    decile_means = [float(np.mean(values)) if values else float("nan") for values in deciles]
    long_short = decile_means[0] - decile_means[-1] if decile_means else float("nan")
    return {
        "months": len(ics),
        "ic_mean": float(ics.mean()) if len(ics) else float("nan"),
        "ic_std": float(ics.std(ddof=1)) if len(ics) > 1 else float("nan"),
        "icir": float(ics.mean() / ics.std(ddof=1) * np.sqrt(12)) if len(ics) > 1 and ics.std(ddof=1) > 0 else float("nan"),
        "ic_positive": float((ics > 0).mean()) if len(ics) else float("nan"),
        "deciles": decile_means,
        "long_short_monthly": long_short,
        "monthly": monthly,
        "ics": [float(value) for value in ics],
    }


def main() -> None:
    panel = np.load(PANEL)
    factors = np.load(FACTORS)
    dates = [str(item) for item in panel["dates"]]
    dates = np.array(dates)
    stocks = [str(item) for item in panel["stocks"]]
    names = [str(item) for item in panel["names"]]
    month_dates = [str(item) for item in factors["month_dates"]]
    month_indices = factors["month_indices"].astype(int)
    z = factors["z"].astype(np.float64)
    signs = factors["signs"].astype(np.float64)
    eligible = factors["eligible"]
    pool = factors["pool"]
    label = factors["label"].astype(np.float64)
    fwd = factors["fwd_ret"].astype(np.float64)
    factor_names = [str(item) for item in factors["factor_names"]]

    train_months = [i for i, date in enumerate(month_dates) if TRAIN[0] <= date <= TRAIN[1]]
    valid_months = [i for i, date in enumerate(month_dates) if VALIDATION[0] <= date <= VALIDATION[1]]
    test_months = [i for i, date in enumerate(month_dates) if TEST[0] <= date <= TEST[1]]
    print(f"months train={len(train_months)} val={len(valid_months)} test={len(test_months)}", flush=True)

    def stack(month_list):
        xs, ys = [], []
        for month in month_list:
            mask = eligible[:, month] & np.isfinite(label[:, month])
            if mask.sum() < 100:
                continue
            xs.append(z[mask, month, :])
            ys.append(label[mask, month])
        return np.concatenate(xs, axis=0), np.concatenate(ys, axis=0)

    x_train, y_train = stack(train_months)
    x_valid, y_valid = stack(valid_months)
    print(f"train rows {x_train.shape}, valid rows {x_valid.shape}", flush=True)

    scores = {}
    try:
        import lightgbm as lgb

        predictions = []
        seeds = [42, 43, 44]
        best_iterations = []
        for seed in seeds:
            params = {
                "objective": "regression",
                "metric": "l2",
                "learning_rate": 0.03,
                "num_leaves": 31,
                "min_data_in_leaf": 300,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 1,
                "lambda_l2": 2.0,
                "verbosity": -1,
                "seed": seed,
                "num_threads": 8,
            }
            train_set = lgb.Dataset(x_train, label=y_train, feature_name=factor_names)
            valid_set = lgb.Dataset(x_valid, label=y_valid, reference=train_set)
            model = lgb.train(
                params,
                train_set,
                num_boost_round=600,
                valid_sets=[valid_set],
                callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
            )
            best_iterations.append(int(model.best_iteration))
            print(f"seed {seed}: best_iteration {model.best_iteration}", flush=True)
            prediction = np.full((len(stocks), len(month_dates)), np.nan)
            for month in range(len(month_dates)):
                mask = eligible[:, month]
                if mask.sum() == 0:
                    continue
                prediction[mask, month] = model.predict(z[mask, month, :])
            predictions.append(prediction)
        scores["mf_lgbm"] = np.nanmean(np.stack(predictions), axis=0)
        scores["lgbm_best_iterations"] = best_iterations
    except ImportError:
        print("lightgbm missing; skipping mf_lgbm", flush=True)

    # Data-driven factor directions: the sign of each factor's mean training
    # IC, falling back to the literature direction when the training IC is
    # too noisy. This keeps the equal-weight composite free of test leakage.
    single_ic = factors["single_ic"].astype(np.float64)
    with np.errstate(invalid="ignore"):
        train_ic = np.nanmean(single_ic[:, train_months], axis=1)
    directions = np.where(np.abs(train_ic) > 0.005, np.sign(train_ic), signs)
    weighted = np.where(np.isfinite(z), z * directions[None, None, :], np.nan)
    with np.errstate(invalid="ignore"):
        equal = np.nanmean(weighted, axis=2)
    equal[~eligible] = np.nan
    scores["mf_equal"] = equal
    report_directions = {name: float(directions[i]) for i, name in enumerate(factor_names)}
    size = np.full((len(stocks), len(month_dates)), np.nan)
    size[eligible] = -z[:, :, 0][eligible]
    scores["mf_size"] = size

    # Monthly scores on the daily grid (finite only on month-end signal days).
    daily_scores = {}
    for model in MODELS:
        matrix = np.full((len(stocks), len(dates)), np.nan, dtype=np.float32)
        for month, day in enumerate(month_indices):
            matrix[:, day] = scores[model][:, month].astype(np.float32)
        daily_scores[model] = matrix

    report = {
        "factor_names": factor_names,
        "equal_weight_directions": report_directions,
        "models": {},
        "split": {"train": TRAIN, "validation": VALIDATION, "test": TEST},
    }
    try:
        from . import account
    except ImportError:
        import account

    # First signal before 2024 so the account opens on the first 2024 session.
    start_month = max(i for i, date in enumerate(month_dates) if date < "2024-01-01")
    start_day = int(month_indices[start_month])
    test_start_day = int(np.searchsorted(dates, "2024-01-02"))
    end_day = len(dates) - 1
    print(f"backtest from signal {dates[start_day]} to {dates[end_day]}", flush=True)

    benchmark = panel["benchmark_close"].astype(float)
    base = benchmark[test_start_day]
    for model in MODELS:
        evaluation = evaluate(scores[model], fwd, pool, label, month_dates)
        model_report = {
            "factors": factor_names,
            "test_ic": evaluation,
            "backtest": {},
        }
        for capital in CAPITALS:
            rows, units, cost_basis, trades, total_fees = account.run(
                [str(item) for item in dates],
                stocks,
                panel["close"],
                panel["pct_chg"],
                panel["float_mcap"],
                panel["is_st"],
                panel["tradable"],
                daily_scores[model],
                capital,
                start_day,
                end_day,
            )
            trimmed = [row for row in rows if row["date"] >= "2024-01-02"]
            trimmed_trades = [trade for trade in trades if trade["date"] >= "2024-01-02"]
            metrics = account.metrics(trimmed, trimmed_trades, capital)
            model_report["backtest"][str(capital)] = {
                "metrics": metrics,
                "final_equity": float(trimmed[-1]["equity"]),
                "positions": int((units > 0).sum()),
                "equity_curve": [
                    [row["date"], round(float(row["equity"]), 2)] for row in trimmed
                ],
                "trades": len(trimmed_trades),
                "fees": round(float(total_fees), 2),
            }
            print(
                f"{model} {capital}: ret {metrics['ret_pct']:.1f}% dd {metrics['max_drawdown']:.1f}% "
                f"sharpe {metrics['sharpe']:.2f} trades {metrics['trades']}",
                flush=True,
            )
        report["models"][model] = model_report
        print(
            f"{model}: test IC {evaluation['ic_mean']:.4f} +/- {evaluation['ic_std']:.4f} "
            f"ICIR {evaluation['icir']:.2f} positive {evaluation['ic_positive']:.0%}",
            flush=True,
        )

    report["pool"] = {
        "mean_eligible": float(eligible.sum(axis=0).mean()),
        "mean_pool": float(pool.sum(axis=0).mean()),
        "description": "smallest 30% of eligible stocks by float market cap",
    }
    report["benchmark"] = {
        "market_start": float(base),
        "market_end": float(benchmark[end_day]),
        "market_return": float(benchmark[end_day] / base - 1) * 100,
        "note": "equal-weight all-market index built from daily cross-sectional mean pct change",
    }
    report["universe"] = {
        "stocks": len(stocks),
        "eligible_mean": float(eligible.sum(axis=0).mean()),
        "pool_mean": float(pool.sum(axis=0).mean()),
        "test_first_signal": str(month_dates[start_month]),
        "test_end": str(dates[end_day]),
    }
    results = RESULTS
    results.mkdir(parents=True, exist_ok=True)
    np.savez(
        results / "scores.npz",
        month_dates=np.array(month_dates, dtype="U10"),
        month_indices=month_indices,
        **{model: scores[model].astype(np.float32) for model in MODELS},
    )
    (results / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    plot(report, dates, test_start_day, end_day, benchmark, results)
    print(json.dumps({model: report["models"][model]["test_ic"] for model in MODELS}, ensure_ascii=False)[:800])


def plot(report, dates, test_start_day, end_day, benchmark, results: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    figure, axis = plt.subplots(figsize=(10, 5))
    base = benchmark[test_start_day]
    axis.plot(
        dates[test_start_day:end_day + 1],
        (benchmark[test_start_day:end_day + 1] / base - 1) * 100,
        label="CSI300",
        color="#8c98a5",
        linewidth=1.4,
    )
    colors = {"mf_lgbm": "#c62828", "mf_equal": "#1f6feb", "mf_size": "#0a9b5a"}
    labels = {"mf_lgbm": "Multi-factor LightGBM", "mf_equal": "Multi-factor equal weight", "mf_size": "Pure small-cap"}
    for model in MODELS:
        curve = report["models"][model]["backtest"]["100000"]["equity_curve"]
        xs = [item[0] for item in curve]
        ys = [(item[1] / 100000 - 1) * 100 for item in curve]
        axis.plot(xs, ys, label=labels[model], color=colors[model], linewidth=1.4)
    axis.set_title("Small-cap multi-factor test 2024-01-02 ~ %s (100k CNY)" % dates[end_day])
    axis.set_ylabel("cumulative return %")
    axis.legend(loc="upper left", frameon=False)
    axis.grid(alpha=0.25)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(results / "equity.png", dpi=130)
    plt.close(figure)

    figure, axes = plt.subplots(1, 3, figsize=(13, 3.6), sharey=True)
    for axis, model in zip(axes, ["mf_lgbm", "mf_equal", "mf_size"]):
        ics = report["models"][model]["test_ic"]["ics"]
        axis.bar(range(len(ics)), ics, color="#1f6feb")
        axis.axhline(0, color="#333", linewidth=0.8)
        axis.set_title(f"{labels[model]}: mean IC {np.mean(ics):.3f}")
        axis.set_xlabel("test month")
        axis.grid(alpha=0.2)
    axes[0].set_ylabel("Rank IC")
    figure.tight_layout()
    figure.savefig(results / "ic.png", dpi=130)
    plt.close(figure)


if __name__ == "__main__":
    main()
