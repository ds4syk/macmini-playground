"""Hong Kong portfolio backtest and dashboard payload builder.

The trading rules mirror the A-share low-turnover account: hold about 30 names,
sell only when a name falls outside the top 50, replace at most 5 names every
5 trading days, execute at the next session's open, and cap each order at 1% of
the previous session's turnover.  Hong Kong specifics are board lots from the
HKEX security master, no daily price limit, and the HSBC Trade25 cost schedule
in :mod:`experiments.hk_models.fees`.

Outputs two dashboard families:

* ``hk``      -- 5 models x 4 capital buckets over the recent window;
* ``hk-eval`` -- 5 models at HKD 1m over the 2024-01-02 -> 2026-11-25 test
  segment plus the model-evaluation table.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import fees

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
DATA = Path(os.environ.get("HK_DATA_DIR", ROOT / "data" / "hk"))

MODELS = [
    {
        "id": "hk-attn-gru",
        "route": "attn_gru",
        "name": "GRU + Attention",
        "short": "GRU+Attn",
        "color": "#0a9b5a",
        "note": "25 维日线特征 · 60 日窗口 · 5 日截面排序",
    },
    {
        "id": "hk-tcn",
        "route": "tcn",
        "name": "TCN",
        "short": "TCN",
        "color": "#1f6feb",
        "note": "4 个因果残差块 · 感受野 61 日",
    },
    {
        "id": "hk-transformer",
        "route": "transformer",
        "name": "Transformer",
        "short": "Transformer",
        "color": "#7b2ff2",
        "note": "2 层 4 头自注意力 · 均值汇聚",
    },
    {
        "id": "hk-cnn",
        "route": "cnn",
        "name": "CNN",
        "short": "CNN",
        "color": "#f08c00",
        "note": "3 层一维卷积 · 全局平均池化",
    },
]


def log(message: str) -> None:
    print(message, flush=True)


def model_by_route(route: str) -> dict:
    return next(model for model in MODELS if model["route"] == route)


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def _signal_days(score: np.ndarray, start: int, end: int) -> list[int]:
    return [day for day in range(start, end + 1) if np.isfinite(score[:, day]).any()]


def backtest(panel: dict, metadata: dict, score: np.ndarray, capital: float,
             start: int, end: int, config: dict = CONFIG,
             cost_multiplier: float = 1.0) -> dict:
    dates = metadata["dates"]
    codes = metadata["stocks"]
    lots = metadata["lots"]
    count = len(codes)
    units = np.zeros(count, dtype=np.int64)
    cost_basis = np.zeros(count, dtype=float)
    cash = float(capital)
    trades: list[dict] = []
    rows: list[dict] = []
    signals = _signal_days(score, start, end)
    anchors = set(signals[:: int(config["rebalance_days"])])
    month = dates[start][:7]
    month_turnover = 0.0
    month_active = False
    total_fees = 0.0
    total_traded = 0.0

    for day in range(start, end + 1):
        row_month = dates[day][:7]
        if row_month != month:
            if month_active:
                cash -= float(config["monthly_fee"]) * cost_multiplier
                total_fees += float(config["monthly_fee"]) * cost_multiplier
            month = row_month
            month_turnover = 0.0
            month_active = False

        raw_open = panel["raw_open"][:, day].astype(float)
        raw_close = panel["raw_close"][:, day].astype(float)
        preclose = panel["preclose"][:, day].astype(float)
        valuation_open = np.where(raw_open > 0, raw_open, preclose)
        equity_open = cash + float(np.dot(units, valuation_open))
        tradable = (panel["tradable"][:, day] > 0) & (raw_open > 0)
        previous_amount = panel["amount"][:, day - 1].astype(float) if day > start else np.zeros(count)
        traded_today = 0.0
        fees_today = 0.0

        target_units = units.copy()
        if day > start and (day - 1) in anchors:
            stocks = np.flatnonzero(np.isfinite(score[:, day - 1]))
            values = score[stocks, day - 1]
            ranked = stocks[np.argsort(-values, kind="stable")]
            held = np.flatnonzero(units > 0)
            held_set = set(int(index) for index in held)
            position = {int(index): rank + 1 for rank, index in enumerate(ranked)}
            sell_list = [
                int(index) for index in held
                if position.get(int(index), 10 ** 9) > int(config["buffer_size"])
            ]
            sell_list.sort(key=lambda index: position.get(index, 10 ** 9), reverse=True)
            sell_list = sell_list[: int(config["max_replace"])]
            sell_set = set(sell_list)
            keep = [index for index in held_set if index not in sell_set]
            pool = [int(index) for index in ranked if int(index) not in held_set]
            if len(held) == 0:
                capacity = int(config["portfolio_size"])
            else:
                gaps = max(0, int(config["portfolio_size"]) - len(held))
                capacity = max(0, min(int(config["portfolio_size"]) - len(keep),
                                     int(config["max_replace"]) + gaps))
            purchases = pool[:capacity]
            target_units = units.copy()
            target_units[sell_list] = 0
            budget = equity_open / int(config["portfolio_size"])
            for index in purchases:
                target_units[index] = fees.target_shares(
                    budget, raw_open[index], fees.lot_size(codes[index], lots))

        # sells first
        sell_request = np.maximum(units - target_units, 0)
        for index in np.flatnonzero(sell_request > 0):
            if not tradable[index]:
                continue
            lot = fees.lot_size(codes[index], lots)
            price = float(raw_open[index])
            liquidity = int(previous_amount[index] * float(config["liquidity_cap"]) // (price * lot)) * lot
            shares = min(int(sell_request[index]), liquidity)
            shares = int(shares // lot) * lot
            if shares <= 0:
                continue
            notional = shares * price
            cost = fees.total_order_cost(notional, month_turnover, config, cost_multiplier)
            avg = cost_basis[index] / max(int(units[index]), 1)
            realized = (price - avg) * shares - cost["total"]
            cash += notional - cost["total"]
            units[index] -= shares
            cost_basis[index] -= avg * shares
            if units[index] <= 0:
                cost_basis[index] = 0.0
            month_turnover += notional
            total_traded += notional
            total_fees += cost["total"]
            fees_today += cost["total"]
            traded_today += notional
            month_active = True
            trades.append({
                "date": dates[day], "code": codes[index],
                "name": metadata["names"].get(codes[index], codes[index]),
                "side": "sell", "shares": int(shares), "price": price,
                "amount": notional, "fee": cost["total"], "realized_pnl": realized,
                "reason": "跌出前 50 名 / 调仓卖出",
            })

        # buys
        if day > start and (day - 1) in anchors:
            buy_request = np.maximum(target_units - units, 0)
            for index in np.flatnonzero(buy_request > 0):
                if not tradable[index]:
                    continue
                lot = fees.lot_size(codes[index], lots)
                price = float(raw_open[index])
                if price <= 0:
                    continue
                liquidity = int(previous_amount[index] * float(config["liquidity_cap"]) // (price * lot)) * lot
                shares = min(int(buy_request[index]), int(cash // (price * lot)) * lot, liquidity)
                while shares > 0:
                    notional = shares * price
                    cost = fees.total_order_cost(notional, month_turnover, config, cost_multiplier)
                    if notional + cost["total"] <= cash + 1e-9:
                        break
                    shares -= lot
                if shares <= 0:
                    continue
                notional = shares * price
                cost = fees.total_order_cost(notional, month_turnover, config, cost_multiplier)
                cash -= notional + cost["total"]
                cost_basis[index] += notional + cost["total"]
                units[index] += shares
                month_turnover += notional
                total_traded += notional
                total_fees += cost["total"]
                fees_today += cost["total"]
                traded_today += notional
                month_active = True
                trades.append({
                    "date": dates[day], "code": codes[index],
                    "name": metadata["names"].get(codes[index], codes[index]),
                    "side": "buy", "shares": int(shares), "price": price,
                    "amount": notional, "fee": cost["total"], "realized_pnl": 0.0,
                    "reason": "截面排名买入",
                })

        equity = cash + float(np.dot(units, raw_close))
        minimum_cash = -float(config["monthly_fee"]) * cost_multiplier - 1e-6
        if cash < minimum_cash or np.any(units < 0) or not math.isfinite(equity) or equity <= 0:
            raise RuntimeError(f"account invariant failed on {dates[day]}")
        if units.any() or traded_today > 0:
            month_active = True
        rows.append({
            "date": dates[day], "cash": cash, "equity": equity,
            "traded": traded_today, "fees": fees_today,
            "holdings": int((units > 0).sum()),
        })

    if month_active:
        cash -= float(config["monthly_fee"]) * cost_multiplier
        total_fees += float(config["monthly_fee"]) * cost_multiplier
        rows[-1]["cash"] = cash
        rows[-1]["equity"] = cash + float(np.dot(units, panel["raw_close"][:, end]))
        rows[-1]["fees"] += float(config["monthly_fee"]) * cost_multiplier

    return {
        "rows": rows, "units": units, "cost_basis": cost_basis,
        "trades": trades, "total_fees": total_fees, "total_traded": total_traded,
        "capital": capital,
    }


def metrics(rows: list[dict], trades: list[dict], capital: float) -> dict:
    equity = np.array([row["equity"] for row in rows], dtype=float)
    if len(equity) < 2:
        return {"ret_pct": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0,
                "sharpe": 0.0, "volatility": 0.0, "turnover": 0.0,
                "annualized_turnover": 0.0, "annualized_cost": 0.0,
                "win_rate": 0.0, "trades": len(trades), "average_holdings": 0.0}
    daily = np.diff(np.r_[capital, equity]) / np.r_[capital, equity[:-1]]
    peak = np.maximum.accumulate(np.r_[capital, equity])[1:]
    drawdown = equity / peak - 1
    sells = [trade for trade in trades if trade["side"] == "sell"]
    wins = sum(1 for trade in sells if trade["realized_pnl"] > 0)
    traded = sum(trade["amount"] for trade in trades)
    costs = sum(trade["fee"] for trade in trades)
    years = len(equity) / 252.0
    return {
        "ret_pct": float((equity[-1] / capital - 1) * 100),
        "annualized_return": float(((equity[-1] / capital) ** (1 / max(years, 1e-9)) - 1) * 100),
        "max_drawdown": float(drawdown.min() * 100),
        "sharpe": float(np.mean(daily) / max(np.std(daily, ddof=1), 1e-12) * np.sqrt(252)),
        "volatility": float(np.std(daily, ddof=1) * np.sqrt(252) * 100),
        "turnover": float(traded / capital),
        "annualized_turnover": float(traded / capital / max(years, 1e-9)),
        "annualized_cost": float(costs / capital / max(years, 1e-9) * 100),
        "win_rate": float(wins / len(sells) * 100) if sells else 0.0,
        "trades": int(len(trades)),
        "average_holdings": float(np.mean([row["holdings"] for row in rows])),
    }


# ---------------------------------------------------------------------------
# benchmarks and evaluation
# ---------------------------------------------------------------------------


def index_series() -> dict:
    frame = pd.read_csv(DATA / "indices.csv", dtype={"date": str})
    out = {}
    for code, group in frame.groupby("code"):
        out[code] = group.set_index("date").sort_index()
    return out


def equal_weight_curve(panel: dict, metadata: dict, start: int, end: int) -> pd.Series:
    close = panel["raw_close"][:, start:end + 1].astype(float)
    tradable = panel["tradable"][:, start:end + 1] > 0
    previous = close[:, :-1]
    current = close[:, 1:]
    valid = (previous > 0) & (current > 0) & tradable[:, :-1] & tradable[:, 1:]
    returns = np.where(valid, current / np.where(previous > 0, previous, np.nan) - 1, np.nan)
    # A suspended stock is not a -100% return; only names trading on both
    # sessions enter the equal-weight average.
    daily = np.nanmean(returns, axis=0)
    daily = np.nan_to_num(daily, nan=0.0)
    curve = np.cumprod(1.0 + daily)
    return pd.Series(np.r_[1.0, curve], index=metadata["dates"][start:end + 1])


def curve_metrics(values: np.ndarray) -> dict:
    values = np.asarray(values, dtype=float)
    daily = np.diff(values) / values[:-1]
    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1
    years = len(values) / 252.0
    return {
        "ret_pct": float((values[-1] - 1) * 100),
        "annualized_return": float((values[-1] ** (1 / max(years, 1e-9)) - 1) * 100),
        "max_drawdown": float(drawdown.min() * 100),
        "sharpe": float(np.mean(daily) / max(np.std(daily, ddof=1), 1e-12) * np.sqrt(252)),
    }


def top30_spread(score: np.ndarray, panel: dict, metadata: dict, signal_dates: list[int],
                 config: dict = CONFIG) -> dict:
    spreads = []
    for day in signal_dates[:: int(config["rebalance_days"])]:
        stocks = np.flatnonzero(np.isfinite(score[:, day]) & np.isfinite(panel["returns"][:, day]))
        if len(stocks) < int(config["portfolio_size"]):
            continue
        values = score[stocks, day]
        returns = panel["returns"][stocks, day]
        order = np.argsort(-values, kind="stable")[: int(config["portfolio_size"])]
        spreads.append(float(returns[order].mean() - returns.mean()))
    if not spreads:
        return {"mean": None, "t": None}
    values = np.asarray(spreads)
    return {
        "mean": float(values.mean()),
        "t": float(values.mean() / values.std(ddof=1) * np.sqrt(len(values)))
        if values.std(ddof=1) > 0 else None,
    }


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def _money(value: float) -> float:
    return round(float(value), 2)


def _json_safe(value):
    """Replace non-finite floats before writing JSON (NaN is not valid JSON)."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else 0.0
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _portfolio(model: dict, capital: int, result: dict, metadata: dict, panel: dict,
               start: int, end: int, signal_day: int, display_start: int) -> dict:
    rows = result["rows"]
    equity = np.array([row["equity"] for row in rows], dtype=float)
    display_rows = [row for row in rows if row["date"] >= metadata["dates"][display_start]]
    display_equity = np.array([row["equity"] for row in display_rows], dtype=float)
    trades = [trade for trade in result["trades"] if trade["date"] >= metadata["dates"][start]]
    today_pnl = equity[-1] - equity[-2] if len(equity) > 1 else 0.0
    today_pct = (equity[-1] / equity[-2] - 1) * 100 if len(equity) > 1 and equity[-2] else 0.0
    positions = []
    raw_close = panel["raw_close"][:, end].astype(float)
    for index in np.flatnonzero(result["units"] > 0):
        shares = int(result["units"][index])
        price = float(raw_close[index])
        avg = float(result["cost_basis"][index]) / max(shares, 1)
        positions.append({
            "symbol": metadata["stocks"][index],
            "name": metadata["names"].get(metadata["stocks"][index], metadata["stocks"][index]),
            "sector": "港股主板",
            "shares": shares,
            "avg_cost": round(avg, 3),
            "price": round(price, 3),
            "value": _money(shares * price),
            "pnl": _money((price - avg) * shares),
            "pnl_pct": round((price / avg - 1) * 100, 2) if avg else 0.0,
            "weight": round(shares * price / equity[-1] * 100, 2) if equity[-1] else 0.0,
        })
    positions.sort(key=lambda item: item["value"], reverse=True)
    recent = sorted(trades, key=lambda trade: (trade["date"], trade["code"]), reverse=True)[:24]
    trade_rows = [{
        "date": trade["date"], "symbol": trade["code"], "name": trade["name"],
        "side": trade["side"], "shares": trade["shares"], "price": round(trade["price"], 3),
        "amount": _money(trade["amount"]), "fee": _money(trade["fee"]),
        "realized_pnl": _money(trade["realized_pnl"]), "reason": trade["reason"],
    } for trade in recent]
    return {
        "id": f"{model['id']}__{capital}",
        "model_id": model["id"],
        "model_name": model["name"],
        "capital": int(capital),
        "capital_label": CONFIG["capital_labels"][str(capital)],
        "cash": _money(rows[-1]["cash"]),
        "total": _money(equity[-1]),
        "ret_pct": round((equity[-1] / capital - 1) * 100, 2),
        "today_pnl": _money(today_pnl),
        "today_pct": round(today_pct, 2),
        "positions": positions,
        "trades": trade_rows,
        "decision": None,
        "metrics": {key: round(value, 2) for key, value in metrics(display_rows, trades, capital).items()},
        "series": [
            {"date": row["date"], "total": _money(row["equity"]),
             "ret": round((row["equity"] / capital - 1) * 100, 2)}
            for row in display_rows
        ],
    }


