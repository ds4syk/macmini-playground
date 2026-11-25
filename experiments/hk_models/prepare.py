"""Build the Hong Kong feature panel with the same 25-factor recipe as the A-share line.

Inputs are the per-security CSVs produced by :mod:`experiments.hk_models.fetch`
plus ``universe.csv``.  Output mirrors ``~/labs/VLA/Share/gru/data/panel.npz``:
daily features, eligible mask, 5-day open-to-open forward returns, split samples
and raw/adjusted price arrays for the backtester.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from . import fetch

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
DATA = Path(os.environ.get("HK_DATA_DIR", ROOT / "data" / "hk"))


def log(message: str) -> None:
    print(message, flush=True)


def stock_features(frame: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    close = frame.adj_close.ffill()
    opening = frame.adj_open.fillna(close.shift(1))
    high = frame.adj_high.fillna(close)
    low = frame.adj_low.fillna(close)
    amount = frame.amount.fillna(0)
    ret = close.pct_change(fill_method=None)
    features = {
        "return_1": ret,
        "open_gap": opening / close.shift(1) - 1,
        "intraday_return": close / opening - 1,
        "range": (high - low) / close,
        "close_position": (close - low) / (high - low).replace(0, np.nan),
        "turnover": frame.adj_turnover.fillna(0),
        "trading": frame.tradable,
    }
    features["close_position"] = features["close_position"].fillna(0.5)
    for window in (5, 20, 60):
        features[f"return_{window}"] = close.pct_change(window, fill_method=None)
        features[f"ma_gap_{window}"] = close / close.rolling(window).mean() - 1
        features[f"volatility_{window}"] = ret.rolling(window).std()
        features[f"amount_ratio_{window}"] = np.log1p(amount) - np.log1p(amount.rolling(window).mean())
    features["relative_return_20"] = features["return_20"] - market["market_return_20"]
    for name in market:
        features[name] = market[name]
    return pd.DataFrame(features).replace([np.inf, -np.inf], np.nan)


def main() -> None:
    parser = argparse.ArgumentParser(description="prepare HK model panel")
    parser.add_argument("--output", default=str(DATA))
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    universe = pd.read_csv(HERE / "universe.csv", dtype={"code": str})
    if universe.empty:
        raise SystemExit("universe.csv is empty: run fetch + build_universe first")
    indices = pd.read_csv(DATA / "indices.csv", dtype={"date": str})
    hsi = indices[indices.code.eq("HSI")].set_index("date").sort_index()
    hsi.index = pd.DatetimeIndex(hsi.index)
    dates = pd.DatetimeIndex(hsi.index)
    dates = dates[(dates >= pd.Timestamp(CONFIG["data_start"])) & (dates <= pd.Timestamp(CONFIG["data_end"]))]
    dates = dates[dates.dayofweek < 5]
    log(f"calendar: {len(dates)} sessions {dates[0].date()} -> {dates[-1].date()}")

    market = pd.DataFrame(index=dates)
    hsi_close = hsi["raw_close"].reindex(dates).astype(float)
    for window in (1, 5, 20, 60):
        market[f"market_return_{window}"] = hsi_close.pct_change(window, fill_method=None)
    market["market_volatility_20"] = hsi_close.pct_change(fill_method=None).rolling(20).std()

    stocks = universe["code"].tolist()
    names = dict(zip(universe["code"], universe["name"]))
    lots = {str(code): int(lot) for code, lot in zip(universe["code"], universe["lot"])}
    shape = (len(stocks), len(dates))
    arrays = {
        key: np.zeros(shape, dtype=np.float32)
        for key in ["adj_open", "adj_close", "raw_open", "raw_close", "preclose", "amount", "tradable", "is_st", "delisted"]
    }
    feature_values = []
    quality = []
    for row, code in enumerate(stocks):
        path = DATA / "raw" / f"{code}.csv"
        if not path.exists():
            raise SystemExit(f"missing history: {path}")
        frame = pd.read_csv(path, dtype={"date": str}).set_index("date")
        frame.index = pd.DatetimeIndex(frame.index)
        frame = frame.reindex(dates)
        for column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("adj_open", "adj_close", "adj_high", "adj_low"):
            frame[column] = frame[column].where(frame[column] > 0)
        # Actual HKD amount for the recent backtest window; adjusted turnover is
        # only used to rank liquidity before raw prices are available.
        frame["amount"] = np.where(
            frame.raw_close.gt(0),
            frame.adj_volume * frame.raw_close,
            frame.adj_volume * frame.adj_close,
        )
        frame["tradable"] = (
            frame.adj_open.gt(0) & frame.adj_close.gt(0) & frame.adj_volume.gt(0)
        ).astype(float)
        features = stock_features(frame, market)
        feature_values.append(features.to_numpy(dtype=np.float32))
        close = frame.adj_close.ffill()
        opening = frame.adj_open.fillna(close.shift(1))
        arrays["adj_open"][row] = opening.fillna(0)
        arrays["adj_close"][row] = close.fillna(0)
        arrays["raw_open"][row] = frame.raw_open.fillna(0)
        arrays["raw_close"][row] = frame.raw_close.ffill().fillna(0)
        arrays["preclose"][row] = frame.raw_close.shift(1).fillna(0)
        arrays["amount"][row] = frame.amount.fillna(0)
        arrays["tradable"][row] = frame.tradable.to_numpy(dtype=float)
        first_date = frame.adj_close.first_valid_index()
        quality.append(
            {
                "code": code,
                "name": names.get(code, code),
                "lot": int(lots.get(code, 100)),
                "first_date": str(first_date.date()) if first_date is not None else "",
                "last_date": str(frame.adj_close.last_valid_index().date())
                if frame.adj_close.last_valid_index() is not None
                else "",
                "tradable_rows": int(frame.tradable.sum()),
            }
        )
    feature_names = list(features.columns)
    features = np.stack(feature_values)
    log(f"features: {features.shape}, names={len(feature_names)}")

    finite = np.isfinite(features).all(axis=2)
    complete_window = np.zeros(shape, dtype=bool)
    for row in range(len(stocks)):
        complete_window[row] = (
            pd.Series(finite[row]).rolling(CONFIG["lookback"]).sum().eq(CONFIG["lookback"])
        )
    eligible = complete_window & (arrays["tradable"] > 0) & (arrays["is_st"] == 0)
    horizon = int(CONFIG["horizon"])
    returns = np.full(shape, np.nan, dtype=np.float32)
    returns[:, :-(horizon + 1)] = np.divide(
        arrays["adj_open"][:, horizon + 1:],
        arrays["adj_open"][:, 1:-horizon],
        out=np.full_like(arrays["adj_open"][:, horizon + 1:], np.nan),
        where=arrays["adj_open"][:, 1:-horizon] > 0,
    ) - 1
    first_sample = int(dates.searchsorted(pd.Timestamp(CONFIG["sample_start"])))
    signals = np.arange(first_sample, len(dates) - horizon - 2, 1)
    split_ranges = {
        "train": (CONFIG["sample_start"], CONFIG["train_end"]),
        "validation": (CONFIG["validation_start"], CONFIG["validation_end"]),
        "test": (CONFIG["test_start"], CONFIG["data_end"]),
        "all": (CONFIG["sample_start"], CONFIG["data_end"]),
    }
    samples = {}
    for name, (start, end) in split_ranges.items():
        selected = signals[(dates[signals] >= start) & (dates[signals + horizon + 1] <= end)]
        pairs = [
            (int(stock), int(signal))
            for signal in selected
            for stock in np.flatnonzero(eligible[:, signal] & np.isfinite(returns[:, signal]))
        ]
        samples[name] = np.asarray(pairs, dtype=np.int32).reshape(-1, 2)
        if len(pairs) < 1000:
            raise RuntimeError(f"too few {name} samples: {len(pairs)}")
        log(f"{name}: {len(pairs)} samples {dates[selected[0]].date()} -> {dates[selected[-1]].date()}")
    train_dates = (dates >= pd.Timestamp(CONFIG["sample_start"])) & (dates <= pd.Timestamp(CONFIG["train_end"]))
    fitting = features[:, train_dates][(arrays["tradable"][:, train_dates] > 0) & finite[:, train_dates]]
    center = np.median(fitting, axis=0)
    scale = np.maximum(np.std(fitting, axis=0), 1e-6)
    log(f"normalisation: {len(fitting)} training rows, {len(center)} features")

    temporary = output / "panel.tmp.npz"
    with temporary.open("wb") as destination:
        np.savez_compressed(
            destination,
            features=features,
            eligible=eligible,
            returns=returns,
            benchmark_open=hsi["raw_open"].reindex(dates).to_numpy(np.float32),
            benchmark_close=hsi_close.to_numpy(np.float32),
            center=center,
            scale=scale,
            **arrays,
            **{f"samples_{name}": value for name, value in samples.items()},
        )
    temporary.replace(output / "panel.npz")

    metadata = {
        "market": "HK",
        "dates": dates.strftime("%Y-%m-%d").tolist(),
        "stocks": stocks,
        "names": names,
        "lots": lots,
        "feature_names": feature_names,
        "feature_count": int(features.shape[2]),
        "stock_count": int(len(stocks)),
        "date_count": int(len(dates)),
        "actual_start": str(dates[0].date()),
        "actual_end": str(dates[-1].date()),
        "splits": {
            name: {
                "samples": int(len(value)),
                "signal_start": str(dates[value[:, 1].min()].date()),
                "signal_end": str(dates[value[:, 1].max()].date()),
                "last_label_exit": str(dates[value[:, 1].max() + horizon + 1].date()),
            }
            for name, value in samples.items()
        },
        "label": "back-adjusted open[t+6] / open[t+1] - 1",
        "universe": {
            "size": int(len(stocks)),
            "source": "HKEX ListOfSecurities main board HKD equity, Tencent quote screen",
            "screen": {
                "min_market_cap_hkd": 2_000_000_000,
                "min_median_amount_hkd": CONFIG["min_median_amount"],
                "min_price_hkd": CONFIG["min_price"],
                "first_date_at_or_before": CONFIG["min_history_start"],
            },
            "survivorship": "current HKEX list only; delisted names are absent",
        },
        "quality": quality,
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    ready = {
        "config_sha256": hashlib.sha256((HERE / "config.json").read_bytes()).hexdigest(),
        "panel_sha256": hashlib.sha256((output / "panel.npz").read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256((output / "metadata.json").read_bytes()).hexdigest(),
    }
    (output / "ready.json").write_text(json.dumps(ready, indent=2), encoding="utf-8")
    log(json.dumps({key: metadata[key] for key in ["stock_count", "date_count", "feature_count", "actual_end", "splits"]}, indent=2))


if __name__ == "__main__":
    main()
