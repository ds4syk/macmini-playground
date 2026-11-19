# 券商模拟盘接入（中信 / 同花顺）

这套工具把看板里的模型组合**按权重复刻**到外部模拟账户。中信信e投没有公开的下单 API，
只生成可手工执行的调仓清单；同花顺网页模拟盘（moni.10jqka.com.cn）有网页交易界面，
可以用浏览器自动化**自动下单**（见第 8 节），也保留手工清单作为兜底。

## 1. 快速开始

```bash
# 看有哪些模型/本金/持仓
python3 -m paper_trader.broker list

# 中信：优先按 3 万生成（模拟资金可自定义，20 万只是默认值）
python3 -m paper_trader.broker orders --broker citic \
  --family multifactor --model mf-lgbm --capital 30000 --out docs/broker_examples

# 中信兜底：如果 App 资金设置最低只能 20 万，再用 20 万版本
python3 -m paper_trader.broker orders --broker citic \
  --family multifactor --model mf-lgbm --capital 30000 \
  --capital-override 200000 --out docs/broker_examples

# 同花顺：优先按 3 万生成；但 App 初始资金是档位（常见 10/20/50/100 万），
# 没有 3 万档时用最小档（10 万）或 20 万，按比例缩放
python3 -m paper_trader.broker orders --broker ths \
  --family multifactor --model mf-lgbm --capital 30000 --out docs/broker_examples

# 同花顺：所有模型的 3 万清单（仅当模拟号支持该初始金额）
python3 -m paper_trader.broker orders --broker ths --capital 30000 --out broker_orders/ths

# 已有持仓 → 生成差额单（CSV: symbol,shares,price）
python3 -m paper_trader.broker orders --broker citic \
  --family multifactor --model mf-lgbm --capital 30000 \
  --positions broker_orders/citic_positions.csv --cash 5000
```

每个账户输出两个文件：

- `citic-multifactor-mf-lgbm-30000.csv`：代码、名称、买卖、数量、参考价、金额、估算费用；
- 同名 `.md`：账户、信号日、下一信号、买卖合计、剩余现金、下单注意。

清单按**目标权重**复刻：`目标市值 = 模型权重 × 模拟账户权益`，再按 A 股整手规则取整；模型自己的现金比例会保留（多因子 3 万约 5.6%）。剩余现金高于模型目标 10 个百分点时，清单会提示「小资金无法完整复制」。

## 2. 下单规则

- **买入**：主板 100 股整数倍；科创板 `688/689` 至少 200 股，之后可 1 股递增。
- **卖出**：可以零股；清仓时一次卖出。
- **费用**：清单按买入 0.10%、卖出 0.20%（含印花税估算）、每笔最低 5 元估算；实际以券商为准。
- **参考价**是模型最后收盘价，不是实时价；下单时用限价单，按盘口调整。
- **T+1**、涨跌停、停牌等规则由券商/同花顺模拟盘执行；本工具不模拟撮合。

## 3. 中信证券（信e投）

公开的第三方操作整理（入口以 App 实际版本为准）：

1. 手机应用商店下载 **中信证券信e投**，用中信账户登录。
2. 底部「行情」→「模拟交易」，或直接搜索「模拟盘」；同意《模拟交易服务协议》。
3. 系统分配虚拟资金。公开操作说明称开通后可在**资金设置**里调整初始金额（上限 100 万），**20 万只是默认值、不是最低限额**，所以优先按 **3 万** 生成；如果 App 的资金设置实际只给固定档位/最低 20 万，再用 `--capital-override 200000` 的 20 万版本（数量等比放大，收益率不变）。
4. 按 CSV 在「交易」里逐笔输入代码、数量、限价；成交后在「持仓」里核对。
5. 下一期调仓时，把模拟盘持仓导出/抄成 `symbol,shares,price` 的 CSV，用 `--positions` 生成差额单。

**自动化（QMT / PTrade）**：中信支持 QMT（迅投）和 PTrade（恒生）这类量化终端，但需要客户经理开通量化权限，公开资料称常见资产门槛 **30 万或 50 万**，软件由客户经理提供安装包/激活码，运行在 Windows。3 万账户通常达不到门槛；如果你有 30/50 万且想走这条路，告诉我，我再接 API。

## 4. 同花顺

