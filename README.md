# 策略实验室 · 多模型 A 股日线纸面交易平台

个人用的策略验证平台。仪表盘有 **三个模型家族**，可随时切换：

1. **自训模型（默认）**：`~/labs/VLA/Share` 里的 5 条股票收益预测路线（GRU+Attention / TCN / Transformer / CNN / GDN+NTP），2016–2021 训练、2022–2023 验证、2024–2026-11-09 测试；5 日截面排序 + 低换手组合，每只模型各 1/3/5/10 万本金。
2. **多因子 · 小市值（新增）**：全 A 股小市值池（流通市值最小 30%）上的 14 因子月度选股，LightGBM / 等权 / 纯小市值三个变体，2024–2026 测试区间回放，每只模型各 1/3/5/10 万本金。
3. **LLM 模型（保留）**：DeepSeek V4.1 Flash / V4 Pro、GLM-5.3 / 5.3 Flash / 5.2，日线决策，每只模型各 1/3/5/10 万本金。

前端按同花顺 / 券商 App 的信息密度展示净值、持仓、成交、模型对比与决策，并部署到腾讯云，通过随机 token 私密路径访问。

> 模拟盘，仅供策略研究，不构成任何投资建议。

## 当前实例

- 仪表盘：`https://paper.suyingke.chat/<token>/`（token 只保存在本机 `~/.config/paper-trader/deploy.env`，不进入仓库）
- 自训模型：5 × 4 = 20 个账本；信号截至 2026-10-30，持仓每日估值延续到最新交易日（当前 2026-11-12）
- 多因子 · 小市值：3 × 4 = 12 个账本；2024-01-02 至 2026-11-12 月度调仓回放，最后一次信号 2026-10-30
- LLM 模型：5 × 4 = 20 个账本；每个交易日 15:35 自动结算并生成次日决策，17:10 自动重试限额失败的模型；当前已到 2026-11-12
- 本地：行情缓存、SQLite 账本、自训模型分数重放、决策调度、静态站构建都在 Mac mini 上
- 云端：腾讯云 2C/3G 上的 Caddy 只做静态文件 + token 路径校验，不承载交易逻辑

## 三个家族

### 自训模型（VLA/Share）

| 路线 | 参数量 | 测试 Rank IC（种子均值） | 结构 |
| --- | ---: | ---: | --- |
| GRU + Attention | 48,962 | 0.0595 | 两层 GRU + 全历史加性注意力 |
| TCN | 95,745 | 0.0434 | 4 个因果残差块，感受野 61 日 |
| Transformer | 74,689 | 0.0539 | 2 层 4 头自注意力 |
| CNN | 32,193 | 0.0574 | 3 层一维卷积 |
| GDN + NTP | — | 0.0567 | gated delta-rule + 10 类 token NTP + Muon |

- 输入：最近 60 个交易日、25 维日线特征；标签：次日开盘到第六个交易日开盘的 5 日收益，按当日截面排名做 Huber 回归。
- 股票池：653 只历史沪深 300 成分股；回放信号区间 2026-09-01 → 2026-10-30，持仓估值延续到最新交易日。
- 组合：低换手规则——持有约 30 只、跌出前 50 名才卖、每 5 个交易日最多替换 5 只；次日开盘成交，100 股整手（`sh.688` 200 股），买卖 0.10%/0.20%（每笔最低 5 元），1% 成交额上限。
- 数据来源：`~/labs/VLA/Share/v2/local_data/` 的 `panel.npz`、`metadata.json` 和五条路线的测试分数矩阵；本地用 `paper_trader/local_models.py` 重放，不需要 torch。
- 数据口径限制：测试分数矩阵最后一次有效信号是 2026-10-30（5 日标签需要未来数据）；持仓按最新收盘价每日估值（当前至 2026-11-12），但不会凭空产生新信号。

### 多因子 · 小市值（新增）

