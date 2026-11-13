"""Tencent (gtimg) daily-bar market data with a local SQLite cache."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Dict, Iterable, List

from . import config, net, store  # noqa: F401

API = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}


def _lookback_start(days: int = 420) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def fetch_remote(code: str, start: str, end: str, count: int = 360) -> List[dict]:
    param = "%s,day,%s,%s,%d,qfq" % (code, start, end, count)
    url = API + "?param=" + urllib.parse.quote(param, safe=",")
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            node = (payload.get("data") or {}).get(code) or {}
            rows = node.get("qfqday") or node.get("day") or []
            parsed = []
            for row in rows:
                if len(row) < 6:
                    continue
                parsed.append(
                    {
                        "date": str(row[0]),
                        "open": float(row[1]),
                        "close": float(row[2]),
                        "high": float(row[3]),
                        "low": float(row[4]),
                        "volume": float(row[5]),
                    }
                )
            return parsed
        except Exception as exc:  # noqa: BLE001 - surfaced below
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("fetch %s failed: %s" % (code, last_error))


def refresh_bars(conn, codes: Iterable[str], days: int = 420) -> Dict[str, int]:
    codes = list(codes)
    start = _lookback_start(days)
    end = date.today().isoformat()
    updated: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_remote, code, start, end): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            rows = future.result()
            payload = [
                (code, r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"])
                for r in rows
            ]
            # Stocks are keyed both by provider code (sh600519) for indexing and
            # by 6-digit symbol for the trading engine. Indices keep only the
            # provider code to avoid colliding with a same-numbered stock.
            index_codes = {item["code"] for item in config.INDICES}
            short = code[2:]
            if code not in index_codes and short.isdigit() and len(short) == 6:
                payload += [
                    (short, r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"])
                    for r in rows
                ]
            updated[code] = store.bulk_upsert_bars(conn, payload)
    return updated


def ensure_bars(conn, codes: Iterable[str], min_history: int = 130) -> None:
    missing = []
    for code in codes:
        lo, hi = store.bar_range(conn, code)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM bars WHERE symbol = ?", (code,)
        ).fetchone()["n"]
        if count < min_history or not hi:
            missing.append(code)
    if missing:
        refresh_bars(conn, missing)
    else:
        # Incremental refresh: pull a short window and upsert (idempotent).
        refresh_bars(conn, list(codes), days=30)


def all_codes() -> List[str]:
    return [item["code"] for item in config.UNIVERSE] + [
        item["code"] for item in config.INDICES
    ]