1. 同花顺 App →「模拟炒股」/「模拟交易」，开通模拟账户（虚拟资金）。
2. 同花顺模拟号创建时选的是**初始资金档位**（公开整理常见 10/20/50/100 万，App Store 写送 20 万），没有 3 万档；严格 3 万做不了。要用同花顺就选最小档（10 万）或 20 万，按比例缩放；要严格 3 万用中信或我们自己的 3 万账本。
3. 按清单逐笔下单；同花顺模拟盘的撮合、费用、整手规则以 App 为准（不同版本可能简化费用）。
3. 同花顺**没有公开的散户下单 API**；`easytrader` 之类通过模拟客户端点击的方案不稳定，也可能违反券商/平台条款，不建议用于真钱或长期自动运行。

## 5. 多模型怎么摆

- 一个模拟账户对应一个「模型 × 本金」组合；账户名就是 `broker-family-model-capital`。
- 如果模拟盘只能开一个账户：严格 3 万用 **中信**（资金设置可自定义）；同花顺用最小档 10 万或 20 万按比例缩放。多因子 8 只、现金约 5.6%；本地/LLM 模型持仓多、30 万以下整手损耗大，建议用 10 万或 20 万账户，或只在看板里跟踪。
- 30 万以下常见问题：目标市值不到一手，清单会跳过该股、剩余现金偏高；`.md` 会给出提示。此时放大本金比硬凑整手更接近模型。
- 当前可跟踪的 30k 组合：多因子 3 个变体 + 本地 5 个 + LLM 5 个，共 13 个；全部生成用 `--capital 30000`。

### 5.1 同时验证 3 万和 10 万

- 账户数：中信信e投公开说明里只描述一个模拟资金账号，能否多开以 App「账户管理」为准；同花顺可以「退出当前模拟账号 → 新建」，旧号记录保留、可切换（通常同时只有一个活跃）。所以最稳的是**中信 3 万 + 同花顺 10 万**，两个平台各一个；如果中信能多开，也可以两个都放中信。
- 3 万模型是 N=8，10 万模型是 N=40；它们是**两套不同组合**，不是同一策略简单缩放。3 万更集中（测试 +100%、回撤 -30.8%），10 万更分散（+56.8%、回撤 -22.3%）。
- 想只比较“本金规模”本身：固定同一个 N（如 N=8），把 3 万模型放到 10 万账户上复刻：
  `orders --broker ths --family multifactor --model mf-lgbm --capital 30000 --capital-override 100000`
  这是“同策略、不同本金”的对照；默认的 10 万模型则是“同信号、按 10 万重新选 N”。

### 5.2 为什么策略和本金/仓位相关

**信号本身和本金无关**：14 因子、LightGBM、季度调仓、打分排序对所有本金都一样。变的是**可执行的组合**：

1. **整手**：A 股买入 100 股整数倍。3 万 × 8 只 ≈ 3,750 元/只，很多小市值股票一手就要 2,000–5,000 元；10 万可以摊到 40 只，单只目标更小也能买。
2. **最低佣金**：每笔最低 5 元。3 万/8 只一笔 3,750 元的买入，5 元约 0.13%；10 万/40 只一笔 2,500 元，5 元约 0.20%，但交易笔数/换手不同，综合成本要一起算。
3. **现金拖累**：3 万如果硬买 40 只，每只 750 元，大部分买不起一手，现金会剩 80–90%；所以 3 万选 8 只，10 万选 40 只。
4. **分散度/回撤**：8 只更集中，收益和回撤都更大；40 只更分散，回撤小、收益也被摊薄。N 是在 2022–2023 验证集上按本金分别选的，不是拍脑袋。

所以看板里 3 万和 10 万是两条不同的“可执行组合”；比较收益时要记住 N 不同。想分离“本金效应”，用上面的固定 N 对照。

## 6. 自动化路线

| 阶段 | 做法 | 前提 |
| --- | --- | --- |
| 中信（半自动） | 本工具出清单 → 手工在信e投下单 → 回填持仓 | 模拟账户 |
| 同花顺（自动） | `paper_trader.ths_web` 挂接已登录浏览器，按清单市价下单、回填持仓（第 8 节） | Chrome + 一次扫码登录 + playwright |
| 全自动 | QMT/PTrade Python API 读清单下单 | 量化权限、Windows、30/50 万门槛 |
| 全自动+ | 每日/每季自动跑「数据→因子→打分→目标→订单」 | 实时数据源、调度、风控、券商 API |

