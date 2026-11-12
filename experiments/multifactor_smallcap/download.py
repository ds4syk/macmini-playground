"""Download point-in-time A-share valuation panels from Eastmoney.

The small-cap multi-factor experiment needs a broader universe than the 653
historical CSI300 names in ``Share/v2``.  Eastmoney's ``RPT_VALUEANALYSIS_DET``
data-centre report returns, for one trading date, every A-share's close,
pct change, total/float market cap, free shares, PE(TTM), PE(static), PB, PS,
PCF and point-in-time industry board.  That single bulk endpoint is both much
lighter and far more reliable than per-stock kline calls: the per-stock kline
host reset connections after a few dozen requests on 2026-11-12, while this
report answered hundreds of requests without throttling.

One CSV per trading date is written under ``data/by_date``; holidays keep an
empty file so reruns can resume without re-querying.

The report has no volume/amount and no open price, so the experiment executes
at the next close instead of the next open and skips turnover/Amihud factors.
That trade-off is documented in the README.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
DATA = ROOT / "data"
BY_DATE = DATA / "by_date"

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_NAME,TRADE_DATE,CLOSE_PRICE,"
    "CHANGE_RATE,TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A,FREE_SHARES_A,"
    "PE_TTM,PE_LAR,PB_MRQ,PS_TTM,PCF_OCF_TTM"
)
HEADER = [
    "date",
    "code",
    "name",
    "industry",
    "close",
    "pct_chg",
    "total_mcap",
    "float_mcap",
    "free_shares",
    "pe_ttm",
    "pe_lyr",
    "pb",
    "ps",
    "pcf",
]
PAGE_SIZE = 5000
START = "2018-01-01"
END = "2026-11-12"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def fetch_page(day: str, page: int, retries: int = 4) -> dict:
    params = {
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "pageSize": str(PAGE_SIZE),
        "pageNumber": str(page),
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": COLUMNS,
        "source": "WEB",
        "client": "WEB",
        "filter": f"(TRADE_DATE='{day}')",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except Exception as exc:  # noqa: BLE001 - network retry
            last = exc
            time.sleep(0.8 * (attempt + 1) + random.random() * 0.5)
    raise RuntimeError(f"{day} page {page} failed: {last!r}")


def fetch_day(day: str) -> int:
    destination = BY_DATE / f"{day}.csv.gz"
    if destination.exists():
        return -1
    rows = []
    page = 1
    while True:
        payload = fetch_page(day, page)
        result = payload.get("result") or {}
        data = result.get("data") or []
        for row in data:
            name = str(row.get("SECURITY_NAME_ABBR") or "")
            rows.append(
                [
                    day,
                    str(row.get("SECURITY_CODE") or ""),
                    name,
                    str(row.get("BOARD_NAME") or ""),
                    row.get("CLOSE_PRICE"),
                    row.get("CHANGE_RATE"),
                    row.get("TOTAL_MARKET_CAP"),
                    row.get("NOTLIMITED_MARKETCAP_A"),
                    row.get("FREE_SHARES_A"),
                    row.get("PE_TTM"),
                    row.get("PE_LAR"),
                    row.get("PB_MRQ"),
                    row.get("PS_TTM"),
                    row.get("PCF_OCF_TTM"),
                ]
            )
        pages = result.get("pages") or 1
        if page >= int(pages) or not data:
            break
        page += 1
        time.sleep(0.15)
    if not rows:
        # Weekend / holiday: keep an empty marker so reruns skip it.
        payload = io.StringIO()
        csv.writer(payload).writerow(HEADER)
        with gzip.open(destination, "wt", encoding="utf-8") as handle:
            handle.write(payload.getvalue())
        return 0
    payload = io.StringIO()
    writer = csv.writer(payload)
    writer.writerow(HEADER)
    writer.writerows(rows)
    temporary = destination.with_suffix(destination.suffix + ".%d.part" % os.getpid())
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        handle.write(payload.getvalue())
    temporary.replace(destination)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Eastmoney daily valuation panels")
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()

    BY_DATE.mkdir(parents=True, exist_ok=True)
    current = date.fromisoformat(args.start)
    last = date.fromisoformat(args.end)
    days = []
    while current <= last:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    print(f"{len(days)} weekdays {days[0]} ~ {days[-1]}", flush=True)
    started = time.time()
    done = 0
    failures = []
    latest_rows = []
    for day in days:
        try:
            count = fetch_day(day)
            done += 1
            if count > 0:
                latest_rows.append((day, count))
            if done % 25 == 0 or done == len(days):
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                remaining = (len(days) - done) / rate / 60 if rate else -1
                print(
                    f"{done}/{len(days)} {day} rows={count} {rate:.2f} days/s "
                    f"eta {remaining:.0f}m",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - keep going
            failures.append((day, repr(exc)))
            print(f"FAIL {day}: {exc!r}", flush=True)
        time.sleep(args.sleep)

    # Latest non-empty panel supplies names and industries for the stock list.
    stock_rows = []
    for day, _ in reversed(latest_rows):
        path = BY_DATE / f"{day}.csv.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                stock_rows.append(row)
        if stock_rows:
            break
    if stock_rows:
        with (DATA / "stock_basic.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["code", "name", "industry", "as_of"])
            writer.writeheader()
            for row in stock_rows:
                writer.writerow(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "industry": row["industry"],
                        "as_of": row["date"],
                    }
                )
    if failures:
        (DATA / "download_failures.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    manifest = {
        "provider": "Eastmoney RPT_VALUEANALYSIS_DET",
        "url": BASE,
        "columns": COLUMNS,
        "start": args.start,
        "end": args.end,
        "days": len(days),
        "downloaded": done,
        "failures": len(failures),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (DATA / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"done {done}/{len(days)} failures={len(failures)}", flush=True)


if __name__ == "__main__":
    main()
