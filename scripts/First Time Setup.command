#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Mac Engine Room — First Time Setup
#  Double-click this file to install what's needed, then build and
#  install the app. Only needs to be run once on a new machine.
# ═══════════════════════════════════════════════════════════════

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Mac Engine Room — First Time Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. Check for Python 3 ────────────────────────────────────
echo "→ Checking for Python 3..."
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "  ✗ Python 3 is not installed."
    echo "    Download it from: https://www.python.org/downloads/"
    echo ""
    echo "  After installing Python, run this setup again."
    echo ""
    echo "Press any key to close..."
    read -n 1
    exit 1
fi
PYVER=$(python3 --version 2>&1)
echo "  ✓ Found $PYVER"
echo ""

# ── 2. Install Python packages (pinned versions) ─────────────
echo "→ Installing Python packages..."
echo ""
pip3 install -r "$PROJECT_ROOT/Application Files/requirements.txt" \
    2>&1 | grep -E "^(Successfully|Requirement|Installing)" || true
echo ""
echo "  ✓ All packages installed"
echo ""

# ── 3. Build, install and launch the app ─────────────────────
echo "→ Building Mac Engine Room.app..."
echo ""
if ! "$PROJECT_ROOT/scripts/install.sh"; then
    echo "  ✗ Build failed — check output above"
    echo ""
    echo "Press any key to close..."
    read -n 1
    exit 1
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✓ Setup complete! Mac Engine Room is running in the menu bar."
echo ""
echo "  It opens at login and keeps itself up to date; you can"
echo "  turn either off in its Settings."
echo "  To rebuild your own changes: double-click 'Build Mac Engine Room.command' in scripts"
echo "═══════════════════════════════════════════════════════════"
echo ""

read -p "Press any key to close..." -n 1
osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