目前**未来信号仍需手动跑**：多因子上一次调仓是 2026-09-30，下一次季度信号在 2026-12-31 收盘、2027-01-04 起执行，需要重新下载全市场数据、算因子、打分、导出；自训/LLM 也一样。看板里的名单是历史快照，不是实时信号。

## 7. 需要你提供/确认

1. **中信**：按 3 万生成（资金设置可自定义，20 万只是默认）；如果 App 实际最低只能 20 万，用 `--capital-override 200000` 的兜底版本。
2. **中信量化**：QMT/PTrade 需要你本人找客户经理开通量化权限（常见 30/50 万门槛、Windows、安装包/激活码）；开通后再接 API，不需要现在决定。
3. **同花顺**：初始资金是档位（常见 10/20/50/100 万），没有 3 万档；用最小档 10 万或 20 万按比例缩放，严格 3 万请用中信或自有账本。
4. **先跟哪些模型**：建议先「多因子 3 万 + 本地 Transformer 3 万 + LLM GLM 3 万」，跑 1–2 个季度再扩。
5. **成交回填**：每期调仓后把 `symbol,shares,price` 的持仓 CSV 发我（或贴截图），我用 `--positions` 生成差额单并核对。
6. 不要把交易密码/短信验证码发给任何人或写进仓库；API 只用券商官方授权方式。

