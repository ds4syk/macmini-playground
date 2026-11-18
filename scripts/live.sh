#!/usr/bin/env bash
# 交易时段每 2 分钟同步成交、估值并部署实盘跟踪页；非交易时段直接退出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

dow=$(date +%u)
hhmm=$((10#$(date +%H%M)))
if [[ "$dow" -le 5 && "$hhmm" -ge 925 && "$hhmm" -le 1510 ]]; then
  PYTHONPATH="$ROOT" /usr/bin/python3 -m paper_trader.live run --deploy >> "$HOME/.config/paper-trader/live.log" 2>&1
fi
