"""Monthly cross-sectional factors for the small-cap multi-factor experiment.

Fourteen price, size, value, risk and industry factors are computed on the
last trading day of every month from 2018-01 to 2026-10.  Each factor is
winsorised at five MADs, z-scored inside the eligible universe, and (except
size) industry-neutralised by demeaning inside the point-in-time board.

The investable pool is the smallest 30% of eligible stocks by float market
cap.  The label is the forward 20-trading-day return rank in [-1, 1], measured
from the next close (close-only execution).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
PANEL = ROOT / "panel" / "panel.npz"
OUT = ROOT / "factors"

FACTOR_NAMES = [
    "size",
    "ep",
    "bp",
    "sp",
    "cfp",
    "ret_20",
    "ret_60",
    "ret_120",
    "rev_5",
    "vol_20",
    "vol_60",
    "maxret_20",
    "beta_60",
    "high_252",
]
SIGNS = {
    "size": -1.0,
    "ep": 1.0,
    "bp": 1.0,
    "sp": 1.0,
    "cfp": 1.0,
    "ret_20": 1.0,
    "ret_60": 1.0,
    "ret_120": 1.0,
    "rev_5": 1.0,
    "vol_20": -1.0,
    "vol_60": -1.0,
    "maxret_20": -1.0,
    "beta_60": -1.0,
    "high_252": 1.0,
}
MIN_AMOUNT_PROXY = 1.0e9  # float market cap floor in CNY (no volume in the source)
MIN_LISTED_DAYS = 250
POOL_QUANTILE = 0.30


def zscore(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    x = np.where(mask, values, np.nan).astype(np.float64)
    finite = np.isfinite(x)
    if finite.sum() < 30:
        return np.full_like(x, np.nan)
    median = np.nanmedian(x)
    mad = np.nanmedian(np.abs(x - median))
    if not np.isfinite(mad) or mad <= 0:
        mad = np.nanstd(x)
    if not np.isfinite(mad) or mad <= 0:
        return np.full_like(x, np.nan)
    x = np.clip(x, median - 5 * 1.4826 * mad, median + 5 * 1.4826 * mad)
    mean, std = np.nanmean(x), np.nanstd(x)
    if not np.isfinite(std) or std <= 0:
        return np.full_like(x, np.nan)
    return (x - mean) / std


def industry_neutralise(values: np.ndarray, mask: np.ndarray, industry: np.ndarray) -> np.ndarray:
    result = np.array(values, dtype=np.float64, copy=True)
    for group in np.unique(industry[mask]):
        members = mask & (industry == group)
        if members.sum() < 20:
            continue
        block = result[members]
        finite = np.isfinite(block)
        if finite.sum() < 20:
            continue
        block[finite] -= np.nanmean(block)
        result[members] = block
    finite = np.isfinite(result) & mask
    if finite.sum() > 30:
        mean, std = np.nanmean(result[finite]), np.nanstd(result[finite])
        if std > 0:
            result[finite] = (result[finite] - mean) / std
    result[~mask] = np.nan
    return result


def month_end_indices(dates: list[str]) -> list[int]:
    indices = []
    for index in range(1, len(dates)):
        month = dates[index][:7]
        next_month = dates[index + 1][:7] if index + 1 < len(dates) else ""
        if month != next_month and next_month and month >= "2018-01":
            indices.append(index)
    return indices


def main() -> None:
    panel = np.load(PANEL)
    dates = [str(item) for item in panel["dates"]]
    stocks = [str(item) for item in panel["stocks"]]
    industries = [str(item) for item in panel["industries"]]
    industry_codes, industry_ids = np.unique(industries, return_inverse=True)
    close = panel["close"].astype(np.float64)
    pct = panel["pct_chg"].astype(np.float64)
    float_mcap = panel["float_mcap"].astype(np.float64)
    pe = panel["pe"].astype(np.float64)
    pb = panel["pb"].astype(np.float64)
    ps = panel["ps"].astype(np.float64)
    pcf = panel["pcf"].astype(np.float64)
    listed_days = panel["listed_days"].astype(np.int32)
    tradable = panel["tradable"]
    is_st = panel["is_st"]
    market_pct = panel["benchmark_pct"].astype(np.float64)

    n_stocks, n_dates = close.shape
    months = month_end_indices(dates)
    n_months = len(months)
    print(f"{n_months} month ends {dates[months[0]]} ~ {dates[months[-1]]}", flush=True)

    z = np.full((n_stocks, n_months, len(FACTOR_NAMES)), np.nan, dtype=np.float32)
    eligible = np.zeros((n_stocks, n_months), dtype=bool)
    pool = np.zeros((n_stocks, n_months), dtype=bool)
    fwd_ret = np.full((n_stocks, n_months), np.nan, dtype=np.float32)
    label = np.full((n_stocks, n_months), np.nan, dtype=np.float32)
    mcap_t = np.full((n_stocks, n_months), np.nan, dtype=np.float32)
    close_t = np.zeros((n_stocks, n_months), dtype=np.float32)
    pool_size = np.zeros(n_months, dtype=np.int32)
    single_ic = np.zeros((len(FACTOR_NAMES), n_months), dtype=np.float32)

    masked_pct = np.where(tradable, pct, np.nan)
    for month_index, t in enumerate(months):
        window20 = slice(max(0, t - 19), t + 1)
        window60 = slice(max(0, t - 59), t + 1)
        window252 = slice(max(0, t - 251), t + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            ret20 = close[:, t] / close[:, t - 20] - 1.0
            ret60 = close[:, t] / close[:, t - 60] - 1.0
            ret120 = close[:, t] / close[:, t - 120] - 1.0
            rev5 = -(close[:, t] / close[:, t - 5] - 1.0)
            vol20 = np.nanstd(masked_pct[:, window20], axis=1)
            vol60 = np.nanstd(masked_pct[:, window60], axis=1)
            maxret = np.nanmax(masked_pct[:, window20], axis=1)
            high252 = close[:, t] / np.nanmax(close[:, window252], axis=1) - 1.0
            bench = market_pct[window60]
            bench = np.where(np.isfinite(bench), bench, 0.0)
            bench_var = np.nanvar(bench)
            beta60 = np.full(n_stocks, np.nan)
            if bench_var > 0:
                stock = np.where(np.isfinite(masked_pct[:, window60]), masked_pct[:, window60], 0.0)
                stock_mean = np.nanmean(masked_pct[:, window60], axis=1)
                cov = np.nanmean((stock - stock_mean[:, None]) * (bench[None, :] - bench.mean()), axis=1)
                beta60 = cov / bench_var
            ep = np.where(np.abs(pe[:, t]) > 1e-6, 1.0 / pe[:, t], np.nan)
            bp = np.where(np.abs(pb[:, t]) > 1e-6, 1.0 / pb[:, t], np.nan)
            sp = np.where(np.abs(ps[:, t]) > 1e-6, 1.0 / ps[:, t], np.nan)
            cfp = np.where(np.abs(pcf[:, t]) > 1e-6, 1.0 / pcf[:, t], np.nan)
            size = np.log(float_mcap[:, t])
        eligible_now = (
            tradable[:, t]
            & (listed_days[:, t] >= MIN_LISTED_DAYS)
            & (~is_st[:, t])
            & (close[:, t] >= 2.0)
            & np.isfinite(float_mcap[:, t])
            & (float_mcap[:, t] >= MIN_AMOUNT_PROXY)
        )
        if eligible_now.sum() < 300:
            print(f"skip {dates[t]}: only {int(eligible_now.sum())} eligible", flush=True)
            continue
        raw = {
            "size": size,
            "ep": ep,
            "bp": bp,
            "sp": sp,
            "cfp": cfp,
            "ret_20": ret20,
            "ret_60": ret60,
            "ret_120": ret120,
            "rev_5": rev5,
            "vol_20": vol20,
            "vol_60": vol60,
            "maxret_20": maxret,
            "beta_60": beta60,
            "high_252": high252,
        }
        for factor_index, name in enumerate(FACTOR_NAMES):
            values = zscore(raw[name], eligible_now)
            if name != "size":
                values = industry_neutralise(values, eligible_now, industry_ids)
            z[:, month_index, factor_index] = values.astype(np.float32)

        mcap = float_mcap[:, t]
        order = np.argsort(np.where(eligible_now, mcap, np.inf), kind="stable")
        n_pool = max(1, int(np.ceil(POOL_QUANTILE * eligible_now.sum())))
        pool_now = np.zeros(n_stocks, dtype=bool)
        pool_now[order[:n_pool]] = True
        pool_now &= eligible_now

        exit_day = t + 21
        fwd = np.full(n_stocks, np.nan)
        if exit_day < n_dates:
            with np.errstate(invalid="ignore", divide="ignore"):
                fwd = close[:, exit_day] / close[:, t + 1] - 1.0
        label_now = np.full(n_stocks, np.nan)
        label_mask = eligible_now & np.isfinite(fwd)
        if label_mask.sum() >= 100:
            valid_positions = np.flatnonzero(label_mask)
            ordered = valid_positions[np.argsort(fwd[valid_positions], kind="stable")]
            n_valid = len(ordered)
            ranks = np.empty(n_stocks, dtype=np.float64)
            ranks[ordered] = np.arange(n_valid)
            label_now[ordered] = ranks[ordered] * (2.0 / max(n_valid - 1, 1)) - 1.0
        eligible[:, month_index] = eligible_now
        pool[:, month_index] = pool_now
        pool_size[month_index] = int(pool_now.sum())
        fwd_ret[:, month_index] = fwd.astype(np.float32)
        label[:, month_index] = label_now.astype(np.float32)
        mcap_t[:, month_index] = mcap.astype(np.float32)
        close_t[:, month_index] = close[:, t].astype(np.float32)

        for factor_index in range(len(FACTOR_NAMES)):
            values = z[:, month_index, factor_index].astype(np.float64)
            mask = pool_now & np.isfinite(values) & np.isfinite(fwd)
            if mask.sum() > 30 and np.std(values[mask]) > 0 and np.std(fwd[mask]) > 0:
                single_ic[factor_index, month_index] = float(np.corrcoef(values[mask], fwd[mask])[0, 1])

        if month_index % 12 == 0:
            print(
                f"{dates[t]}: eligible {int(eligible_now.sum())}, pool {int(pool_now.sum())}, "
                f"label {int(np.isfinite(label_now).sum())}",
                flush=True,
            )

    with np.errstate(invalid="ignore"):
        test_mask = np.array(
            [dates[t] >= "2024-01-01" and np.isfinite(fwd_ret[:, i]).any() for i, t in enumerate(months)]
        )
        factor_ic = np.nanmean(np.where(test_mask[None, :], single_ic, np.nan), axis=1)
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(
        OUT / "factors.npz",
        month_dates=np.array([dates[t] for t in months], dtype="U10"),
        month_indices=np.array(months, dtype=np.int32),
        factor_names=np.array(FACTOR_NAMES, dtype="U16"),
        signs=np.array([SIGNS[name] for name in FACTOR_NAMES], dtype=np.float32),
        z=z,
        eligible=eligible,
        pool=pool,
        pool_size=pool_size,
        fwd_ret=fwd_ret,
        label=label,
        float_mcap=mcap_t,
        close=close_t,
        single_ic=single_ic,
        factor_ic=factor_ic,
        industry_names=np.array([str(item) for item in industry_codes], dtype="U40"),
        industry_ids=industry_ids.astype(np.int16),
    )
    summary = {
        "months": len(months),
        "first": dates[months[0]],
        "last": dates[months[-1]],
        "mean_eligible": float(eligible.sum(axis=0).mean()),
        "mean_pool": float(pool_size.mean()),
        "factors": FACTOR_NAMES,
        "signs": {name: SIGNS[name] for name in FACTOR_NAMES},
        "factor_test_ic": {name: float(factor_ic[i]) for i, name in enumerate(FACTOR_NAMES)},
        "min_float_mcap": MIN_AMOUNT_PROXY,
        "min_listed_days": MIN_LISTED_DAYS,
        "pool_quantile": POOL_QUANTILE,
    }
    (OUT / "factors_meta.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
