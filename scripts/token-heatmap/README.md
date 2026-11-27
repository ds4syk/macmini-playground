# 本机 + lab DSH Token 热力图

把 macmini 与 lab server 两套 DSH 的会话日志合在一起统计：总 token、按机器 / 模型 /
月份拆分、人民币费用估算，并画一张按天的日历热力图（合计 + 每台机器各一张）。

## 范围

- 只统计 DSH 会话：`~/.dsh/sessions/*/*/session.v4.jsonl.zstd`，旧版
  `session.jsonl.zstd` 作为兜底（同一会话优先 v4）。
- 计入 `assistant/message` 的 provider `usage` 和 `compaction/summary` 摘要调用；
  按 assistant message id 去重，fork / seeded 会话复制的父会话历史只计一次。
- 不含 Claude Code / Codex / OpenCode 等其它 CLI，也不含会话标题生成、Search /
  Fetch / OCR / 语音等没有本地 provider usage 事件的调用。
- 费用是按公开单价的估算，不是账单。

## 取 lab 数据

lab 上不需要装 Python 依赖，只要把会话目录同步到本机：

```bash
rsync -a --delete lab:.dsh/sessions/ /tmp/lab-dsh-sessions/
```

## 运行

```bash
python3 scripts/token-heatmap/combined_token_heatmap.py \
  --source local="$HOME/.dsh/sessions" \
  --source lab=/tmp/lab-dsh-sessions \
  --out-dir scripts/token-heatmap
```

首次运行会拉取 `https://models.dev/api.json` 并缓存到
`~/.cache/dsh-token-usage/models.dev.json`；24 小时内复用，`--refresh-models`
强制刷新。渲染 PNG 需要本机有 Chrome / Chromium。

输出：

- `token-heatmap.png` — 1241px 宽的热力图：合计日历 + 本机 / lab 分图。
- `token-heatmap-report.md` — 人读报告。
- `token-heatmap-usage.json` — 机器可读明细（分机器、分模型、分月、逐日）。

## 计价口径

- DeepSeek 路由按 DeepSeek 官方 CNY 价目（2026-11 核对）：每条请求按自身时间戳判断
  高峰 / 空闲，空闲价为高峰价一半；缓存写入并入缓存未命中输入。
  `deepseek-flash` 高峰价：缓存命中 ¥0.04/M、未命中 ¥2/M、输出 ¥8/M。
- 其他模型按 models.dev 的 USD 单价 × `--usd-cny`（默认 7.1）换算；provider 需与
  DSH 路由对应，匹配不到就标为未定价，不猜价格。
- `zai-coding-cn` 的 GLM 路由在 models.dev 上是订阅制（$0/token），报告会另给一个
  按其它 provider 列表价折算的参考值。
- 统计时刻以日志中 `step/start` 为准，缺失时退回事件时间；按 Asia/Shanghai 归日。

## 2026-11-27 快照

| 项目 | 数值 |
| --- | ---: |
| 范围 | 2026-08-29 09:01 — 2026-11-27 19:43（Asia/Shanghai） |
| 会话 / 请求 | 214 个有记录会话（258 个会话目录）/ 38,183 次 |
| 合计 Token | 10,511,541,617（105.12 亿） |
| 本机 / lab | 48.40 亿（¥199.51）/ 56.72 亿（¥272.52） |
| 缓存读取 / 未缓存输入 / 输出 | 104.35 亿 / 4592.0 万 / 3084.3 万 |
| 缓存命中 | 99.56% |
| API 费用 | **¥472.03** |
| 单日峰值 | 2026-11-03 · 16.44 亿 |

热力图见 `token-heatmap.png`，完整分模型 / 分月 / 逐日数据见
`token-heatmap-report.md` 与 `token-heatmap-usage.json`。
