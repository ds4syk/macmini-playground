"""Hong Kong cash-equity cost model.

The commission schedule follows HSBC Hong Kong's Trade25 account (the plan the
ticket names): a HKD 25 monthly fee, zero brokerage commission while the
month-to-date turnover is at or below HKD 250,000, then 0.25% with a HKD 100
minimum per order.  Statutory/venue charges are added on every order:

* stamp duty 0.1%, rounded up to the nearest HKD 1;
* HKEX trading fee 0.00565%;
* SFC transaction levy 0.0027%;
* AFRC transaction levy 0.00015%;
* CCASS settlement fee 0.002%, minimum HKD 2 and maximum HKD 100.

The crossing order is charged the full 0.25% (not only the excess) so the
simulation never understates costs.  This is a research approximation, not a
broker quote.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "monthly_commission_free_turnover": 250_000.0,
    "monthly_fee": 25.0,
    "commission_rate": 0.0025,
    "commission_min": 100.0,
    "stamp_duty": 0.001,
    "trading_fee": 0.0000565,
    "sfc_levy": 0.000027,
    "afrc_levy": 0.0000015,
    "settlement_fee": 0.00002,
    "settlement_min": 2.0,
    "settlement_max": 100.0,
}


def order_fees(notional: float, config: dict | None = None, multiplier: float = 1.0) -> dict:
    """Venue/statutory charges for one buy or sell order, excluding commission."""
    config = {**DEFAULTS, **(config or {})}
    notional = max(float(notional), 0.0)
    multiplier = max(float(multiplier), 0.0)
    if notional <= 0:
        return {"stamp_duty": 0.0, "trading_fee": 0.0, "sfc_levy": 0.0,
                "afrc_levy": 0.0, "settlement_fee": 0.0, "statutory_total": 0.0}
    stamp = math.ceil(notional * float(config["stamp_duty"]))
    trading = notional * float(config["trading_fee"])
    sfc = notional * float(config["sfc_levy"])
    afrc = notional * float(config["afrc_levy"])
    settlement = min(max(notional * float(config["settlement_fee"]),
                         float(config["settlement_min"])),
                     float(config["settlement_max"]))
    stamp, trading, sfc, afrc, settlement = (value * multiplier for value in
                                              (stamp, trading, sfc, afrc, settlement))
    return {
        "stamp_duty": stamp,
        "trading_fee": trading,
        "sfc_levy": sfc,
        "afrc_levy": afrc,
        "settlement_fee": settlement,
        "statutory_total": stamp + trading + sfc + afrc + settlement,
    }


def commission(notional: float, month_turnover: float, config: dict | None = None,
                multiplier: float = 1.0) -> float:
    """HSBC Trade25 brokerage commission for one order."""
    config = {**DEFAULTS, **(config or {})}
    notional = max(float(notional), 0.0)
    multiplier = max(float(multiplier), 0.0)
    if notional <= 0:
        return 0.0
    threshold = float(config["monthly_commission_free_turnover"])
    if month_turnover + notional <= threshold + 1e-9:
        return 0.0
    return max(notional * float(config["commission_rate"]), float(config["commission_min"])) * multiplier


def total_order_cost(notional: float, month_turnover: float, config: dict | None = None,
                     multiplier: float = 1.0) -> dict:
    charges = order_fees(notional, config, multiplier)
    fee = commission(notional, month_turnover, config, multiplier)
    return {**charges, "commission": fee, "total": charges["statutory_total"] + fee}


def lot_size(code: str, lots: dict) -> int:
    return int(lots.get(str(code), 100))


def target_shares(value: float, price: float, lot: int) -> int:
    """Largest whole board lot not exceeding ``value``."""
    if price <= 0 or lot <= 0 or value <= 0:
        return 0
    return int(value // (price * lot)) * lot
