"""Paper-trading execution, valuation and performance metrics."""
from __future__ import annotations

import math
import sqlite3
from typing import Dict, List, Optional

from . import config, store


def fees(side: str, amount: float) -> float:
    commission = max(config.COMMISSION_MIN, amount * config.COMMISSION_RATE)
    stamp = amount * config.STAMP_DUTY_RATE if side == "sell" else 0.0
    transfer = amount * config.TRANSFER_FEE_RATE
    return round(commission + stamp + transfer, 2)


def _prev_close(conn: sqlite3.Connection, symbol: str, date: str) -> Optional[float]:
    row = conn.execute(
        "SELECT close FROM bars WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (symbol, date),
    ).fetchone()
    return float(row["close"]) if row else None


def _prices(conn: sqlite3.Connection, date: str, symbols: List[str], field: str = "open") -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not symbols:
        return out
    for symbol in symbols:
        row = conn.execute(
            "SELECT %s AS p FROM bars WHERE symbol = ? AND date = ?" % field,
            (symbol, date),
        ).fetchone()
        if row and row["p"]:
            out[symbol] = float(row["p"])
    return out


def execute_targets(
    conn: sqlite3.Connection,
    portfolio: sqlite3.Row,
    model: Dict[str, str],
    date: str,
    targets: List[Dict],
    reason: str = "",
) -> Dict:
    """Rebalance one portfolio to the model's target weights at the day's open."""
    portfolio_id = portfolio["id"]
    cash = float(portfolio["cash"])
    positions = {row["symbol"]: row for row in store.get_positions(conn, portfolio_id)}
    target_weights = {t["symbol"]: float(t["weight"]) for t in targets}
    reason_map = {t["symbol"]: t.get("reason") or reason for t in targets}

    symbols = set(positions) | set(target_weights)
    opens = _prices(conn, date, sorted(symbols), "open")
    trade_count = 0
    skipped = []

    # Total equity marked at the execution price.
    total = cash + sum(float(pos["shares"]) * opens.get(sym, 0.0) for sym, pos in positions.items())
    if total <= 0:
        return {"trades": 0, "skipped": ["no equity"]}

    # Sells first so cash is available for buys.
    for symbol, pos in list(positions.items()):
        price = opens.get(symbol)
        if not price:
            skipped.append("no open %s" % symbol)
            continue
        prev = _prev_close(conn, symbol, date)
        if prev and price <= prev * (1 - config.PRICE_LIMIT):
            skipped.append("limit-down %s" % symbol)
            continue
        if pos["last_buy_date"] == date:
            skipped.append("T+1 %s" % symbol)
            continue
        target_value = target_weights.get(symbol, 0.0) * total
        current_value = pos["shares"] * price
        delta = target_value - current_value
        if delta >= -1.0:
            continue
        lots = int(abs(delta) // (price * config.LOT_SIZE)) * config.LOT_SIZE
        shares = min(int(pos["shares"]), lots)
        if shares <= 0:
            continue
        amount = shares * price
        fee = fees("sell", amount)
        realized = (price - float(pos["avg_cost"])) * shares - fee
        cash += amount - fee
        remaining = int(pos["shares"]) - shares
        if remaining <= 0:
            conn.execute(
                "DELETE FROM positions WHERE portfolio_id = ? AND symbol = ?",
                (portfolio_id, symbol),
            )
        else:
            conn.execute(
                "UPDATE positions SET shares = ? WHERE portfolio_id = ? AND symbol = ?",
                (remaining, portfolio_id, symbol),
            )
        store.record_trade(
            conn,
            portfolio_id,
            date,
            symbol,
            "sell",
            shares,
            price,
            fee,
            realized,
            reason_map.get(symbol, "调仓卖出"),
            model["id"],
        )
        trade_count += 1

    # Buys, largest target first.
    for symbol in sorted(target_weights, key=target_weights.get, reverse=True):
        price = opens.get(symbol)
        if not price:
            skipped.append("no open %s" % symbol)
            continue
        prev = _prev_close(conn, symbol, date)
        if prev and price >= prev * (1 + config.PRICE_LIMIT):
            skipped.append("limit-up %s" % symbol)
            continue
        pos = store.get_position(conn, portfolio_id, symbol)
        current_shares = int(pos["shares"]) if pos else 0
        target_value = target_weights[symbol] * total
        delta = target_value - current_shares * price
        if delta <= 1.0:
            continue
        lots = int(delta // (price * config.LOT_SIZE)) * config.LOT_SIZE
        if lots <= 0:
            continue
        # Keep at least one commission's worth of cash buffer.
        while lots > 0 and lots * price + fees("buy", lots * price) > cash:
            lots -= config.LOT_SIZE
        if lots <= 0:
            skipped.append("insufficient cash %s" % symbol)
            continue
        amount = lots * price
        fee = fees("buy", amount)
        cash -= amount + fee
        if pos:
            old_shares = int(pos["shares"])
            new_shares = old_shares + lots
            new_cost = (old_shares * float(pos["avg_cost"]) + amount + fee) / new_shares
            conn.execute(
                """UPDATE positions SET shares = ?, avg_cost = ?, last_buy_date = ?
                   WHERE portfolio_id = ? AND symbol = ?""",
                (new_shares, new_cost, date, portfolio_id, symbol),
            )
        else:
            conn.execute(
                """INSERT INTO positions (portfolio_id, symbol, shares, avg_cost, last_buy_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (portfolio_id, symbol, lots, (amount + fee) / lots, date),
            )
        store.record_trade(
            conn,
            portfolio_id,
            date,
            symbol,
            "buy",
            lots,
            price,
            fee,
            0.0,
            reason_map.get(symbol, "目标权重买入"),
            model["id"],
        )
        trade_count += 1

    conn.execute(
        "UPDATE portfolios SET cash = ? WHERE id = ?", (round(cash, 2), portfolio_id)
    )
    conn.commit()
    return {"trades": trade_count, "skipped": skipped, "equity": total}


def mark_to_market(conn: sqlite3.Connection, portfolio: sqlite3.Row, date: str) -> Dict:
    portfolio_id = portfolio["id"]
    cash = float(portfolio["cash"])
    positions = store.get_positions(conn, portfolio_id)
    closes = _prices(conn, date, [p["symbol"] for p in positions], "close")
    market_value = 0.0
    for pos in positions:
        price = closes.get(pos["symbol"])
        if price is None:
            row = conn.execute(
                "SELECT close FROM bars WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (pos["symbol"], date),
            ).fetchone()
            price = float(row["close"]) if row else float(pos["avg_cost"])
        market_value += int(pos["shares"]) * price
    store.save_equity(conn, portfolio_id, date, cash, market_value)
    return {"cash": cash, "market_value": market_value, "total": cash + market_value}


def _daily_returns(totals: List[float]) -> List[float]:
    return [
        totals[i] / totals[i - 1] - 1.0
        for i in range(1, len(totals))
        if totals[i - 1]
    ]


def metrics(equity_rows: List[sqlite3.Row], trades: List[sqlite3.Row], capital: float) -> Dict:
    if not equity_rows:
        return {
            "ret_pct": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "volatility": 0.0,
            "win_rate": 0.0,
            "trades": 0,
            "turnover": 0.0,
        }
    totals = [float(row["total"]) for row in equity_rows]
    rets = _daily_returns(totals)
    peak = totals[0]
    max_dd = 0.0
    for value in totals:
        peak = max(peak, value)
        max_dd = min(max_dd, value / peak - 1.0)
    if rets:
        avg = sum(rets) / len(rets)
        var = sum((r - avg) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
        vol = math.sqrt(var) * math.sqrt(252)
        sharpe = (avg * 252) / vol if vol else 0.0
    else:
        vol = 0.0
        sharpe = 0.0
    sells = [t for t in trades if t["side"] == "sell"]
    wins = sum(1 for t in sells if float(t["realized_pnl"]) > 0)
    traded = sum(float(t["amount"]) for t in trades)
    return {
        "ret_pct": (totals[-1] / capital - 1.0) * 100.0,
        "max_drawdown": max_dd * 100.0,
        "sharpe": sharpe,
        "volatility": vol * 100.0,
        "win_rate": (wins / len(sells) * 100.0) if sells else 0.0,
        "trades": len(trades),
        "turnover": traded / max(capital, 1.0),
    }