def _decision(score: np.ndarray, metadata: dict, signal_day: int, model: dict) -> dict:
    stocks = np.flatnonzero(np.isfinite(score[:, signal_day]))
    values = score[stocks, signal_day]
    order = stocks[np.argsort(-values, kind="stable")][:8]
    return {
        "date": metadata["dates"][signal_day],
        "market_view": "港股 5 日截面排序前 30 名；低换手缓冲 50，最多换 5 只；最后一次信号 %s。"
                       % metadata["dates"][signal_day],
        "confidence": 0.0,
        "targets": [
            {"symbol": metadata["stocks"][index],
             "name": metadata["names"].get(metadata["stocks"][index], metadata["stocks"][index]),
             "weight": round(100 / int(CONFIG["portfolio_size"]), 1),
             "reason": "截面排名前 30"}
            for index in order
        ],
        "error": None,
    }


def _market(indices: dict, dates: list[str]) -> list[dict]:
    out = []
    names = {"HSI": "恒生指数", "HSCEI": "国企指数", "HSTECH": "恒生科技"}
    for code in ("HSI", "HSCEI", "HSTECH"):
        frame = indices.get(code)
        if frame is None:
            continue
        frame = frame[frame.index <= dates[-1]].tail(70)
        if not len(frame):
            continue
        closes = frame.raw_close.astype(float).tolist()
        spark = closes[-30:]
        out.append({
            "code": code, "name": names[code],
            "close": round(closes[-1], 2),
            "chg1": round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) > 1 else 0.0,
            "chg5": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) > 5 else 0.0,
            "chg20": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 20 else 0.0,
            "spark": [round(value, 2) for value in spark],
        })
    return out


