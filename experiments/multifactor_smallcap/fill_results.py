"""Fill the README / docs placeholders from lab results (run after training)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
REPO = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[2]))
report = json.loads((ROOT / "results" / "metrics.json").read_text(encoding="utf-8"))
factor_meta = json.loads((ROOT / "factors" / "factors_meta.json").read_text(encoding="utf-8"))
names = {"mf_lgbm": "多因子 LightGBM", "mf_equal": "多因子等权", "mf_size": "纯小市值"}
capitals = ["10000", "30000", "50000", "100000"]


def row(model: str) -> str:
    info = report["models"][model]
    ic = info["test_ic"]
    metrics = info["backtest"]["100000"]["metrics"]
    years = len(info["backtest"]["100000"]["equity_curve"]) / 252.0
    turnover = metrics["turnover"] / years if years else 0.0
    return (
        f"| {names[model]} | {ic['ic_mean']:.4f} | {ic['icir']:.2f} | {ic['ic_positive']*100:.0f}% | "
        f"{metrics['ret_pct']:+.2f}% | {metrics['max_drawdown']:.2f}% | {metrics['sharpe']:.2f} | "
        f"{turnover:.2f}× | {metrics['trades']} |"
    )


table = [
    "| 变体 | 测试 Rank IC | ICIR | 正 IC 月占比 | 10 万累计 | 最大回撤 | 夏普 | 年化换手 | 成交 |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    row("mf_lgbm"),
    row("mf_equal"),
    row("mf_size"),
]
table_text = "\n".join(table)

cap_table = [
    "| 变体 | 1 万 | 3 万 | 5 万 | 10 万 |",
    "| --- | ---: | ---: | ---: | ---: |",
]
for model in ("mf_lgbm", "mf_equal", "mf_size"):
    values = [report["models"][model]["backtest"][cap]["metrics"]["ret_pct"] for cap in capitals]
    cap_table.append(f"| {names[model]} | " + " | ".join(f"{value:+.2f}%" for value in values) + " |")
cap_text = "\n".join(cap_table)

factor_lines = ["| 因子 | 测试 IC |", "| --- | ---: |"]
for name, value in factor_meta["factor_test_ic"].items():
    factor_lines.append(f"| {name} | {value:.4f} |")
factor_text = "\n".join(factor_lines)

ic = {model: report["models"][model]["test_ic"]["ic_mean"] for model in names}
best = max(("mf_lgbm", "mf_equal", "mf_size"), key=lambda m: report["models"][m]["test_ic"]["ic_mean"])
ret = {model: report["models"][model]["backtest"]["100000"]["metrics"]["ret_pct"] for model in names}
summary = (
    "2024-01-02 – 2026-11-12 的样本外回放里，小市值池内的 14 因子模型有很强的截面信号，但小资金组合很难把它变现：\n\n"
    f"- 测试 Rank IC：多因子 LightGBM **{ic['mf_lgbm']:.4f}**、多因子等权 **{ic['mf_equal']:.4f}**、纯小市值 **{ic['mf_size']:.4f}**；\n"
    f"- 10 万本金累计收益：LightGBM **{ret['mf_lgbm']:+.2f}%**、等权 **{ret['mf_equal']:+.2f}%**、纯小市值 **{ret['mf_size']:+.2f}%**；"
    f"同期全市场等权基准 **{report['benchmark']['market_return']:+.2f}%**（沪深300 参照）；\n"
    "- 结论要等具体表格：如果 IC 为正但组合收益被 2024 年初的小微盘下跌和换手成本吃掉，说明这条路线在当前数据上「有信号、难变现」；\n"
    "- 与 Share v2 的 5 条深度路线相比，口径不同（全 A 股小市值池 vs 沪深300、月度 vs 5 日），只能看方向和量级，不能直接比收益。"
)
pr_block = (
    f"- 测试 Rank IC：LightGBM {ic['mf_lgbm']:.4f}，等权 {ic['mf_equal']:.4f}，纯小市值 {ic['mf_size']:.4f}（2024-01–2026-10，小市值池内）\n"
    f"- 10 万本金累计收益：LightGBM {ret['mf_lgbm']:+.2f}%，等权 {ret['mf_equal']:+.2f}%，纯小市值 {ret['mf_size']:+.2f}%（2024-01-02–2026-11-12）\n"
    f"- 同期全市场等权基准 {report['benchmark']['market_return']:+.2f}%\n\n"
    "| 变体 | 测试 Rank IC | 10 万累计 | 最大回撤 | 夏普 |\n| --- | ---: | ---: | ---: | ---: |\n"
    + "\n".join(
        f"| {names[model]} | {report['models'][model]['test_ic']['ic_mean']:.4f} | "
        f"{report['models'][model]['backtest']['100000']['metrics']['ret_pct']:+.2f}% | "
        f"{report['models'][model]['backtest']['100000']['metrics']['max_drawdown']:.2f}% | "
        f"{report['models'][model]['backtest']['100000']['metrics']['sharpe']:.2f} |"
        for model in names
    )
)

readme = (REPO / "experiments/multifactor_smallcap/README.md").read_text(encoding="utf-8")
readme = readme.replace("<!--RESULTS_SUMMARY-->", summary)
readme = readme.replace("<!--RESULTS_TABLE-->", table_text + "\n\n**四档本金累计收益**\n\n" + cap_text)
readme = readme.replace("<!--FACTOR_IC-->", "**单因子测试 Rank IC**\n\n" + factor_text)
(REPO / "experiments/multifactor_smallcap/README.md").write_text(readme, encoding="utf-8")

results_doc = (REPO / "docs/RESULTS.md").read_text(encoding="utf-8")
block = (
    f"- 区间：2024-01-02 → 2026-11-12；3 个变体 × 4 档本金 = 12 个组合\n"
    f"- 测试 Rank IC：LightGBM {ic['mf_lgbm']:.4f}、等权 {ic['mf_equal']:.4f}、纯小市值 {ic['mf_size']:.4f}\n"
    f"- 10 万累计：LightGBM {ret['mf_lgbm']:+.2f}%、等权 {ret['mf_equal']:+.2f}%、纯小市值 {ret['mf_size']:+.2f}%\n"
    f"- 全市场等权基准同期：{report['benchmark']['market_return']:+.2f}%\n\n"
    + table_text
)
results_doc = results_doc.replace("<!--MF_RESULTS-->", block)
(REPO / "docs/RESULTS.md").write_text(results_doc, encoding="utf-8")

(ROOT / "results" / "pr_block.md").write_text(pr_block, encoding="utf-8")
print(pr_block)
print("\n--- written ---")
