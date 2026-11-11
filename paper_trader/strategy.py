"""Market-context prompts for the daily strategy decisions."""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Dict, List, Optional

from . import config, store


def _pct(current: float, base: float) -> float:
    if not base:
        return 0.0
    return (current / base - 1.0) * 100.0


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / (len(values) - 1))


def features(bars: List[sqlite3.Row]) -> Dict[str, float]:
    closes = [float(row["close"]) for row in bars]
    volumes = [float(row["volume"]) for row in bars]
    if len(closes) < 2:
        return {}
    last = closes[-1]
    daily = [closes[i] / closes[i - 1] - 1.0 for i in range(max(1, len(closes) - 21), len(closes))]
    ma = lambda n: _mean(closes[-n:]) if len(closes) >= n else _mean(closes)
    vol_ratio = volumes[-1] / _mean(volumes[-21:-1]) if len(volumes) >= 21 and _mean(volumes[-21:-1]) else 1.0
    return {
        "close": last,
        "chg1": _pct(last, closes[-2]),
        "chg5": _pct(last, closes[-6]) if len(closes) >= 6 else 0.0,
        "chg20": _pct(last, closes[-21]) if len(closes) >= 21 else 0.0,
        "ma5": ma(5),
        "ma20": ma(20),
        "ma60": ma(60),
        "ma20_bias": _pct(last, ma(20)),
        "ma60_bias": _pct(last, ma(60)),
        "vol20": _stdev(daily) * math.sqrt(252) * 100.0,
        "vol_ratio": vol_ratio,
    }


def market_snapshot(conn: sqlite3.Connection, date: str) -> Dict:
    indices = []
    for item in config.INDICES:
        bars = store.bars_for(conn, item["code"], end_date=date, limit=130)
        if not bars:
            continue
        feat = features(bars)
        indices.append(
            {
                "code": item["code"],
                "name": item["name"],
                "close": feat["close"],
                "chg1": feat["chg1"],
                "chg5": feat["chg5"],
                "chg20": feat["chg20"],
                "spark": [round(float(b["close"]), 2) for b in bars[-30:]],
            }
        )
    up = down = 0
    rows = []
    for item in config.UNIVERSE:
        bars = store.bars_for(conn, item["code"], end_date=date, limit=80)
        if len(bars) < 21:
            continue
        feat = features(bars)
        if feat["chg1"] > 0:
            up += 1
        elif feat["chg1"] < 0:
            down += 1
        rows.append({"item": item, "feat": feat})
    rows.sort(key=lambda row: row["feat"]["chg1"], reverse=True)
    return {"indices": indices, "up": up, "down": down, "rows": rows}


def _fmt(value: float, digits: int = 2) -> str:
    return ("%%.%df" % digits) % value


def holdings_view(
    conn: sqlite3.Connection, portfolio_id: str, date: str
) -> List[Dict]:
    positions = store.get_positions(conn, portfolio_id)
    out = []
    if not positions:
        return out
    prices = store.bars_on(conn, date, [p["symbol"] for p in positions])
    for pos in positions:
        bar = prices.get(pos["symbol"])
        if not bar:
            continue
        value = pos["shares"] * float(bar["close"])
        out.append(
            {
                "symbol": pos["symbol"],
                "shares": int(pos["shares"]),
                "avg_cost": float(pos["avg_cost"]),
                "price": float(bar["close"]),
                "value": value,
                "pnl_pct": _pct(float(bar["close"]), float(pos["avg_cost"])),
            }
        )
    return out


