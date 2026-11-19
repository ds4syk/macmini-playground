#!/usr/bin/env bash
# 交易时段每 2 分钟同步成交、估值并部署实盘跟踪页；非交易时段直接退出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

dow=$(date +%u)
hhmm=$((10#$(date +%H%M)))
if [[ "$dow" -le 5 && "$hhmm" -ge 925 && "$hhmm" -le 1510 ]]; then
  log="$HOME/.config/paper-trader/live.log"
  if ! PYTHONPATH="$ROOT" /usr/bin/python3 -m paper_trader.live run --deploy >>"$log" 2>&1; then
    # 会话过期时先从仍登录的浏览器重导 Cookie，再重试一次
    "$ROOT/.venv-ths/bin/python" -m paper_trader.ths_http export-cookies >>"$log" 2>&1 || true
    PYTHONPATH="$ROOT" /usr/bin/python3 -m paper_trader.live run --deploy >>"$log" 2>&1 || true
  fi
fi
