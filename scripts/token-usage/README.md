# 本机 DSH 历史全部 Token 用量

一次性 / 可重复运行的全量统计：读取本机 macmini 的 DSH 会话日志，汇总历史全部 token，
并按公开单价折算 API 费用。

- **DeepSeek**：DeepSeek 官方 CNY 价目（2026-11 核对），每条请求按自身时间戳判断
  高峰 / 空闲（空闲半价）；缓存写入并入缓存未命中输入。
- **其他模型**：`https://models.dev/api.json` 上对应 provider / model 的 USD 单价，
  按 `--usd-cny`（默认 7.1）换算；provider 对不上时不猜价格，费用显示「未定价」。

## 范围

只统计本机 macmini 的 DSH 会话，不含其它机器，也不含 Claude Code / Codex / OpenCode
等 CLI。另一台 lab server 的 DSH 用量需要在那台机器上单独统计。

## 运行

```bash
python3 scripts/token-usage/all_time.py --out-dir scripts/token-usage
python3 scripts/token-usage/all_time.py --start 2026-10-01 --end 2026-11-01
python3 scripts/token-usage/all_time.py --refresh-models --no-card
```

输出：

- `all-time-report.md` — 人读报告（token 构成、按模型、按月、计价口径）。
- `all-time-card.png` — 与 DSH 日报同风格的 1241×1200 卡片。
- `all-time-usage.json` — 机器可读的汇总。

models.dev 目录缓存在 `~/.cache/dsh-token-usage/models.dev.json`，默认 24 小时内复用；
`--refresh-models` 强制重新获取。

## 口径

- 会话目录同时兼容新版 `session.v4.jsonl.zstd` 和旧版 `session.jsonl.zstd`，同一会话
  优先 v4，避免迁移产生的重复计数。
- 计费单位是 `assistant/message`（带 `usage` 且 `message.source.kind == "model"`）
  加 `compaction/summary` 摘要调用；按 assistant message id 去重。
- `Token = 未缓存输入 + 输出 + 缓存读取 + 缓存写入`。
- `opencode-go` 会话里的 `deepseek-v4-flash` 按该网关当前的 `deepseek-v4.1-flash`
  列表价计。
- 费用是公开单价估算，不是账单；不含 Search / Fetch / OCR / 语音等按次计费项，也不含会话标题生成、网页搜索等没有本地 provider usage 事件的调用。

## 2026-11-26 快照

| 项目 | 数值 |
| --- | ---: |
| 范围 | 2026-08-29 09:01 — 2026-11-26 01:59（Asia/Shanghai） |
| 会话 / 请求 | 85 个有记录会话（100 个会话目录）/ 17,023 次 |
| 合计 Token | 4,801,172,375（48.01 亿） |
| 缓存读取 / 未缓存输入 / 输出 | 4,766,037,568 / 22,112,454 / 13,022,353 |
| 缓存命中 | 99.54% |
| API 费用 | **¥197.56** |

`deepseek-flash` 占 4,583,551,353 token（15,780 请求，¥190.40）；`opencode-go` 的
`deepseek-v4-flash` 163,076,283 token（683 请求，¥7.16）；`glm-5.3` 42,849,841 token
（408 请求）、`glm-5.4` 9,864,552（76 请求）、`glm-5.3-flash` 1,830,346（76 请求）
走 Zhipu Coding Plan，models.dev 标价 $0/token。若把三个订阅制 GLM 按 models.dev
列表价另计（$12.89 + $4.10 + $0.09），合计约 **¥318.85**（仅参考）。

14 个旧版会话（共 229,340,703 token）与 `session_projcache` 的 `tokenUsage` 投影逐会话
一致；10 次 compaction 摘要调用（4,740,169 token）已计入。
