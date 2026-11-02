#!/bin/bash
# Builds Mac Engine Room, installs it in /Applications and launches it. A copy still
# called Pythonflow quits and moves to the Trash when the new one opens (rename.py).
#
#   scripts/install.sh            sign with your Apple Development certificate, if you have one
#   scripts/install.sh --adhoc    sign locally only (the app then can't update itself)

set -euo pipefail
cd "$(dirname "$0")/.."

DEST="/Applications/Mac Engine Room.app"

APP=$(scripts/build.sh "$@")

echo "Installing to $DEST…" >&2
pkill -x "Mac Engine Room" >/dev/null 2>&1 || true
sleep 1
rm -rf "$DEST"
ditto "$APP" "$DEST"
rm -rf "$(dirname "$APP")"

open "$DEST"
echo "Done. Mac Engine Room is running (look for its wattage in the menu bar)." >&2
