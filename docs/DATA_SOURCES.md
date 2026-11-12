# 特征日更的开源数据源

日期：2026-11-12。目标：在不引入外部 LLM 的前提下，把 A 股日线更新到最新交易日，重建 25 维特征 panel，再用本机自训模型打分。

## 结论

**首选 BaoStock 0.9.4**：VLA/Share 原始 panel 就是用它生成的，字段、股票池和复权口径完全一致，免费、无需 token。

实测（2026-11-12 深夜）：

- `query_history_k_data_plus("sh.600000", ...)` 返回 **2026-11-12** 日线（收盘 10.21，成交额 5.08 亿）。
- `query_hs300_stocks()` 返回 **300** 只成分股；当前快照 `updateDate=2026-11-09`（按月快照，日更不需要每天更新成员）。
- `query_history_k_data_plus("sh.000300", ...)` 返回 **2026-11-12** 指数日线（收盘 4043.93）。
- 需要的字段齐全：`date, code, open, high, low, close, preclose, volume, amount, turn, tradestatus, pctChg, isST`；前复权用 `adjustflag="1"` 取 `adj_open/adj_close`。
- 原管线代码在 `~/labs/VLA/Share/gru/src/download.py`（下载）和 `prepare.py`（25 维特征、股票池、标签、标准化），不是临时重写。

## 25 维特征与字段对应

`prepare.py::stock_features` 使用：

- 原始日线：`open/high/low/close/preclose/volume/amount`；
- 换手：`turn / 100`；
- 交易状态：`tradestatus`；
- ST：`isST`；
- 前复权：`adj_open/adj_close`；
- 市场：`sh.000300` 的 1/5/20/60 日收益与 20 日波动；
- 派生：1/5/20/60 日收益、均线偏离、波动率、成交额相对水平、相对沪深 300 收益、振幅、收盘位置等。

## 日更建议流程

1. 从 lab 同步一次历史原料（653 只 `raw/*.csv.gz`、`membership.csv`、`benchmark.csv`、`stock_basic.csv`）；之后只增量更新。
2. 读取每只股票 CSV 的最后日期，用 BaoStock 只拉 `last_date+1 → today` 的日线（前复权 + 不复权各一次），追加；同步更新 `benchmark.csv` 和当期 `membership.csv`。
3. 把 `config.json` 的 `data_end` 设为最新交易日，运行 `prepare.py` 重建本地 panel（只读现有历史 + 新追加行，不重下全历史）。
4. 用本机 `torch 2.15 + MPS` 加载 `latest_*.pt`，对最新信号日打分；五条路线各跑一遍约 0.11 秒（见 `docs/LOCAL_INFERENCE.md`）。
5. 把新分数写回自训家族的分数矩阵，按 5 日低换手规则生成新目标；不调用任何外部 LLM。

## 备选与取舍

| 源 | 优点 | 注意 |
| --- | --- | --- |
| **BaoStock** | 与原 panel 完全同源；无 key；历史 HS300 快照；前复权 | 免费服务，批量下载要限速/重试；当前成分快照是 11-09 |
| **AkShare** | 封装东财/新浪，接口多，更新快 | 字段命名/复权/停牌口径要重新对齐；依赖较重 |
| **东方财富 push2his** | 无 key，实测 200；日线全 | 需自己处理复权、停牌、成分股；无历史成分快照 |
| **Tushare** | 数据规范，接口稳定 | 需要 token/积分；与原 panel 口径需对齐 |

建议：**BaoStock 主用，东财/新浪做收盘后的交叉校验**；AkShare 只在 BaoStock 缺字段时补。

## 现状与下一步

- 本地已有 `panel.npz`（截至 11-09）和五条路线的 `latest_*.pt`；本机 `~/labs/VLA/Share/v2/.venv` 已装好 torch/MPS + baostock。
- 当前自训家族用预计算分数（最后信号 10-30），持仓估值续到 11-12；要产生新信号，还差上面的增量下载 + `prepare.py` 重建 panel 这一步。
- 本地只跑自训模型；LLM 家族仍只走云端 API，不下载/运行外部 LLM。
