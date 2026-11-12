#!/bin/bash
# Install (or remove) the DSH daily/weekly brief launchd jobs.
#
#   ./install.sh --chat oc_xxx     install/refresh both jobs for that Feishu chat
#   ./install.sh                   refresh using ~/datas/dsh-brief/config.env
#   ./install.sh --test            install, then render both cards without posting
#   ./install.sh --uninstall       stop both jobs and remove their LaunchAgents
#
# Daily runs Monday-Saturday 20:00; weekly runs Sunday 20:00. The script and its
# chat id live in ~/datas/dsh-brief so the LaunchAgents never depend on this
# checkout; the chat id is not committed to the repo.

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/datas/dsh-brief"
CONFIG="$DEST/config.env"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
PYTHON="${PYTHON:-$(command -v python3)}"
LABELS=("local.dsh-daily-brief" "local.dsh-weekly-brief")
PLISTS=("local.dsh-daily-brief.plist" "local.dsh-weekly-brief.plist")

CHAT=""
MODE="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --chat) CHAT="${2:?--chat needs a value}"; shift 2 ;;
    --chat=*) CHAT="${1#--chat=}"; shift ;;
    --test) MODE="test"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "uninstall" ]; then
  for label in "${LABELS[@]}"; do
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    rm -f "$LAUNCH_AGENTS/$label.plist"
  done
  echo "removed ${LABELS[*]} (data kept in $DEST)"
  exit 0
fi

mkdir -p "$DEST/logs" "$DEST/out" "$LAUNCH_AGENTS"
if [ -z "$CHAT" ] && [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG"
  CHAT="${DSH_BRIEF_CHAT:-}"
fi
if [ -z "$CHAT" ]; then
  echo "no chat configured; run: $0 --chat oc_xxx" >&2
  exit 2
fi

printf 'DSH_BRIEF_CHAT=%s\n' "$CHAT" > "$CONFIG"
chmod 600 "$CONFIG"
cp "$SRC/brief.py" "$DEST/brief.py"
chmod +x "$DEST/brief.py"
cp "$SRC/README.md" "$DEST/README.md" 2>/dev/null || true

for index in "${!LABELS[@]}"; do
  label="${LABELS[$index]}"
  template="$SRC/${PLISTS[$index]}"
  target="$LAUNCH_AGENTS/$label.plist"
  "$PYTHON" - "$template" "$target" "$DEST" "$PYTHON" "$HOME" "$CHAT" <<'PY'
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
  chmod 644 "$target"
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$target"
  launchctl enable "gui/$UID_NUM/$label"
  echo "installed $label"
  launchctl print "gui/$UID_NUM/$label" | grep -E "state|program|interval" | head -6 || true
done

echo "script: $DEST/brief.py (chat ${CHAT:0:10}…)"
echo "logs: $DEST/logs/"

if [ "$MODE" = "test" ]; then
  TODAY="$("$PYTHON" -c 'import datetime;print(datetime.date.today())')"
  "$PYTHON" "$DEST/brief.py" --period daily --no-post --preview --out "$DEST/out/test-daily-$TODAY.png"
  "$PYTHON" "$DEST/brief.py" --period weekly --no-post --preview --out "$DEST/out/test-weekly-$TODAY.png"
fi
