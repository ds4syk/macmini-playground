#!/usr/bin/env bash
# Run the daily close job: fetch bars, settle open + mark close, ask each model
# for tomorrow's target weights, export the dashboard, then push it to Tencent.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG="${PAPER_DAILY_LOG:-/tmp/paper-trader-daily.log}"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily start ====="
  python3 -u -m paper_trader.runner daily
  status=$?
  echo "daily exit=$status"
  if [[ $status -eq 0 ]]; then
    "$ROOT/scripts/deploy.sh" --skip-export || echo "deploy failed"
  fi
} >>"$LOG" 2>&1
