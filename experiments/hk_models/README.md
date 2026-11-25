# 港股版：同套模型、同套评测

这是 A 股 `~/labs/VLA/Share/v2` 模型线的港股复刻。数据、特征、标签、网络结构、
训练/验证/测试切分和低换手组合规则都尽量保持一致；港股特有的整手、费用与交易
制度单独实现。所有产物都在本目录脚本里可复现。

## 数据源

| 内容 | 来源 | 说明 |
| --- | --- | --- |
| 证券名单、每手股数、板块/币种 | [HKEX List of Securities](https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx) | 只用主板、HKD、非人民币柜台股票 |
| 日线（后复权/不复权） | 腾讯行情 `web.ifzq.gtimg.cn` | 后复权 `hfqday` 做特征/标签，不复权 `day` 做成交与估值 |
| 指数 | 腾讯行情 `hkHSI` / `hkHSCEI` / `hkHSTECH` | 恒生指数、国企指数、恒生科技 |

腾讯的前复权（qfq）序列对高股息股票会出现负价格（例如 01277、00316），
因此改用后复权（hfq）；后复权价格始终为正，且保留了分红再投资的总收益口径。
数据抓取带重试与按股票缓存，`data/hk/` 在 `.gitignore` 里。

## 股票池

1. HKEX 主板 + HKD 计价 + 非人民币柜台；
2. 当前总市值 ≥ 20 亿港元、最新价 ≥ 1 港元、当日成交额 ≥ 200 万港元；
3. 有 2014-06 以前的历史（保证 2016 年训练期有 60 日窗口）；
4. 近 500 个交易日成交额中位数 ≥ 300 万港元；
5. 按成交额/市值排序取前 500（实际约 285 只）。

**局限**：名单是当前港交所名单，已退市/被剔除的股票不在池内，存在幸存者偏差；
这与 A 股自训线使用历史沪深 300 成分股的口径不同，不能直接横比。

## 特征、标签与切分

- 25 维日线特征，与 A 股一致：1/5/20/60 日收益、均线偏离、波动率、成交额比、
  换手率、开盘缺口、日内收益、振幅、收盘位置、交易状态、相对收益，以及
  恒生指数的 1/5/20/60 日收益和 20 日波动。
- 标签：次日开盘到第六个交易日开盘的 5 日收益，按当日截面平均名次转成
  `[-1, 1]` 的 z 分数；Huber 回归，δ=1。
- 切分：训练 2016–2021、验证 2022–2023、测试 2024-01-02–2026-11-25。
- 输入窗口 60 个交易日；逐日采样；特征用训练期中位数/标准差标准化并裁剪到 ±8。

## 模型

四条主路线与 A 股共享超参数：GRU+Attention（48,962 参数）、TCN（95,745）、
Transformer（74,689）、CNN（32,193）。GDN+NTP 是 A 股线的附加原型；港股版因
MPS 上紧凑 recurrent kernel 过慢未纳入，代码保留在 `train.py` 里可单独跑。GRU+Attention / TCN 训练 3 个种子（42/43/44）；Transformer / CNN 各 1 个种子（42）。以可用种子平均分数做集成。

## 组合与交易规则

- 每 5 个交易日调仓，次日开盘成交；持有约 30 只；跌出前 50 名才卖；每次最多
  换 5 只；单票单日不超过前一日成交额的 1%。
- 港股整手：每手股数来自 HKEX 名单（例如腾讯 100 股、汇丰 400 股、煤气 1,000 股），
  买入按整手向下取整；没有涨跌停限制；T+2 交收按日线近似。
- 费用按**汇丰 Trade25**：每月 25 港元月费；当月成交 ≤ 25 万港元时 0 佣金，
  超过后 0.25%（每单最低 100 港元）；另加印花税 0.1%（向上取整到 1 港元）、
  HKEX 交易费 0.00565%、SFC 征费 0.0027%、AFRC 征费 0.00015%、
  CCASS 结算费 0.002%（最低 2、最高 100 港元）。跨过 25 万门槛的那一单按全额
  0.25% 计，避免低估成本。
- 本金档位：10 万 / 30 万 / 50 万 / 100 万港元。港股整手金额大，10 万档只能
  持有少量股票，这是港股与 A 股 100 股整手最直观的差异。

## 运行

```bash
PY=~/labs/VLA/Share/v2/.venv/bin/python
$PY -m experiments.hk_models.fetch all          # 名单、日线、指数、universe.csv
$PY -m experiments.hk_models.prepare            # data/hk/panel.npz + metadata.json
$PY -m experiments.hk_models.train              # 4 主路线，MPS（GRU/TCN 3 种子，其余 1 种子）
$PY -m experiments.hk_models.backtest           # 回测并写 data/hk_family.json / hk_eval_family.json
python3 -m paper_trader.runner export           # 合并进 web/data.json
```

`fetch all` 会重新抓取全部历史；调参时只跑 `prepare/train/backtest` 即可。
`train` 的产物在 `data/hk/artifacts/scores/`，`backtest` 只依赖这些分数矩阵。

## 结果

见 [docs/HK.md](../../docs/HK.md) 与 `results/`（训练完成后由脚本写出）。

## 文件

| 内容 | 位置 |
| --- | --- |
| 抓取 | `experiments/hk_models/fetch.py` |
| 面板 | `experiments/hk_models/prepare.py` |
| 训练/评分 | `experiments/hk_models/train.py` |
| 费用 | `experiments/hk_models/fees.py` |
| 回测/网页 payload | `experiments/hk_models/backtest.py` |
| 股票池 | `experiments/hk_models/universe.csv` |
| 数据/分数 | `data/hk/`（本地，不提交） |
