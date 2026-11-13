#!/usr/bin/env bash
# Deploy the token-gated dashboard to the Tencent Cloud host.
# Usage: scripts/deploy.sh [--skip-export]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PAPER_DEPLOY_ENV:-$HOME/.config/paper-trader/deploy.env}"
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${PAPER_DOMAIN:?PAPER_DOMAIN missing}"
: "${PAPER_TOKEN:?PAPER_TOKEN missing}"
: "${PAPER_HOST:?PAPER_HOST missing}"
: "${PAPER_REMOTE_ROOT:=/srv/paper}"
PAPER_IP="${PAPER_IP:-}"

cd "$ROOT"
if [[ "${1:-}" != "--skip-export" ]]; then
  python3 -m paper_trader.runner export
fi

if [[ ! -f web/data.json ]]; then
  echo "web/data.json missing; run the replay first" >&2
  exit 1
fi

echo "Uploading dashboard to $PAPER_HOST:$PAPER_REMOTE_ROOT/$PAPER_TOKEN ..."
ssh "$PAPER_HOST" "sudo mkdir -p '$PAPER_REMOTE_ROOT/$PAPER_TOKEN' && sudo chown -R ubuntu:ubuntu '$PAPER_REMOTE_ROOT'"
rsync -az --delete web/ "$PAPER_HOST:$PAPER_REMOTE_ROOT/$PAPER_TOKEN/"
ssh "$PAPER_HOST" "sudo chmod -R a+rX '$PAPER_REMOTE_ROOT/$PAPER_TOKEN'"

# Make sure the site block exists and is valid; config is idempotent.
scp -q "$ROOT/scripts/update_caddy.py" "$PAPER_HOST:/tmp/update_caddy.py"
ssh "$PAPER_HOST" "PAPER_TOKEN='$PAPER_TOKEN' PAPER_DOMAIN='$PAPER_DOMAIN' PAPER_ROOT='$PAPER_REMOTE_ROOT' python3 /tmp/update_caddy.py >/tmp/update_caddy.log && docker exec caddy caddy validate --config /etc/caddy/Caddyfile >/tmp/caddy-validate.log 2>&1 && docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/tmp/caddy-reload.log 2>&1 && echo caddy-reloaded"

if [[ -n "$PAPER_IP" ]]; then
  code=$(curl -s -m 20 --resolve "$PAPER_DOMAIN:443:$PAPER_IP" -o /dev/null -w '%{http_code}' "https://$PAPER_DOMAIN/$PAPER_TOKEN/")
  echo "token page HTTP $code"
  if [[ "$code" != "200" ]]; then
    echo "deployment check failed" >&2
    exit 1
  fi
  root_code=$(curl -s -m 20 --resolve "$PAPER_DOMAIN:443:$PAPER_IP" -o /dev/null -w '%{http_code}' "https://$PAPER_DOMAIN/")
  echo "root HTTP $root_code (expected 404)"
fi

echo "Dashboard: https://$PAPER_DOMAIN/$PAPER_TOKEN/"