- 研究代码：`experiments/multifactor_smallcap/`；推理/回放：`paper_trader/multifactor.py`。
- 股票池：当前上市的全部沪深 A 股；每个调仓日剔除 ST、上市不足 250 日、收盘价 < 2 元、流通市值 < 10 亿元的股票，再取流通市值最小的 30% 作为小市值池。
- 因子：市值、EP/BP/SP/CFP、20/60/120 日动量、5 日反转、20/60 日波动、20 日最大单日涨幅、60 日 beta、距 252 日高点；去极值、z-score、行业中性化（市值除外）。
- 模型：14 因子 LightGBM（3 种子）、等权 z 分数合成、纯小市值排序三个变体；标签为 20 日截面收益排名，训练 2018–2021 / 验证 2022–2023 / 测试 2024–2026。
- 组合：月度调仓、50 只等权、缓冲 80 名；次日收盘成交，买卖 0.10%/0.20%（每笔最低 5 元），单票不超过流通市值 1%。
- 数据：东方财富 `RPT_VALUEANALYSIS_DET` 日度全市场快照（2018–2026）；没有成交量与开盘价，因此用收盘执行、不做换手率/Amihud 因子。
- 结果与局限：见 [docs/RESULTS.md](docs/RESULTS.md) 和 [experiments/multifactor_smallcap/README.md](experiments/multifactor_smallcap/README.md)。跨家族标的池和费用口径不同，**收益不可直接比较**。

### LLM 模型（保留）

| 模型 | Provider |
| --- | --- |
| DeepSeek V4.1 Flash | DeepSeek |
| DeepSeek V4 Pro | DeepSeek |
| GLM-5.3 | 智谱 |
| GLM-5.3 Flash | 智谱 |
| GLM-5.2 | 智谱 |

- 标的池：30 只 A 股流动性较好的行业龙头；T 日收盘后决策，T+1 开盘成交；最多 8 只、单票 ≤ 25%。
- 费用：佣金万 2.5（最低 5 元）、卖出印花税万 5、过户费万 0.1。
- 与自训模型的费用、股票池、调仓频率不同，**跨家族收益不可直接对比**；应分别看各自口径。

## 回放口径与“测试集泄漏”

这是这套平台最重要的一条边界。

**自训模型**：

- 训练 2016–2021、验证 2022–2023、测试 2024–2026-11-09；测试段不参与梯度更新与早停。
- 训练/验证/测试的时间切分是严格的；但 **测试区间 2024–2026 已经被查看过**，所以它是“样本外重放”，不是从未看过的严格前向证据。
- 回放只使用每个信号日及以前的数据，次日开盘成交；不存在把未来价格输入给模型的问题。

**多因子 · 小市值**：

- 训练 2018–2021、验证 2022–2023、测试 2024–2026-11-12；测试段不参与训练和早停。
- 信号只使用调仓日及以前的日度快照（收盘价、估值、市值、板块），次日收盘成交；不存在未来价格输入。
- 但股票池只含当前仍在上市的股票，2024–2026 退市的股票不在池内，**存在幸存者偏差**；测试区间也已经被查看过。
- 数据源没有成交量和开盘价，流动性只用流通市值下限过滤，成交按收盘价近似。

**LLM 模型**：

- 每天只把当日及以前的市场数据放进提示词，T+1 开盘成交；这部分没有未来价格泄漏。
- 但商用 LLM 的预训练语料很可能已经包含 2026 年的行情、新闻和讨论。**提示词隔离不能阻止模型“记得”未来**，所以这版 LLM 回放存在真实的测试集泄漏风险，只能作为平台链路演示，不能当作干净的超额收益证据。
- 要得到干净结论：使用训练截止日早于测试区间的自训模型、保持测试段不可见、按时间滚动重训，并在新数据上做真正的前向跟踪。这也是为什么默认展示自训模型、LLM 只作为保留对照。

## 架构

