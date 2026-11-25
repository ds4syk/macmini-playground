"""Fetch a Hong Kong equity panel from Tencent's public quote endpoints.

Tencent is used for both the security list (via the HKEX ``ListOfSecurities``
workbook) and daily OHLCV.  Eastmoney's HK endpoints intermittently reset
connections from this host, while Tencent's ``hkfqkline``/``fqkline`` endpoints
were stable.  Both adjusted (``qfq``) and unadjusted (raw) histories are kept:

* ``adj_*``  -- back-adjusted (hfq) prices/volume for features and labels.
  Tencent's forward-adjusted (qfq) series turns negative for high-dividend
  names, so the backward-adjusted series is used instead;
* ``raw_*``  -- actual traded prices for cash accounting and board-lot sizing.

The amount column is reported by Tencent in ten-thousand HKD; turnover is in
percent.  Nothing here is investment advice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("HK_DATA_DIR", ROOT / "data" / "hk"))
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
HKEX_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
ADJ_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk{code},day,,{end},{count},hfq"
RAW_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=hk{code},day,,{end},{count}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
BENCHMARKS = {
    "HSI": "恒生指数",
    "HSCEI": "恒生中国企业指数",
    "HSTECH": "恒生科技指数",
}


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_bytes(url: str, timeout: int = 25, tries: int = 4) -> bytes:
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with _opener().open(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - network retries
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"request failed after {tries} tries: {url}: {last}")


def http_json(url: str, **kwargs):
    return json.loads(http_bytes(url, **kwargs).decode("utf-8", "replace"))


def _num(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# security master: HKEX list + Tencent quote snapshot
# ---------------------------------------------------------------------------


def load_hkex() -> pd.DataFrame:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "hkex_list.xlsx"
    if not path.exists():
        path.write_bytes(http_bytes(HKEX_URL, timeout=60, tries=3))
    frame = pd.read_excel(path, sheet_name="ListOfSecurities", header=2)
    frame.columns = [str(column).strip() for column in frame.columns]
    frame["code"] = frame["Stock Code"].astype(int).astype(str).str.zfill(5)
    frame["lot"] = (
        frame["Board Lot"].astype(str).str.replace(",", "", regex=False).astype(float).astype(int)
    )
    return frame


def _quote_batch(codes: list[str]) -> list[dict]:
    url = QUOTE_URL.format(codes=",".join("hk" + code for code in codes))
    text = http_bytes(url, timeout=12, tries=3).decode("gbk", "replace")
    rows = []
    for line in text.split(";"):
        if '="' not in line:
            continue
        fields = line.split('="', 1)[1].rstrip('"').split("~")
        if len(fields) < 50:
            continue
        rows.append(
            {
                "code": fields[2],
                "name": fields[1],
                "price": _num(fields[3]),
                "amount": _num(fields[37]),
                "market_cap_yi": _num(fields[45]),
            }
        )
    return rows


def quote_snapshot(codes: Iterable[str]) -> pd.DataFrame:
    codes = sorted(set(codes))
    batches = [codes[index:index + 50] for index in range(0, len(codes), 50)]
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_quote_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except Exception:  # noqa: BLE001 - one bad batch should not stop the scan
                continue
    return pd.DataFrame(rows).drop_duplicates("code")


def candidates() -> pd.DataFrame:
    frame = load_hkex()
    main = frame[
        frame["Category"].eq("Equity")
        & frame["Sub-Category"].astype(str).str.contains("Main Board")
        & frame["Trading Currency"].eq("HKD")
        & frame["RMB Counter"].isna()
        & frame["lot"].gt(0)
    ].copy()
    quotes = quote_snapshot(main["code"].tolist())
    merged = main.merge(quotes, on="code", how="inner")
    merged = merged[
        merged["market_cap_yi"].ge(20.0)  # >= HKD 2bn total market cap
        & merged["price"].ge(float(CONFIG["min_price"]))
        & merged["amount"].ge(2_000_000)
    ].copy()
    merged = merged.rename(columns={"Name of Securities": "name_en"})
    merged = merged[["code", "name", "name_en", "lot", "price", "amount", "market_cap_yi"]]
    merged.sort_values("market_cap_yi", ascending=False, inplace=True)
    DATA.mkdir(parents=True, exist_ok=True)
    merged.to_csv(DATA / "candidates.csv", index=False)
    return merged


# ---------------------------------------------------------------------------
# daily history
# ---------------------------------------------------------------------------


def _parse_adj(code: str, payload: dict) -> list[dict]:
    node = (payload.get("data") or {}).get("hk" + code) or {}
    arrays = [node[key] for key in node if key.endswith("day")]
    if not arrays:
        return []
    rows = arrays[0]
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        close = _num(row[2])
        volume = _num(row[5])
        turnover = _num(row[7]) / 100.0 if len(row) > 7 else 0.0
        # Tencent reports amount in ten-thousand HKD.
        amount = _num(row[8]) * 10_000.0 if len(row) > 8 else volume * close
        out.append(
            {
                "date": row[0],
                "adj_open": _num(row[1]),
                "adj_close": close,
                "adj_high": _num(row[3]),
                "adj_low": _num(row[4]),
                "adj_volume": volume,
                "adj_turnover": turnover,
                "adj_amount": amount,
            }
        )
    return out


def _parse_raw(code: str, payload: dict) -> list[dict]:
    node = (payload.get("data") or {}).get("hk" + code) or {}
    arrays = [node[key] for key in node if key.endswith("day")]
    if not arrays:
        return []
    out = []
    for row in arrays[0]:
        if len(row) < 6:
            continue
        out.append(
            {
                "date": row[0],
                "raw_open": _num(row[1]),
                "raw_close": _num(row[2]),
                "raw_high": _num(row[3]),
                "raw_low": _num(row[4]),
                "raw_volume": _num(row[5]),
            }
        )
    return out


def _fetch_pages(code: str, kind: str, start: str, end: str) -> list[dict]:
    """Walk backwards from ``end`` 640 sessions at a time until ``start``."""
    parser = _parse_adj if kind == "adj" else _parse_raw
    template = ADJ_URL if kind == "adj" else RAW_URL
    rows: list[dict] = []
    cursor = end
    for _ in range(40):
        payload = http_json(template.format(code=code, end=cursor, count=640), timeout=25, tries=4)
        page = parser(code, payload)
        if not page:
            break
        rows.extend(page)
        earliest = min(item["date"] for item in page)
        if earliest <= start:
            break
        cursor = (datetime.fromisoformat(earliest) - timedelta(days=1)).date().isoformat()
    return rows


def fetch_security(code: str) -> dict:
    target = DATA / "raw" / f"{code}.csv"
    if target.exists():
        try:
            existing = pd.read_csv(target, dtype={"date": str})
            if len(existing) and existing["date"].max() >= "2026-11-20":
                return {"code": code, "cached": True, "rows": len(existing)}
        except Exception:  # noqa: BLE001 - rebuild corrupt cache
            pass
    adjusted = _fetch_pages(code, "adj", CONFIG["data_start"], CONFIG["data_end"])
    raw = _fetch_pages(code, "raw", "2023-06-01", CONFIG["data_end"])
    if not adjusted:
        return {"code": code, "cached": False, "rows": 0}
    adjusted_frame = pd.DataFrame(adjusted).drop_duplicates("date")
    if raw:
        raw_frame = pd.DataFrame(raw).drop_duplicates("date")
        frame = adjusted_frame.merge(raw_frame, on="date", how="left")
    else:
        frame = adjusted_frame
    frame = frame.sort_values("date")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(target)
    return {"code": code, "cached": False, "rows": len(frame)}


def fetch_all(workers: int = 12) -> pd.DataFrame:
    universe = candidates()
    codes = universe["code"].tolist()
    print(f"candidates: {len(codes)}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_security, code): code for code in codes}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - keep going, report below
                results.append({"code": futures[future], "error": str(exc), "rows": 0})
            if done % 50 == 0:
                print(f"fetched {done}/{len(codes)}", flush=True)
    report = pd.DataFrame(results)
    report.to_csv(DATA / "fetch_report.csv", index=False)
    failures = report[report.get("error").notna()] if "error" in report else pd.DataFrame()
    print(f"done: {len(report)} securities, {len(failures)} failures", flush=True)
    return universe


def fetch_benchmarks() -> pd.DataFrame:
    frames = []
    for code, name in BENCHMARKS.items():
        rows = _fetch_pages(code, "raw", CONFIG["data_start"], CONFIG["data_end"])
        if not rows:
            continue
        frame = pd.DataFrame(rows)
        frame["code"] = code
        frame["name"] = name
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(result):
        result.sort_values(["code", "date"], inplace=True)
        result.to_csv(DATA / "indices.csv", index=False)
    return result


def build_universe() -> pd.DataFrame:
    """Screen cached histories and persist the investable universe."""
    candidates_frame = pd.read_csv(DATA / "candidates.csv", dtype={"code": str})
    rows = []
    for _, item in candidates_frame.iterrows():
        path = DATA / "raw" / f"{item['code']}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype={"date": str})
        if not len(frame):
            continue
        frame = frame.sort_values("date")
        window = frame[frame["date"] >= CONFIG["data_start"]]
        adjusted = window["adj_close"].where(window["adj_close"] > 0)
        returns = adjusted.ffill().pct_change()
        # Corporate actions that the vendor's adjusted series cannot represent
        # (rights issues, consolidations) show up as negative/near-zero prices
        # or multi-x single-day jumps.  Exclude those names from the universe.
        if (window["adj_close"] < 0).any() or adjusted.min() <= 0.01:
            continue
        if returns.abs().max() > 2.0:
            continue
        amount = window["adj_amount"].tail(500).median()
        price = window["raw_close"].dropna().tail(1)
        rows.append(
            {
                "code": item["code"],
                "name": item["name"],
                "name_en": item["name_en"],
                "lot": int(item["lot"]),
                "market_cap_yi": float(item["market_cap_yi"]),
                "first_date": frame["date"].min(),
                "last_date": frame["date"].max(),
                "sessions": int(len(frame)),
                "median_amount": float(amount),
                "last_price": float(price.iloc[0]) if len(price) else 0.0,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("no cached histories: run `python -m experiments.hk_models.fetch all` first")
    frame = frame[
        frame["first_date"].le(CONFIG["min_history_start"])
        & frame["last_date"].ge("2026-11-20")
        & frame["sessions"].ge(2400)
        & frame["median_amount"].ge(float(CONFIG["min_median_amount"]))
        & frame["last_price"].ge(float(CONFIG["min_price"]))
    ].copy()
    frame.sort_values(["median_amount", "market_cap_yi"], ascending=False, inplace=True)
    size = int(CONFIG["universe_size"])
    frame = frame.head(size).sort_values("code")
    frame.to_csv(Path(__file__).parent / "universe.csv", index=False)
    print(
        f"universe: {len(frame)} names, median amount >= "
        f"{CONFIG['min_median_amount'] / 1e6:.1f}m HKD, first date <= {CONFIG['min_history_start']}",
        flush=True,
    )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description="fetch HK equity histories")
    parser.add_argument("step", choices=["candidates", "fetch", "universe", "benchmarks", "all"])
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    if args.step in ("candidates", "all"):
        frame = candidates()
        print(f"candidates: {len(frame)}", flush=True)
    if args.step in ("fetch", "all"):
        fetch_all(workers=args.workers)
    if args.step in ("benchmarks", "all"):
        frame = fetch_benchmarks()
        print(f"benchmarks: {len(frame)} rows", flush=True)
    if args.step in ("universe", "all"):
        frame = build_universe()
        print(frame[["code", "name", "lot", "median_amount"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