def _candles(indices: dict, dates: list[str]) -> dict:
    frame = indices["HSI"]
    frame = frame[frame.index <= dates[-1]].tail(60)
    return {
        "dates": list(frame.index),
        "values": [[round(row.raw_open, 2), round(row.raw_close, 2),
                    round(row.raw_low, 2), round(row.raw_high, 2)] for row in frame.itertuples()],
        "volumes": [float(row.raw_volume) for row in frame.itertuples()],
    }


def _universe(score: np.ndarray, metadata: dict, panel: dict, indices: dict, day: int) -> list[dict]:
    stocks = np.flatnonzero(np.isfinite(score[:, day]))
    values = score[stocks, day]
    order = stocks[np.argsort(-values, kind="stable")][:30]
    out = []
    for index in order:
        closes = panel["raw_close"][index, max(0, day - 20):day + 1].astype(float)
        closes = closes[closes > 0]
        if not len(closes):
            continue
        out.append({
            "symbol": metadata["stocks"][index],
            "name": metadata["names"].get(metadata["stocks"][index], metadata["stocks"][index]),
            "sector": "港股主板",
            "close": round(float(closes[-1]), 3),
            "chg1": round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) > 1 else 0.0,
            "chg5": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) > 5 else 0.0,
            "chg20": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 20 else 0.0,
            "spark": [round(float(value), 2) for value in closes[-20:]],
            "score": round(float(score[index, day]), 3),
        })
    return out


