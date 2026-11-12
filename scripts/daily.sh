#!/usr/bin/env bash
# Run the daily close job: fetch bars, settle open + mark close, ask each model
# for tomorrow's target weights, retry failed provider calls (the 智谱 coding
# plan has a 5-hour quota that often resets around 17:00), export and deploy.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG="${PAPER_DAILY_LOG:-/tmp/paper-trader-daily.log}"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily start ====="
  python3 -u -m paper_trader.runner daily
  daily_status=$?
  echo "daily exit=$daily_status"
  python3 -u -m paper_trader.runner retry
  retry_status=$?
  echo "retry exit=$retry_status"
  python3 -u -m paper_trader.local_models --marks || echo "local marks failed"
  python3 -u -m paper_trader.runner export
  echo "export exit=$?"
  if [[ $daily_status -eq 0 || $retry_status -eq 0 ]]; then
    "$ROOT/scripts/deploy.sh" --skip-export || echo "deploy failed"
  fi
} >>"$LOG" 2>&1
