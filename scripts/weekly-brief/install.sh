#!/bin/bash
# Install (or remove) the Sunday 20:00 DSH weekly brief launchd job.
#
#   ./install.sh --chat oc_xxx     install/refresh the job for that Feishu chat
#   ./install.sh                   refresh using ~/datas/dsh-weekly-brief/config.env
#   ./install.sh --test            install, then render one card without posting
#   ./install.sh --uninstall       stop the job and remove the LaunchAgent
#
# The script and its chat id live in ~/datas/dsh-weekly-brief so the LaunchAgent
# never depends on this checkout; the chat id is not committed to the repo.

set -euo pipefail

LABEL="local.dsh-weekly-brief"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/datas/dsh-weekly-brief"
CONFIG="$DEST/config.env"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
PYTHON="${PYTHON:-$(command -v python3)}"

CHAT=""
MODE="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --chat) CHAT="${2:?--chat needs a value}"; shift 2 ;;
    --chat=*) CHAT="${1#--chat=}"; shift ;;
    --test) MODE="test"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "uninstall" ]; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (data kept in $DEST)"
  exit 0
fi

mkdir -p "$DEST/logs" "$DEST/out" "$HOME/Library/LaunchAgents"
if [ -z "$CHAT" ] && [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
  CHAT="${DSH_WEEKLY_BRIEF_CHAT:-}"
fi
if [ -z "$CHAT" ]; then
  echo "no chat configured; run: $0 --chat oc_xxx" >&2
  exit 2
fi

printf 'DSH_WEEKLY_BRIEF_CHAT=%s\n' "$CHAT" > "$CONFIG"
chmod 600 "$CONFIG"
cp "$SRC/weekly_brief.py" "$DEST/weekly_brief.py"
chmod +x "$DEST/weekly_brief.py"

"$PYTHON" - "$SRC/local.dsh-weekly-brief.plist" "$PLIST" "$DEST" "$PYTHON" "$HOME" "$CHAT" <<'PY'
import sys
from pathlib import Path

template, target, dest, python, home, chat = sys.argv[1:7]
text = Path(template).read_text()
for placeholder, value in (
    ("__DEST__", dest),
    ("__PYTHON__", python),
    ("__HOME__", home),
    ("__CHAT__", chat),
):
    text = text.replace(placeholder, value)
Path(target).write_text(text)
PY
chmod 644 "$PLIST"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"

echo "installed $LABEL -> $DEST/weekly_brief.py (chat ${CHAT:0:10}…)"
launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state|program|path|interval" | head -8 || true
echo "logs: $DEST/logs/"

if [ "$MODE" = "test" ]; then
  "$PYTHON" "$DEST/weekly_brief.py" --no-post --preview \
    --out "$DEST/out/test-$("$PYTHON" -c 'import datetime;print(datetime.date.today())').png"
fi