def _mean_seed_ic(entry: dict) -> float:
    values = [seed.get("ic", {}).get("mean_ic") or 0.0
              for seed in entry.get("seeds", {}).values()]
    return sum(values) / len(values) if values else 0.0


def build_family(kind: str, panel: dict, metadata: dict, results: dict,
                 metrics_json: dict) -> dict:
    dates = metadata["dates"]
    indices = index_series()
    if kind == "hk":
        capital_list = CONFIG["capitals"]
        start = int(np.searchsorted(dates, "2026-08-25"))
        display_start = start
        label = "港股 · 自训模型"
        method = "港股主板 285 只 · 5 日调仓 · 低换手规则 · 汇丰 Trade25 费用"
    else:
        capital_list = [1_000_000]
        start = int(np.searchsorted(dates, CONFIG["eval_start"]))
        display_start = start
        label = "港股 · 模型评测"
        method = "港股主板 285 只 · 2024-01-02 起测试段 · 5 日调仓 · 低换手规则"
    end = len(dates) - 1
    portfolios = []
    model_cards = []
    latest_signal = max(
        [day for day in range(start, end) if np.isfinite(panel["scores"]["attn_gru"][:, day]).any()],
        default=end - 6,
    )
    for model in MODELS:
        score = panel["scores"][model["route"]]
        items = []
        for capital in capital_list:
            result = results[(model["route"], capital)]
            item = _portfolio(model, capital, result, metadata, panel, start, end,
                              latest_signal, display_start)
            item["decision"] = _decision(score, metadata, latest_signal, model)
            items.append(item)
        portfolios.extend(items)
        best = max(items, key=lambda item: item["ret_pct"])
        ensemble_ic = metrics_json[model["route"]]["ensemble"]["ic"]
        model_cards.append({
            "id": model["id"], "name": model["name"], "short": model["short"],
            "provider": "自训", "color": model["color"], "note": model["note"],
            "avg_ret": round(sum(item["ret_pct"] for item in items) / len(items), 2),
            "best_capital": best["capital"], "best_ret": best["ret_pct"],
            "market_view": "种子均值 IC %.3f（集成 %.3f）· 5 日截面排序"
                           % (_mean_seed_ic(metrics_json[model["route"]]),
                              ensemble_ic.get("mean_ic") or 0),
            "confidence": 0, "decision_date": dates[latest_signal],
            "trade_count": sum(item["metrics"]["trades"] for item in items),
            "latest_total": round(sum(item["total"] for item in items), 2),
            "params": metrics_json[model["route"]]["parameters"],
            "ic": round(ensemble_ic.get("mean_ic") or 0, 4),
        })
    dates_slice = dates[display_start:]
    initial = sum(item["capital"] for item in portfolios)
    current = sum(item["total"] for item in portfolios)
    today_pnl = sum(item["today_pnl"] for item in portfolios)
    hsi = indices["HSI"]
    hsi_close = hsi.raw_close.astype(float)
    base = float(hsi_close.reindex(dates_slice).dropna().iloc[0])
    benchmark = [
        {"date": day, "ret": round((float(hsi_close.get(day, np.nan)) / base - 1) * 100, 2)}
        if day in hsi_close.index and np.isfinite(hsi_close.get(day, np.nan)) else
        {"date": day, "ret": None}
        for day in dates_slice
    ]
    universe = _universe(panel["scores"]["attn_gru"], metadata, panel, indices, latest_signal)
    breadth_values = [item["chg1"] for item in universe]
    payload = {
        "family": kind,
        "label": label,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": dates[-1],
        "trade_days": len(dates_slice),
        "dates": dates_slice,
        "summary": {
            "initial": round(initial, 2), "current": round(current, 2),
            "ret_pct": round((current / initial - 1) * 100, 2) if initial else 0.0,
            "today_pnl": round(today_pnl, 2),
            "today_pct": round(today_pnl / (current - today_pnl) * 100, 2) if current != today_pnl else 0.0,
            "portfolio_count": len(portfolios), "model_count": len(model_cards),
        },
        "market": _market(indices, dates_slice),
        "breadth": {
            "up": sum(1 for value in breadth_values if value > 0),
            "down": sum(1 for value in breadth_values if value < 0),
            "flat": sum(1 for value in breadth_values if value == 0),
            "total": len(breadth_values),
        },
        "benchmark": benchmark,
        "candles": _candles(indices, dates_slice),
        "models": model_cards,
        "portfolios": portfolios,
        "trades": [],
        "universe": universe,
        "meta": {
            "start": dates_slice[0], "end": dates[-1],
            "capital": capital_list,
            "capital_labels": {str(capital): CONFIG["capital_labels"][str(capital)] for capital in capital_list},
            "currency": "HKD", "currency_symbol": "HK$",
            "universe_title": "最新排名 · GRU+Attention",
            "benchmark_label": "恒生指数",
            "method": method,
            "fee_note": "汇丰 Trade25：月成交 ≤25万港币 0 佣金，超出 0.25%（最低 100 港币/单）+ 印花税 0.1% + 交易费/征费/结算费",
            "provenance": {
                "train": "2016–2021", "validation": "2022–2023",
                "test": "2024-01-02–2026-11-25",
                "source": "腾讯行情（港股复权/不复权日线）+ HKEX 证券名单",
                "leakage": "测试区间已被查看，不是严格前向证据；股票池为当前港交所主板名单，存在幸存者偏差。",
            },
            "leakage": "测试段已查看 + 当前名单幸存者偏差；仅作研究对照。",
            "disclaimer": "模拟盘，仅供策略研究，不构成投资建议。港股按港交所整手、T+2 交收制度近似。",
            "last_signal": dates[latest_signal],
            "decision_label": "最后一次信号",
            "status_label": "港股 · 5 日调仓",
            "metric_label": "Rank IC",
            "chart_method": "5 日调仓",
            "kpi_unit": "5 日调仓",
            "footer_note": "行情为腾讯行情港股日线，成交按下一交易日开盘价回放，已按港交所整手与汇丰 Trade25 口径计费。",
            "test_start": CONFIG["eval_start"],
        },
    }
    latest_trades = []
    for model in MODELS:
        for item in portfolios:
            if item["model_id"] != model["id"]:
                continue
            for trade in results[(model["route"], item["capital"])]["trades"]:
                if trade["date"] >= dates_slice[0]:
                    latest_trades.append({
                        "date": trade["date"], "model_id": model["id"],
                        "capital": item["capital"], "symbol": trade["code"],
                        "name": trade["name"], "side": trade["side"],
                        "shares": trade["shares"], "price": round(trade["price"], 3),
                        "amount": _money(trade["amount"]), "reason": trade["reason"],
                    })
    latest_trades.sort(key=lambda row: (row["date"], row["symbol"]), reverse=True)
    payload["trades"] = latest_trades[:30]
    if kind == "hk-eval":
        payload["eval"] = build_eval(payload, panel, metadata, results, metrics_json, indices)
    return payload