> 以上是工具和流程说明，不构成个性化投资建议；模拟盘结果与真实成交（滑点、冲击、费用、涨跌停）会有差异。第三方入口/门槛信息来自公开整理（[中信模拟交易步骤](https://licai.jiantou8.com/user/guide_view_3176789.html)、[中信 QMT/PTrade 说明](https://licai.jiantou8.com/user/guide_view_3269867.html)），以中信/同花顺官方为准。


## 8. 自动下单：同花顺网页模拟盘

同花顺模拟炒股有网页交易区（交易页标题「同花顺虚拟交易所」）。`paper_trader.ths_web`
通过 Chrome 调试端口（CDP）挂接一个人工登录好的 Chrome，读资金/持仓、按清单逐笔发
市价委托；**不保存账号密码**，登录态只存在独立的 Chrome profile 里。

### 8.1 一次性准备

```bash
# 1) 启动带调试端口的 Chrome（独立 profile，不影响日常 Chrome）
scripts/start_ths_chrome.sh

# 2) 建一个带 playwright 的独立环境（平台本身的 python 不用装 playwright）
scripts/setup_ths_bridge.sh

# 3) 生成扫码二维码并等待登录
.venv-ths/bin/python -m paper_trader.ths_web login
#    二维码写到 ~/.config/paper-trader/ths-login-qr.png，
#    用同花顺 App 扫码并在手机上确认；登录态通常可保持数天到一周。
```

登录后先确认连接：

```bash
.venv-ths/bin/python -m paper_trader.ths_web status
```

### 8.2 从模型到委托

```bash
# 新账户：按模拟盘实际权益出清单（当前测试账户是 20 万，所以用 --capital-override 200000）
python3 -m paper_trader.broker orders --broker ths \
  --family multifactor --model mf-lgbm --capital 30000 \
  --capital-override 200000 --out broker_orders/ths

# 先用确认弹窗自检：走到「确定/取消」就取消，不会提交
.venv-ths/bin/python -m paper_trader.ths_web preflight \
  --orders broker_orders/ths/ths-multifactor-mf-lgbm-200000.csv

# 确认无误后真正提交（没有 --yes 只会打印计划）
.venv-ths/bin/python -m paper_trader.ths_web execute \
  --orders broker_orders/ths/ths-multifactor-mf-lgbm-200000.csv --yes
```

下一期调仓时把模拟盘持仓导回来，生成差额单：

```bash
.venv-ths/bin/python -m paper_trader.ths_web positions \
  --out broker_orders/ths/current.csv
# 命令会打印可用余额；把它填进 --cash
python3 -m paper_trader.broker orders --broker ths \
  --family multifactor --model mf-lgbm \
  --positions broker_orders/ths/current.csv --cash 12345.67 --out broker_orders/ths
```

### 8.3 规则与安全边界

- **只用市价单**：固定选页面上的「剩余撤销」类型（沪市为最优五档即时成交剩余撤销，
  深市为即时成交剩余撤销）；清单里的参考价只用于估算金额，实际成交价以平台为准，
  和目标权重会有偏差。
- **整手规则**：主板买入必须是 100 股整数倍；科创板至少 200 股；卖出允许零股，
  但模拟盘可能按平台规则拒单，失败时人工处理。
- **只操作模拟盘**：代码只按「同花顺虚拟交易所」交易页标题识别页面，加载的账号是同花顺
  模拟账号；不会碰真实资金账户。
- **默认不执行**：`execute` 不加 `--yes` 只打印计划；`preflight` 不提交；余额不足时
  默认拒绝（可用 `--skip-affordability` 跳过，不推荐）。
- **有执行日志**：每次提交写到 `~/.config/paper-trader/executions/`，含清单路径、账户
  快照和每笔结果，便于核对。
- **会话会过期**：交易页失效时工具会自动回平台页重进交易区；平台登录态也过期时，
  重新跑 `login` 扫码即可。
- **页面改版风险**：选择器集中在 `paper_trader/ths_page.py` 的 `SELECTORS`，页面改版
  后需要按实际 DOM 更新；下单前先跑 `preflight`。
- **合规**：只用于自己的模拟盘学习，不用于商业代客；第三方自动化可能违反平台条款，
  真实账户请走券商官方 QMT/PTrade 授权。

### 8.4 2026-11-17 实测记录

- 登录：同花顺 App 扫码（`login` 生成二维码）后在网页端登录成功；会话保存在
  `~/.config/paper-trader/chrome-profile`，后续 `status` 可直接复用。
- 交易页：`https://mncg.10jqka.com.cn/cgiwt/index/index`，标题「同花顺虚拟交易所」。
  首页的「进入交易区」按钮在本次登录态下没有显示，工具改从「我的模拟炒股」个人主页
  取带 `usrid`/`token` 的交易区直链再打开，更稳。
- 账户：同花顺模拟账号 20 万虚拟资金、0 持仓；`status` 读到可用余额/总资产 200000。
- 下单自检：`preflight` 在沪市 600000 与深市 002133 两只股票上都走到确认弹窗，
  弹窗显示市场代码、股东账号、证券代码、市价策略和数量均正确，点「取消」后
  「当日委托」仍为「无数据」——没有提交任何委托。
- 未验证：确认提交后的「合同编号」解析与撤单。本轮测试时已收盘，市价委托不会
  成交，也避免在账户里留下测试持仓；建议选好模型后，在交易时段先小单跑一次
  `execute --yes`，核对当日委托/成交后再跑全量清单。

## 9. HTTP 直连（快速路径，推荐）

同花顺模拟盘的网页本身就是 AJAX 交易，`paper_trader.ths_http` 直接调它的 JSON 接口，
不再逐点页面：浏览器只在首次登录时用一次，导出 Cookie（本机 600 权限），之后查询/下单
都是普通 HTTP 请求。

### 9.1 一次性登录

```bash
scripts/start_ths_chrome.sh                              # 带调试端口的 Chrome
.venv-ths/bin/python -m paper_trader.ths_http login      # 扫码 → 导出 Cookie + 股东账号
```

登录页是官方 `upass.10jqka.com.cn`；Cookie 只写到
`~/.config/paper-trader/ths_cookies.json`，不会进仓库。

### 9.2 日常命令（系统 python3 即可，不需要浏览器或 playwright）

```bash
time python3 -m paper_trader.ths_http status
python3 -m paper_trader.ths_http quote --code 600000
python3 -m paper_trader.ths_http orders
python3 -m paper_trader.ths_http positions --out broker_orders/ths/current.csv
python3 -m paper_trader.ths_http preflight --orders broker_orders/ths/xxx.csv
python3 -m paper_trader.ths_http execute --orders broker_orders/ths/xxx.csv --yes
```

### 9.3 实测耗时（2026-11-17，20 万模拟账号、0 持仓）

| 命令 | 耗时 |
| --- | ---: |
| `status`（资金 + 持仓） | 0.32 秒 |
| `quote --code 600000`（单只行情） | 0.24 秒 |
| `preflight`（8 笔清单逐只查行情/账号） | 0.73 秒 |
| `positions`（导出 CSV） | 0.40 秒 |

对比浏览器 DOM 方案单次数十秒。股东账号从行情返回的 `extend` 字段（`gdzhlb*2,A547…;1,0017…;`）
里取：沪市 `mkcode=2`、深市 `mkcode=1`；市价委托固定用 `tradeStockSjwt/` + `sjwttype`
（沪 21 / 深 14，最优五档即时成交剩余撤销）。

### 9.4 与浏览器方案的关系

- `ths_http`：日常用，单次 HTTP 通常 0.2–0.5 秒；登录和 Cookie 由 `login` 负责。
- `ths_web`：CDP + Playwright 点页面，慢（单次数十秒），保留作登录/兜底，页面改版时
  按 DOM 修比改接口字段更直观。
- Cookie 过期时 `status` 会报「资金账号不存在 / 登录过期」，重跑 `login` 即可。
- 这些是网页版自用 AJAX 接口，不是同花顺官方公开 API；同花顺改版时路径/字段可能要更新，
  以 `ths_simulated_API`、`RobotTrader` 等公开实现和实际抓包为准。

页面结构参考 MIT 许可的 [ai_ths_moni_trading_mcp](https://github.com/trading-a/ai_ths_moni_trading_mcp)
（Copyright (c) 2026 jsiq@qq.com），按本平台 CLI 重写。


## 10. 多模型实盘跟踪（同花顺共享账户 + 3 万子账本）

2026-11-18 起，按选定的 6 个模型做实时跟踪：多因子 LightGBM / 等权 / 纯小市值、
GRU + Attention、Transformer、DeepSeek V4 Pro。每个模型 3 万，共 18 万；同花顺只有
一个 20 万模拟账户（初始资金固定，不能改成 3 万），所以用**共享账户执行 + 每模型独立
子账本记账**，收益分开算。

### 10.1 数据流

```
live/orders.json（启动买单，合约号 → 模型）
        │
        ├─ 同花顺 delegate/qryChengjiao（当日成交）
        │        └─ 按合约号归属到子账本：现金、持仓、成交明细
        │
        ├─ 腾讯行情（st_price / st_pclose）→ 持仓估值
        │
        └─ web/live.json + web/live.html → 腾讯云 token 页面（30 秒刷新）
```

- 子账本：`~/.config/paper-trader/live/ledger.json`，每个模型 3 万现金起步；买入按成交
  金额扣现金、卖出加回；费用按买 0.10%/卖 0.20%、每笔最低 5 元估算。
- 估值：`equity = 子账本现金 + Σ(持仓股数 × 最新价)`，`ret = equity / 30000 − 1`。
- 账户总资产、可用/冻结资金、持仓市值来自同花顺资金接口。

### 10.2 命令与调度

```bash
# 手工同步 + 估值 + 部署
PYTHONPATH=. python3 -m paper_trader.live run --deploy

# 只同步成交 / 只估值
PYTHONPATH=. python3 -m paper_trader.live sync
PYTHONPATH=. python3 -m paper_trader.live value

# 安装交易时段每 120 秒刷新的 launchd 任务
scripts/install_live.sh
```

- 页面：`https://paper.suyingke.chat/<token>/live.html`；看板右上角有「实盘跟踪」入口。
- launchd：`com.suyingke.papertrader.live`，每 120 秒，09:25–15:10 运行
  `scripts/live.sh`（同步 → 估值 → rsync 部署）；日志 `~/.config/paper-trader/live.log`。
- 非交易时段任务直接退出，不产生请求。

### 10.3 当前口径与限制

- 启动（11-18 10:50）按各模型**最近一次信号**的目标组合买入：多因子来自 09-30、
  自训来自 11-12、LLM 来自 11-17；不是当天新信号。下一期调仓需要先刷新信号、重新生成
  清单，再按子账本做差额单。
- 市价单会**分批成交**，子账本按成交明细记账；收盘前未成交部分仍算现金，不强行补单。
- 同花顺实际费用可能低于我们的估算（页面按 0.10%/0.20% 计），跨模型比较口径一致。
- 3 万是平台的记账基准，不是同花顺的资金档位；实际下单按 3 万目标折算，共用 20 万账户。
- 多因子小市值 3 万只能买到 4–8 只（整手限制），现金占比高于模型目标，这是真实约束。
