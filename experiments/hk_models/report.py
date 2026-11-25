"""Render the Hong Kong results tables and docs/HK.md from local artifacts."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
DATA = Path(os.environ.get("HK_DATA_DIR", ROOT / "data" / "hk"))
RESULTS = HERE / "results"


def pct(value, digits=2):
    return f"{float(value):+.{digits}f}%"


def num(value, digits=3):
    return "-" if value is None else f"{float(value):.{digits}f}"


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((DATA / "metadata.json").read_text())
    metrics = json.loads((DATA / "artifacts" / "metrics.json").read_text())
    hk = json.loads((ROOT / "data" / "hk_family.json").read_text())
    evaluation = json.loads((ROOT / "data" / "hk_eval_family.json").read_text())

    rows = []
    for route, result in metrics.items():
        seed_ics = [seed["ic"]["mean_ic"] for seed in result["seeds"].values()]
        seed_ics = [value for value in seed_ics if value is not None]
        ensemble = result["ensemble"]
        rows.append({
            "route": route,
            "model": next(item["name"] for item in hk["models"] if item["id"].endswith(route.replace("_", "-")) or
                          item["id"] == "hk-" + route.replace("_", "-")),
            "parameters": result["parameters"],
            "seed_mean_ic": sum(seed_ics) / len(seed_ics) if seed_ics else 0.0,
            "seed_std_ic": pd.Series(seed_ics).std(ddof=1) if len(seed_ics) > 1 else 0.0,
            "ensemble_ic": ensemble["ic"]["mean_ic"],
            "ensemble_icir": ensemble["ic"]["icir"],
            "decile": (ensemble.get("decile_spread") or {}).get("mean"),
            "train_seconds": result["train_seconds"],
            "best_epoch": sum(seed["best_epoch"] or 0 for seed in result["seeds"].values())
                          / max(len(result["seeds"]), 1),
        })
    summary = pd.DataFrame(rows)
    summary.to_csv(RESULTS / "model_summary.csv", index=False)
    (RESULTS / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    evaluation_rows = pd.DataFrame(evaluation["eval"]["rows"])
    evaluation_rows.to_csv(RESULTS / "eval_summary.csv", index=False)
    pd.DataFrame(evaluation["eval"]["cost_sensitivity"]).to_csv(
        RESULTS / "cost_sensitivity.csv", index=False)
    history_path = DATA / "artifacts" / "training_history.csv"
    if history_path.exists():
        shutil.copy2(history_path, RESULTS / "training_history.csv")

    lines = [
        "# 港股模型线 · 结果与口径",
        "",
        "> 模拟盘/研究结果，不构成任何投资建议。数据由腾讯行情与港交所名单生成，",
        "> 测试段 2024-01-02–2026-11-25 已被查看，不是严格前向证据。",
        "",
        "## 一句话结论",
        "",
        "同一套 25 维日线特征、60 日窗口、5 日截面排序与四条主网络结构，在港股主板 "
        f"{metadata['stock_count']} 只股票上重训后，测试段 Rank IC 约为 "
        f"{summary.ensemble_ic.min():.3f}–{summary.ensemble_ic.max():.3f}；"
        "组合收益与回撤见下表。港股整手金额大，10 万港元档只能持有少量股票；"
        "费用按汇丰 Trade25 口径计入，1× 与 0×/2× 的差异单独列出。",
        "",
        "## 数据与股票池",
        "",
        f"- 股票池：{metadata['stock_count']} 只港交所主板 HKD 股票；"
        f"{metadata['actual_start']} → {metadata['actual_end']}，{metadata['date_count']} 个交易日、"
        f"{metadata['feature_count']} 维特征。",
        "- 名单：港交所 `ListOfSecurities`；日线：腾讯行情后复权（特征/标签）+ 不复权（成交/估值）。",
        "- 筛选：当前市值 ≥ 20 亿港元、近 500 日中位成交额 ≥ 300 万港元、"
        "2014-06 前有历史、排除复权序列无法表达公司行为的股票。",
        "- 局限：当前名单存在幸存者偏差；港股 T+2、部分成交、VCM 用日线近似。",
        "",
        "## 训练/验证/测试",
        "",
        "| 集合 | 信号区间 | 股票-窗口样本 |",
        "| --- | --- | ---: |",
    ]
    for name in ("train", "validation", "test"):
        split = metadata["splits"][name]
        lines.append(f"| {name} | {split['signal_start']} → {split['signal_end']} | {split['samples']:,} |")
    lines += [
        "",
        "训练 2016–2021、验证 2022–2023、测试 2024-01-02–2026-11-25；早停按验证 Rank IC 选择最优轮次。",
        "",
        "## 模型测试段表现",
        "",
        "| 模型 | 参数量 | 种子均值 Rank IC | 种子标准差 | 集成 Rank IC | ICIR | 十分位多空差 | 平均最优轮次 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.itertuples():
        lines.append(
            f"| {row.model} | {int(row.parameters):,} | {row.seed_mean_ic:.4f} | {row.seed_std_ic:.4f} | "
            f"{row.ensemble_ic:.4f} | {num(row.ensemble_icir, 2)} | "
            f"{(row.decile * 100 if row.decile is not None else 0):+.2f}% | {row.best_epoch:.1f} |")
    lines += [
        "",
        "## 组合回测 · 2024-01-02–2026-11-25 · 100 万港元",
        "",
        "| 模型 | 区间收益 | 年化 | 最大回撤 | 夏普 | 年换手 | 年费用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in evaluation_rows[evaluation_rows.type.eq("model")].itertuples():
        lines.append(
            f"| {row.name} | {pct(row.ret_pct)} | {pct(row.annualized)} | {pct(row.max_drawdown)} | "
            f"{row.sharpe:.2f} | {row.annualized_turnover:.1f}x | {pct(row.annualized_cost)} |")
    for row in evaluation_rows[evaluation_rows.type.eq("benchmark")].itertuples():
        lines.append(
            f"| {row.name}（对照） | {pct(row.ret_pct)} | {pct(row.annualized)} | {pct(row.max_drawdown)} | "
            f"{row.sharpe:.2f} | - | - |")
    lines += [
        "",
        "## 费用敏感性 · GRU+Attention · 100 万港元",
        "",
        "| 情景 | 区间收益 | 年化 | 最大回撤 | 夏普 | 年费用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in evaluation["eval"]["cost_sensitivity"]:
        lines.append(
            f"| {row['label']} | {pct(row['ret_pct'])} | {pct(row['annualized'])} | "
            f"{pct(row['max_drawdown'])} | {row['sharpe']:.2f} | {pct(row['annualized_cost'])} |")
    lines += [
        "",
        "## 港股交易制度与费用口径",
        "",
        "- 每手股数来自港交所名单；买入按整手向下取整；卖出按整手，零股视为不可交易。",
        "- 无涨跌停限制；T+2 交收按日线近似；未建模 VCM、部分成交与流动性冲击。",
        "- 汇丰 Trade25：每月 25 港元月费；当月成交 ≤ 25 万港元 0 佣金，超过后 0.25%，",
        "  每单最低 100 港元；跨过门槛的那一单按全额 0.25% 计。",
        "- 印花税 0.1%（向上取整到 1 港元）、HKEX 交易费 0.00565%、SFC 征费 0.0027%、",
        "  AFRC 征费 0.00015%、CCASS 结算费 0.002%（最低 2、最高 100 港元）。",
        "",
        "## 网页与截图",
        "",
        "- 新增两个分页：`港股 · 自训模型`（4 模型 × 4 档港元本金）与",
        "  `港股 · 模型评测`（测试段 IC、收益/回撤/夏普/换手/费用、费用敏感性）。",
        "- 分页截图：`docs/screenshots/hk-before-desktop.png`（改前，三档 A 股）、",
        "  `docs/screenshots/hk-after-desktop.png`（改后，港股自训）、",
        "  `docs/screenshots/hk-eval-after-desktop.png`（改后，港股评测）。",
        "- 生成顺序：`fetch` → `prepare` → `train` → `backtest` → `paper_trader.runner export`。",
        "",
    ]
    (RESULTS / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {RESULTS / 'RESULTS.md'} and result tables")


if __name__ == "__main__":
    main()
