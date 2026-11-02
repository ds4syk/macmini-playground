# macmini-monitor

Mac mini 的轻量 CPU / 内存监控：由 launchd 每分钟执行一次，**异常持续 10 分钟**才通过飞书告警；恢复也需要持续 5 分钟才确认，避免瞬时尖峰和抖动造成告警轰炸。

只依赖 macOS 自带的 Python 3 和系统命令（`top`、`vm_stat`、`sysctl`、`launchctl`），不需要 `pip install` 任何东西。

## 监控与告警规则

每 60 秒采样一次。两个指标各自独立判断：

| 指标 | 进入告警区（满足任一） | 触发条件 | 恢复条件 |
| --- | --- | --- | --- |
| CPU | 整机 busy% ≥ 85% | 连续 10 分钟 | busy% ≤ 65%，连续 5 分钟 |
| 内存 | 可用内存 ≤ 10% | 连续 10 分钟 | 可用内存 ≥ 15%、压缩内存 ≤ 15%、内核压力正常，连续 5 分钟 |
| 内存 | 压缩内存 ≥ 25% | 连续 10 分钟 | 同上 |
| 内存 | Swap ≥ 30% 且可用内存 ≤ 15% | 连续 10 分钟 | 同上 |
| 内存 | 内核内存压力为 warning / critical | 连续 10 分钟 | 同上 |

补充说明：

- **CPU busy%** 取自 `top -l 2 -s 1` 的第二次采样（`100 - idle`），不是开机以来的平均值；告警消息同时带上 1/5/15 分钟 load average 和核心数。
- **可用内存** = `vm_stat` 的 `free + inactive + speculative`。macOS 会把大量内存用作可回收缓存，单看 `free` 会长期接近 0，不能作为“内存紧张”的依据；`active` 页面也不应算作可用。当前口径下，可用内存跌到 10% 以下才是真正需要干预的紧张状态。
- **压缩内存**（compressor）和 **Swap** 是内存压力的直接后果。Swap 单独偏高不算告警（macOS 会保留已分配的交换空间），只有同时可用内存偏低才计入。
- **内核内存压力** 读取 `kern.memorystatus_vm_pressure_level`（1=正常、2=警告、4=严重），这是系统自己的压力判断，作为补充信号；该 sysctl 不存在时自动忽略。
- 告警持续期间每 60 分钟发一条“仍告警”提醒；恢复后发一条恢复通知。告警期间不会因为同一条件反复刷屏。

阈值都在 `~/.config/macmini-monitor/config.json` 里，改完下一轮采样立即生效；只有 `sample_interval_seconds` 会写进 launchd 的 `StartInterval`，修改后需要重新执行一次 `install`。

## 安装

在仓库目录执行：

```bash
python3 macmini-resource-monitor/macmini_monitor.py install --chat-id oc_你的群聊ID
```

`install` 会：

1. 把脚本复制到 `~/Library/Application Support/macmini-monitor/macmini_monitor.py`（之后即使仓库目录被清理也不影响运行）；
2. 生成 `~/.config/macmini-monitor/config.json`（权限 600，包含 chat_id，**不要提交到仓库**）；
3. 写入并加载 `~/Library/LaunchAgents/local.macmini-resource-monitor.plist`；
4. 立即执行第一轮采样（正常时不会发消息）。

`--chat-id` 也可以用环境变量 `FEISHU_CHAT_ID` 提供，避免出现在 shell 历史里：

```bash
FEISHU_CHAT_ID=oc_xxx python3 macmini-resource-monitor/macmini_monitor.py install
```

安装后验证：

```bash
python3 "$HOME/Library/Application Support/macmini-monitor/macmini_monitor.py" status
python3 "$HOME/Library/Application Support/macmini-monitor/macmini_monitor.py" test-notify
```

`test-notify` 会发一条明确标注为【测试】的飞书消息，用于确认机器人/群聊配置正确。

## 通知方式

支持两种，按配置优先级：

1. **飞书自定义机器人 webhook**（推荐给其它机器/独立部署）：在群设置里添加“自定义机器人”，把 webhook 地址填到 `notify.webhook_url`。只支持不带签名校验的 webhook。
2. **lark-cli / lark-bot**（本机现有配置）：`notify.chat_id` + `notify.lark_cli`。`install` 会自动探测 `~/.local/bin/lark-bot` 或 `~/.local/bin/lark-cli`，也可以显式传 `--lark-cli /path/to/lark-cli`。机器人必须已经在目标群里。

