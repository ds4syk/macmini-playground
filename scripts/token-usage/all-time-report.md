# 本机 DSH 历史全部 Token 用量

- 统计范围：2026-08-29 09:01 — 2026-11-26 01:59（Asia/Shanghai）
- 范围：仅本机 macmini 的 DSH 会话；不含其它机器，也不含 Claude Code / Codex / OpenCode 等 CLI。
- 数据源：`~/.dsh/sessions/*/*/session.v4.jsonl.zstd` 与旧版 `session.jsonl.zstd`（同一会话优先 v4）；计入每条 `assistant/message` 的 provider `usage` 和 `compaction/summary` 摘要调用。
- 会话：85 个有 token 记录（扫描 100 个会话目录）
- 模型请求：17,023
- 合计 Token：**4,801,172,375**（48.01 亿）
- 缓存命中率：99.54%
- API 费用估算：**¥197.56**

## Token 构成

| 类别 | Token | 占比 |
| --- | ---: | ---: |
| 缓存读取 | 4,766,037,568 | 99.27% |
| 未缓存输入 | 22,112,454 | 0.46% |
| 缓存写入 | 0 | 0.00% |
| 输出 | 13,022,353 | 0.27% |
| **合计** | **4,801,172,375** | 100% |

## 按模型

| 模型 | Provider | Token | 请求 | 费用 | 单价来源 |
| --- | --- | ---: | ---: | ---: | --- |
| `deepseek-flash` | `deepseek-official` | 4,583,551,353 | 15,780 | ¥190.40 | DeepSeek 官方价目 |
| `deepseek-v4-flash` | `opencode-go` | 163,076,283 | 683 | ¥7.16 | models.dev · opencode-go（deepseek-v4.1-flash） |
| `glm-5.3` | `zai-coding-cn` | 42,849,841 | 408 | ¥0.00 | models.dev · zhipuai-coding-plan |
| `glm-5.4` | `zai-coding-cn` | 9,864,552 | 76 | ¥0.00 | models.dev · zhipuai-coding-plan |
| `glm-5.3-flash` | `zai-coding-cn` | 1,830,346 | 76 | ¥0.00 | models.dev · zhipuai-coding-plan |
| **合计** | | **4,801,172,375** | **17,023** | **¥197.56** | |

## 按月

| 月份 | Token | 请求 |
| --- | ---: | ---: |
| 2026-08 | 229,340,703 | 1,243 |
| 2026-10 | 1,700,125,259 | 5,873 |
| 2026-11 | 2,871,706,413 | 9,907 |

## 计价口径

- DeepSeek 路由按 DeepSeek 官方 CNY 价目（2026-11 核对）：每条请求按自身时间戳判断高峰/空闲，空闲价为高峰价一半；缓存写入并入缓存未命中输入。`deepseek-flash` 高峰价：缓存命中 ¥0.04/M、未命中 ¥2/M、输出 ¥8/M。
- 其他模型按 models.dev（2026-11-26T01:55:47+08:00）USD 单价 × 7.1 换算为人民币；provider 需与 DSH 路由对应，匹配不到就不猜价格。
- `opencode-go` 会话里的 `deepseek-v4-flash` 按 models.dev 上该网关当前的 `deepseek-v4.1-flash` 列表价计（$0.15 / $0.6 / $0.003 per M）。
- `glm-5.3` 当前路由在 models.dev 上是订阅制（$0/token）；若按该模型在其他 provider 的列表价（zai）折算，约 $12.89。
- `glm-5.4` 当前路由在 models.dev 上是订阅制（$0/token）；若按该模型在其他 provider 的列表价（tencent-token-plan）折算，约 $4.10。
- `glm-5.3-flash` 当前路由在 models.dev 上是订阅制（$0/token）；若按该模型在其他 provider 的列表价（zai）折算，约 $0.09。
- 若把订阅制的模型按上述列表价计入，合计约 ¥318.85（仅作参考）。
- 不含 Search / Fetch / OCR / 语音等按次计费项，也不含会话标题生成、网页搜索等没有本地 provider usage 事件的调用；费用是公开单价估算，不是账单。
