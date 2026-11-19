#!/usr/bin/env bash
# 安装/更新「交易时段每 2 分钟刷新实盘跟踪页」的 launchd 任务。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.suyingke.papertrader.live"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"

sed "s#__ROOT__#$ROOT#g" "$ROOT/scripts/$LABEL.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
echo "已安装 ${LABEL}（每 120 秒，交易时段 09:25–15:10 运行）"
echo "日志：$HOME/.config/paper-trader/live.log"
