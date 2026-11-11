"""Import the user's self-trained VLA/Share models as a dashboard family.

The five routes (GRU+Attention, TCN, Transformer, CNN, GDN+NTP) were trained
in ~/labs/VLA/Share/v2 with train 2016-2021 / validation 2022-2023 /
test 2024-2026-11-09.  This module replays their *precomputed out-of-sample
score matrices* for a window with a faithful copy of the account model:
100-share lots (200 for sh.688), 0.10%/0.20% costs with 5 CNY minimum,
1% of previous-day amount liquidity cap, T+1, next-open execution and the
low-turnover rule (30 names, top-50 buffer, max 5 replacements / 5 sessions).

No torch is needed.  Run with system python3 (numpy only).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import urllib.request
from pathlib import Path
from typing import Dict, List

import numpy as np

from . import config, export, store

VLA_DIR = Path(os.environ.get("VLA_DIR", Path.home() / "labs" / "VLA" / "Share" / "v2"))
LOCAL_START = os.environ.get("LOCAL_START", "2026-09-01")
LOCAL_END = os.environ.get("LOCAL_END", "2026-11-09")

MODELS = [
    {
        "id": "local-attn-gru",
        "route": "attn_gru",
        "name": "GRU + Attention",
        "short": "GRU+Attn",
        "color": "#0a9b5a",
        "params": 48962,
        "ic": 0.0595,
        "ic_ensemble": 0.0636,
        "note": "25 维日线特征 · 60 日窗口 · 5 日截面排序",
    },
    {
        "id": "local-tcn",
        "route": "tcn",
        "name": "TCN",
        "short": "TCN",
        "color": "#1f6feb",
        "params": 95745,
        "ic": 0.0434,
        "ic_ensemble": 0.0506,
        "note": "4 个因果残差块 · 感受野 61 日",
    },
    {
        "id": "local-transformer",
        "route": "transformer",
        "name": "Transformer",
        "short": "Transformer",
        "color": "#7b2ff2",
        "params": 74689,
        "ic": 0.0539,
        "ic_ensemble": 0.0593,
        "note": "2 层 4 头自注意力 · 均值汇聚",
    },
    {
        "id": "local-cnn",
        "route": "cnn",
        "name": "CNN",
        "short": "CNN",
        "color": "#f08c00",
        "params": 32193,
        "ic": 0.0574,
        "ic_ensemble": 0.0638,
        "note": "3 层一维卷积 · 全局平均汇聚",
    },
    {
        "id": "local-gdn-ntp",
        "route": "gdn_ntp",
        "name": "GDN + NTP",
        "short": "GDN+NTP",
        "color": "#00a6a6",
        "params": 0,
        "ic": 0.0567,
        "ic_ensemble": 0.0663,
        "note": "gated delta-rule + 10 类 token NTP · Muon",
    },
]

CAPITALS = [10000, 30000, 50000, 100000]


def _lot(code: str) -> int:
    return 200 if str(code).startswith("sh.688") else 100


def _fee(notional: float, rate: float) -> float:
    return max(notional * rate, 5.0) if notional > 0 else 0.0


def _limits(codes: List[str], date: str, is_st: np.ndarray) -> np.ndarray:
    limits = np.full(len(codes), 0.10)
    for i, code in enumerate(codes):
        if code.startswith("sh.688") or (
            code.startswith(("sz.300", "sz.301")) and date >= "2020-08-24"
        ):
            limits[i] = 0.20
        elif is_st[i] > 0 and date < "2026-07-06":
            limits[i] = 0.05
    return limits


def _target_shares(indices, value, raw_open, codes):
    target = np.zeros(len(codes), dtype=np.int64)
    for index in indices:
        price = raw_open[index]
        if price <= 0:
            continue
        lot = _lot(codes[index])
        target[index] = int(value // (price * lot)) * lot
    return target


def _names(codes: List[str]) -> Dict[str, str]:
    """Resolve Chinese names through Tencent's quote endpoint, with fallback."""
    out: Dict[str, str] = {}
    unique = sorted(set(codes))
    for start in range(0, len(unique), 50):
        chunk = unique[start:start + 50]
        qcodes = [c.replace(".", "") for c in chunk]
        url = "https://qt.gtimg.cn/q=" + ",".join(qcodes)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as resp:
                text = resp.read().decode("gbk", "replace")
            for line in text.split(";"):
                parts = line.split("~")
                if len(parts) < 3:
                    continue
                match = re.search(r"([a-z]{2}\d{6})", parts[0])
                code = match.group(1) if match else ""
                name = parts[1].strip()
                if code and name:
                    out[code[:2] + "." + code[2:]] = name
        except Exception:
            pass
    for code in unique:
        out.setdefault(code, code)
    return out