def build_prompt(
    conn: sqlite3.Connection, model_id: str, date: str, reference_portfolio: str
) -> Dict[str, str]:
    snap = market_snapshot(conn, date)
    indices = snap["indices"]
    rows = snap["rows"]
    holdings = holdings_view(conn, reference_portfolio, date)

    total = sum(pos["value"] for pos in holdings)
    cash_weight = 1.0
    eq = conn.execute(
        "SELECT cash, total FROM equity WHERE portfolio_id = ? AND date = ?",
        (reference_portfolio, date),
    ).fetchone()
    if eq and eq["total"]:
        total = float(eq["total"])
        cash_weight = float(eq["cash"]) / total

    lines = []
    lines.append("【市场概览 %s 收盘】" % date)
    for idx in indices:
        lines.append(
            "- %s：%.2f，日涨跌 %+.2f%%，5日 %+.2f%%，20日 %+.2f%%"
            % (
                idx["name"],
                idx["close"],
                idx["chg1"],
                idx["chg5"],
                idx["chg20"],
            )
        )
    lines.append(
        "- 候选池涨跌家数：上涨 %d / 下跌 %d（共 %d 只）"
        % (snap["up"], snap["down"], len(rows))
    )
    top = rows[:5]
    bottom = rows[-5:]
    lines.append(
        "- 今日领涨："
        + "，".join(
            "%s(%s) %+.1f%%" % (r["item"]["name"], r["item"]["symbol"], r["feat"]["chg1"])
            for r in top
        )
    )
    lines.append(
        "- 今日领跌："
        + "，".join(
            "%s(%s) %+.1f%%" % (r["item"]["name"], r["item"]["symbol"], r["feat"]["chg1"])
            for r in bottom
        )
    )

    lines.append("")
    lines.append("【候选池（仅可买这些，收盘数据）】")
    lines.append(
        "代码|名称|行业|收盘|日%|5日%|20日%|MA20乖离%|20日年化波动%|量比"
    )
    for row in rows:
        item, feat = row["item"], row["feat"]
        lines.append(
            "%s|%s|%s|%.2f|%+.2f|%+.2f|%+.2f|%+.2f|%.1f|%.2f"
            % (
                item["symbol"],
                item["name"],
                item["sector"],
                feat["close"],
                feat["chg1"],
                feat["chg5"],
                feat["chg20"],
                feat["ma20_bias"],
                feat["vol20"],
                feat["vol_ratio"],
            )
        )

    lines.append("")
    lines.append("【参考组合（10万本金账户）当前持仓，按收盘价】")
    if holdings:
        for pos in holdings:
            weight = pos["value"] / total if total else 0
            lines.append(
                "- %s：%d股，成本 %.2f，现价 %.2f，浮盈亏 %+.1f%%，权重 %.1f%%"
                % (
                    pos["symbol"],
                    pos["shares"],
                    pos["avg_cost"],
                    pos["price"],
                    pos["pnl_pct"],
                    weight * 100,
                )
            )
    else:
        lines.append("- 空仓")
    lines.append("- 现金权重约 %.1f%%" % (cash_weight * 100))

    total_weight = sum(
        (pos["value"] / total if total else 0) for pos in holdings
    )
    lines.append(
        "当前股票总仓位约 %.1f%%，本组合目标权重由你决定。"
        % (total_weight * 100)
    )

    lines.append("")
    lines.append("【交易规则】")
    lines.append(
        "- A股日线，T日收盘后决策，T+1日开盘价成交；100股一手；不能加杠杆、不能做空。"
    )
    lines.append(
        "- 最多 %d 只股票，单票目标权重不超过 %.0f%%，权重合计不超过 100%%，其余为现金。"
        % (config.MAX_POSITIONS, config.MAX_WEIGHT * 100)
    )
    lines.append(
        "- 佣金万2.5（最低5元）、卖出印花税万5、过户费万0.1，频繁交易会侵蚀收益。"
    )
    lines.append(
        "- 只能使用上面给出的数据，不要臆测未来，不要引用未给出的新闻或消息。"
    )
    lines.append(
        "- 1万本金账户只能买得起100股单价低于约100元的股票，请考虑小资金的可执行性。"
    )
    lines.append("")
    lines.append("【输出格式】严格输出一个 JSON 对象，不要 Markdown 代码块：")
    lines.append(
        json.dumps(
            {
                "market_view": "一句话市场判断（不超过80字）",
                "confidence": 0.65,
                "targets": [
                    {"symbol": "600519", "weight": 0.2, "reason": "不超过40字的理由"}
                ],
            },
            ensure_ascii=False,
        )
    )
    lines.append(
        "targets 是目标权重而非买卖指令；weight=0.2 表示希望该股票占总资产20%。不持有的股票不要出现在 targets 中。"
    )

    system = (
        "你是一位管理真金白银的A股日线基金经理，风格稳健、重视回撤与行业分散。"
        "你只能依据用户提供的行情数据做决策，禁止编造事实。"
        "你的输出必须是可被程序解析的 JSON，不能包含 JSON 以外的文字。"
        "投资有风险，但你的任务是最大化风险调整后的收益，而不是永远空仓。"
    )
    return {"system": system, "user": "\n".join(lines)}
