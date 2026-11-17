"""同花顺模拟炒股 HTTP 直连（快速路径）。

浏览器只用一次登录并导出 Cookie；之后查资金/持仓、查行情、发委托都走
``mncg.10jqka.com.cn`` 的 JSON 接口，不再点击页面，单次调用通常 0.2–0.5 秒。

接口与字段参考了这些公开实现（仅作协议参考，未引入代码）：
- https://github.com/Cfu4536/ths_simulated_API
- https://github.com/jaysinco/RobotTrader
- https://github.com/glei4134-collab/ths-trading-ai

安全边界与 ``paper_trader.ths_web`` 相同：只操作同花顺模拟盘、不保存账密、
``execute`` 必须显式 ``--yes``、每笔提交写执行日志。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

try:  # 日常 CLI 用系统 python 即可（只需要 requests）
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from paper_trader.ths_web import (
    COOKIE_PATH,
    SheetOrder,
    _check_affordable,
    _print_plan,
    account_summary,
    read_sheet,
    validate_orders,
    write_execution_log,
)

BASE = "https://mncg.10jqka.com.cn/cgiwt/delegate"
HOST = "mncg.10jqka.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
# 市价委托类型：最优五档即时成交剩余撤销（沪 21 / 深 14）
SJWT_TYPE = {"2": "21", "1": "14"}


class ThsHttpError(RuntimeError):
    """HTTP 接口调用失败。"""


class CookieMissing(ThsHttpError):
    """没有可用的 Cookie，需要先登录并导出。"""


def load_cookies(path: Path = COOKIE_PATH) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise CookieMissing(
            "找不到 %s。请先运行：.venv-ths/bin/python -m paper_trader.ths_http login" % path
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("cookies"):
        raise CookieMissing("Cookie 文件为空，请重新登录。")
    return data


def cookie_header(cookies: Iterable[Dict[str, Any]], host: str = HOST) -> str:
    """只挑出对 ``host`` 生效的 Cookie 拼成请求头。"""
    parts = []
    for cookie in cookies:
        domain = (cookie.get("domain") or "").lstrip(".")
        if domain and host != domain and not host.endswith("." + domain):
            continue
        parts.append("%s=%s" % (cookie["name"], cookie["value"]))
    return "; ".join(parts)


def _pick(data: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if data.get(name) not in (None, ""):
            return data[name]
    return None


class Client:
    """同花顺模拟盘 HTTP 客户端。"""

    def __init__(self, cookie_path: Path = COOKIE_PATH, timeout: float = 8.0):
        if requests is None:
            raise ThsHttpError("缺少 requests；请用系统 python3 运行，或 pip install requests。")
        data = load_cookies(cookie_path)
        self.cookies = data["cookies"]
        self.accounts: Dict[str, str] = data.get("accounts") or {}
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {
            "Host": HOST,
            "Origin": "https://" + HOST,
            "Referer": "https://%s/cgiwt/index/index" % HOST,
            "User-Agent": USER_AGENT,
            "Cookie": cookie_header(self.cookies),
        }

    def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = BASE + path
        try:
            response = self.session.post(url, data=data, headers=self.headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ThsHttpError("请求失败：%s" % exc) from exc
        if response.status_code != 200:
            raise ThsHttpError("HTTP %s：%s" % (response.status_code, url))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ThsHttpError("返回不是 JSON（登录可能已过期）：%s" % response.text[:120]) from exc
        if payload.get("errorcode") not in (None, 0, "0"):
            raise ThsHttpError("%s：%s" % (payload.get("errorcode"), payload.get("errormsg", "")))
        return payload

    def _query(self, classes: str, cmd: str = "cmd_wt_mairu") -> Dict[str, Any]:
        payload = self._post("/updateclass", {"type": cmd, "updateClass": classes})
        result = payload.get("result") or {}
        if "qryzijin" not in result and not any(key.startswith("qry") for key in result):
            raise ThsHttpError("查询返回结构异常（登录可能已过期）：%s" % str(payload)[:160])
        for key, node in result.items():
            if isinstance(node, dict):
                code = node.get("errorcode")
                try:
                    code = int(code or 0)
                except (TypeError, ValueError):
                    code = 0
                if code:
                    raise ThsHttpError("%s：%s" % (key, node.get("errormsg", "").strip()))
        return result

    def account(self) -> Dict[str, Any]:
        result = self._query("qryzijin|")
        return ((result.get("qryzijin") or {}).get("result") or {}).get("data") or {}

    def positions(self) -> List[Dict[str, Any]]:
        result = self._query("qryzijin|qryChicang|")
        items = ((result.get("qryChicang") or {}).get("result") or {}).get("list") or []
        positions = []
        for item in items:
            positions.append(
                {
                    "symbol": str(_pick(item, "d_2102", "stockcode") or ""),
                    "name": str(_pick(item, "d_2103", "stockname") or ""),
                    "shares": int(float(_pick(item, "d_2117", "balance") or 0)),
                    "available": int(float(_pick(item, "d_2121", "available") or 0)),
                    "price": float(_pick(item, "d_2122", "price") or 0),
                    "market": str(_pick(item, "d_2108", "mktype") or ""),
                    "account": str(_pick(item, "d_2106", "gdzh") or ""),
                    "mkcode": str(_pick(item, "d_2167", "mkcode") or ""),
                }
            )
        return positions

    def _list(self, key: str) -> List[Dict[str, Any]]:
        result = self._query("qryzijin|%s|" % key)
        return ((result.get(key) or {}).get("result") or {}).get("list") or []

    def orders(self) -> List[Dict[str, Any]]:
        """当日委托；接口在不同版本里叫 qryDelegated / qryChedan，按顺序尝试。"""
        for key in ("qryDelegated", "qryChedan"):
            try:
                items = self._list(key)
            except ThsHttpError:
                continue
            if items:
                return items
        return []

    def deals(self) -> List[Dict[str, Any]]:
        for key in ("qryChengjiao", "qryDeal"):
            try:
                items = self._list(key)
            except ThsHttpError:
                continue
            if items:
                return items
        return []

    def quote(self, symbol: str, side: str = "buy") -> Dict[str, Any]:
        cmd = "cmd_wt_mairu" if side == "buy" else "cmd_wt_maichu"
        payload = self._post("/qrystock/", {"type": cmd, "stockcode": symbol})
        return ((payload.get("result") or {}).get("data")) or {}

    @staticmethod
    def _shareholders(quote: Dict[str, Any]) -> Dict[str, str]:
        """从行情返回的 extend 字段解析市场 → 股东账号（gdzhlb*2,A547...;1,0017...;）。"""
        extend = str(quote.get("extend") or "")
        match = re.search(r"gdzhlb\*([^|]+)", extend)
        if not match:
            return {}
        result = {}
        for item in match.group(1).split(";"):
            if "," not in item:
                continue
            market, account = item.split(",", 1)
            market = market.strip()
            account = account.strip()
            if market and account:
                result[market] = account
        return result

    def _account_for(self, quote: Dict[str, Any], side: str) -> str:
        account = str(_pick(quote, "gdzh", "d_2106") or "")
        if account:
            return account
        market = str(_pick(quote, "mkcode", "d_2167") or "")
        shareholders = self._shareholders(quote)
        if market and market in shareholders:
            return shareholders[market]
        if market and market in self.accounts and self.accounts[market] not in ("1", "2"):
            return self.accounts[market]
        raise ThsHttpError(
            "拿不到 %s 市场的股东账号；请重新运行 login 导出账户信息。" % (market or "该")
        )

    def place(self, side: str, symbol: str, shares: int) -> Dict[str, Any]:
        quote = self.quote(symbol, side)
        price = float(_pick(quote, "st_price", "cur_price") or 0)
        market = str(_pick(quote, "mkcode", "d_2167") or "")
        if not market:
            raise ThsHttpError("行情接口没有返回市场代码：%s" % symbol)
        account = self._account_for(quote, side)
        sjwttype = str(_pick(quote, "sjwttype") or SJWT_TYPE.get(market, ""))
        if not sjwttype:
            raise ThsHttpError("不支持该市场（%s）的市价委托。" % market)
        cmd = "cmd_wt_mairu" if side == "buy" else "cmd_wt_maichu"
        payload = self._post(
            "/tradeStockSjwt/",
            {
                "type": cmd,
                "mkcode": market,
                "gdzh": account,
                "stockcode": symbol,
                "sjwttype": sjwttype,
                "amount": shares,
            },
        )
        payload["_quote_price"] = price
        return payload

    def cancel(self, order_id: str, order_date: str) -> Dict[str, Any]:
        return self._post("/cancelDelegated/", {"htbh": order_id, "wtrq": order_date})


def _positions_csv(rows: List[Dict[str, Any]]) -> str:
    lines = ["symbol,shares,price"]
    for row in rows:
        lines.append("%s,%d,%.2f" % (row["symbol"], row["shares"], row["price"]))
    return "\n".join(lines) + "\n"


def _quote_summary(quote: Dict[str, Any]) -> str:
    market = str(_pick(quote, "mkcode") or "")
    account = _pick(quote, "gdzh") or Client._shareholders(quote).get(market) or "?"
    return "最新价 %s，可买 %s，市场 %s，股东 %s" % (
        _pick(quote, "st_price", "cur_price") or "-",
        _pick(quote, "canbuy") or "-",
        market or "-",
        account,
    )


def command_status(args: argparse.Namespace) -> int:
    client = Client(Path(args.cookies))
    account = client.account()
    positions = client.positions()
    if args.json:
        print(json.dumps({"account": account, "positions": positions}, ensure_ascii=False, indent=2))
        return 0
    keys = ["kyje", "zjye", "zzc", "gpsz", "djje", "kqje"]
    labels = {"kyje": "可用余额", "zjye": "资金余额", "zzc": "总资产", "gpsz": "证券市值",
              "djje": "冻结余额", "kqje": "可取金额"}
    for key in keys:
        if key in account:
            print("%-6s %.2f" % (labels.get(key, key), float(account[key] or 0)))
    print("持仓 %d 只：" % len(positions))
    for row in positions:
        print("  %s %-6s %6d 股（可用 %d） 现价 %8.2f" % (
            row["symbol"], row["name"], row["shares"], row["available"], row["price"]
        ))
    return 0


def command_positions(args: argparse.Namespace) -> int:
    client = Client(Path(args.cookies))
    account = client.account()
    positions = client.positions()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_positions_csv(positions), encoding="utf-8")
    print("已写入 %s（%d 只持仓）" % (out, len(positions)))
    print("可用余额 %.2f 元" % float(account.get("kyje") or 0))
    return 0


def command_quote(args: argparse.Namespace) -> int:
    client = Client(Path(args.cookies))
    quote = client.quote(args.code, args.side)
    print(json.dumps(quote, ensure_ascii=False, indent=2))
    print(_quote_summary(quote))
    return 0


def command_orders(args: argparse.Namespace) -> int:
    client = Client(Path(args.cookies))
    print(json.dumps({"orders": client.orders(), "deals": client.deals()}, ensure_ascii=False, indent=2))
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    client = Client(Path(args.cookies))
    result = client.cancel(args.order_id, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _preflight(client: Client, orders: List[SheetOrder]) -> int:
    for order in orders:
        before = client.quote(order.symbol, order.side)
        if not before:
            print("失败：%s 没有行情" % order.symbol, file=sys.stderr)
            return 1
        price = float(_pick(before, "st_price", "cur_price") or 0)
        can_buy = int(float(_pick(before, "canbuy") or 0))
        print("自检 %s %s %s 股：%s" % (order.side, order.symbol, order.shares, _quote_summary(before)))
        if order.side == "buy" and can_buy and can_buy < order.shares:
            print("  警告：可买 %d 股 < 清单 %d 股（余额或整手限制）" % (can_buy, order.shares))
        if not price:
            print("  警告：行情没有最新价")
    print("自检完成：只查行情和账号，未提交委托。")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    orders = read_sheet(Path(args.orders))
    problems = validate_orders(orders)
    if problems:
        print("清单校验失败：", file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return 2
    client = Client(Path(args.cookies))
    return _preflight(client, orders)


def command_execute(args: argparse.Namespace) -> int:
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
    client = Client(Path(args.cookies))
    account = client.account()
    summary = {
        "可用余额": float(account.get("kyje") or 0),
        "总资产": float(account.get("zzc") or 0),
    }
    _print_plan(orders, summary)
    blocked = _check_affordable(orders, summary, skip=args.skip_affordability)
    if blocked:
        print("拒绝提交：" + blocked[0], file=sys.stderr)
        return 2
    if args.dry_run:
        return _preflight(client, orders)
    if not args.yes:
        print("\n未加 --yes，不执行；确认无误后重跑并加 --yes。", file=sys.stderr)
        return 2
    ordered = [o for o in orders if o.side == "sell"] + [o for o in orders if o.side == "buy"]
    results = []
    for order in ordered:
        try:
            result = client.place(order.side, order.symbol, order.shares)
        except ThsHttpError as exc:
            results.append({"order": order.__dict__, "ok": False, "error": str(exc)})
            print("失败：%s %s %s 股 — %s" % (order.side, order.symbol, order.shares, exc), file=sys.stderr)
            break
        results.append({"order": order.__dict__, "ok": True, "result": result})
        print("已提交：%s %s %s 股，返回 %s" % (
            order.side, order.symbol, order.shares,
            {k: v for k, v in result.items() if k != "_quote_price"},
        ))
        time.sleep(0.3)
    path = write_execution_log({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "orders_file": str(args.orders),
        "account_before": account,
        "results": results,
        "transport": "http",
    })
    print("执行日志：%s" % path)
    return 0 if all(item["ok"] for item in results) else 1


def command_order(args: argparse.Namespace) -> int:
    order = SheetOrder(
        symbol=str(args.code).zfill(6),
        name="",
        side="buy" if args.side in ("buy", "买入") else "sell",
        shares=int(args.shares),
        price=float(args.price),
        amount=float(args.price) * int(args.shares),
        fee=0.0,
    )
    problems = validate_orders([order])
    if problems:
        for problem in problems:
            print("拒绝：" + problem, file=sys.stderr)
        return 2
    client = Client(Path(args.cookies))
    account = client.account()
    summary = {"可用余额": float(account.get("kyje") or 0), "总资产": float(account.get("zzc") or 0)}
    blocked = _check_affordable([order], summary, skip=False)
    if blocked:
        print("拒绝：" + blocked[0], file=sys.stderr)
        return 2
    if not args.yes:
        print("未加 --yes，不执行。将提交：%s %s %s 股（市价）。" % (
            order.side, order.symbol, order.shares
        ))
        return 2
    result = client.place(order.side, order.symbol, order.shares)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    log = write_execution_log({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "order": order.__dict__, "result": result, "transport": "http"})
    print("执行日志：%s" % log)
    return 0


def command_login(args: argparse.Namespace) -> int:
    """用浏览器完成扫码登录，并把 Cookie/账户信息导出给 HTTP 客户端。"""
    import asyncio

    from paper_trader import ths_page

    async def run() -> int:
        platform = await ths_page.open_platform_page()
        if not await ths_page.is_logged_in(platform):
            print("在 Chrome 里打开同花顺登录页并生成二维码；请用同花顺 App 扫码。")
            await ths_page.login_with_qr(Path(args.qr_out), timeout=args.timeout)
        else:
            print("已检测到同花顺登录态，跳过扫码。")
        page = await ths_page.ensure_trading_page()
        account = account_summary(await ths_page.read_account(page))
        count = await ths_page.export_cookies(COOKIE_PATH)
        # 顺手记录市场 → 股东账号，HTTP 下单时用；接口自己返回 gdzh 时不依赖它。
        accounts = {}
        try:
            for option in await page.locator("select[name='gddm'] option").all():
                text = (await option.inner_text()).strip()
                for key, label in (("2", "沪A"), ("1", "深A")):
                    if label not in text:
                        continue
                    parts = text.split()
                    shareholder = parts[-1] if parts else ""
                    if re.fullmatch(r"[A-Za-z0-9]{6,}", shareholder):
                        accounts[key] = shareholder
        except Exception:
            pass
        data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        if accounts:
            data["accounts"] = accounts
            COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(COOKIE_PATH, 0o600)
        print("已导出 %d 条 Cookie 到 %s（账户 %s）。" % (count, COOKIE_PATH, accounts or "未知"))
        print("可用余额 %.2f 元。" % account.get("可用余额", 0.0))
        await ths_page.disconnect()
        return 0

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同花顺模拟盘 HTTP 直连（快速路径）")
    parser.add_argument("--cookies", default=str(COOKIE_PATH), help="Cookie 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="查资金与持仓")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=command_status)

    p_positions = sub.add_parser("positions", help="导出持仓 CSV（broker.py --positions）")
    p_positions.add_argument("--out", default="broker_orders/ths_positions.csv")
    p_positions.set_defaults(func=command_positions)

    p_quote = sub.add_parser("quote", help="查单只股票行情")
    p_quote.add_argument("--code", required=True)
    p_quote.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_quote.set_defaults(func=command_quote)

    p_orders = sub.add_parser("orders", help="查当日委托和成交")
    p_orders.set_defaults(func=command_orders)

    p_cancel = sub.add_parser("cancel", help="撤单")
    p_cancel.add_argument("--order-id", required=True)
    p_cancel.add_argument("--date", required=True, help="委托日期，如 20261117")
    p_cancel.set_defaults(func=command_cancel)

    p_pre = sub.add_parser("preflight", help="只查行情/账号，不提交")
    p_pre.add_argument("--orders", required=True)
    p_pre.set_defaults(func=command_preflight)

    p_exec = sub.add_parser("execute", help="按清单市价下单")
    p_exec.add_argument("--orders", required=True)
    p_exec.add_argument("--yes", action="store_true")
    p_exec.add_argument("--dry-run", action="store_true")
    p_exec.add_argument("--max-orders", type=int, default=60)
    p_exec.add_argument("--skip-affordability", action="store_true")
    p_exec.set_defaults(func=command_execute)

    p_order = sub.add_parser("order", help="手工下一笔市价单")
    p_order.add_argument("--side", choices=["buy", "sell", "买入", "卖出"], required=True)
    p_order.add_argument("--code", required=True)
    p_order.add_argument("--shares", type=int, required=True)
    p_order.add_argument("--price", type=float, required=True, help="参考价（余额检查用）")
    p_order.add_argument("--yes", action="store_true")
    p_order.set_defaults(func=command_order)

    p_login = sub.add_parser("login", help="浏览器扫码登录并导出 Cookie（一次性）")
    p_login.add_argument("--qr-out", default=str(Path.home() / ".config/paper-trader/ths-login-qr.png"))
    p_login.add_argument("--timeout", type=int, default=1800)
    p_login.set_defaults(func=command_login)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ThsHttpError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:  # ths_page.ThsError：浏览器/登录问题
        print("失败：%s" % exc, file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
