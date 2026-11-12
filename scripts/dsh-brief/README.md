# DSH 日报 / 周报（20:00 飞书卡片）

一套卡片逻辑，两个调度：

- **日报**：周一至周六 20:00（周日由周报覆盖）
- **周报**：周日 20:00

`brief.py` 把工作、token 用量（按模型拆分、含任务占比饼图和 API 费用换算）和计划渲染成一张
PNG 卡片，用 `gpt`（dsh-superagent）机器人发到部署时指定的飞书群。

所有数据都在本机读取，不调用模型：

| 内容 | 来源 |
| --- | --- |
| Token 用量 / 模型请求 / 会话数 / 缓存命中 | `~/.dsh/sessions/*/*/session.v4.jsonl.zstd` 里每条 `assistant/message` 的 `usage`（provider 计费口径） |
| 模型 Token / API 费用 | 每条请求的 `source.provider` / `source.model`，按 DeepSeek 官方价目和该请求所在时段（高峰 / 空闲）换算 |
| 任务 Token 占比饼图 | 按 session 目录名 `worker-<ticketId>` 归因到对应工单；个人会话与编排会话归入「非任务会话」 |
| 今日/本周重点、风险、明日/下周重点 | `~/.dsh/state/superagent/orchestrator.sqlite` 中对应时段的工单、assignment 状态与 worker report |
| PR 是否待合并 | `gh pr view <url>` / `gh pr list -R ds4syk/macmini-playground --state open`，网络失败时只影响 PR 行 |

口径：

- 总 token = 未缓存输入 + 输出 + 缓存读取 + 缓存写入。
- 缓存命中 = 缓存读取 ÷（缓存读取 + 未缓存输入）。
- 日报窗口：当天 00:00 → 运行时刻；周报窗口：本周一 00:00 → 运行时刻。
- 饼图：显示 token 最多的 4 个任务，其余任务合并为「其他任务」，个人/编排会话为「非任务会话」；
  圆心是本周期内所有任务 token 占总 token 的比例。
- 数字使用「万 / 亿」，与群里此前的卡片一致。

## API 价格换算

卡片上的「API 费用」和「模型 Token / 费用」是估算，不是账单。单价取
[DeepSeek 官方「模型 & 价格」](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)（2026-10 核对），
单位：元 / 百万 tokens。

| 模型 | 缓存命中输入 | 缓存未命中输入 | 输出 |
| --- | --- | --- | --- |
| `deepseek-flash` | 0.04 | 2 | 8 |
| `deepseek-v4-pro` | 0.30 | 9 | 27 |

- 上表是**高峰价**；官方空闲时段价为高峰价的一半。高峰时段为北京时间周一至周五
  （不含法定节假日）9:00–12:00、14:00–18:00，其余时间（含周末、节假日全天）为空闲时段。
- 每条请求按它自己的时间戳判断高峰 / 空闲，所以同一周期内的请求可能有一部分半价。
- 缓存写入不单列，按官方口径并入「缓存未命中输入」。
- 旧模型名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 按 `deepseek-flash` 计价。
- 未收录的模型名不猜价格，费用显示「—」，卡片脚注会注明有未计价模型。
- 不含网页搜索 / 抓取等按次计费项；实际扣费以官方账单为准。
- 价格表在 `brief.py` 顶部（`CNY_PEAK_PRICES` / `MODEL_ALIASES` / `BUILTIN_HOLIDAYS`），
  官方调价后改这里并重新 `install.sh`。

## 安装（一次性）

```bash
./scripts/dsh-brief/install.sh --chat oc_xxx   # 首次安装：指定目标群，装日报 + 周报两个 LaunchAgent
./scripts/dsh-brief/install.sh --test          # 安装后再渲染日报、周报各一张（不发送）
```

群 id 只写入 `~/datas/dsh-brief/config.env` 与两个 LaunchAgent，不提交到仓库；
之后不带 `--chat` 重跑会沿用该文件。可以用 `--chat` 换群。

安装后：

- LaunchAgent：`~/Library/LaunchAgents/local.dsh-daily-brief.plist`（周一至周六 20:00）、
  `~/Library/LaunchAgents/local.dsh-weekly-brief.plist`（周日 20:00）
- 程序与产出：`~/datas/dsh-brief/`（`out/` 保存最近生成的卡片）
- 日志：`~/datas/dsh-brief/logs/{daily,weekly}-brief.{out,err}.log`

卸载：

```bash
./scripts/dsh-brief/install.sh --uninstall
```

## 手动运行

```bash
# 生成卡片但不发送（默认周报，本周）
python3 scripts/dsh-brief/brief.py --no-post

# 日报
python3 scripts/dsh-brief/brief.py --period daily --no-post

# 发一版预览（卡片带「预览版」标记）
python3 scripts/dsh-brief/brief.py --period daily --preview

# 指定区间（补跑/测试）
python3 scripts/dsh-brief/brief.py --period weekly --start 2026-11-09 --end 2026-11-11T23:30 --no-post

# 真正发到群里
python3 scripts/dsh-brief/brief.py --period daily
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--period daily` / `weekly` | 日报或周报，默认 `weekly` |
| `--start` / `--end` | 统计区间，`YYYY-MM-DD` 或 ISO 时间；缺省按 period 计算 |
| `--preview` | 卡片加「预览版」标记 |
| `--chat oc_xxx` | 发送目标，部署时写入 LaunchAgent 的 `DSH_BRIEF_CHAT` |
| `--repo owner/name` | 用于查询待合并 PR 的仓库，默认 `ds4syk/macmini-playground` |
| `--no-post` / `--dry-run` | 只渲染，不发送 |
| `--demo` | 用示例数据渲染，用于检查版式（卡片会标注「示例数据」） |

## 结构

```
brief.py                        # 数据汇总 + HTML 排版 + Chromium 截图 + 飞书发送（日报/周报共用）
install.sh                      # 安装/卸载两个 LaunchAgent
local.dsh-daily-brief.plist     # 日报 LaunchAgent 模板
local.dsh-weekly-brief.plist    # 周报 LaunchAgent 模板
example-daily.png               # 日报示例卡片（示例数据）
example-weekly.png              # 周报示例卡片（示例数据）
```

渲染使用 Playwright 缓存里的 `chrome-headless-shell`（`~/Library/Caches/ms-playwright/...`），
找不到浏览器时自动降级为同内容的 Markdown 文本消息；`zstd` 用于解压 DSH 会话日志。

## 注意

- 卡片内容来自本机工单与会话统计，可能包含你其他飞书会话里的任务标题；请只把本仓库/PR
  用于代码评审，不要把实际卡片截图贴到公开位置。
- 运行是幂等的：同一时刻只会有一个实例（`~/datas/dsh-brief/brief.lock`）。
- 如果 20:00 机器在睡眠，launchd 会在唤醒后补一次。
- 周日不跑日报，只发周报；需要周日也发日报时，把 `local.dsh-daily-brief.plist` 的
  `StartCalendarInterval` 改成单个 20:00 字典即可。
- 修改脚本后重新执行 `install.sh` 才会更新 `~/datas` 下的副本。
