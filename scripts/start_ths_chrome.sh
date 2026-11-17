#!/usr/bin/env bash
# 启动一个带 CDP 调试端口的 Chrome（独立 profile），用于同花顺网页模拟盘桥接。
# 已登录的会话保存在 profile 里，之后可直接复用；不会动用户日常 Chrome 的资料。
set -euo pipefail

PORT="${THS_CDP_PORT:-9222}"
PROFILE="${THS_CHROME_PROFILE:-$HOME/.config/paper-trader/chrome-profile}"
URL="${1:-https://moni.10jqka.com.cn/}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if curl -fsS -m 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "Chrome 调试端口 $PORT 已就绪。"
  exit 0
fi
if [[ ! -x "$CHROME" ]]; then
  echo "找不到 Google Chrome：$CHROME" >&2
  exit 1
fi
mkdir -p "$PROFILE"
LOG="$HOME/.config/paper-trader/chrome.log"
nohup "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check --disable-features=Translate \
  --window-size=1440,960 \
  "$URL" >"$LOG" 2>&1 &
echo "已启动 Chrome：CDP 127.0.0.1:${PORT}，profile ${PROFILE}，日志 ${LOG}"
echo "接着执行：python -m paper_trader.ths_web login"
