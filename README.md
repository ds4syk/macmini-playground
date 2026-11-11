# 策略实验室 · 多模型 A 股日线纸面交易平台

个人用的策略验证平台：**5 个大模型** 各自管理 **1 万 / 3 万 / 5 万 / 10 万** 四档本金的 A 股日线组合。每个交易日收盘后用当日及以前的数据做决策，下一交易日开盘价成交；前端按同花顺 / 券商 App 的信息密度展示净值、持仓、成交与模型决策，并部署到腾讯云，通过一个带随机 token 的私密链接访问。

> 模拟盘，仅供策略研究与模型对比，不构成任何投资建议。

## 当前实例

- 仪表盘：`https://paper.suyingke.chat/<token>/`（token 只保存在本机 `~/.config/paper-trader/deploy.env`，不进入仓库）
- 组合：5 个模型 × 4 档本金 = 20 个独立账本
- 区间：2026-09-01 起回放 46 个交易日，之后每个交易日 15:35 自动续跑
- 本地服务：行情缓存、SQLite 账本、决策调度、静态站点构建全部在 Mac mini 上运行
- 云端：腾讯云 2C/3G 上的 Caddy 只做静态文件 + token 路径校验，不承载交易逻辑

## 模型与本金

| 模型 | Provider | 说明 |
| --- | --- | --- |
| DeepSeek V4.1 Flash | DeepSeek | 低延迟，适合每天高频调用 |
| DeepSeek V4 Pro | DeepSeek | 推理更强，关闭 thinking 以控制时延 |
| GLM-5.3 | 智谱 | 旗舰模型 |
| GLM-5.3 Flash | 智谱 | 低延迟版本 |
| GLM-5.2 | 智谱 | 上一代旗舰，作为对照 |

同一模型在 4 档本金下使用同一份目标权重，因此可以直接观察 **小资金买不起一手 / 佣金最低 5 元 / 整手取整** 对收益的侵蚀。

## 交易口径

- 标的池：30 只 A 股流动性较好的行业龙头（白酒、银行、保险、电池、半导体、券商、有色、电力、煤炭、家电、医药、光伏等），只允许从池内选股。
- 数据：腾讯行情前复权日线（`proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline`），本地 SQLite 缓存，刷新时按代码 upsert。
- 时点：T 日收盘后决策，T+1 日开盘价成交，杜绝未来函数。
- 约束：最多 8 只、单票 ≤ 25%、权重合计 ≤ 100%、不做空、不加杠杆、100 股整手、T+1。
- 费用：佣金万 2.5（最低 5 元）、卖出印花税万 5、过户费万 0.1；开盘价触及涨跌停时跳过对应方向的成交。
- 指标：累计收益、今日盈亏、最大回撤、夏普、年化波动、胜率、换手、成交笔数；沪深 300 作为基准。

## 架构

```
Mac mini (本地)
┌────────────────────────────────────────────────────────────┐
│ 腾讯行情 ──► market.py ──► SQLite (bars / equity / trades) │
│                              ▲                             │
│  launchd 15:35 ──► runner.py ─┴─► engine.py（撮合/估值）   │
│                       │                                    │
│                       └─► models.py ──► DeepSeek / 智谱 API│
│                              │                             │
│                              └─► export.py ──► web/data.json│
└──────────────────────────────┬─────────────────────────────┘
                               │ rsync + Caddy reload
Tencent Cloud (2C/3G)          ▼
┌────────────────────────────────────────────────────────────┐
│ Caddy :443  https://paper.suyingke.chat/<token>/           │
│   handle_path /<token>/* → /srv/paper/<token>/ (static)    │
│   其他所有路径 → 404                                        │
└────────────────────────────────────────────────────────────┘
```

## 本地运行

要求：macOS + Python 3.9+，无需第三方 Python 包；前端 ECharts 已 vendor 到 `web/assets/vendor/`。

1. 私密配置（不要提交）：

```bash
mkdir -p ~/.config/paper-trader
cat > ~/.config/paper-trader/.env <<'EOF'
DEEPSEEK_API_KEY=...
ZAI_CODING_CN_API_KEY=...
EOF
chmod 600 ~/.config/paper-trader/.env
```

2. 初始化与回放：

```bash
python3 -m paper_trader.runner fetch                 # 拉取/刷新行情（慢，失败可重试）
python3 -m paper_trader.runner replay \
  --start 2026-09-01 --end 2026-11-11               # 46 个交易日 × 5 个模型
python3 -m paper_trader.runner export                # 生成 web/data.json
python3 -m paper_trader.runner serve --port 8899     # http://127.0.0.1:8899/
```

