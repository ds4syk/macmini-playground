"""Turn paper-trading portfolio weights into broker/simulator order sheets.

The dashboard families are research ledgers.  This module replicates one of
their portfolios into an external simulator account (CITIC 信e投 模拟 or
同花顺模拟炒股) by target *weights*, then produces a CSV / Markdown order
sheet that can be entered manually, plus a reconciliation view for an account
that already holds positions.

It deliberately does not place orders: neither CITIC's retail simulator nor
同花顺 expose a public order API.  See docs/BROKER.md for the manual steps and
for the QMT/PTrade route (which needs broker permission and a Windows host).
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "web" / "data.json"
BROKERS = {
    "citic": "中信证券信e投模拟交易",
    "ths": "同花顺模拟炒股",
}
BUY_FEE = 0.001
SELL_FEE = 0.002
MIN_FEE = 5.0
NEXT_SIGNAL = {
    "multifactor": "下一季度调仓信号 2026-12-31 收盘，2027-01-04 起执行（需重新跑信号）",
    "local": "按模型下一次信号（约每 5 个交易日）；当前为历史快照",
    "llm": "每个交易日 15:35 后生成次日决策；当前为历史快照",
}


def normalize_symbol(symbol: str) -> str:
    text = str(symbol).strip().lower()
    for prefix in ("sh.", "sz.", "sh", "sz"):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
            break
    for suffix in (".sh", ".sz", ".ss"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.zfill(6)


def is_star(code: str) -> bool:
    return code.startswith(("688", "689"))


def round_buy_shares(code: str, shares: float, mode: str = "floor") -> int:
    """A-share buy rule: 100-share lots; STAR 200 minimum, then 1-share steps.

    ``mode``: ``floor`` (account engine), ``nearest`` or ``up`` (order sheets;
    ``up`` fills a small account closer to the target, capped by the caller).
    """
    shares = int(max(0, shares))
    if is_star(code):
        return shares if shares >= 200 else 0
    if mode == "up":
        return ((shares + 99) // 100) * 100
    if mode == "nearest":
        return max(0, int(round(shares / 100.0))) * 100
    return (shares // 100) * 100


def trade_fee(amount: float, rate: float) -> float:
    return max(amount * rate, MIN_FEE)


@dataclass
class Position:
    symbol: str
    name: str
    shares: int
    price: float
    value: float
    weight: float


@dataclass
class Order:
    symbol: str
    name: str
    side: str
    shares: int
    price: float
    amount: float
    fee: float
    reason: str


def portfolio_positions(portfolio: dict) -> List[Position]:
    total = float(portfolio.get("total") or portfolio.get("capital") or 0)
    result = []
    for item in portfolio.get("positions", []):
        value = float(item.get("value") or 0)
        result.append(
            Position(
                symbol=normalize_symbol(item["symbol"]),
                name=str(item.get("name", "")),
                shares=int(item.get("shares") or 0),
                price=float(item.get("price") or 0),
                value=value,
                weight=value / total if total else 0.0,
            )
        )
    return result


def target_shares_by_weight(positions: Iterable[Position], equity: float) -> Dict[str, int]:
    targets = {}
    for position in positions:
        if position.price <= 0 or position.weight <= 0:
            continue
        targets[position.symbol] = round_buy_shares(
            position.symbol, equity * position.weight / position.price
        )
    return targets


def plan_orders(
    portfolio: dict,
    capital: Optional[float] = None,
    current: Optional[Dict[str, int]] = None,
    cash: Optional[float] = None,
    current_prices: Optional[Dict[str, float]] = None,
) -> dict:
    """Replicate the portfolio's weights at ``capital`` (or current equity).

    ``current`` maps symbol -> shares already held in the simulator; ``cash``
    is its cash.  With no current book this is a fresh-account buy list.
    """
    positions = portfolio_positions(portfolio)
    if not positions:
        raise ValueError("portfolio has no positions")
    by_symbol = {position.symbol: position for position in positions}
    current = {normalize_symbol(symbol): int(shares) for symbol, shares in (current or {}).items() if int(shares) > 0}
    current_prices = {normalize_symbol(symbol): float(price) for symbol, price in (current_prices or {}).items()}
    if current:
        equity = float(cash or 0) + sum(
            shares * (by_symbol[symbol].price if symbol in by_symbol else 0.0)
            for symbol, shares in current.items()
        )
        start_cash = float(cash or 0)
    else:
        equity = float(capital if capital is not None else portfolio.get("capital") or 0)
        start_cash = equity
    if equity <= 0:
        raise ValueError("equity must be positive")

    raw_shares = {
        position.symbol: equity * position.weight / position.price
        for position in positions
        if position.price > 0 and position.weight > 0
    }
    targets = {
        symbol: round_buy_shares(symbol, raw)
        for symbol, raw in raw_shares.items()
    }
    caps = {
        symbol: round_buy_shares(symbol, raw * 1.5)
        for symbol, raw in raw_shares.items()
    }
    target_values = {
        symbol: float(raw * by_symbol[symbol].price) for symbol, raw in raw_shares.items()
    }
    orders: List[Order] = []
    sell_proceeds = 0.0
    for symbol, shares in sorted(current.items()):
        target = targets.get(symbol, 0)
        if shares > target:
            position = by_symbol.get(symbol)
            price = (
                position.price
                if position and position.price > 0
                else current_prices.get(symbol, 0.0)
            )
            amount = shares * price
            fee = trade_fee(amount, SELL_FEE) if amount else 0.0
            orders.append(
                Order(
                    symbol=symbol,
                    name=position.name if position else "",
                    side="卖出",
                    shares=shares - target,
                    price=price,
                    amount=amount,
                    fee=fee,
                    reason="目标权重为 0 或低于当前持仓",
                )
            )
            sell_proceeds += amount - fee
    for symbol, target in sorted(targets.items()):
        held = current.get(symbol, 0)
        if target <= held:
            continue
        position = by_symbol[symbol]
        shares = target - held
        amount = shares * position.price
        orders.append(
            Order(
                symbol=symbol,
                name=position.name,
                side="买入",
                shares=shares,
                price=position.price,
                amount=amount,
                fee=trade_fee(amount, BUY_FEE),
                reason="补足目标权重",
            )
        )

    cash_weight = float(portfolio.get("cash") or 0) / float(portfolio.get("total") or 1)
    target_cash = cash_weight * equity
    available = start_cash + sell_proceeds - target_cash
    buys = [order for order in orders if order.side == "买入"]
    total_buy = sum(order.amount + order.fee for order in buys)
    # Trim from the last (smallest) buy if the initial floor lots do not fit.
    while total_buy > available + 1e-6:
        trimmed = False
        for order in sorted(buys, key=lambda item: target_values.get(item.symbol, 0.0)):
            if order.shares > 0:
                order.shares = round_buy_shares(order.symbol, order.shares - 100)
                order.amount = order.shares * order.price
                order.fee = trade_fee(order.amount, BUY_FEE) if order.amount else 0.0
                trimmed = True
                break
        if not trimmed:
            break
        total_buy = sum(order.amount + order.fee for order in buys)
    # Deploy residual cash round-robin, one lot per name per pass, capped at
    # 1.5x target.  This avoids leaving 20%+ idle cash on a 30k account while
    # keeping the weights closer to the model than filling the first name.
    if total_buy < available - 1e-6:
        changed = True
        while changed:
            changed = False
            for order in sorted(buys, key=lambda item: target_values.get(item.symbol, 0.0), reverse=True):
                cap = caps.get(order.symbol, 0)
                if order.shares + 100 > cap:
                    continue
                extra = order.price * 100
                extra_fee = trade_fee(order.amount + extra, BUY_FEE) - order.fee
                if total_buy + extra + extra_fee > available + 1e-6:
                    continue
                order.shares += 100
                order.amount = order.shares * order.price
                order.fee = trade_fee(order.amount, BUY_FEE) if order.amount else 0.0
                total_buy = sum(item.amount + item.fee for item in buys)
                changed = True
    orders = [order for order in orders if order.shares > 0]
    end_cash = start_cash
    for order in orders:
        if order.side == "卖出":
            end_cash += order.amount - order.fee
        else:
            end_cash -= order.amount + order.fee
    return {
        "equity": equity,
        "start_cash": start_cash,
        "end_cash": end_cash,
        "orders": orders,
        "targets": targets,
        "cash_weight": cash_weight,
    }


def format_markdown(
    broker: str,
    account: str,
    family: str,
    portfolio: dict,
    plan: dict,
    as_of: str,
) -> str:
    lines = [
        f"# {BROKERS.get(broker, broker)} · 调仓清单",
        "",
        f"- 账户：`{account}`",
        f"- 模型：{portfolio.get('model_name', portfolio.get('model_id'))}（{portfolio.get('model_id')}）",
        f"- 本金/权益：{plan['equity']:.2f} 元",
        f"- 模型信号日：{portfolio.get('decision', {}).get('date', '-')}；数据截至：{as_of}",
        f"- 下一信号：{NEXT_SIGNAL.get(family, '按模型下一次信号')}",
        "",
        "| 代码 | 名称 | 方向 | 数量 | 参考价 | 金额 | 估算费用 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for order in plan["orders"]:
        lines.append(
            f"| {order.symbol} | {order.name} | {order.side} | {order.shares} | "
            f"{order.price:.2f} | {order.amount:.2f} | {order.fee:.2f} |"
        )
    buy = [order for order in plan["orders"] if order.side == "买入"]
    sell = [order for order in plan["orders"] if order.side == "卖出"]
    lines += [
        "",
        f"- 买入 {len(buy)} 笔 / {sum(order.amount for order in buy):.2f} 元；"
        f"卖出 {len(sell)} 笔 / {sum(order.amount for order in sell):.2f} 元；"
        f"费用合计 {sum(order.fee for order in plan['orders']):.2f} 元",
        f"- 起始现金 {plan['start_cash']:.2f} 元，预计剩余现金 {plan['end_cash']:.2f} 元",
    ]
    cash_pct = plan["end_cash"] / plan["equity"] if plan["equity"] else 0.0
    if cash_pct > plan["cash_weight"] + 0.10:
        lines += [
            "",
            f"> 注意：整手/小资金限制导致剩余现金 {cash_pct:.1%}，高于模型目标 {plan['cash_weight']:.1%}。"
            "30k 无法完整复制多持仓模型，可放大本金或选择持仓更少的模型（如多因子 8 只）。",
        ]
    lines += [
        "",
        "## 下单注意",
        "",
        "- 参考价是模型最新收盘价，**不是实时价**；请在券商/同花顺界面用限价单，按当时盘口调整。",
        "- A 股买入 100 股整数倍；科创板 688/689 至少 200 股、之后 1 股递增；卖出可以零股。",
        "- 费用按买入 0.10%、卖出 0.20%（含印花税估算）、每笔最低 5 元；实际以券商为准。",
        "- 模拟盘若不能设置相同本金（如中信默认 20 万），可按比例放大数量，收益率不变。",
        "",
    ]
    return "\n".join(lines)


def load_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_portfolios(data: dict, family: Optional[str] = None, model: Optional[str] = None, capital: Optional[float] = None):
    for family_id, family_data in data["families"].items():
        if family and family_id != family:
            continue
        for portfolio in family_data.get("portfolios", []):
            if model and portfolio.get("model_id") != model:
                continue
            if capital is not None and float(portfolio.get("capital") or 0) != float(capital):
                continue
            if not portfolio.get("positions"):
                continue
            yield family_id, family_data, portfolio


def read_positions(path: Optional[Path]) -> tuple[Dict[str, int], Dict[str, float]]:
    if not path:
        return {}, {}
    result: Dict[str, int] = {}
    prices: Dict[str, float] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = normalize_symbol(row.get("symbol") or row.get("代码") or "")
            shares = int(float(row.get("shares") or row.get("数量") or 0))
            price = float(row.get("price") or row.get("价格") or 0)
            if symbol and shares:
                result[symbol] = shares
                if price > 0:
                    prices[symbol] = price
    return result, prices


def write_sheet(
    out_dir: Path,
    broker: str,
    family: str,
    portfolio: dict,
    as_of: str,
    capital: Optional[float] = None,
    current: Optional[Dict[str, int]] = None,
    cash: Optional[float] = None,
    current_prices: Optional[Dict[str, float]] = None,
) -> Path:
    effective = int(capital if capital is not None else portfolio.get("capital") or 0)
    account = f"{broker}-{family}-{portfolio['model_id']}-{effective}"
    plan = plan_orders(
        portfolio, capital=capital, current=current, cash=cash, current_prices=current_prices
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{account}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name", "side", "shares", "price", "amount", "fee", "reason"])
        for order in plan["orders"]:
            writer.writerow(
                [
                    order.symbol,
                    order.name,
                    order.side,
                    order.shares,
                    f"{order.price:.2f}",
                    f"{order.amount:.2f}",
                    f"{order.fee:.2f}",
                    order.reason,
                ]
            )
    (out_dir / f"{account}.md").write_text(
        format_markdown(broker, account, family, portfolio, plan, as_of), encoding="utf-8"
    )
    return csv_path


def command_list(args) -> None:
    data = load_data(Path(args.data))
    for family_id, family_data in data["families"].items():
        print(f"\n[{family_id}] {family_data.get('meta', {}).get('label', '')} as_of={family_data.get('as_of')}")
        for portfolio in family_data.get("portfolios", []):
            positions = portfolio.get("positions") or []
            print(
                f"  {portfolio['model_id']:<18} {int(portfolio.get('capital') or 0):>7} "
                f"positions={len(positions):>2} cash={float(portfolio.get('cash') or 0):>10.2f} "
                f"signal={portfolio.get('decision', {}).get('date', '-')}"
            )


def command_orders(args) -> None:
    data = load_data(Path(args.data))
    out_dir = Path(args.out)
    as_of = {}
    count = 0
    current, current_prices = read_positions(Path(args.positions)) if args.positions else ({}, {})
    for family_id, family_data, portfolio in iter_portfolios(
        data, family=args.family, model=args.model, capital=args.capital
    ):
        path = write_sheet(
            out_dir,
            args.broker,
            family_id,
            portfolio,
            family_data.get("as_of", ""),
            capital=args.capital_override,
            current=current or None,
            cash=args.cash,
            current_prices=current_prices or None,
        )
        print(path)
        count += 1
    print(f"\n{count} 个账户的清单已写入 {out_dir}")


def command_template(args) -> None:
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("symbol,shares\n600000,100\n", encoding="utf-8")
    print(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper portfolio -> broker/simulator order sheets")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list model/capital portfolios")
    p_list.set_defaults(func=command_list)

    p_orders = sub.add_parser("orders", help="write order sheets")
    p_orders.add_argument("--broker", choices=sorted(BROKERS), required=True)
    p_orders.add_argument("--family", choices=["local", "multifactor", "llm"])
    p_orders.add_argument("--model")
    p_orders.add_argument("--capital", type=float)
    p_orders.add_argument("--capital-override", type=float, help="simulator capital if different from the model bucket")
    p_orders.add_argument("--positions", help="CSV of current simulator positions: symbol,shares")
    p_orders.add_argument("--cash", type=float, help="current simulator cash (with --positions)")
    p_orders.add_argument("--out", default="broker_orders")
    p_orders.set_defaults(func=command_orders)

    p_template = sub.add_parser("template", help="write an empty positions CSV")
    p_template.add_argument("--out", default="broker_orders/positions.csv")
    p_template.set_defaults(func=command_template)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
