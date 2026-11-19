"""多模型实盘跟踪：把选定模型的 3 万子账本落到同花顺共享账户，并生成实时收益页。

数据流：
- ``orders.json``：启动时按 3 万目标提交的买单（合约号 → 模型）；
- 同花顺 ``deals``（当日成交）：按合约号归属到模型，更新子账本的现金/持仓；
- 腾讯行情：给子账本持仓估值，算净值、收益率、当日盈亏；
- ``web/live.json`` + ``web/live.html``：部署到 token 门禁的腾讯云站点。

浏览器不参与日常运行：只用 ``paper_trader.ths_http`` 的 HTTP 接口。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from paper_trader import ths_http

ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = Path.home() / ".config" / "paper-trader" / "live"
ORDERS_PATH = LIVE_DIR / "orders.json"
LEDGER_PATH = LIVE_DIR / "ledger.json"
LIVE_JSON = ROOT / "web" / "live.json"
LIVE_HTML = ROOT / "web" / "live.html"
DEPLOY_ENV = Path.home() / ".config" / "paper-trader" / "deploy.env"
# 日常 deploy.sh 会 rsync --delete 这个工作区的 web/，把 live 页也放进去，避免被删掉
DEPLOY_WEB = Path(os.environ.get(
    "PAPER_WEB_DIR", Path.home() / ".superagent-work" / "paper-trading-platform-fe9b2dd99152" / "web"
))

CAPITAL = 30000.0
BUY_FEE = 0.001
SELL_FEE = 0.002
MIN_FEE = 5.0


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def init_ledger() -> Dict[str, Any]:
    """从 orders.json 初始化子账本（只做一次，之后保留成交记录）。"""
    existing = _load_json(LEDGER_PATH)
    if existing:
        return existing
    orders = _load_json(ORDERS_PATH)
    if not orders:
        raise SystemExit("缺少 %s；先运行启动下单脚本。" % ORDERS_PATH)
    ledger = {
        "started_at": orders.get("started_at"),
        "capital": float(orders.get("capital") or CAPITAL),
        "models": {},
    }
    for model_id, model in orders["models"].items():
        contracts = {}
        for order in model.get("orders", []):
            if order.get("htbh"):
                contracts[order["htbh"]] = {"symbol": order["symbol"], "shares": order.get("shares", 0)}
        ledger["models"][model_id] = {
            "family": model.get("family", ""),
            "name": model.get("name", model_id),
            "cash": float(ledger["capital"]),
            "positions": {},
            "fills": [],
            "contracts": contracts,
            "orders": len(model.get("orders", [])),
        }
    _write_json(LEDGER_PATH, ledger)
    return ledger


def sync(client: Optional[ths_http.Client] = None) -> Dict[str, Any]:
    """把当日成交归属到子账本；重复成交按成交编号去重。"""
    client = client or ths_http.Client()
    ledger = init_ledger()
    seen = {fill["deal_id"] for model in ledger["models"].values() for fill in model["fills"]}
    contract_map = {}
    for model_id, model in ledger["models"].items():
        for contract, info in model["contracts"].items():
            contract_map[contract] = model_id
    added = 0
    for deal in client.deals():
        deal_id = str(deal.get("d_2130") or "")
        if not deal_id or deal_id in seen:
            continue
        contract = str(deal.get("d_2135") or "")
        model_id = contract_map.get(contract)
        if model_id is None:
            continue
        model = ledger["models"][model_id]
        symbol = str(deal.get("d_2102") or "")
        side = "buy" if str(deal.get("d_2109")) == "买入" else "sell"
        shares = int(float(deal.get("d_2128") or 0))
        price = float(deal.get("d_2129") or 0)
        amount = float(deal.get("d_2131") or 0) or shares * price
        fee = max(amount * (BUY_FEE if side == "buy" else SELL_FEE), MIN_FEE) if amount else 0.0
        position = model["positions"].setdefault(
            symbol, {"name": str(deal.get("d_2103") or ""), "shares": 0, "cost": 0.0}
        )
        if side == "buy":
            model["cash"] -= amount + fee
            position["shares"] += shares
            position["cost"] += amount + fee
        else:
            model["cash"] += amount - fee
            position["shares"] -= shares
        if position["shares"] <= 0:
            model["positions"].pop(symbol, None)
        model["fills"].append(
            {
                "deal_id": deal_id,
                "time": str(deal.get("d_2142") or ""),
                "symbol": symbol,
                "name": str(deal.get("d_2103") or ""),
                "side": side,
                "shares": shares,
                "price": price,
                "amount": amount,
                "fee": round(fee, 2),
            }
        )
        seen.add(deal_id)
        added += 1
    _write_json(LEDGER_PATH, ledger)
    return {"added": added, "ledger": ledger}


def _quote_prices(client: ths_http.Client, symbols: List[str]) -> Dict[str, Dict[str, float]]:
    result = {}
    for symbol in symbols:
        try:
            quote = client.quote(symbol)
        except ths_http.ThsHttpError:
            continue
        price = float(quote.get("st_price") or 0)
        prev = float(quote.get("st_pclose") or 0)
        if price:
            result[symbol] = {"price": price, "prev_close": prev}
    return result


def value(client: Optional[ths_http.Client] = None) -> Dict[str, Any]:
    """用最新行情给每个子账本估值，写 web/live.json。"""
    client = client or ths_http.Client()
    ledger = sync(client)["ledger"]
    symbols = sorted({
        symbol
        for model in ledger["models"].values()
        for symbol in model["positions"].keys()
    })
    quotes = _quote_prices(client, symbols)
    account = client.account()
    account_positions = client.positions()
    account_mv = sum(float(p["price"]) * p["shares"] for p in account_positions)
    models = []
    for model_id, model in ledger["models"].items():
        rows = []
        market_value = 0.0
        today_pnl = 0.0
        for symbol, position in model["positions"].items():
            quote = quotes.get(symbol, {})
            price = float(quote.get("price") or 0)
            prev = float(quote.get("prev_close") or price)
            value_ = price * position["shares"]
            market_value += value_
            if prev:
                today_pnl += (price - prev) * position["shares"]
            rows.append(
                {
                    "symbol": symbol,
                    "name": position.get("name", ""),
                    "shares": position["shares"],
                    "price": round(price, 2),
                    "value": round(value_, 2),
                    "cost": round(position["cost"], 2),
                    "pnl": round(value_ - position["cost"], 2),
                }
            )
        rows.sort(key=lambda row: row["value"], reverse=True)
        equity = model["cash"] + market_value
        models.append(
            {
                "id": model_id,
                "name": model.get("name", model_id),
                "family": model.get("family", ""),
                "capital": ledger["capital"],
                "cash": round(model["cash"], 2),
                "equity": round(equity, 2),
                "ret_pct": round((equity / ledger["capital"] - 1) * 100, 2),
                "today_pnl": round(equity - ledger["capital"], 2),
                "market_value": round(market_value, 2),
                "positions": rows,
                "fills": len(model["fills"]),
            }
        )
    models.sort(key=lambda item: item["ret_pct"], reverse=True)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": ledger.get("started_at"),
        "capital": ledger["capital"],
        "account": {
            "total": float(account.get("zzc") or 0),
            "cash": float(account.get("kyje") or 0),
            "frozen": float(account.get("djje") or 0),
            "market_value": round(account_mv, 2),
        },
        "models": models,
    }
    _write_json(LIVE_JSON, payload)
    return payload


def deploy() -> int:
    """把 live.html/live.json 推到腾讯云 token 路径。"""
    env = {}
    for line in DEPLOY_ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip()
    token = env.get("PAPER_TOKEN")
    host = env.get("PAPER_HOST", "tencent")
    if not token:
        raise SystemExit("deploy.env 缺少 PAPER_TOKEN")
    remote_dir = "/srv/paper/%s" % token
    for path in (LIVE_HTML, LIVE_JSON):
        subprocess.run(["rsync", "-az", str(path), "%s:%s/" % (host, remote_dir)], check=True)
    # 同步到部署工作区，让每日 deploy.sh（rsync --delete web/）保留 live 页
    if DEPLOY_WEB.is_dir():
        for path in (LIVE_HTML, LIVE_JSON):
            try:
                shutil.copy2(path, DEPLOY_WEB / path.name)
            except OSError:
                pass
    subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "chmod a+r %s/live.html %s/live.json" % (remote_dir, remote_dir)], check=True)
    domain = env.get("PAPER_DOMAIN", "")
    print("https://%s/%s/live.html" % (domain, token))
    return 0


def command_run(args: argparse.Namespace) -> int:
    payload = value()
    summary = " | ".join("%s %+.2f%%" % (m["name"], m["ret_pct"]) for m in payload["models"])
    print("%s  %s" % (payload["updated_at"], summary))
    if args.deploy:
        deploy()
    return 0


def command_sync(args: argparse.Namespace) -> int:
    result = sync()
    print("新增成交 %d 笔" % result["added"])
    return 0


def command_value(args: argparse.Namespace) -> int:
    payload = value()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多模型实盘跟踪（同花顺共享账户）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="同步成交 + 估值 + 可选部署")
    p_run.add_argument("--deploy", action="store_true")
    p_run.set_defaults(func=command_run)
    p_sync = sub.add_parser("sync", help="只同步成交")
    p_sync.set_defaults(func=command_sync)
    p_value = sub.add_parser("value", help="只估值并输出 live.json")
    p_value.set_defaults(func=command_value)
    p_deploy = sub.add_parser("deploy", help="部署 live.html/live.json")
    p_deploy.set_defaults(func=lambda args: deploy())
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ths_http.ThsHttpError as exc:
        print("失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
