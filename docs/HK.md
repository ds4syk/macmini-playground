# 港股模型线：数据、模型与评测

> 模拟盘/研究结果，不构成任何投资建议。测试段 2024-01-02–2026-11-25 已被查看，
> 不是严格前向证据；股票池来自当前港交所名单，存在幸存者偏差。

这套港股线是 A 股 `~/labs/VLA/Share/v2` 的复刻：同一份 25 维日线特征、60 日输入、
5 日截面排序标签、四条主网络结构、同一套低换手组合规则；只替换数据、市场基准和
交易制度/费用。研究代码在 [`experiments/hk_models/`](../experiments/hk_models/)，
训练完成后生成的数字表在 [`experiments/hk_models/results/RESULTS.md`](../experiments/hk_models/results/RESULTS.md)。

## 数据源

| 内容 | 来源 | 用途 |
| --- | --- | --- |
| 证券名单、每手股数、板块/币种 | [HKEX List of Securities](https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx) | 主板 + HKD + 非人民币柜台筛选 |
| 日线（后复权） | 腾讯行情 `hkfqkline` | 特征与 5 日标签 |
| 日线（不复权） | 腾讯行情 `kline` | 成交价、现金与整手金额 |
| 指数 | 腾讯行情 `hkHSI` / `hkHSCEI` / `hkHSTECH` | 恒生指数/国企指数/恒生科技 |

腾讯的前复权（qfq）序列对高股息股票会出现负价格（例如 01277、00316），
因此改用后复权（hfq）；后复权价格始终为正，并保留分红再投资的总收益口径。
抓取脚本带重试、按股票缓存和公司行为质量过滤，`data/hk/` 不提交。

## 股票池

1. 港交所主板、HKD 计价、非人民币柜台；
2. 当前总市值 ≥ 20 亿港元、最新价 ≥ 1 港元、当日成交额 ≥ 200 万港元；
3. 2014-06 以前有历史，保证 2016 年训练期有 60 日窗口；
4. 近 500 个交易日成交额中位数 ≥ 300 万港元；
5. 排除复权序列无法表达公司行为（负价/近零价、单日 >2 倍跳变）的股票。

最终 285 只。**局限**：这是当前名单，已退市/被剔除的股票不在池内；与 A 股自训线
使用历史沪深 300 成分股的口径不同，不能直接横比。

## 模型与切分

- 25 维特征：1/5/20/60 日收益、均线偏离、波动率、成交额比、换手率、开盘缺口、
  日内收益、振幅、收盘位置、交易状态、相对收益，以及恒生指数的 1/5/20/60 日
  收益与 20 日波动。
- 标签：次日开盘到第六个交易日开盘的 5 日收益，按当日截面平均名次转成
  `[-1, 1]` 的 z 分数；Huber 回归，δ=1。
- 训练 2016–2021、验证 2022–2023、测试 2024-01-02–2026-11-25；早停只看验证集。
- 四条主路线：GRU+Attention（48,962 参数）、TCN（95,745）、Transformer（74,689）、
  CNN（32,193）。GDN+NTP 是 A 股线的附加原型；港股版因本机 MPS 上紧凑
  recurrent kernel 过慢，本轮未纳入，保留在 `train.py` 里可单独跑。
- GRU+Attention / TCN 各 3 个种子（42/43/44）；Transformer / CNN 各 1 个种子（42）。以可用种子平均分数做集成。
- 与 A 股的唯一实现差异：港股股票池较小，验证 Huber 损失在前几轮几乎不变，
  早停改为按验证 Rank IC 选择最优轮次；其余超参与 A 股一致。

## 港股交易制度与费用

- 每手股数来自港交所名单（腾讯 100 股、汇丰 400 股、煤气 1,000 股等）；买入按整手
  向下取整；卖出也按整手；零股视为不可交易。
- 无涨跌停限制；T+2 交收按日线近似；未逐笔模拟 VCM、部分成交和流动性冲击。
- 每 5 个交易日调仓，次日开盘成交；持有约 30 只；跌出前 50 名才卖；每次最多换 5 只；
  单票单日不超过前一日成交额的 1%。
- 费用按**汇丰 Trade25**：每月 25 港元月费；当月成交 ≤ 25 万港元时 0 佣金，
  超过后 0.25%，每单最低 100 港元；跨过门槛的那一单按全额 0.25% 计。
- 另加：印花税 0.1%（向上取整到 1 港元）、HKEX 交易费 0.00565%、
  SFC 征费 0.0027%、AFRC 征费 0.00015%、CCASS 结算费 0.002%（最低 2、最高 100 港元）。
- 题面里的“金融 Trae 的 25”没有对应到可核实的券商产品；本版采用可公开核实的
  汇丰 Trade25 口径，费用参数集中在 `experiments/hk_models/fees.py`，可替换。