def _raw_close(panel, day: int) -> np.ndarray:
    raw_open = panel["raw_open"][:, day].astype(float)
    adjusted_open = panel["adj_open"][:, day].astype(float)
    adjusted_close = panel["adj_close"][:, day].astype(float)
    preclose = panel["preclose"][:, day].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(adjusted_open > 0, raw_open * adjusted_close / adjusted_open, preclose)
    return np.where(raw > 0, raw, preclose)


def backtest(panel, metadata, scores, capital: float, start: int, end: int, model: dict):
    dates = metadata["dates"]
    codes = metadata["stocks"]
    config_data = json.loads((VLA_DIR / "config.json").read_text())
    portfolio_size = int(config_data["portfolio_size"])
    buffer_size = int(config_data["buffer_size"])
    max_replace = int(config_data["max_replace"])
    rebalance_every = int(config_data["rebalance_days"])
    buy_cost = float(config_data["buy_cost"])
    sell_cost = float(config_data["sell_cost"])

    signal_days = [day for day in range(start, end + 1) if np.isfinite(scores[:, day]).any()]
    rebalance_days = set(signal_days[::rebalance_every])
    units = np.zeros(len(codes), dtype=np.int64)
    cash = float(capital)
    target_units = units.copy()
    cost_basis = np.zeros(len(codes), dtype=float)
    trades = []
    rows = []
    total_traded = 0.0
    total_fees = 0.0

    for day in range(start, end + 1):
        raw_open = panel["raw_open"][:, day].astype(float)
        adjusted_open = panel["adj_open"][:, day].astype(float)
        adjusted_close = panel["adj_close"][:, day].astype(float)
        preclose = panel["preclose"][:, day].astype(float)
        valuation_open = np.where(raw_open > 0, raw_open, preclose)
        raw_close = _raw_close(panel, day)
        units[panel["delisted"][:, day] > 0] = 0
        equity_open = cash + float(np.dot(units, valuation_open))
        traded = 0.0
        daily_fee = 0.0

        if day > start and (day - 1) in rebalance_days:
            stocks = np.flatnonzero(panel["eligible"][:, day - 1])
            values = scores[stocks, day - 1]
            order = np.argsort(-values, kind="stable")
            ranked = stocks[order]
            held = np.flatnonzero(units > 0)
            held_set = set(int(index) for index in held)
            position = {int(index): rank + 1 for rank, index in enumerate(ranked)}
            sell_list = [
                int(index)
                for index in held
                if position.get(int(index), 10 ** 9) > buffer_size
            ]
            sell_list.sort(key=lambda index: position.get(index, 10 ** 9), reverse=True)
            sell_list = sell_list[:max_replace]
            keep = [index for index in held_set if index not in set(sell_list)]
            pool = [int(index) for index in ranked if int(index) not in held_set]
            if len(held) == 0:
                capacity = portfolio_size
            else:
                gaps = max(0, portfolio_size - len(held))
                capacity = max(0, min(portfolio_size - len(keep), max_replace + gaps))
            purchases = pool[:capacity]
            target_units = units.copy()
            target_units[sell_list] = 0
            buy_target = _target_shares(
                purchases, equity_open / portfolio_size, raw_open, codes
            )
            target_units[purchases] = buy_target[purchases]

        tradable = panel["tradable"][:, day] > 0
        limits = _limits(codes, dates[day], panel["is_st"][:, day])
        ratio = np.divide(raw_open, preclose, out=np.zeros(len(codes)), where=preclose > 0)
        buyable = (
            tradable
            & (raw_open > 0)
            & (panel["is_st"][:, day] == 0)
            & (ratio < 1 + limits - 0.0005)
        )
        sellable = tradable & (raw_open > 0) & (ratio > 1 - limits + 0.0005)
        liquidity_cap = np.floor(
            panel["amount"][:, day - 1].astype(float) * 0.01 / np.maximum(raw_open, 1e-9)
        ).astype(np.int64) if day > start else np.zeros(len(codes), dtype=np.int64)

        sell_request = np.maximum(units - target_units, 0)
        sales = np.minimum(sell_request, liquidity_cap) * sellable.astype(np.int64)
        sales = np.maximum(sales, 0)
        for index in np.flatnonzero(sales > 0):
            shares = int(sales[index])
            price = float(raw_open[index])
            notional = shares * price
            fee = _fee(notional, sell_cost)
            avg = cost_basis[index] / max(units[index], 1)
            realized = (price - avg) * shares - fee
            cash += notional - fee
            units[index] -= shares
            total_traded += notional
            total_fees += fee
            traded += notional
            daily_fee += fee
            if units[index] <= 0:
                cost_basis[index] = 0.0
            else:
                cost_basis[index] -= avg * shares
            trades.append(
                {
                    "date": dates[day],
                    "symbol": codes[index],
                    "side": "sell",
                    "shares": shares,
                    "price": price,
                    "amount": notional,
                    "fee": fee,
                    "realized_pnl": realized,
                    "reason": "跌出前 50 名 / 调仓卖出",
                }
            )

        if day > start and (day - 1) in rebalance_days:
            for index in ranked if "ranked" in locals() else []:
                index = int(index)
                desired = int(target_units[index] - units[index])
                if desired <= 0 or not buyable[index]:
                    continue
                price = float(raw_open[index])
                if price <= 0:
                    continue
                lot = _lot(codes[index])
                affordable = int(cash // (price * (1 + buy_cost)))
                shares = min(
                    desired,
                    (affordable // lot) * lot,
                    int(liquidity_cap[index] // lot) * lot,
                )
                while shares > 0:
                    notional = shares * price
                    if notional + _fee(notional, buy_cost) <= cash + 1e-9:
                        break
                    shares -= lot
                if shares <= 0:
                    continue
                notional = shares * price
                fee = _fee(notional, buy_cost)
                cash -= notional + fee
                cost_basis[index] += notional + fee
                units[index] += shares
                total_traded += notional
                total_fees += fee
                traded += notional
                daily_fee += fee
                trades.append(
                    {
                        "date": dates[day],
                        "symbol": codes[index],
                        "side": "buy",
                        "shares": shares,
                        "price": price,
                        "amount": notional,
                        "fee": fee,
                        "realized_pnl": 0.0,
                        "reason": "截面排名买入",
                    }
                )

        equity = cash + float(np.dot(units, raw_close))
        if cash < -1e-6 or np.any(units < 0) or not math.isfinite(equity) or equity <= 0:
            raise RuntimeError("account invariant failed on %s %s" % (model["route"], dates[day]))
        rows.append(
            {
                "date": dates[day],
                "cash": cash,
                "equity": equity,
                "traded": traded,
                "fees": daily_fee,
            }
        )
        # reset ranked for next day's condition check
        if "ranked" in locals():
            del ranked

    return rows, units, cost_basis, trades, total_traded, total_fees


def _metrics(rows, trades, capital):
    equity = np.array([row["equity"] for row in rows], dtype=float)
    daily = np.diff(np.r_[capital, equity]) / np.r_[capital, equity[:-1]]
    peak = np.maximum.accumulate(np.r_[capital, equity])[1:]
    drawdown = equity / peak - 1
    sells = [t for t in trades if t["side"] == "sell"]
    wins = sum(1 for t in sells if t["realized_pnl"] > 0)
    vol = float(np.std(daily, ddof=1) * np.sqrt(252)) if len(daily) > 1 else 0.0
    sharpe = float(np.mean(daily) / max(np.std(daily, ddof=1), 1e-12) * np.sqrt(252)) if len(daily) > 1 else 0.0
    return {
        "ret_pct": (equity[-1] / capital - 1) * 100,
        "max_drawdown": float(drawdown.min() * 100),
        "sharpe": sharpe,
        "volatility": vol * 100,
        "win_rate": (wins / len(sells) * 100) if sells else 0.0,
        "trades": len(trades),
        "turnover": float(sum(t["amount"] for t in trades) / capital),
    }


def _portfolio(route, capital, rows, units, cost_basis, trades, names, panel, codes, end, dates, signal_day):
    model_id = next(m["id"] for m in MODELS if m["route"] == route)
    equity = [round(row["equity"], 2) for row in rows]
    series = [
        {
            "date": row["date"],
            "total": round(row["equity"], 2),
            "ret": round((row["equity"] / capital - 1) * 100, 2),
        }
        for row in rows
    ]
    raw_close = _raw_close(panel, end)
    positions = []
    for index in np.flatnonzero(units > 0):
        shares = int(units[index])
        price = float(raw_close[index])
        value = shares * price
        avg = cost_basis[index] / max(shares, 1)
        positions.append(
            {
                "symbol": names.get(codes[index], codes[index]),
                "name": names.get(codes[index], codes[index]),
                "sector": "",
                "shares": shares,
                "avg_cost": round(avg, 3),
                "price": round(price, 3),
                "value": round(value, 2),
                "pnl": round((price - avg) * shares, 2),
                "pnl_pct": round((price / avg - 1) * 100, 2) if avg else 0,
                "weight": round(value / equity[-1] * 100, 2) if equity[-1] else 0,
            }
        )
    positions.sort(key=lambda item: item["value"], reverse=True)
    trade_rows = []
    for trade in sorted(trades, key=lambda t: (t["date"], t["symbol"]), reverse=True)[:24]:
        trade_rows.append(
            {
                "date": trade["date"],
                "symbol": names.get(trade["symbol"], trade["symbol"]),
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
    today_pct = (
        (equity[-1] / equity[-2] - 1) * 100 if len(equity) > 1 and equity[-2] else 0.0
    )
    model = next(m for m in MODELS if m["route"] == route)
    decision = {
        "date": dates[signal_day],
        "market_view": "5 日截面排序前 30 名；低换手缓冲 50，最多换 5 只；最后一次信号 %s。" % dates[signal_day],
        "confidence": 0.0,
        "targets": [
            {
                "symbol": p["symbol"],
                "name": p["name"],
                "weight": round(p["weight"], 1),
                "reason": "当前持仓",
            }
            for p in positions[:8]
        ],
        "error": None,
    }
    return {
        "id": "%s__%d" % (model_id, capital),
        "model_id": model_id,
        "model_name": model["name"],
        "capital": capital,
        "capital_label": config.CAPITAL_LABELS[capital],
        "cash": round(rows[-1]["cash"], 2),
        "total": round(equity[-1], 2),
        "ret_pct": round((equity[-1] / capital - 1) * 100, 2),
        "today_pnl": round(today_pnl, 2),
        "today_pct": round(today_pct, 2),
        "positions": positions,
        "trades": trade_rows,
        "decision": decision,
        "metrics": {k: round(v, 2) for k, v in _metrics(rows, trades, capital).items()},
        "series": series,
    }


def build_family() -> dict:
    panel = dict(np.load(VLA_DIR / "local_data" / "panel.npz", allow_pickle=False))
    metadata = json.loads((VLA_DIR / "local_data" / "metadata.json").read_text())
    dates = np.array(metadata["dates"])
    codes = list(metadata["stocks"])
    start = int(np.searchsorted(dates, LOCAL_START))
    end = int(np.searchsorted(dates, LOCAL_END))
    if dates[start] != LOCAL_START:
        start = int(np.searchsorted(dates, LOCAL_START, side="left"))
    if dates[end] != LOCAL_END:
        end = int(np.searchsorted(dates, LOCAL_END, side="right")) - 1
    # Only signal dates whose labels are inside the panel are useful.
    if start >= end:
        raise SystemExit("invalid local window %s ~ %s" % (LOCAL_START, LOCAL_END))

    scores = {}
    for model in MODELS:
        score_dir = VLA_DIR / "local_data" / "scores"
        path = score_dir / ("%s_ensemble.npz" % model["route"])
        if path.exists():
            scores[model["route"]] = np.load(path)["score"]
        else:
            seeds = [
                np.load(score_dir / ("%s_%d.npz" % (model["route"], seed)))["score"]
                for seed in (42, 43, 44)
                if (score_dir / ("%s_%d.npz" % (model["route"], seed))).exists()
            ]
            if not seeds:
                raise SystemExit("no score matrix for %s" % model["route"])
            scores[model["route"]] = np.nanmean(np.stack(seeds), axis=0)

    last_signal = max(
        day for day in range(start, end + 1) if np.isfinite(scores["attn_gru"][:, day]).any()
    )
    all_trades = []
    positions_names = set()
    portfolios = []
    for model in MODELS:
        for capital in CAPITALS:
            rows, units, cost_basis, trades, _, _ = backtest(
                panel, metadata, scores[model["route"]], capital, start, end, model
            )
            for trade in trades:
                positions_names.add(trade["symbol"])
            positions_names.update(
                codes[index] for index in np.flatnonzero(units > 0)
            )
            all_trades.extend(trades)
            portfolios.append(
                _portfolio(
                    model["route"],
                    capital,
                    rows,
                    units,
                    cost_basis,
                    trades,
                    {},  # names patched below
                    panel,
                    codes,
                    end,
                    dates,
                    last_signal,
                )
            )

    names = _names(list(positions_names))
    # Patch names into portfolios and trades.
    for portfolio in portfolios:
        for pos in portfolio["positions"]:
            key = pos["symbol"]
            pos["name"] = names.get(key, key)
            pos["symbol"] = key.replace(".", "")
        for trade in portfolio["trades"]:
            key = trade["symbol"]
            trade["name"] = names.get(key, key)
            trade["symbol"] = key.replace(".", "")
        portfolio["decision"]["targets"] = [
            {
                "symbol": pos["symbol"],
                "name": pos["name"],
                "weight": pos["weight"],
                "reason": "当前持仓",
            }
            for pos in portfolio["positions"][:8]
        ]
    for trade in all_trades:
        trade["name"] = names.get(trade["symbol"], trade["symbol"])

    # The precomputed test matrices end where the 5-day label is still
    # observable (2026-10-30); equity is marked through 11-09.
    last = end
    signal_day = last_signal
    last = signal_day
    raw_close = _raw_close(panel, last)
    stocks = np.flatnonzero(panel["eligible"][:, signal_day])
    values = scores["attn_gru"][stocks, signal_day]
    finite = np.isfinite(values)
    stocks = stocks[finite]
    values = values[finite]
    order = np.argsort(-values)
    top = stocks[order][:30]
    top_names = _names([codes[i] for i in top])
    universe = []
    for rank, index in enumerate(top, 1):
        code = codes[index]
        adj = panel["adj_close"][index]
        chg1 = (adj[last] / adj[last - 1] - 1) * 100 if last > 0 and adj[last - 1] else 0
        chg5 = (adj[last] / adj[last - 5] - 1) * 100 if last >= 5 and adj[last - 5] else 0
        chg20 = (adj[last] / adj[last - 20] - 1) * 100 if last >= 20 and adj[last - 20] else 0
        adj_hist = panel["adj_close"][index, max(0, last - 19): last + 1]
        spark = [
            round(float(v) / float(adj_hist[-1]) * float(raw_close[index]), 2)
            if adj_hist[-1]
            else round(float(raw_close[index]), 2)
            for v in adj_hist
        ]
        universe.append(
            {
                "symbol": code.replace(".", ""),
                "name": top_names.get(code, code),
                "sector": "#%d" % rank,
                "close": round(float(raw_close[index]), 2),
                "chg1": round(float(chg1), 2),
                "chg5": round(float(chg5), 2),
                "chg20": round(float(chg20), 2),
                "score": round(float(scores["attn_gru"][index, signal_day]), 3),
                "spark": spark,
            }
        )

    by_model: Dict[str, List[dict]] = {}
    for portfolio in portfolios:
        by_model.setdefault(portfolio["model_id"], []).append(portfolio)
    models_payload = []
    for model in MODELS:
        items = by_model.get(model["id"], [])
        returns = [item["ret_pct"] for item in items]
        best = max(items, key=lambda item: item["ret_pct"]) if items else None
        models_payload.append(
            {
                "id": model["id"],
                "name": model["name"],
                "short": model["short"],
                "provider": "自训",
                "color": model["color"],
                "note": model["note"],
                "avg_ret": round(sum(returns) / len(returns), 2) if returns else 0,
                "best_capital": best["capital"] if best else 0,
                "best_ret": best["ret_pct"] if best else 0,
                "market_view": "Rank IC %.3f（集成 %.3f）· 5 日截面排序" % (model["ic"], model["ic_ensemble"]),
                "confidence": 0,
                "decision_date": dates[last_signal],
                "trade_count": sum(item["metrics"]["trades"] for item in items),
                "latest_total": round(sum(item["total"] for item in items), 2),
                "params": model["params"],
                "ic": model["ic"],
            }
        )

    conn = store.connect()
    market = export._index_payload(conn, dates[end])
    candles = export._index_candles(conn, dates[end])
    benchmark_close = panel["benchmark_close"]
    bench_base = float(benchmark_close[start])
    benchmark = [
        {
            "date": dates[i],
            "ret": round((float(benchmark_close[i]) / bench_base - 1) * 100, 2)
            if bench_base
            else None,
        }
        for i in range(start, end + 1)
    ]

    current = sum(p["total"] for p in portfolios)
    initial = sum(CAPITALS) * len(MODELS)
    today_pnl = sum(p["today_pnl"] for p in portfolios)
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

    return {
        "family": "local",
        "label": "自训模型",
        "as_of": dates[end],
        "trade_days": len(portfolios[0]["series"]) if portfolios else 0,
        "dates": [row["date"] for row in portfolios[0]["series"]] if portfolios else [],
        "generated_at": "",
        "summary": {
            "initial": initial,
            "current": round(current, 2),
            "ret_pct": round((current / initial - 1) * 100, 2) if initial else 0,
            "today_pnl": round(today_pnl, 2),
            "today_pct": round(
                today_pnl / (current - today_pnl) * 100 if current - today_pnl else 0, 2
            ),
            "portfolio_count": len(portfolios),
            "model_count": len(MODELS),
        },
        "market": market,
        "breadth": {
            "up": sum(1 for u in universe if u["chg1"] > 0),
            "down": sum(1 for u in universe if u["chg1"] < 0),
            "flat": sum(1 for u in universe if u["chg1"] == 0),
            "total": len(universe),
        },
        "benchmark": benchmark,
        "candles": candles,
        "models": models_payload,
        "portfolios": portfolios,
        "trades": latest_trades,
        "universe": universe,
        "meta": {
            "start": dates[start],
            "end": dates[end],
            "last_signal": dates[last_signal],
            "capital": CAPITALS,
            "capital_labels": {str(k): v for k, v in config.CAPITAL_LABELS.items()},
            "universe_title": "最新排名 · GRU+Attention",
            "method": "5 日调仓 · 653 只沪深300历史成分股 · 低换手规则",
            "fee_note": "自训模型口径：买卖 0.10%/0.20%，每笔最低 5 元，1% 成交额上限",
            "provenance": {
                "train": "2016–2021",
                "validation": "2022–2023",
                "test": "2024–2026-11-09",
                "source": "~/labs/VLA/Share/v2（本地分数矩阵重放）",
                "leakage": "无 LLM 预训练泄漏；但测试区间 2024–2026 已被查看，不是严格前向。",
            },
            "disclaimer": "模拟盘，仅供策略研究，不构成投资建议。自训模型使用本地复现的低换手账户口径。",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Build the self-trained model family")
    parser.add_argument("--out", default=str(config.DATA_DIR / "local_family.json"))
    args = parser.parse_args()
    family = build_family()
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(family, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        "wrote %s: %d portfolios, as_of %s, current %.2f"
        % (path, len(family["portfolios"]), family["as_of"], family["summary"]["current"])
    )


if __name__ == "__main__":
    main()