```
Mac mini (本地)
┌──────────────────────────────────────────────────────────────┐
│ 腾讯行情 ──► market.py ──► SQLite (bars / equity / trades)   │
│                              ▲                               │
│  VLA/Share 分数矩阵 ──► local_models.py ──► local_family.json  │
│  多因子小市值分数 ──► multifactor.py ──► multifactor_family.json│
│                              │                               │
│  launchd 15:35 ──► runner.py ─┴─► engine.py（LLM 日线撮合）  │
│                       │                                      │
│                       ├─► models.py ──► DeepSeek / 智谱 API  │
│                       └─► export.py ──► web/data.json         │
└──────────────────────────────┬───────────────────────────────┘
                               │ rsync + Caddy reload
Tencent Cloud (2C/3G)          ▼
┌──────────────────────────────────────────────────────────────┐
│ Caddy :443  https://paper.suyingke.chat/<token>/             │
│   handle_path /<token>/* → /srv/paper/<token>/ (static)      │
│   其他所有路径 → 404                                          │
└──────────────────────────────────────────────────────────────┘
```

## 本地运行

要求：macOS + Python 3.9+；自训模型重放需要 numpy（系统 Python 已带），不需要 torch；LLM 模型调用现有 DeepSeek / 智谱 API。

1. 私密配置（不要提交）：

```bash
mkdir -p ~/.config/paper-trader
cat > ~/.config/paper-trader/.env <<'EOF'
DEEPSEEK_API_KEY=...
ZAI_CODING_CN_API_KEY=...
EOF
chmod 600 ~/.config/paper-trader/.env
```

2. 自训模型（读取 `~/labs/VLA/Share/v2`）：

```bash
python3 -m paper_trader.local_models        # 生成 data/local_family.json
```

3. 多因子 · 小市值（读取 `~/labs/VLA/Share/multifactor/local_export`）：

```bash
python3 -m paper_trader.multifactor         # 生成 data/multifactor_family.json
```

4. LLM 回放（46 个交易日 × 5 个模型）：

```bash
python3 -m paper_trader.runner fetch         # 刷新行情（慢，可重试）
python3 -m paper_trader.runner replay --start 2026-09-01 --end 2026-11-11
python3 -m paper_trader.runner export        # 合并三个家族，生成 web/data.json
python3 -m paper_trader.runner serve --port 8899
```

5. 每日收盘后：

```bash
python3 -m paper_trader.runner daily         # LLM 家族结算 + 生成次日决策
python3 -m paper_trader.runner retry         # 重试限额失败的模型
python3 -m paper_trader.local_models --marks # 自训家族持仓估值延续
python3 -m paper_trader.runner export        # 合并三个家族，生成 web/data.json
scripts/deploy.sh --skip-export
```

`scripts/install_scheduler.sh` 安装工作日 **15:35 结算 + 17:10 重试** 的 launchd 任务（17:10 主要是等智谱 5 小时限额重置）。自训模型的分数矩阵更新需要在 lab 重跑 `Share/v2` 的 score 导出后重新执行 `local_models.py`；当前信号截至 2026-10-30，持仓估值每日延续。

## 部署到腾讯云

1. Cloudflare DNS：`paper.suyingke.chat A 43.134.76.212`，DNS only（避免 CDN 缓存 token 路径）。
2. Caddy 使用 Cloudflare DNS-01 自动签发证书；`scripts/update_caddy.py` 幂等写入站点块，只放行 `/<token>/` 及其子路径，其他路径 404，并加 `noindex`、`no-store`、gzip。
3. 宿主机 `/srv/paper` 以只读方式挂载进 caddy 容器（`docker-compose.yml` 的 `- /srv/paper:/srv/paper:ro`），只需做一次。
4. `scripts/deploy.sh` 执行 export → rsync → caddy validate/reload → HTTP 检查；token 轮换只需改 `deploy.env` 后重新部署。

## 本地算力结论（Mac mini M4 / 16GB）