3. 每日收盘后：

```bash
python3 -m paper_trader.runner daily                 # 结算今日开盘 + 收盘估值 + 生成明日决策
scripts/deploy.sh --skip-export                      # 推送静态站
```

`scripts/install_scheduler.sh` 会安装一个工作日 15:35 的 launchd 任务，执行 `scripts/daily.sh`（跑 daily + 部署）。

## 部署到腾讯云

首次部署的前置条件（已在本机实例上配置）：

1. Cloudflare DNS：`paper.suyingke.chat A 43.134.76.212`，DNS only（不走 CDN，避免 token 路径被缓存）。
2. Caddy 使用 Cloudflare DNS-01 自动签发证书；`scripts/update_caddy.py` 会幂等地写入站点块：
   - 只放行 `/<token>/` 及其子路径；
   - `/<token>` 308 跳到 `/<token>/`；
   - 其他路径一律 404；
   - 响应带 `noindex, nofollow`、`no-store`。
3. 宿主机 `/srv/paper` 以只读方式挂载进 caddy 容器（`docker-compose.yml` 中 `- /srv/paper:/srv/paper:ro`）。这一步只需要做一次。

之后每次发布：

```bash
scripts/deploy.sh                  # export + rsync + caddy validate/reload + HTTP 检查
scripts/deploy.sh --skip-export    # 复用已有 web/data.json
```

token 轮换：修改 `~/.config/paper-trader/deploy.env` 里的 `PAPER_TOKEN` 后重新执行 `scripts/deploy.sh`。旧 token 路径会立即失效；`update_caddy.py` 会替换旧站点块，并把旧配置留作 `Caddyfile.bak-paper-*`。

## 本地算力结论（Mac mini M4 / 16GB）

- 实际运行：行情、回放、账本、调度、静态站构建都在本机完成，回放 46 个交易日、20 个组合的 CPU/内存占用都很低（Python 进程常驻 < 100MB，峰值 < 300MB）。
- 推理：16GB 统一内存可以跑单个 7B/8B 量化模型（约 4–6GB 权重），但同时服务 5 个当代模型不现实；本平台按日频调用 DeepSeek / 智谱 API，每天只有 5 次调用，成本和时延都可控。
- 如果希望完全本地推理，可把 `models.py` 的 provider 指向本机 Ollama / MLX 的 OpenAI 兼容端点；日频决策对吞吐要求不高，但 7B 级模型的中文金融决策质量会明显弱于当前 5 个模型。

## 目录

```
paper_trader/
  config.py      模型、股票池、本金、费用等静态配置
  market.py      腾讯前复权日线抓取 + SQLite 缓存
  store.py       SQLite schema 与读写
  engine.py      目标权重撮合、费用、T+1、估值、指标
  strategy.py    行情特征与模型提示词
  models.py      多 provider 调用、JSON 解析与权重约束
  runner.py      fetch / replay / daily / export / serve
  export.py      生成前端 data.json
web/             静态仪表盘（index.html + app.js + styles.css + ECharts）
scripts/         deploy.sh / daily.sh / install_scheduler.sh / update_caddy.py
tests/           engine / 费用 / 权重归一化单元测试
docs/screenshots before.png（初始骨架）与 after*.png（最终仪表盘）
```

## 验证

```bash
python3 -m unittest discover -s tests -v
```

覆盖：费用（含最低佣金、印花税）、权重裁剪/归一化/未知代码、买入与收盘估值、同日 T+1 卖出保护。

回放结果与前端截图见 `docs/screenshots/` 与 PR 描述。

## 已知局限

- 历史区间是 **回放**：模型在 2026-11 被要求基于当时可见的数据做决策，不是当时真实运行的记录；回放仅用于初始化展示和验证链路。
- 模型输出具有随机性（temperature 0.4），不同日期重跑可能得到不同权重。
- 使用前复权价格近似真实成交价，未模拟分红送转的现金/股份变动。
- 开盘价一次性成交，未模拟滑点、盘口深度、涨跌停排队；涨跌停仅做方向性跳过。
- 单一数据源（腾讯行情），未做多源交叉校验。
- 模型 API 与服务可用性依赖外部账号；密钥只放在 `~/.config/paper-trader/.env`，不进入 git、镜像或前端。
