"""Monthly account model shared by the lab experiment and the local dashboard.

The engine follows the Share v2 account conventions (100-share lots, 200 for
``sh.688``, 0.10%/0.20% all-in costs with a 5 CNY minimum, a position cap
relative to float market cap, limit-up/down and ST filters, and a buffer rule
that sells only when a name drops out of the top ``buffer_size``).  Two
deliberate differences come from the Eastmoney bulk valuation source:

* execution and marking use the **close** (the report has no open price);
* the liquidity cap uses float market cap instead of previous-day amount
  (the report has no volume).

Only numpy is required, so the Mac mini can replay the precomputed scores
without pandas, LightGBM or torch.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

DEFAULTS = {
    "portfolio_size": 50,
    "buffer_size": 80,
    "max_replace": 50,
    "buy_cost": 0.001,
    "sell_cost": 0.002,
    "min_fee": 5.0,
    "liquidity": 0.01,  # max 1% of float market cap per name
}


def lot_size(code: str) -> int:
    body = code.replace("sh.", "").replace("sz.", "")
    return 200 if body.startswith("688") else 100


def fee(notional: float, rate: float, minimum: float) -> float:
    return max(notional * rate, minimum)


def price_limit(code: str, is_st: bool) -> float:
    body = code.replace("sh.", "").replace("sz.", "")
    if is_st:
        return 0.05
    if body.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def target_shares(indices, value, prices, codes):
    result = np.zeros(len(codes), dtype=np.int64)
    for index in indices:
        index = int(index)
        price = float(prices[index])
        if price <= 0:
            continue
        lot = lot_size(codes[index])
        result[index] = int(value // (price * lot)) * lot
    return result


def run(
    dates: List[str],
    stocks: List[str],
    close: np.ndarray,
    pct_chg: np.ndarray,
    float_mcap: np.ndarray,
    is_st: np.ndarray,
    tradable: np.ndarray,
    score: np.ndarray,
    capital: float,
    start: int,
    end: int,
    params: Dict | None = None,
):
    """Run the monthly account from ``start`` to ``end`` (inclusive).

    ``score`` is ``[stock, day]`` and finite only on signal days (month ends).
    ``start`` is the signal day immediately before the first execution day, so
    the account opens positions at the next close.
    """
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    portfolio_size = int(p["portfolio_size"])
    buffer_size = int(p["buffer_size"])
    max_replace = int(p["max_replace"])
    buy_cost = float(p["buy_cost"])
    sell_cost = float(p["sell_cost"])
    min_fee = float(p["min_fee"])
    liquidity = float(p["liquidity"])

    signal_days = {day for day in range(start, end + 1) if np.isfinite(score[:, day]).any()}
    units = np.zeros(len(stocks), dtype=np.int64)
    cash = float(capital)
    cost_basis = np.zeros(len(stocks), dtype=float)
    trades = []
    rows = []
    total_fees = 0.0
    last_price = np.zeros(len(stocks), dtype=float)
    base_limits = np.array(
        [
            0.20 if code.replace("sh.", "").replace("sz.", "").startswith(("300", "301", "688", "689")) else 0.10
            for code in stocks
        ]
    )

    for day in range(start, end + 1):
        raw_prices = close[:, day].astype(float)
        valid_price = np.isfinite(raw_prices) & (raw_prices > 0)
        last_price[valid_price] = raw_prices[valid_price]
        prices = np.where(valid_price, raw_prices, last_price)
        movement = np.where(np.isfinite(pct_chg[:, day]), pct_chg[:, day], 0.0)
        equity_open = cash + float(np.dot(units, prices))
        traded = 0.0
        daily_fee = 0.0
        ranked = None

        if day > start and (day - 1) in signal_days:
            ranks = np.full(len(stocks), np.inf)
            values = score[:, day - 1]
            finite = np.isfinite(values)
            order = np.argsort(-values[finite], kind="stable")
            finite_indices = np.flatnonzero(finite)
            ranks[finite_indices[order]] = np.arange(len(finite_indices))
            ranked = np.argsort(ranks, kind="stable")
            held = np.flatnonzero(units > 0)
            held_set = set(int(index) for index in held)
            sells = [int(index) for index in held if ranks[int(index)] >= buffer_size]
            sells.sort(key=lambda index: ranks[index], reverse=True)
            sells = sells[:max_replace]
            keep = held_set - set(sells)
            candidates = [int(index) for index in ranked if int(index) not in held_set]
            gaps = max(0, portfolio_size - len(held))
            capacity = max(0, min(portfolio_size - len(keep), max_replace + gaps))
            purchases = candidates[:capacity]
            target = np.zeros(len(stocks), dtype=np.int64)
            target[sells] = 0
            buy_target = target_shares(
                purchases, equity_open / max(portfolio_size, 1), prices, stocks
            )
            target[purchases] = buy_target[purchases]
        else:
            target = units.copy()

        limits = np.where(is_st[:, day], 0.05, base_limits)
        can_buy = (
            tradable[:, day]
            & valid_price
            & (~is_st[:, day])
            & (movement < limits - 0.005)
        )
        can_sell = tradable[:, day] & valid_price & (movement > -limits + 0.005)
        mcap = np.where(np.isfinite(float_mcap[:, day]) & (float_mcap[:, day] > 0), float_mcap[:, day], 0.0)
        liquidity_shares = np.floor(mcap * liquidity / np.maximum(prices, 1e-9))
        liquidity_shares = np.nan_to_num(liquidity_shares, nan=0.0, posinf=0.0, neginf=0.0).astype(np.int64)

        sell_request = np.maximum(units - target, 0)
        sales = np.minimum(sell_request, liquidity_shares) * can_sell.astype(np.int64)
        for index in np.flatnonzero(sales > 0):
            index = int(index)
            shares = int(sales[index])
            price = float(prices[index])
            notional = shares * price
            cost = fee(notional, sell_cost, min_fee)
            average = cost_basis[index] / max(units[index], 1)
            cash += notional - cost
            units[index] -= shares
            cost_basis[index] -= average * shares
            if units[index] <= 0:
                cost_basis[index] = 0.0
            total_fees += cost
            traded += notional
            daily_fee += cost
            trades.append(
                {
                    "date": dates[day],
                    "symbol": stocks[index],
                    "side": "sell",
                    "shares": shares,
                    "price": price,
                    "amount": notional,
                    "fee": cost,
                    "realized_pnl": (price - average) * shares - cost,
                    "reason": "跌破缓冲或调仓卖出",
                }
            )

        if day > start and (day - 1) in signal_days:
            for index in ranked if ranked is not None else []:
                index = int(index)
                desired = int(target[index] - units[index])
                if desired <= 0 or not can_buy[index]:
                    continue
                price = float(prices[index])
                if price <= 0:
                    continue
                lot = lot_size(stocks[index])
                affordable = int(cash // max(price, 1e-9))
                shares = min(desired, (affordable // lot) * lot, int(liquidity_shares[index] // lot) * lot)
                while shares > 0:
                    notional = shares * price
                    if notional + fee(notional, buy_cost, min_fee) <= cash + 1e-9:
                        break
                    shares -= lot
                if shares <= 0:
                    continue
                notional = shares * price
                cost = fee(notional, buy_cost, min_fee)
                cash -= notional + cost
                cost_basis[index] += notional + cost
                units[index] += shares
                total_fees += cost
                traded += notional
                daily_fee += cost
                trades.append(
                    {
                        "date": dates[day],
                        "symbol": stocks[index],
                        "side": "buy",
                        "shares": shares,
                        "price": price,
                        "amount": notional,
                        "fee": cost,
                        "realized_pnl": 0.0,
                        "reason": "多因子打分买入",
                    }
                )

        equity = cash + float(np.dot(units, prices))
        if not np.isfinite(equity) or equity <= 0 or cash < -1e-6 or np.any(units < 0):
            raise RuntimeError(f"account invariant failed on {dates[day]}")
        rows.append({"date": dates[day], "cash": cash, "equity": equity, "traded": traded, "fees": daily_fee})

    return rows, units, cost_basis, trades, total_fees


def metrics(rows, trades, capital: float) -> Dict[str, float]:
    equity = np.array([row["equity"] for row in rows], dtype=float)
    daily = np.diff(np.r_[capital, equity]) / np.r_[capital, equity[:-1]]
    peak = np.maximum.accumulate(np.r_[capital, equity])[1:]
    drawdown = equity / peak - 1
    sells = [trade for trade in trades if trade["side"] == "sell"]
    wins = sum(1 for trade in sells if trade["realized_pnl"] > 0)
    std = float(np.std(daily, ddof=1)) if len(daily) > 1 else 0.0
    return {
        "ret_pct": float(equity[-1] / capital - 1) * 100,
        "max_drawdown": float(drawdown.min() * 100),
        "sharpe": float(np.mean(daily) / std * np.sqrt(252)) if std > 0 else 0.0,
        "volatility": std * np.sqrt(252) * 100,
        "win_rate": (wins / len(sells) * 100) if sells else 0.0,
        "trades": len(trades),
        "turnover": float(sum(trade["amount"] for trade in trades) / capital),
    }
