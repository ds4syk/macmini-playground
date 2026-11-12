"""LightGBM hyper-parameter sweep, selected on the 2022-2023 validation IC."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
FACTORS = ROOT / "factors" / "factors.npz"
OUT = ROOT / "results"
TRAIN = ("2018-01-01", "2021-12-31")
VALIDATION = ("2022-01-01", "2023-12-31")
TEST = ("2024-01-01", "2026-12-31")
SEEDS = (42, 43, 44)
CONFIGS = [
    {"name": "current", "num_leaves": 31, "min_data_in_leaf": 300, "feature_fraction": 0.8, "lambda_l2": 2.0},
    {"name": "shallow", "num_leaves": 15, "min_data_in_leaf": 500, "feature_fraction": 0.8, "lambda_l2": 2.0},
    {"name": "deep", "num_leaves": 63, "min_data_in_leaf": 200, "feature_fraction": 0.8, "lambda_l2": 2.0},
    {"name": "wide-min-data", "num_leaves": 31, "min_data_in_leaf": 1000, "feature_fraction": 0.8, "lambda_l2": 2.0},
    {"name": "sparse-features", "num_leaves": 31, "min_data_in_leaf": 300, "feature_fraction": 0.6, "lambda_l2": 5.0},
]


def rank(values):
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values))
    return ranks


def spearman(left, right):
    a, b = rank(left), rank(right)
    a -= a.mean()
    b -= b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denom) if denom > 0 else float("nan")


def main() -> None:
    import lightgbm as lgb

    factors = np.load(FACTORS)
    month_dates = [str(item) for item in factors["month_dates"]]
    z = factors["z"].astype(np.float64)
    eligible = factors["eligible"]
    pool = factors["pool"]
    label = factors["label"].astype(np.float64)
    fwd = factors["fwd_ret"].astype(np.float64)
    factor_names = [str(item) for item in factors["factor_names"]]

    def stack(months):
        xs, ys = [], []
        for month in months:
            mask = eligible[:, month] & np.isfinite(label[:, month])
            if mask.sum() < 100:
                continue
            xs.append(z[mask, month, :])
            ys.append(label[mask, month])
        return np.concatenate(xs), np.concatenate(ys)

    train_months = [i for i, date in enumerate(month_dates) if TRAIN[0] <= date <= TRAIN[1]]
    valid_months = [i for i, date in enumerate(month_dates) if VALIDATION[0] <= date <= VALIDATION[1]]
    test_months = [i for i, date in enumerate(month_dates) if TEST[0] <= date <= TEST[1]]
    x_train, y_train = stack(train_months)
    x_valid, y_valid = stack(valid_months)

    def ic_by_split(prediction, months):
        ics = []
        for month in months:
            mask = pool[:, month] & np.isfinite(prediction[:, month]) & np.isfinite(fwd[:, month])
            if mask.sum() < 30:
                continue
            ics.append(spearman(prediction[mask, month], fwd[mask, month]))
        return float(np.nanmean(ics)), len(ics)

    results = []
    for config in CONFIGS:
        predictions = []
        iterations = []
        for seed in SEEDS:
            params = {
                "objective": "regression",
                "metric": "l2",
                "learning_rate": 0.03,
                "min_data_in_leaf": config["min_data_in_leaf"],
                "feature_fraction": config["feature_fraction"],
                "bagging_fraction": 0.8,
                "bagging_freq": 1,
                "lambda_l2": config["lambda_l2"],
                "num_leaves": config["num_leaves"],
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
            iterations.append(int(model.best_iteration))
            prediction = np.full(z.shape[:2], np.nan)
            for month in range(z.shape[1]):
                mask = eligible[:, month]
                if mask.sum():
                    prediction[mask, month] = model.predict(z[mask, month, :])
            predictions.append(prediction)
        average = np.nanmean(np.stack(predictions), axis=0)
        valid_ic, valid_n = ic_by_split(average, valid_months)
        test_ic, test_n = ic_by_split(average, test_months)
        results.append(
            {
                "config": config,
                "iterations": iterations,
                "validation_ic": round(valid_ic, 4),
                "validation_months": valid_n,
                "test_ic": round(test_ic, 4),
                "test_months": test_n,
            }
        )
        print(
            f"{config['name']}: val IC {valid_ic:.4f} ({valid_n}m) test IC {test_ic:.4f} "
            f"iters {iterations}",
            flush=True,
        )
    results.sort(key=lambda row: row["validation_ic"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tuning_lgbm.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("best by validation IC:", results[0]["config"]["name"], results[0]["validation_ic"])


if __name__ == "__main__":
    main()
