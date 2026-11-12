"""Render the experiment results as Markdown tables for the README."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("MF_ROOT", "/mnt/storage1/syk/experiments/Share/multifactor"))
RESULTS = ROOT / "results"
FACTORS = ROOT / "factors"


def main() -> None:
    report = json.loads((RESULTS / "metrics.json").read_text(encoding="utf-8"))
    factor_meta = json.loads((FACTORS / "factors_meta.json").read_text(encoding="utf-8"))
    names = {
        "mf_lgbm": "多因子 LightGBM",
        "mf_equal": "多因子等权",
        "mf_size": "纯小市值",
    }
    capitals = ["10000", "30000", "50000", "100000"]
    print("## 模型与组合（10 万本金）\n")
    print("| 变体 | 测试 Rank IC | ICIR | 正 IC 月占比 | 10 万累计 | 最大回撤 | 夏普 | 年化换手 | 成交 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for model in ("mf_lgbm", "mf_equal", "mf_size"):
        info = report["models"][model]
        ic = info["test_ic"]
        back = info["backtest"]["100000"]
        metrics = back["metrics"]
        curve = back["equity_curve"]
        years = (len(curve) / 252.0) if curve else 1.0
        turnover = metrics["turnover"] / years if years else 0.0
        print(
            f"| {names[model]} | {ic['ic_mean']:.4f} | {ic['icir']:.2f} | {ic['ic_positive']*100:.0f}% | "
            f"{metrics['ret_pct']:+.2f}% | {metrics['max_drawdown']:.2f}% | {metrics['sharpe']:.2f} | "
            f"{turnover:.2f}× | {metrics['trades']} |"
        )
    print("\n## 四档本金累计收益\n")
    print("| 变体 | 1 万 | 3 万 | 5 万 | 10 万 |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for model in ("mf_lgbm", "mf_equal", "mf_size"):
        values = [report["models"][model]["backtest"][cap]["metrics"]["ret_pct"] for cap in capitals]
        print(f"| {names[model]} | " + " | ".join(f"{value:+.2f}%" for value in values) + " |")
    print("\n## 单因子测试 Rank IC（小市值池内）\n")
    print("| 因子 | 测试 IC |")
    print("| --- | ---: |")
    for name, value in factor_meta["factor_test_ic"].items():
        print(f"| {name} | {value:.4f} |")
    print("\n## 分位收益（10 万，多因子 LightGBM）\n")
    deciles = report["models"]["mf_lgbm"]["test_ic"]["deciles"]
    print("| D1（最低分） | D5 | D10（最高分） | D10-D1 月均 |")
    print("| ---: | ---: | ---: | ---: |")
    print(
        f"| {deciles[0]*100:+.2f}% | {deciles[4]*100:+.2f}% | {deciles[9]*100:+.2f}% | "
        f"{report['models']['mf_lgbm']['test_ic']['long_short_monthly']*100:+.2f}% |"
    )
    print("\n## 基准\n")
    print(json.dumps(report.get("benchmark"), ensure_ascii=False))
    print("\n## 等权方向（训练期 IC 符号）\n")
    print(json.dumps(report.get("equal_weight_directions"), ensure_ascii=False))


if __name__ == "__main__":
    main()