```bash
# 使用 webhook
python3 macmini-resource-monitor/macmini_monitor.py install \
  --webhook-url 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx'

# 使用现有飞书机器人 CLI
python3 macmini-resource-monitor/macmini_monitor.py install \
  --chat-id oc_xxx \
  --lark-cli "$HOME/.local/bin/lark-bot"
```

发送失败会按 `notify.max_attempts`（默认 3 次）重试；仍失败则保留待发通知，下一分钟继续尝试，不会因为一次网络抖动丢掉告警。

## 常用命令

```bash
# 查看当前指标、告警状态、LaunchAgent 状态
python3 "$HOME/Library/Application Support/macmini-monitor/macmini_monitor.py" status

# 只采样和判断，不写状态、不发消息（调试阈值用）
python3 "$HOME/Library/Application Support/macmini-monitor/macmini_monitor.py" once --dry-run

# 发测试通知
python3 "$HOME/Library/Application Support/macmini-monitor/macmini_monitor.py" test-notify

# 更新脚本（从仓库重新复制并重载 LaunchAgent；保留配置）
python3 macmini-resource-monitor/macmini_monitor.py install

# 卸载（保留配置/状态/日志）
python3 macmini-resource-monitor/macmini_monitor.py uninstall

# 卸载并删除配置/状态/日志
python3 macmini-resource-monitor/macmini_monitor.py uninstall --purge
```

`status --json` 输出机器可读的完整状态；`status --sample` 强制立即再采样一次。

## 状态、日志与防抖

- 状态文件：`~/Library/Application Support/macmini-monitor/state.json`
  - 记录每个指标的首次异常时间、告警开始时间、最近一次成功通知时间、待发通知等。
  - 原子写入；launchd 重启或短暂故障不会丢失“持续了多久”。
  - 两次采样间隔超过 5 分钟（睡眠/关机/长时间未运行）会重置连续计数，避免把跨空档的两次读数误判为持续异常。
- 日志：`~/Library/Logs/macmini-monitor/monitor.log`
  - JSON Lines，每轮一行，包含 CPU/内存指标、是否排队通知、是否发送成功。5 MB 轮转，保留 3 份。
  - launchd 的 stdout/stderr 在 `launchd.out.log` / `launchd.err.log`，正常情况下为空。
- 同一次告警的 `--idempotency-key` 固定，发送超时重试不会在群里产生重复消息。
- 如果告警通知本身还没发出去条件就恢复了，会丢弃这条过期告警，不补发；如果已经发过告警，则发一条恢复通知。

## 为什么这样定阈值

- **10 分钟持续窗口**：CPU 编译、视频导出、系统索引都会产生几分钟的高负载；10 分钟能过滤掉大多数正常突发，又能在真正卡死前给出提醒。
- **85% CPU**：10 核机器上意味着持续有 8.5 核在工作，已经接近“机器明显变慢”的程度；不是 100% 才告警。
- **10% 可用内存**：16 GB 机器约 1.6 GB 可用。macOS 会先回收 inactive/speculative 页面，低于这个值通常已经开始压缩内存或使用 Swap。
- **压缩内存 25% / Swap 30%**：压缩内存超过 4 GB（16 GB 机器）或 Swap 超过 4.8 GB 且可用内存低于 15%，说明系统已经在为内存四处腾挪，适合作为补充告警条件。
- **恢复迟滞**：恢复线比告警线更宽松（CPU 65%、可用内存 15%、压缩内存 15%），并且要持续 5 分钟，避免在阈值附近来回抖动导致“告警-恢复”循环。

所有数字都可以在配置里按机器实际用途调整。

## 测试

```bash
cd macmini-resource-monitor
python3 -m unittest discover -s tests -v
```

单元测试覆盖：持续窗口、异常样本重置、恢复迟滞、提醒间隔、未送达告警的过期处理、采样间隔空档、内存各触发条件、配置校验、状态原子读写、plist 生成、webhook 与 lark-cli 通知参数。

## 限制

- 只监控 CPU 和内存；磁盘、网络、温度不在告警范围内（`status` 会显示根分区剩余空间，仅作参考）。
- 依赖 launchd 的用户会话；机器关机或用户未登录时不会采样。
- 飞书通知依赖机器人 token 或 webhook 有效；如果通知通道本身失效，`status` 和日志会显示最近一次发送错误。
- `lark-cli` 发送方式使用本机已安装的飞书机器人配置；换机器部署需要自行提供 `lark-cli` 或改用 webhook。
