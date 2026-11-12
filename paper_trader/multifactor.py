"""Build the small-cap multi-factor dashboard family.

Reads the lab export under ``~/labs/VLA/Share/multifactor/local_export`` and
replays the three monthly score matrices with the shared numpy account engine.
The replay window is the full 2024-01-02 .. latest test window (not the paper
trading start), so the dashboard can show the model's out-of-sample test years
next to the two existing families.

No torch or LightGBM is needed locally: only numpy.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np

from . import config, export, store
from experiments.multifactor_smallcap import account

MF_DIR = Path(os.environ.get("MF_DIR", Path.home() / "labs" / "VLA" / "Share" / "multifactor" / "local_export"))
LOCAL_START = os.environ.get("MF_LOCAL_START", "2024-01-02")

MODELS = [
    {
        "route": "mf_lgbm",
        "id": "mf-lgbm",
        "name": "多因子 LightGBM",
        "short": "多因子 LGBM",
        "color": "#c62828",
        "note": "14 因子 · LightGBM 3 种子 · 季度调仓",
    },
    {
        "route": "mf_equal",
        "id": "mf-equal",
        "name": "多因子等权",
        "short": "多因子等权",
        "color": "#1f6feb",
        "note": "14 因子截面 z 分数等权 · 季度调仓",
    },
    {
        "route": "mf_size",
        "id": "mf-size",
        "name": "纯小市值",
        "short": "小市值",
        "color": "#0a9b5a",
        "note": "只按流通市值最小排序 · 季度调仓",
    },
]
CAPITALS = [(10000, "1万"), (30000, "3万"), (50000, "5万"), (100000, "10万")]


def symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def load() -> tuple[dict, dict]:
    panel = dict(np.load(MF_DIR / "panel.npz", allow_pickle=False))
    metadata = json.loads((MF_DIR / "metadata.json").read_text(encoding="utf-8"))
    return panel, metadata


def build_portfolio(
    model: dict,
    capital: int,
    rows: List[dict],
    units: np.ndarray,
    cost_basis: np.ndarray,
    trades: List[dict],
    panel: dict,
    metadata: dict,
    signal_day: int,
    names: Dict[str, str],
    metrics: Dict[str, float],
    last_close: np.ndarray,
    end: int,
) -> dict:
    dates = metadata["dates"]
    stocks = metadata["stocks"]
    equity = [round(row["equity"], 2) for row in rows]
    series = [
        {"date": row["date"], "total": round(row["equity"], 2), "ret": round((row["equity"] / capital - 1) * 100, 2)}
        for row in rows
    ]
    positions = []
    for index in np.flatnonzero(units > 0):
        index = int(index)
        shares = int(units[index])
        price = float(last_close[index]) if np.isfinite(last_close[index]) and last_close[index] > 0 else 0.0
        value = shares * price
        average = cost_basis[index] / max(shares, 1)
        positions.append(
            {
                "symbol": symbol(stocks[index]),
                "name": names.get(stocks[index], stocks[index]),
                "sector": "",
                "shares": shares,
                "avg_cost": round(average, 3),
                "price": round(price, 3),
                "value": round(value, 2),
                "pnl": round((price - average) * shares, 2),
                "pnl_pct": round((price / average - 1) * 100, 2) if average else 0.0,
                "weight": round(value / equity[-1] * 100, 2) if equity[-1] else 0.0,
            }
        )
    positions.sort(key=lambda item: item["value"], reverse=True)
    trade_rows = []
    for trade in sorted(trades, key=lambda item: (item["date"], item["symbol"]), reverse=True)[:24]:
        trade_rows.append(
            {
                "date": trade["date"],
                "symbol": symbol(trade["symbol"]),
                "name": names.get(trade["symbol"], trade["symbol"]),
                "side": trade["side"],
                "shares": trade["shares"],
                "price": round(trade["price"], 3),
                "amount": round(trade["amount"], 2),
                "fee": round(trade["fee"], 2),
                "realized_pnl": round(trade["realized_pnl"], 2),
                "reason": trade["reason"],
            }
        )
    today_pnl = equity[-1] - equity[-2] if len(equity) > 1 else 0.0
    today_pct = (equity[-1] / equity[-2] - 1) * 100 if len(equity) > 1 and equity[-2] else 0.0
    decision = {
        "date": dates[signal_day],
        "market_view": (
            "小市值池内 14 因子截面打分，取前 50 名；季度调仓、全量换成前 50 名；"
            "信号 %s 收盘生成，下一交易日收盘成交；持仓估值至 %s。"
            % (dates[signal_day], dates[end])
        ),
        "confidence": 0.0,
        "targets": [
            {"symbol": position["symbol"], "name": position["name"], "weight": position["weight"], "reason": "当前持仓"}
            for position in positions[:8]
        ],
    }
    return {
        "id": "%s__%d" % (model["id"], capital),
        "model_id": model["id"],
        "model_name": model["name"],
        "capital": capital,
        "capital_label": next(label for value, label in CAPITALS if value == capital),
        "cash": round(rows[-1]["cash"], 2),
        "total": round(rows[-1]["equity"], 2),
        "ret_pct": round((rows[-1]["equity"] / capital - 1) * 100, 2),
        "today_pnl": round(today_pnl, 2),
        "today_pct": round(today_pct, 2),
        "positions": positions,
        "trades": trade_rows,
        "decision": decision,
        "metrics": {key: round(value, 2) for key, value in metrics.items()},
        "series": series,
    }


def build_family() -> dict:
    panel, metadata = load()
    dates = metadata["dates"]
    stocks = metadata["stocks"]
    names = {code: name for code, name in zip(stocks, metadata["names"])}
    scores = {
        model["route"]: np.load(MF_DIR / "scores" / ("%s_ensemble.npz" % model["route"]))["score"]
        for model in MODELS
    }
    start = int(np.searchsorted(dates, LOCAL_START))
    if start >= len(dates):
        raise SystemExit("invalid multifactor start %s" % LOCAL_START)
    signal_days = sorted(metadata["signal_days"])
    start_signal = max(day for day in signal_days if day < start) if any(day < start for day in signal_days) else signal_days[0]
    params = json.loads((MF_DIR / "config.json").read_text(encoding="utf-8")) if (MF_DIR / "config.json").exists() else {}
    stride = max(1, int(params.get("rebalance_stride", 1)))
    last_rebalance = signal_days[::stride][-1]
    latest_score = signal_days[-1]
    end = len(dates) - 1
    last_close = np.full(len(stocks), np.nan)
    close_matrix = panel["close"]
    for index in range(len(stocks)):
        row = close_matrix[index, : end + 1]
        finite = np.flatnonzero(np.isfinite(row))
        if len(finite):
            last_close[index] = row[finite[-1]]
    params = json.loads((MF_DIR / "config.json").read_text(encoding="utf-8")) if (MF_DIR / "config.json").exists() else {}

    portfolios = []
    all_trades = []
    for model in MODELS:
        for capital, _ in CAPITALS:
            rows, units, cost_basis, trades, _ = account.run(
                dates,
                stocks,
                panel["close"],
                panel["pct_chg"],
                panel["float_mcap"],
                panel["is_st"],
                panel["tradable"],
                scores[model["route"]],
                capital,
                start_signal,
                end,
                params,
            )
            trimmed_rows = [row for row in rows if row["date"] >= LOCAL_START]
            trimmed_trades = [trade for trade in trades if trade["date"] >= LOCAL_START]
            metrics = account.metrics(trimmed_rows, trimmed_trades, capital)
            portfolios.append(
                build_portfolio(
                    model,
                    capital,
                    trimmed_rows,
                    units,
                    cost_basis,
                    trimmed_trades,
                    panel,
                    metadata,
                    last_rebalance,
                    names,
                    metrics,
                    last_close,
                    end,
                )
            )
            all_trades.extend(trimmed_trades)

    by_model: Dict[str, List[dict]] = {}
    for portfolio in portfolios:
        by_model.setdefault(portfolio["model_id"], []).append(portfolio)
    model_payload = []
    results = json.loads((MF_DIR / "metrics.json").read_text(encoding="utf-8")) if (MF_DIR / "metrics.json").exists() else {}
    for model in MODELS:
        items = by_model.get(model["id"], [])
        returns = [item["ret_pct"] for item in items]
        best = max(items, key=lambda item: item["ret_pct"]) if items else None
        ic = results.get("models", {}).get(model["route"], {}).get("test_ic", {}).get("ic_mean")
        if ic is None or not np.isfinite(float(ic)):
            ic = None
        model_payload.append(
            {
                "id": model["id"],
                "name": model["name"],
                "short": model["short"],
                "provider": "多因子",
                "color": model["color"],
                "note": model["note"],
                "avg_ret": round(sum(returns) / len(returns), 2) if returns else 0,
                "best_capital": best["capital"] if best else 0,
                "best_ret": best["ret_pct"] if best else 0,
                "market_view": "测试 Rank IC %.3f · 小市值池内季度调仓" % ic if ic is not None else "小市值池内季度调仓",
                "confidence": 0,
                "decision_date": dates[last_rebalance],
                "trade_count": sum(item["metrics"]["trades"] for item in items),
                "latest_total": round(sum(item["total"] for item in items), 2),
                "params": "14 因子",
                "ic": ic if ic is not None else 0.0,
            }
        )

    last_signal_scores = scores["mf_lgbm"][:, latest_score]
    order = np.argsort(-last_signal_scores, kind="stable")
    top = [int(index) for index in order[:30] if np.isfinite(last_signal_scores[index])]
    universe = []
    for rank, index in enumerate(top, 1):
        code = stocks[index]
        closes = panel["close"][index]
        price = float(last_close[index]) if np.isfinite(last_close[index]) and last_close[index] > 0 else 0.0

        def change(back):
            if end < back or not price:
                return 0.0
            base = closes[end - back]
            if not np.isfinite(base) or base <= 0:
                return 0.0
            return (price / base - 1) * 100

        spark = []
        for value in closes[max(0, end - 19): end + 1]:
            if np.isfinite(value) and value > 0:
                spark.append(round(float(value), 2))
        universe.append(
            {
                "symbol": symbol(code),
                "name": names.get(code, code),
                "sector": "#%d" % rank,
                "close": round(price, 2),
                "chg1": round(float(change(1)), 2),
                "chg5": round(float(change(5)), 2),
                "chg20": round(float(change(20)), 2),
                "score": round(float(last_signal_scores[index]), 3),
                "spark": spark,
            }
        )

    benchmark_close = panel["benchmark_close"]
    base = float(benchmark_close[start])
    benchmark = [
        {"date": dates[i], "ret": round((float(benchmark_close[i]) / base - 1) * 100, 2) if base else None}
        for i in range(start, end + 1)
    ]

    latest_trades = []
    for portfolio in portfolios:
        for trade in portfolio["trades"]:
            latest_trades.append(
                {
                    "date": trade["date"],
                    "model_id": portfolio["model_id"],
                    "capital": portfolio["capital"],
                    "symbol": trade["symbol"],
                    "name": trade["name"],
                    "side": trade["side"],
                    "shares": trade["shares"],
                    "price": trade["price"],
                    "amount": trade["amount"],
                    "reason": trade["reason"],
                }
            )
    latest_trades.sort(key=lambda item: (item["date"], item["model_id"]), reverse=True)
    latest_trades = latest_trades[:30]

    conn = store.connect()
    market = export._index_payload(conn, dates[end])
    candles = export._index_candles(conn, dates[end])
    current = sum(item["total"] for item in portfolios)
    initial = sum(capital for capital, _ in CAPITALS) * len(MODELS)
    today_pnl = sum(item["today_pnl"] for item in portfolios)
    return {
        "family": "multifactor",
        "label": "多因子 · 小市值",
        "as_of": dates[end],
        "trade_days": len(portfolios[0]["series"]) if portfolios else 0,
        "dates": [row["date"] for row in portfolios[0]["series"]] if portfolios else [],
        "generated_at": "",
        "summary": {
            "initial": initial,
            "current": round(current, 2),
            "ret_pct": round((current / initial - 1) * 100, 2) if initial else 0,
            "today_pnl": round(today_pnl, 2),
            "today_pct": round(today_pnl / (current - today_pnl) * 100 if current - today_pnl else 0, 2),
            "portfolio_count": len(portfolios),
            "model_count": len(MODELS),
        },
        "market": market,
        "breadth": {
            "up": sum(1 for item in universe if item["chg1"] > 0),
            "down": sum(1 for item in universe if item["chg1"] < 0),
            "flat": sum(1 for item in universe if item["chg1"] == 0),
            "total": len(universe),
        },
        "benchmark": benchmark,
        "candles": candles,
        "models": model_payload,
        "portfolios": portfolios,
        "trades": latest_trades,
        "universe": universe,
        "meta": {
            "start": dates[start],
            "end": dates[end],
            "last_signal": dates[last_rebalance],
            "latest_score": dates[latest_score],
            "capital": [value for value, _ in CAPITALS],
            "capital_labels": {str(value): label for value, label in CAPITALS},
            "universe_title": "最新打分 · 多因子 LightGBM",
            "method": "季度调仓 · 全 A 股小市值池（流通市值最小 30%）· 14 因子",
            "fee_note": "买卖 0.10%/0.20%，每笔最低 5 元，单票不超过流通市值 1%",
            "provenance": {
                "train": "2018–2021",
                "validation": "2022–2023",
                "test": "2024–2026-11-12",
                "source": "Eastmoney 日度估值面板 · lab 训练",
                "leakage": "测试区间 2024–2026 已被查看，不是严格前向；股票池为当前上市股票，存在幸存者偏差。",
            },
            "disclaimer": "模拟盘，仅供策略研究，不构成投资建议。多因子小市值口径与沪深300/LLM 家族不同，跨家族收益不可直接比较。",
            "valuation_note": "持仓按最新收盘估值至 %s；最近一次季度调仓 %s；最新打分 %s。" % (
                dates[end], dates[last_rebalance], dates[latest_score]
            ),
            "kind": "quant",
            "status_label": "多因子 · 季度调仓",
            "chart_method": "2024 起季度回放",
            "kpi_unit": "季度调仓",
            "benchmark_label": "全市场等权",
            "chart_sub": "三个多因子变体在同一本金下的收益率对比",
            "kpi_sub": "个组合 · 季度调仓 · 小市值池",
            "metric_label": "测试 IC",
            "decision_label": "最近一次季度调仓",
            "universe_note": "小市值池内打分前 30 名",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the multifactor dashboard family")
    parser.add_argument("--out", default=str(config.DATA_DIR / "multifactor_family.json"))
    args = parser.parse_args()
    payload = build_family()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        "multifactor family: %s ~ %s, %d portfolios, total %.2f"
        % (payload["meta"]["start"], payload["meta"]["end"], len(payload["portfolios"]), payload["summary"]["current"])
    )


if __name__ == "__main__":
    main()
