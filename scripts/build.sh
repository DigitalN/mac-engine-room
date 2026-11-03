#!/bin/bash
# Builds Mac Engine Room.app and prints its path. Used by install.sh and package.sh.
#
#   scripts/build.sh            sign with your Apple Development certificate, if you have one
#   scripts/build.sh --adhoc    sign locally only (the app then can't update itself)
#
# Installed copies only accept updates signed by the same developer team, so releases
# must be signed with a certificate from the same Apple account every time.

set -euo pipefail
cd "$(dirname "$0")/.."

# Build outside the repo: folders synced by iCloud Drive (like ~/Documents) add
# Finder metadata to app bundles, which codesign rejects.
WORK="$HOME/Library/Caches/MacEngineRoom-build"
APP="$WORK/dist/Mac Engine Room.app"
LOG="$WORK/build.log"

if [[ "${1:-}" == "--adhoc" ]]; then
    IDENTITY="-"
    echo "Signing locally (ad hoc); this copy won't update itself." >&2
else
    # The first *valid* Apple Development identity (expired certificates stay in the keychain).
    IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Apple Development/ {print $2; exit}')
    if [[ -z "$IDENTITY" ]]; then
        cat >&2 <<'MSG'
No Apple Development certificate found, so signing locally (ad hoc): this copy works
but won't update itself. A free Apple ID is enough to get one: Xcode → Settings →
Apple Accounts → your "(Personal Team)" → Manage Certificates… → + → Apple Development.
MSG
        IDENTITY="-"
    fi
fi

rm -rf "$WORK"
mkdir -p "$WORK"
echo "Building…" >&2
if ! python3 -m PyInstaller "Application Files/MacEngineRoom.spec" --noconfirm --clean \
    --distpath "$WORK/dist" --workpath "$WORK/build" >"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    echo "Build failed; full log: $LOG" >&2
    exit 1
fi

# The fan helper ("Fan Helper"), the only part that runs as root: Swift, so it needs Xcode
# or its Command Line Tools. launchd runs it from the bundle once the app registers it.
HELPER="$APP/Contents/MacOS/Mac Engine Room Fans"
if xcrun --find swiftc >/dev/null 2>&1; then
    echo "Building the fan helper…" >&2
    if ! xcrun swiftc -O -target arm64-apple-macos13.0 -import-objc-header "Fan Helper/bridge.h" \
        "Fan Helper/main.swift" "Fan Helper/Fans.swift" "Fan Helper/SMC.swift" -lbsm -o "$HELPER" >>"$LOG" 2>&1; then
        tail -20 "$LOG" >&2
        echo "Building the fan helper failed; full log: $LOG" >&2
        exit 1
    fi
    mkdir -p "$APP/Contents/Library/LaunchDaemons"
    cp "Fan Helper/com.pythonflow.monitor.fans.plist" "$APP/Contents/Library/LaunchDaemons/"
else
    echo "No Swift compiler (xcode-select --install adds one), so this build has no fan control." >&2
fi

xattr -cr "$APP"
# --deep signs everything PyInstaller bundled; the helper then gets its own identifier and
# the hardened runtime, and the app is sealed again around it.
if ! codesign --force --deep -s "$IDENTITY" "$APP" >>"$LOG" 2>&1 \
    || { [[ -e "$HELPER" ]] && ! { codesign --force -s "$IDENTITY" -o runtime -i com.pythonflow.monitor.fans "$HELPER" \
                                   && codesign --force -s "$IDENTITY" "$APP"; } >>"$LOG" 2>&1; } \
    || ! codesign --verify --deep --strict "$APP" >>"$LOG" 2>&1; then
    tail -5 "$LOG" >&2
    echo "Signing failed; full log: $LOG" >&2
    exit 1
fi
TEAM=$(codesign -d --verbose=2 "$APP" 2>&1 | sed -n 's/^TeamIdentifier=//p')
echo "Signed (team: $TEAM)." >&2

echo "$APP"
