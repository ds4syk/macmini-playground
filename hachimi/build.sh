#!/usr/bin/env bash
# Build the mod and install it into the local Steam copy of Slay the Spire 2.
# Override the game location with STS2_GAME_DIR if needed.
set -euo pipefail

cd "$(dirname "$0")"

dotnet build -c Release src/Hachimi.csproj

GAME="${STS2_GAME_DIR:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2}"
MODS_DIR="$GAME/SlayTheSpire2.app/Contents/MacOS/mods"
DEST="$MODS_DIR/Hachimi"

if [[ ! -d "$MODS_DIR" ]]; then
  echo "mods directory not found: $MODS_DIR" >&2
  exit 1
fi

mkdir -p "$DEST"
cp out/Hachimi.dll out/Hachimi.json "$DEST/"
echo "installed: $DEST"
ls -la "$DEST"
