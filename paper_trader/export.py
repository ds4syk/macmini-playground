"""Build the static dashboard payload from SQLite."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Dict, List

from . import config, engine, store


def _round(value: float, digits: int = 2) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    return round(float(value), digits)


def _latest_close(conn: sqlite3.Connection, symbol: str, date: str) -> float:
    row = conn.execute(
        "SELECT close FROM bars WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
        (symbol, date),
    ).fetchone()
    return float(row["close"]) if row else 0.0


def _index_payload(conn: sqlite3.Connection, date: str) -> List[dict]:
    out = []
    for item in config.INDICES:
        bars = store.bars_for(conn, item["code"], end_date=date, limit=70)
        if not bars:
            continue
        closes = [float(b["close"]) for b in bars]
        spark = closes[-30:]
        out.append(
            {
                "code": item["code"],
                "name": item["name"],
                "close": _round(closes[-1]),
                "chg1": _round((closes[-1] / closes[-2] - 1) * 100) if len(closes) > 1 else 0,
                "chg5": _round((closes[-1] / closes[-6] - 1) * 100) if len(closes) > 5 else 0,
                "chg20": _round((closes[-1] / closes[-21] - 1) * 100) if len(closes) > 20 else 0,
                "spark": [_round(c) for c in spark],
            }
        )
    return out


def _index_candles(conn: sqlite3.Connection, date: str, limit: int = 60) -> dict:
    bars = store.bars_for(conn, "sh000001", end_date=date, limit=limit)
    return {
        "dates": [b["date"] for b in bars],
        "values": [
            [
                _round(b["open"]),
                _round(b["close"]),
                _round(b["low"]),
                _round(b["high"]),
            ]
            for b in bars
        ],
        "volumes": [int(b["volume"]) for b in bars],
    }


def _benchmark(conn: sqlite3.Connection, dates: List[str]) -> List[dict]:
    if not dates:
        return []
    bars = {
        row["date"]: float(row["close"])
        for row in conn.execute(
            "SELECT date, close FROM bars WHERE symbol = 'sh000300' AND date >= ? AND date <= ?",
            (dates[0], dates[-1]),
        )
    }
    base = bars.get(dates[0])
    if not base:
        return []
    return [
        {"date": day, "ret": _round((bars[day] / base - 1) * 100) if day in bars else None}
        for day in dates
    ]


def _portfolio_payload(
    conn: sqlite3.Connection, row: sqlite3.Row, dates: List[str], model: dict
) -> dict:
    pid = row["id"]
    equity_rows = list(
        conn.execute(
            "SELECT date, cash, market_value, total FROM equity WHERE portfolio_id = ? ORDER BY date",
            (pid,),
        )
    )
    eq_by_date = {r["date"]: r for r in equity_rows}
    series = []
    last_total = float(row["capital"])
    for day in dates:
        item = eq_by_date.get(day)
        if item:
            last_total = float(item["total"])
        series.append(
            {
                "date": day,
                "total": _round(last_total),
                "ret": _round((last_total / float(row["capital"]) - 1) * 100),
            }
        )
    totals = [p["total"] for p in series]
    today_pnl = totals[-1] - totals[-2] if len(totals) > 1 else 0.0
    today_pct = (totals[-1] / totals[-2] - 1) * 100 if len(totals) > 1 and totals[-2] else 0.0

    positions = []
    as_of = dates[-1] if dates else ""
    for pos in store.get_positions(conn, pid):
        price = _latest_close(conn, pos["symbol"], as_of)
        value = int(pos["shares"]) * price
        meta = config.universe_by_symbol().get(pos["symbol"], {})
        positions.append(
            {
                "symbol": pos["symbol"],
                "name": meta.get("name", pos["symbol"]),
                "sector": meta.get("sector", ""),
                "shares": int(pos["shares"]),
                "avg_cost": _round(pos["avg_cost"], 3),
                "price": _round(price, 3),
                "value": _round(value),
                "pnl": _round(value - int(pos["shares"]) * float(pos["avg_cost"])),
                "pnl_pct": _round(
                    (price / float(pos["avg_cost"]) - 1) * 100 if pos["avg_cost"] else 0
                ),
                "weight": _round(value / totals[-1] * 100 if totals[-1] else 0),
            }
        )
    positions.sort(key=lambda p: p["value"], reverse=True)

    trades = []
    for trade in conn.execute(
        "SELECT * FROM trades WHERE portfolio_id = ? ORDER BY date DESC, id DESC LIMIT 40",
        (pid,),
    ):
        meta = config.universe_by_symbol().get(trade["symbol"], {})
        trades.append(
            {
                "date": trade["date"],
                "symbol": trade["symbol"],
                "name": meta.get("name", trade["symbol"]),
                "side": trade["side"],
                "shares": int(trade["shares"]),
                "price": _round(trade["price"], 3),
                "amount": _round(trade["amount"]),
                "fee": _round(trade["fee"]),
                "realized_pnl": _round(trade["realized_pnl"]),
                "reason": trade["reason"] or "",
            }
        )

    dec = store.latest_decision(conn, model["id"])
    decision = None
    if dec:
        targets = []
        for target in json.loads(dec["targets_json"] or "[]"):
            meta = config.universe_by_symbol().get(target.get("symbol"), {})
            targets.append(
                {
                    "symbol": target.get("symbol"),
                    "name": meta.get("name", target.get("symbol")),
                    "weight": _round(target.get("weight", 0) * 100),
                    "reason": target.get("reason", ""),
                }
            )
        decision = {
            "date": dec["date"],
            "market_view": dec["market_view"] or "",
            "confidence": _round(dec["confidence"] or 0),
            "targets": targets,
            "error": dec["error"],
        }

    trade_rows = list(
        conn.execute("SELECT * FROM trades WHERE portfolio_id = ?", (pid,))
    )
    stats = engine.metrics(equity_rows, trade_rows, float(row["capital"]))
    return {
        "id": pid,
        "model_id": model["id"],
        "model_name": model["name"],
        "capital": int(row["capital"]),
        "capital_label": config.CAPITAL_LABELS.get(int(row["capital"]), ""),
        "cash": _round(float(row["cash"])),
        "total": _round(totals[-1]),
        "ret_pct": _round((totals[-1] / float(row["capital"]) - 1) * 100),
        "today_pnl": _round(today_pnl),
        "today_pct": _round(today_pct),
        "positions": positions,
        "trades": trades,
        "decision": decision,
        "metrics": {
            "max_drawdown": _round(stats["max_drawdown"]),
            "sharpe": _round(stats["sharpe"]),
            "volatility": _round(stats["volatility"]),
            "win_rate": _round(stats["win_rate"]),
            "trades": int(stats["trades"]),
            "turnover": _round(stats["turnover"]),
        },
        "series": series,
    }


def build_payload(conn: sqlite3.Connection) -> dict:
    dates = [
        row["date"]
        for row in conn.execute(
            "SELECT DISTINCT date FROM equity WHERE portfolio_id LIKE '%__100000' ORDER BY date"
        )
    ]
    if not dates:
        dates = [
            row["date"]
            for row in conn.execute("SELECT DISTINCT date FROM equity ORDER BY date")
        ]
    as_of = dates[-1] if dates else ""
    models = [dict(m) for m in config.MODELS]
    portfolios = []
    by_model: Dict[str, List[dict]] = {m["id"]: [] for m in models}
    for model in models:
        for capital in config.CAPITALS:
            row = conn.execute(
                "SELECT * FROM portfolios WHERE id = ?",
                (config.portfolio_id(model["id"], capital),),
            ).fetchone()
            if not row:
                continue
            payload = _portfolio_payload(conn, row, dates, model)
            portfolios.append(payload)
            by_model[model["id"]].append(payload)

    model_cards = []
    for model in models:
        items = by_model[model["id"]]
        if not items:
            continue
        returns = [p["ret_pct"] for p in items]
        best = max(items, key=lambda p: p["ret_pct"])
        dec = store.latest_decision(conn, model["id"])
        model_cards.append(
            {
                "id": model["id"],
                "name": model["name"],
                "short": model.get("short", model["name"]),
                "provider": model["provider"],
                "color": model["color"],
                "note": model.get("note", ""),
                "avg_ret": _round(sum(returns) / len(returns)),
                "best_capital": best["capital"],
                "best_ret": best["ret_pct"],
                "market_view": (dec["market_view"] if dec else "") or "",
                "confidence": _round(dec["confidence"] if dec else 0),
                "decision_date": dec["date"] if dec else "",
                "trade_count": sum(p["metrics"]["trades"] for p in items),
                "latest_total": _round(sum(p["total"] for p in items)),
            }
        )

    initial = sum(float(r["capital"]) for r in conn.execute("SELECT capital FROM portfolios"))
    current = sum(p["total"] for p in portfolios)
    today_pnl = sum(p["today_pnl"] for p in portfolios)
    latest_trades = []
    for trade in conn.execute(
        "SELECT t.*, p.model_id, p.capital FROM trades t JOIN portfolios p ON p.id = t.portfolio_id ORDER BY t.date DESC, t.id DESC LIMIT 30"
    ):
        meta = config.universe_by_symbol().get(trade["symbol"], {})
        latest_trades.append(
            {
                "date": trade["date"],
                "model_id": trade["model_id"],
                "capital": int(trade["capital"]),
                "symbol": trade["symbol"],
                "name": meta.get("name", trade["symbol"]),
                "side": trade["side"],
                "shares": int(trade["shares"]),
                "price": _round(trade["price"], 3),
                "amount": _round(trade["amount"]),
                "reason": trade["reason"] or "",
            }
        )

    universe = []
    snap_rows = []
    for item in config.UNIVERSE:
        bars = store.bars_for(conn, item["code"], end_date=as_of, limit=70)
        if not bars:
            continue
        closes = [float(b["close"]) for b in bars]
        chg1 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else 0
        chg5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 else 0
        chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 20 else 0
        spark = closes[-20:]
        universe.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "sector": item["sector"],
                "close": _round(closes[-1]),
                "chg1": _round(chg1),
                "chg5": _round(chg5),
                "chg20": _round(chg20),
                "spark": [_round(c) for c in spark],
            }
        )
        snap_rows.append(chg1)
    breadth = {
        "up": sum(1 for c in snap_rows if c > 0),
        "down": sum(1 for c in snap_rows if c < 0),
        "flat": sum(1 for c in snap_rows if c == 0),
        "total": len(snap_rows),
    }

    return {
        "family": "llm",
        "label": "LLM 模型（保留）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": as_of,
        "trade_days": len(dates),
        "dates": dates,
        "summary": {
            "initial": _round(initial),
            "current": _round(current),
            "ret_pct": _round((current / initial - 1) * 100 if initial else 0),
            "today_pnl": _round(today_pnl),
            "today_pct": _round(today_pnl / (current - today_pnl) * 100 if current - today_pnl else 0),
            "portfolio_count": len(portfolios),
            "model_count": len(models),
        },
        "market": _index_payload(conn, as_of),
        "breadth": breadth,
        "benchmark": _benchmark(conn, dates),
        "candles": _index_candles(conn, as_of),
        "models": model_cards,
        "portfolios": portfolios,
        "trades": latest_trades,
        "universe": universe,
        "meta": {
            "start": dates[0] if dates else "",
            "end": as_of,
            "capital": config.CAPITALS,
            "capital_labels": {str(k): v for k, v in config.CAPITAL_LABELS.items()},
            "universe_title": "候选池行情",
            "method": "日线 · 30 只样本池 · 收盘决策次日开盘成交",
            "fee_note": "LLM 口径：佣金万 2.5（最低 5 元）+ 卖出印花税万 5 + 过户费万 0.1",
            "provenance": {
                "train": "商用 LLM（预训练截止未知）",
                "validation": "—",
                "test": "回放 2026-09-01 至 2026-11-11",
                "leakage": "商用 LLM 的预训练知识可能覆盖回放区间，存在测试集泄漏；仅用于链路演示，不能当作干净的超额收益证据。",
            },
            "disclaimer": "模拟盘，仅供策略研究，不构成投资建议。数据来自腾讯行情，按前复权日线回放。",
        },
    }


def write_export(conn: sqlite3.Connection, path) -> dict:
    from datetime import datetime as _dt

    llm = build_payload(conn)
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    llm["generated_at"] = now
    families = {"llm": llm}
    local_path = config.DATA_DIR / "local_family.json"
    if local_path.exists():
        local = json.loads(local_path.read_text(encoding="utf-8"))
        local["generated_at"] = now
        families = {"local": local, "llm": llm}
    payload = {
        "generated_at": now,
        "default_family": "local" if "local" in families else "llm",
        "families": families,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return payload
