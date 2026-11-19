"""把看板组合接入同花顺网页模拟盘：读账户/持仓，按清单自动下单。

分工：
- ``paper_trader.broker`` 负责「模型组合 → 调仓清单 CSV」；
- 本模块负责「清单 CSV ↔ 同花顺网页模拟盘」：读资金和持仓、导出成 broker 能用的
  ``symbol,shares,price`` CSV、按市价委托逐笔提交。

前置条件：Chrome 已用调试端口启动，并人工登录同花顺模拟盘（扫码即可）：
``scripts/start_ths_chrome.sh`` → ``python -m paper_trader.ths_web login``。

安全边界：只操作同花顺模拟盘（交易页标题「同花顺虚拟交易所」）；没有账密存储；``execute`` 默认
拒绝执行，必须显式 ``--yes``；所有提交都会写一份本地执行日志。真实券商（中信信e投）
没有公开下单 API，仍按 ``docs/BROKER.md`` 走手工清单。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from paper_trader import ths_page
from paper_trader.broker import is_star, normalize_symbol

STATE_DIR = Path.home() / ".config" / "paper-trader"
EXEC_LOG_DIR = STATE_DIR / "executions"
COOKIE_PATH = STATE_DIR / "ths_cookies.json"

SIDES = {"买入": "buy", "卖出": "sell", "buy": "buy", "sell": "sell"}


@dataclass
class SheetOrder:
    symbol: str
    name: str
    side: str
    shares: int
    price: float
    amount: float
    fee: float
    reason: str = ""


@dataclass
class PositionRow:
    symbol: str
    name: str
    shares: int
    price: float

    def as_csv_row(self) -> List[str]:
        return [self.symbol, str(self.shares), "%.2f" % self.price]


def parse_money(text: Any) -> float:
    """把页面上的金额文本转成 float；空值/``-``/乱码返回 0。"""
    if text is None:
        return 0.0
    cleaned = re.sub(r"[,，\s￥¥元股]", "", str(text))
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else 0.0


def read_sheet(path: Path) -> List[SheetOrder]:
    """读取 broker.py 生成的清单 CSV。"""
    orders: List[SheetOrder] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [field for field in ("symbol", "side", "shares") if field not in headers]
        if missing:
            raise ValueError("清单缺少列：%s" % ", ".join(missing))
        for row in reader:
            if not (row.get("symbol") or "").strip():
                continue
            orders.append(
                SheetOrder(
                    symbol=normalize_symbol(row.get("symbol", "")),
                    name=(row.get("name") or "").strip(),
                    side=SIDES.get((row.get("side") or "").strip(), (row.get("side") or "").strip()),
                    shares=int(float(row.get("shares") or 0)),
                    price=float(row.get("price") or 0),
                    amount=float(row.get("amount") or 0),
                    fee=float(row.get("fee") or 0),
                    reason=(row.get("reason") or "").strip(),
                )
            )
    return orders


def validate_orders(orders: Iterable[SheetOrder]) -> List[str]:
    """返回不能提交的原因；空列表表示可以提交。"""
    problems: List[str] = []
    seen = set()
    for order in orders:
        label = "%s %s" % (order.symbol, order.name)
        if order.side not in ("buy", "sell"):
            problems.append("%s：方向 %r 不是 buy/sell" % (label, order.side))
            continue
        if not re.fullmatch(r"\d{6}", order.symbol):
            problems.append("%s：代码不是 6 位数字" % label)
        if order.shares <= 0:
            problems.append("%s：数量必须为正" % label)
        elif order.side == "buy" and is_star(order.symbol):
            if order.shares < 200:
                problems.append("%s：科创板买入至少 200 股" % label)
        elif order.side == "buy" and order.shares % 100 != 0:
            problems.append("%s：主板买入数量必须是 100 股整数倍" % label)
        if order.price <= 0:
            problems.append("%s：参考价必须为正" % label)
        if order.symbol in seen:
            problems.append("%s：同一代码出现多次" % label)
        seen.add(order.symbol)
    return problems


def _pick(record: Dict[str, str], *keywords: str) -> str:
    """按表头关键字取值（同花顺表头偶尔改措辞）。"""
    for keyword in keywords:
        for header, value in record.items():
            if keyword in header and value:
                return value
    return ""


def position_rows(records: List[Dict[str, str]]) -> List[PositionRow]:
    """把同花顺持仓表记录标准化成 broker.py 能读的 symbol/shares/price。"""
    rows: List[PositionRow] = []
    for record in records:
        digits = re.sub(r"\D", "", _pick(record, "证券代码", "股票代码", "代码"))
        if not re.fullmatch(r"\d{6}", digits):
            continue
        symbol = normalize_symbol(digits)
        shares = int(parse_money(
            _pick(record, "股票余额", "持仓数量", "当前数量", "可用余额", "余额")
        ))
        price = parse_money(_pick(record, "市价", "当前价", "最新价", "现价"))
        if shares <= 0:
            continue
        rows.append(
            PositionRow(
                symbol=symbol,
                name=_pick(record, "证券名称", "股票名称", "名称"),
                shares=shares,
                price=price,
            )
        )
    return rows


def positions_csv(rows: List[PositionRow]) -> str:
    lines = ["symbol,shares,price"]
    lines.extend(",".join(row.as_csv_row()) for row in rows)
    return "\n".join(lines) + "\n"


def account_summary(account: Dict[str, str]) -> Dict[str, float]:
    return {name: parse_money(value) for name, value in account.items()}


def write_execution_log(payload: Dict[str, Any]) -> Path:
    EXEC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = EXEC_LOG_DIR / ("%s.json" % stamp)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_plan(orders: List[SheetOrder], account: Optional[Dict[str, float]] = None) -> None:
    buys = [order for order in orders if order.side == "buy"]
    sells = [order for order in orders if order.side == "sell"]
    print("买入 %d 笔 / 参考金额 %.2f 元；卖出 %d 笔 / 参考金额 %.2f 元" % (
        len(buys), sum(order.amount for order in buys), len(sells), sum(order.amount for order in sells)
    ))
    if account:
        print("账户：可用余额 %.2f 元，总资产 %.2f 元" % (
            account.get("可用余额", 0.0), account.get("总资产", 0.0)
        ))
    for order in orders:
        print("  %s %-6s %-6s %6d 股  参考价 %8.2f  金额 %10.2f" % (
            order.side, order.symbol, order.name, order.shares, order.price, order.amount
        ))


def _check_affordable(orders: List[SheetOrder], account: Dict[str, float], skip: bool) -> List[str]:
    if skip:
        return []
    available = account.get("可用余额", 0.0)
    buy_total = sum(order.amount for order in orders if order.side == "buy")
    sell_total = sum(order.amount for order in orders if order.side == "sell")
    if buy_total > available + sell_total * 0.95 + 1:
        return [
            "买入参考金额 %.2f 元超过可用余额 %.2f 元（卖出回款按 95%% 计），拒绝提交；"
            "请先卖后买或减少下单量。" % (buy_total, available)
        ]
    return []


async def _status_command(args: argparse.Namespace) -> int:
    page = await ths_page.ensure_trading_page()
    account_text = await ths_page.read_account(page)
    positions = await ths_page.read_positions(page)
    account = account_summary(account_text)
    rows = position_rows(positions)
    if args.json:
        print(json.dumps(
            {
                "account": account,
                "raw_account": account_text,
                "positions": [asdict(row) for row in rows],
                "raw_positions": positions,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("同花顺模拟盘（%s）" % ths_page.TRADE_PAGE_TITLE)
        for name, value in account.items():
            print("  %-6s %s" % (name, value))
        print("持仓 %d 只：" % len(rows))
        for row in rows:
            print("  %s %-6s %6d 股  现价 %8.2f" % (row.symbol, row.name, row.shares, row.price))
    return 0


async def _positions_command(args: argparse.Namespace) -> int:
    page = await ths_page.ensure_trading_page()
    account = account_summary(await ths_page.read_account(page))
    rows = position_rows(await ths_page.read_positions(page))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(positions_csv(rows), encoding="utf-8")
    print("已写入 %s（%d 只持仓）" % (out, len(rows)))
    print("可用余额 %.2f 元；下一步可用：" % account.get("可用余额", 0.0))
    print("  python3 -m paper_trader.broker orders --broker ths --positions %s --cash %.2f ..." % (
        out, account.get("可用余额", 0.0)
    ))
    return 0


async def _execute_command(args: argparse.Namespace) -> int:
    orders = read_sheet(Path(args.orders))
    problems = validate_orders(orders)
    if problems:
        print("清单校验失败：", file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return 2
    if len(orders) > args.max_orders:
        print("订单数 %d 超过 --max-orders=%d，拒绝执行。" % (len(orders), args.max_orders), file=sys.stderr)
        return 2
    page = await ths_page.ensure_trading_page()
    account = account_summary(await ths_page.read_account(page))
    _print_plan(orders, account)
    blocked = _check_affordable(orders, account, skip=args.skip_affordability)
    if blocked:
        print("拒绝提交：" + blocked[0], file=sys.stderr)
        return 2
    if args.dry_run:
        print("\n--dry-run：只走到确认弹窗并取消，不提交委托。")
        result = await ths_page.place_market_order(
            page, orders[0].side, orders[0].symbol, orders[0].shares, dry_run=True
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        print("\n未加 --yes，不执行；确认无误后重跑并加 --yes。", file=sys.stderr)
        return 2
    # 先卖后买，卖出回款再用于买入。
    ordered = [order for order in orders if order.side == "sell"] + [
        order for order in orders if order.side == "buy"
    ]
    results: List[Dict[str, Any]] = []
    for order in ordered:
        try:
            result = await ths_page.place_market_order(
                page, order.side, order.symbol, order.shares, dry_run=False
            )
        except ths_page.ThsError as exc:
            results.append({"order": asdict(order), "ok": False, "error": str(exc)})
            print("失败：%s %s %s 股 — %s" % (order.side, order.symbol, order.shares, exc), file=sys.stderr)
            break
        results.append({"order": asdict(order), "ok": True, "result": result})
        print("已提交：%s %s %s 股 合同编号 %s" % (
            order.side, order.symbol, order.shares, result.get("order_id") or "-"
        ))
        await page.wait_for_timeout(800)
    path = write_execution_log(
        {
            "at": datetime.now().isoformat(timespec="seconds"),
            "orders_file": str(args.orders),
            "account_before": account,
            "results": results,
        }
    )
    print("执行日志：%s" % path)
    return 0 if all(item["ok"] for item in results) else 1


async def _preflight_command(args: argparse.Namespace) -> int:
    orders = read_sheet(Path(args.orders))
    problems = validate_orders(orders)
    if problems:
        print("清单校验失败：", file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return 2
    page = await ths_page.ensure_trading_page()
    account = account_summary(await ths_page.read_account(page))
    _print_plan(orders, account)
    picks = [orders[0]]
    other_side = next((order for order in orders if order.side != orders[0].side), None)
    if other_side is not None:
        picks.append(other_side)
    for order in picks:
        result = await ths_page.place_market_order(
            page, order.side, order.symbol, order.shares, dry_run=True
        )
        print("自检通过：%s %s %s 股（已取消确认弹窗，未提交）" % (
            order.side, order.symbol, order.shares
        ))
        print(json.dumps(result, ensure_ascii=False)[:300])
    return 0


async def _login_command(args: argparse.Namespace) -> int:
    qr_out = Path(args.qr_out)
    platform = await ths_page.open_platform_page()
    if await ths_page.is_logged_in(platform):
        print("已检测到同花顺登录态，跳过扫码。")
    else:
        print("在 Chrome 里打开同花顺登录页并生成二维码；请用同花顺 App 扫码。")
        await ths_page.login_with_qr(qr_out, timeout=args.timeout, refresh_every=args.refresh_every)
        print("登录完成，进入交易区…")
    page = await ths_page.ensure_trading_page()
    account = account_summary(await ths_page.read_account(page))
    count = await ths_page.export_cookies(COOKIE_PATH)
    print("已导出 %d 条同花顺 Cookie 到 %s（HTTP 直连用）。" % (count, COOKIE_PATH))
    print("交易页就绪；可用余额 %.2f 元，总资产 %.2f 元。" % (
        account.get("可用余额", 0.0), account.get("总资产", 0.0)
    ))
    return 0


async def _order_command(args: argparse.Namespace) -> int:
    side = SIDES.get(args.side, args.side)
    order = SheetOrder(
        symbol=normalize_symbol(args.code),
        name="",
        side=side,
        shares=int(args.shares),
        price=float(args.price or 0),
        amount=float(args.price or 0) * int(args.shares),
        fee=0.0,
    )
    problems = validate_orders([order])
    if problems:
        for problem in problems:
            print("拒绝：" + problem, file=sys.stderr)
        return 2
    page = await ths_page.ensure_trading_page()
    account = account_summary(await ths_page.read_account(page))
    blocked = _check_affordable([order], account, skip=False)
    if blocked:
        print("拒绝：" + blocked[0], file=sys.stderr)
        return 2
    if not args.yes:
        print("未加 --yes，不执行。将提交：%s %s %s 股（市价）。" % (side, order.symbol, order.shares))
        return 2
    result = await ths_page.place_market_order(page, side, order.symbol, order.shares)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    log = write_execution_log({
        "at": datetime.now().isoformat(timespec="seconds"),
        "order": asdict(order),
        "result": result,
    })
    print("执行日志：%s" % log)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同花顺网页模拟盘桥接：读账户/持仓、按清单市价下单"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="查看模拟盘资金与持仓")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_status_command)

    p_positions = sub.add_parser("positions", help="导出持仓为 broker.py --positions 用的 CSV")
    p_positions.add_argument("--out", default="broker_orders/ths_positions.csv")
    p_positions.set_defaults(func=_positions_command)

    p_execute = sub.add_parser("execute", help="按 broker.py 生成的清单下单")
    p_execute.add_argument("--orders", required=True)
    p_execute.add_argument("--yes", action="store_true", help="确认提交委托（不加只打印计划）")
    p_execute.add_argument("--dry-run", action="store_true", help="走到确认弹窗后取消，不提交")
    p_execute.add_argument("--max-orders", type=int, default=60)
    p_execute.add_argument("--skip-affordability", action="store_true", help="跳过可用余额检查")
    p_execute.set_defaults(func=_execute_command)

    p_preflight = sub.add_parser("preflight", help="用确认弹窗自检清单（不下单）")
    p_preflight.add_argument("--orders", required=True)
    p_preflight.set_defaults(func=_preflight_command)

    p_login = sub.add_parser("login", help="生成扫码登录二维码并等待登录")
    p_login.add_argument("--qr-out", default=str(STATE_DIR / "ths-login-qr.png"))
    p_login.add_argument("--timeout", type=int, default=1800)
    p_login.add_argument("--refresh-every", type=int, default=60)
    p_login.set_defaults(func=_login_command)

    p_order = sub.add_parser("order", help="手工下一笔市价单")
    p_order.add_argument("--side", choices=["buy", "sell", "买入", "卖出"], required=True)
    p_order.add_argument("--code", required=True)
    p_order.add_argument("--shares", type=int, required=True)
    p_order.add_argument("--price", type=float, required=True, help="参考价（用于金额展示与余额检查）")
    p_order.add_argument("--yes", action="store_true")
    p_order.set_defaults(func=_order_command)
    return parser


async def _run(args: argparse.Namespace) -> int:
    try:
        return await args.func(args)
    finally:
        await ths_page.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    import asyncio

    try:
        return asyncio.run(_run(args))
    except ths_page.ThsError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
