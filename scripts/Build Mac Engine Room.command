#!/bin/bash
# Double-click this file to rebuild Mac Engine Room.app from your copy of the code.
# Pulls the latest from your git remote, runs the tests, then builds, installs to
# /Applications and launches. (Installed copies also update themselves from GitHub
# releases; this is for running your own changes.)

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

echo "═══════════════════════════════════════════"
echo "  Building Mac Engine Room..."
echo "═══════════════════════════════════════════"
echo ""

echo "→ Pulling latest from git..."
git pull --ff-only 2>&1
echo ""

echo "→ Running tests..."
python3 -m pytest tests -q 2>&1 | tail -3
echo ""

if scripts/install.sh; then
    echo "✓ Build complete!"
    osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
else
    echo "✗ Build failed — check output above"
    echo ""
    echo "Press any key to close..."
    read -n 1
fi
