"""Build the daily panel from the Eastmoney bulk valuation files.

Inputs::

    data/by_date/<date>.csv.gz   one row per A-share per trading date
    data/stock_basic.csv         latest code/name/industry snapshot

Outputs::

    panel/panel.npz              float32 tensors [stock, date]
    panel/metadata.json          dates, stocks, names, industries, coverage

The panel deliberately carries no volume or open price: the source report has
neither, so the experiment executes at the next close and skips turnover /
Amihud factors.  Point-in-time ``is_st`` comes from the name on each date, and
industry is the latest board (used for cross-sectional neutralisation).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
DATA = ROOT / "data"
BY_DATE = DATA / "by_date"
OUT = ROOT / "panel"

FIELDS = [
    "close",
    "pct_chg",
    "total_mcap",
    "float_mcap",
    "free_shares",
    "pe",
    "pb",
    "ps",
    "pcf",
]


def valid_code(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301"))


def main() -> None:
    files = sorted(BY_DATE.glob("*.csv.gz"))
    if not files:
        raise SystemExit("no by_date files; run download.py first")
    frames = []
    for path in files:
        frame = pd.read_csv(path, dtype={"date": str, "code": str})
        if len(frame):
            frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    frame = frame[frame["code"].map(valid_code)]
    frame["pct_chg"] = pd.to_numeric(frame["pct_chg"], errors="coerce") / 100.0
    frame = frame.drop_duplicates(["code", "date"])
    dates = sorted(frame["date"].unique().tolist())
    codes = sorted(frame["code"].unique().tolist())
    date_index = {value: index for index, value in enumerate(dates)}
    code_index = {value: index for index, value in enumerate(codes)}
    n_stocks, n_dates = len(codes), len(dates)
    print(f"panel {n_stocks} stocks x {n_dates} dates {dates[0]} ~ {dates[-1]}", flush=True)

    def empty(value=np.nan) -> np.ndarray:
        return np.full((n_stocks, n_dates), value, dtype=np.float32)

    close = empty()
    pct_chg = empty()
    total_mcap = empty()
    float_mcap = empty()
    free_shares = empty()
    pe = empty()
    pb = empty()
    ps = empty()
    pcf = empty()
    is_st = np.zeros((n_stocks, n_dates), dtype=bool)
    names = [""] * n_stocks
    industries = [""] * n_stocks

    for row in frame.itertuples(index=False):
        stock = code_index[row.code]
        day = date_index[row.date]
        close[stock, day] = row.close
        pct_chg[stock, day] = row.pct_chg if np.isfinite(row.pct_chg) else 0.0
        total_mcap[stock, day] = row.total_mcap
        float_mcap[stock, day] = row.float_mcap
        free_shares[stock, day] = row.free_shares
        pe[stock, day] = row.pe_ttm
        pb[stock, day] = row.pb
        ps[stock, day] = row.ps
        pcf[stock, day] = row.pcf
        names[stock] = row.name or names[stock]
        if row.industry:
            industries[stock] = row.industry
        upper = str(row.name or "").upper()
        is_st[stock, day] = "ST" in upper or "*ST" in upper
    for tensor in (pe, pb, ps, pcf, float_mcap, total_mcap, free_shares):
        tensor[~np.isfinite(tensor) | (tensor <= 0)] = np.nan

    tradable = np.isfinite(close) & (close > 0)
    listed = np.cumsum(tradable, axis=1).astype(np.int32)
    # The source only starts in 2018, so stocks already trading on the first
    # day are seasoned even though their in-panel history is short.
    listed[tradable[:, 0]] += 1000
    listed_days = np.clip(listed, 0, 32000).astype(np.int16)

    with np.errstate(invalid="ignore"):
        market_pct = np.nanmean(np.where(tradable, pct_chg, np.nan), axis=0)
    market_pct = np.where(np.isfinite(market_pct), market_pct, 0.0).astype(np.float32)
    benchmark_close = np.cumprod(1.0 + market_pct).astype(np.float32)

    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(
        OUT / "panel.npz",
        dates=np.array(dates, dtype="U10"),
        stocks=np.array(codes, dtype="U9"),
        names=np.array(names, dtype="U40"),
        industries=np.array(industries, dtype="U40"),
        close=close,
        pct_chg=pct_chg,
        total_mcap=total_mcap,
        float_mcap=float_mcap,
        free_shares=free_shares,
        pe=pe,
        pb=pb,
        ps=ps,
        pcf=pcf,
        is_st=is_st,
        tradable=tradable,
        listed_days=listed_days,
        benchmark_close=benchmark_close,
        benchmark_pct=market_pct,
    )
    metadata = {
        "dates": dates,
        "stocks": codes,
        "names": names,
        "industries": industries,
        "stock_count": n_stocks,
        "date_count": n_dates,
        "actual_start": dates[0],
        "actual_end": dates[-1],
        "rows": int(len(frame)),
        "source": "Eastmoney RPT_VALUEANALYSIS_DET bulk by trade date",
        "universe": "all currently listed Shanghai/Shenzhen A-shares",
        "limitation": "currently listed universe only; delisted names absent; close-only execution; no volume",
    }
    (OUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    print(f"panel written: {n_stocks} stocks, {n_dates} dates, {len(frame)} rows", flush=True)


if __name__ == "__main__":
    main()