def build_eval(payload: dict, panel: dict, metadata: dict, results: dict,
               metrics_json: dict, indices: dict) -> dict:
    start = int(np.searchsorted(metadata["dates"], CONFIG["eval_start"]))
    end = len(metadata["dates"]) - 1
    signal_dates = [day for day in range(start, end) if np.isfinite(panel["scores"]["attn_gru"][:, day]).any()]
    rows = []
    for model in MODELS:
        item = next(p for p in payload["portfolios"] if p["model_id"] == model["id"])
        ic = metrics_json[model["route"]]["ensemble"]["ic"]
        spread = metrics_json[model["route"]]["ensemble"].get("decile_spread", {})
        top = top30_spread(panel["scores"][model["route"]], panel, metadata, signal_dates)
        rows.append({
            "key": model["id"], "name": model["name"], "type": "model",
            "mean_ic": round(ic.get("mean_ic") or 0, 4),
            "icir": round(ic.get("icir") or 0, 3),
            "decile": round(spread.get("mean") or 0, 4),
            "top30": round(top.get("mean") or 0, 4),
            "ret_pct": item["ret_pct"], "annualized": item["metrics"]["annualized_return"],
            "max_drawdown": item["metrics"]["max_drawdown"],
            "sharpe": item["metrics"]["sharpe"],
            "annualized_turnover": item["metrics"]["annualized_turnover"],
            "annualized_cost": item["metrics"]["annualized_cost"],
            "params": metrics_json[model["route"]]["parameters"],
        })
    equal = equal_weight_curve(panel, metadata, start, end)
    equal_stats = curve_metrics(equal.to_numpy())
    rows.append({
        "key": "equal_weight", "name": "股票池等权", "type": "benchmark",
        "mean_ic": None, "icir": None, "decile": None, "top30": None,
        "ret_pct": round(equal_stats["ret_pct"], 2),
        "annualized": round(equal_stats["annualized_return"], 2),
        "max_drawdown": round(equal_stats["max_drawdown"], 2),
        "sharpe": round(equal_stats["sharpe"], 2),
        "annualized_turnover": 0.0, "annualized_cost": 0.0, "params": 0,
    })
    hsi = indices["HSI"].raw_close.astype(float).reindex(metadata["dates"][start:end + 1]).ffill()
    hsi_stats = curve_metrics((hsi / hsi.iloc[0]).to_numpy())
    rows.append({
        "key": "hsi", "name": "恒生指数", "type": "benchmark",
        "mean_ic": None, "icir": None, "decile": None, "top30": None,
        "ret_pct": round(hsi_stats["ret_pct"], 2),
        "annualized": round(hsi_stats["annualized_return"], 2),
        "max_drawdown": round(hsi_stats["max_drawdown"], 2),
        "sharpe": round(hsi_stats["sharpe"], 2),
        "annualized_turnover": 0.0, "annualized_cost": 0.0, "params": 0,
    })
    sensitivity = []
    for multiplier, label in ((0.0, "0× 费用"), (1.0, "1× 费用"), (2.0, "2× 费用")):
        if multiplier == 1.0:
            result = results.get(("attn_gru", 1_000_000, "eval"))
        else:
            result = results.get(("attn_gru", 1_000_000, multiplier))
        if result is None:
            continue
        stats = metrics(result["rows"], result["trades"], 1_000_000)
        sensitivity.append({
            "label": label, "ret_pct": round(stats["ret_pct"], 2),
            "annualized": round(stats["annualized_return"], 2),
            "max_drawdown": round(stats["max_drawdown"], 2),
            "sharpe": round(stats["sharpe"], 2),
            "annualized_cost": round(stats["annualized_cost"], 2),
        })
    return {
        "window": [metadata["dates"][start], metadata["dates"][end]],
        "label": "港股测试段 2024-01-02 → 2026-11-25",
        "rows": rows,
        "cost_sensitivity": sensitivity,
        "notes": [
            "训练 2016–2021、验证 2022–2023、测试 2024–2026-11-25；测试段未参与梯度更新与早停。",
            "股票池为当前港交所主板名单，退市/被剔除股票不在池内，存在幸存者偏差。",
            "港股按整手成交，费用按汇丰 Trade25 口径近似；无涨跌停限制，T+2 交收按日线近似。",
            "IC 为 5 日收益的日频截面 Spearman 相关；top30 为前 30 名等权相对全池的 5 日超额。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="HK backtest and dashboard payload")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    panel = dict(np.load(DATA / "panel.npz", allow_pickle=False))
    metadata = json.loads((DATA / "metadata.json").read_text())
    scores = {}
    for model in MODELS:
        path = DATA / "artifacts" / "scores" / f"{model['route']}_ensemble.npz"
        scores[model["route"]] = np.load(path)["score"]
    panel["scores"] = scores
    metrics_json = json.loads((DATA / "artifacts" / "metrics.json").read_text())
    dates = metadata["dates"]
    end = len(dates) - 1
    results = {}
    for model in MODELS:
        for capital in CONFIG["capitals"]:
            results[(model["route"], capital)] = backtest(
                panel, metadata, scores[model["route"]], capital,
                int(np.searchsorted(dates, "2026-08-25")), end)
        results[(model["route"], 1_000_000, 1.0)] = results[(model["route"], 1_000_000)]
    # full test segment for the evaluation tab and cost sensitivity
    for model in MODELS:
        results[(model["route"], 1_000_000, "eval")] = backtest(
            panel, metadata, scores[model["route"]], 1_000_000,
            int(np.searchsorted(dates, CONFIG["eval_start"])), end)
    for multiplier in (0.0, 2.0):
        results[("attn_gru", 1_000_000, multiplier)] = backtest(
            panel, metadata, scores["attn_gru"], 1_000_000,
            int(np.searchsorted(dates, CONFIG["eval_start"])), end,
            cost_multiplier=multiplier)
    output = Path(args.output) if args.output else ROOT / "data"
    hk = build_family("hk", panel, metadata, results, metrics_json)
    # The eval family needs full-period portfolios; swap the result source.
    eval_results = dict(results)
    for model in MODELS:
        for capital in CONFIG["capitals"]:
            eval_results[(model["route"], capital)] = results[(model["route"], 1_000_000, "eval")] if capital == 1_000_000 else results[(model["route"], capital)]
    eval_family = build_family("hk-eval", panel, metadata, eval_results, metrics_json)
    (output / "hk_family.json").write_text(
        json.dumps(_json_safe(hk), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (output / "hk_eval_family.json").write_text(
        json.dumps(_json_safe(eval_family), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {output / 'hk_family.json'} and {output / 'hk_eval_family.json'}")
    log(json.dumps({"hk": hk["summary"], "eval": eval_family["summary"]}, indent=2))


if __name__ == "__main__":
    main()