## 结果解读

- 集成 Rank IC：GRU+Attention 0.012、TCN 0.040、Transformer 0.010、CNN 0.014；
  只有 TCN 接近可用，其余三条在测试段接近随机。
- 100 万港元组合 2024–2026：GRU+Attention -8.3%、TCN +18.6%、Transformer +33.2%、
  CNN +57.4%；同期股票池等权 +54.0%、恒生指数 +30.8%。
- **不要把组合收益单独当作 alpha**：四条路线的十分位多空差都为负，说明排序不是单调的；
  Transformer/CNN 的正收益更像少数持仓的路径依赖，和 IC 不一致。
- 费用敏感性（GRU+Attention，100 万）：0× 费用 +33.9% → 1× -8.3% → 2× -17.1%；
  Trade25 在月成交超过 25 万后的 0.25% 佣金是 1m 档的主要拖累。
- 10 万港元档佣金更低，但整手限制持仓数；港股小资金的可执行性与 A 股 100 股整手差异很大。

## 港股多因子

- 代码：`experiments/hk_models/multifactor.py`；数据只用港股（腾讯行情日线 + HKEX 名单）。
- 因子（10 个）：总市值、20/60/120 日动量、5 日反转、20/60 日波动、20 日最大单日涨幅、60 日 beta、距 252 日高点。
- 标签：月末截面、20 日 forward return 的截面排名；季度调仓；训练 2016–2021 / 验证 2022–2023 / 测试 2024-01-02–2026-11-25。
- 变体：LightGBM（3 种子）、等权 z 分数、纯小市值；10/30/50/100 万港元分别持有 4/8/10/40 只，次日开盘、港股整手、汇丰 Trade25 费用。
- 结果（100 万港元）：LightGBM +30.35%、等权 +32.66%、纯小市值 +186.05%；同期股票池等权 +53.99%、恒生指数 +30.84%。
- 纯小市值 10 万档只有约 4 只，收益由少数微盘路径驱动，**不能当稳定 alpha**；LightGBM/等权的 Rank IC 也只有 0.021/0.056，十分位差为负。
- 局限：没有 PE/PB/CFP/SP（免费港股历史基本面不可靠）和行业中性化；测试段已查看、当前名单有幸存者偏差。

## 网页分页

仪表盘新增两个分页：

- **港股 · 自训模型**：4 个模型 × 10/30/50/100 万港元本金，展示 2026-08-25 起的
  净值、持仓、成交和最新决策。
- **港股 · 模型评测**：2024-01-02–2026-11-25 测试段的 Rank IC/ICIR、十分位差、
  Top30 超额、收益/回撤/夏普/年换手/年费用，以及 GRU+Attention 的 0×/1×/2×
  费用敏感性。

改前/改后截图在 `docs/screenshots/`：

| 场景 | 文件 |
| --- | --- |
| 改前（三个 A 股分页） | [hk-before-desktop.png](screenshots/hk-before-desktop.png) |
| 改后（港股自训） | [hk-after-desktop.png](screenshots/hk-after-desktop.png) |
| 改后（港股多因子） | [hk-mf-after-desktop.png](screenshots/hk-mf-after-desktop.png) |
| 改后（港股评测） | [hk-eval-after-desktop.png](screenshots/hk-eval-after-desktop.png) |

## 复现

```bash
PY=~/labs/VLA/Share/v2/.venv/bin/python
$PY -m experiments.hk_models.fetch all
$PY -m experiments.hk_models.prepare
$PY -m experiments.hk_models.train
$PY -m experiments.hk_models.backtest
$PY -m experiments.hk_models.report
python3 -m paper_trader.runner export
scripts/deploy.sh --skip-export
```

`fetch all` 会重新抓取全部历史；调参时只跑 `prepare/train/backtest`。训练产物在
`data/hk/artifacts/`，回测只依赖这些分数矩阵；网页数据写在 `data/hk_family.json`
与 `data/hk_eval_family.json`。

## 已知局限

- 测试段已查看，不是严格前向；需要锁定方案后在新数据上做真正 forward。
- 当前港交所名单有幸存者偏差；退市股、历史 ST/停牌状态没有完整还原。
- 后复权序列按最新复权因子计算，含分红再投资；历史特征会随未来分红/拆股变化，
  这是常见的复权近似，不是严格 point-in-time。
- 日线近似 T+2、VCM、部分成交与滑点；没有盘口深度/冲击成本模型。
- 港股整手金额大，10 万港元档只能持有少量股票；这是可执行性约束，不是 bug。
- 单一数据源（腾讯行情 + 港交所名单），未做多源交叉校验。