- 平台服务（行情、SQLite、回放、自训分数重放、调度、静态构建）全部在本机完成；回放 20+20 个组合的常驻内存 <100MB，峰值 <300MB。
- 自训模型是小型网络（3–10 万参数），本地 CPU 重放分数矩阵即可；不跑 torch 训练。
- 多因子小市值在 lab 的 CPU 队列训练（LightGBM），Mac mini 只重放 12 个组合的月度分数矩阵，不需要 LightGBM/torch。
- LLM 推理按日调用 DeepSeek / 智谱 API，每天 5 次。16GB 统一内存能跑单个 7B/8B 量化模型，但同时服务 5 个当代模型不现实；需要完全本地推理时，可把 `models.py` 的 provider 指向本机 Ollama / MLX 的 OpenAI 兼容端点。

## 目录

```
paper_trader/
  config.py        模型、股票池、本金、费用等静态配置
  market.py        腾讯前复权日线抓取 + SQLite 缓存
  store.py         SQLite schema 与读写
  engine.py        LLM 家族目标权重撮合、费用、T+1、估值、指标
  strategy.py      行情特征与 LLM 提示词
  models.py        多 provider 调用、JSON 解析与权重约束
  local_models.py  自训模型分数重放 → 自训家族 JSON
  multifactor.py   小市值多因子分数重放 → 多因子家族 JSON
  runner.py        fetch / replay / daily / export / serve
  export.py        合并三个家族，生成前端 data.json
experiments/       多因子小市值的下载、准备、因子、训练与回测代码
web/               静态仪表盘（默认自训模型，可切多因子 / LLM）
scripts/           deploy.sh / daily.sh / install_scheduler.sh / update_caddy.py
tests/             engine / 费用 / 权重归一化单元测试
docs/screenshots/  before.png、after-*.png、local-*.png、llm-*.png
docs/RESULTS.md    两个家族的回放结果快照
docs/OPERATIONS.md 部署与运维
```

## 验证

```bash
python3 -m unittest discover -s tests -v
```

覆盖：费用、权重裁剪/归一化/未知代码、买入与收盘估值、同日 T+1 卖出保护、多因子账户的持仓/现金/整手不变量。前端用 Playwright 在 1440×900 与 390px 下检查无 console 错误、无横向溢出。

## 结果快照

见 [`docs/RESULTS.md`](docs/RESULTS.md)。关键数字：

- 自训模型 20 个账本，2026-09-01 → 2026-11-12：合计 +0.14%，沪深 300 同期约 -12.31%；领先 GRU+Attention 10 万 +2.17%。
- LLM 模型 20 个账本，2026-09-01 → 2026-11-12：合计 +1.52%，沪深 300 同期 -12.31%；领先 GLM-5.3 Flash 3 万 +3.06%。
- 多因子 · 小市值 2024-01-02 → 2026-11-12：测试 Rank IC 0.096（LightGBM）、0.084（等权）、0.030（纯小市值）；10 万累计 +15.51%/+8.96%/+13.58%，同期全市场等权 +50.25%。IC 很强但 10 万 × 50 只期末约 65% 现金，信号有效、执行损耗大；详见 [docs/RESULTS.md](docs/RESULTS.md)。
- 三个家族口径不同，仅作各自链路验证。

## 已知局限

- 自训模型测试区间 2024–2026 已查看，不是严格前向；分数矩阵最后一次信号 2026-10-30。
- 多因子小市值没有退市股票，存在幸存者偏差；没有成交量和开盘价，用收盘执行。
- LLM 回放存在预训练知识泄漏风险，不能作为干净的超额收益证据。
- 前复权/原始价格近似成交；未模拟滑点、盘口深度、涨跌停排队；自训模型不处理分红送转。
- 单一数据源（腾讯行情 / 本地 panel），未做多源交叉校验。
- 自训模型 1 万/3 万本金在 30 只目标下大量现金拖累；这是真实的可执行性约束，不是 bug。
- 本轮 Kimi 前端优化因 Sensenova 上游持续返回 HTTP 429（多次重试）未执行；界面已用 Playwright 做桌面 1440px 与移动 390px 自检（无 console 错误、无横向溢出）。
