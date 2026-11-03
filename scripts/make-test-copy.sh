#!/bin/bash
# Builds, then makes ~/Applications/Mac Engine Room Test.app and prints its path. The copy has
# its own bundle identifier, fan helper label and socket, so it can't touch the installed app
# or its helper. Running it and cleaning up: "Testing fan control" in docs/DEVELOPMENT.md.

set -euo pipefail
cd "$(dirname "$0")/.."

APP=$(scripts/build.sh)
T="$HOME/Applications/Mac Engine Room Test.app"
ID=$(security find-identity -v -p codesigning | awk '/"Apple Development/ {print $2; exit}')
if [[ -z "$ID" ]]; then
    echo "No Apple Development certificate: a test copy needs one to have fan control." >&2
    exit 1
fi

mkdir -p "$HOME/Applications"
rm -rf "$T"
ditto "$APP" "$T"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.pythonflow.monitor.test" "$T/Contents/Info.plist"
P="$T/Contents/Library/LaunchDaemons"
mv "$P/com.pythonflow.monitor.fans.plist" "$P/com.pythonflow.monitor.test.fans.plist"
/usr/libexec/PlistBuddy -c "Set :Label com.pythonflow.monitor.test.fans" \
  -c "Set :AssociatedBundleIdentifiers:0 com.pythonflow.monitor.test" \
  -c "Set :Sockets:Listener:SockPathName /var/run/com.pythonflow.monitor.test.fans.sock" \
  "$P/com.pythonflow.monitor.test.fans.plist"
codesign --force -s "$ID" -o runtime -i com.pythonflow.monitor.test.fans "$T/Contents/MacOS/Mac Engine Room Fans" 2>/dev/null
codesign --force -s "$ID" "$T" 2>/dev/null
codesign --verify --deep --strict "$T"
echo "$T"
