#!/bin/bash
# Claude Local Dashboard - Uninstall Script

set -e

echo "========================================"
echo "Claude Local Dashboard - Uninstaller"
echo "========================================"
echo ""

# Stop the dashboard
if [ -f "./stop.sh" ]; then
    ./stop.sh
fi

# Unload launchd agents
echo "[1/4] Unloading launchd agents..."
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
for plist in "$LAUNCHD_DIR"/com.local.claude.*.plist; do
    if [ -f "$plist" ]; then
        launchctl unload "$plist" 2>/dev/null || true
        echo "  Unloaded: $(basename "$plist")"
    fi
done

# Remove launchd agents
echo "[2/4] Removing launchd agents..."
rm -f "$LAUNCHD_DIR"/com.local.claude.*.plist

# Remove shell integration
echo "[3/4] Removing shell integration..."
ZSHRC="$HOME/.zshrc"
if [ -f "$ZSHRC" ]; then
    # Remove the Claude Dashboard section
    sed -i.backup '/# Claude Dashboard wrapper/,/^}$/d' "$ZSHRC" 2>/dev/null || true
    echo "  Removed from ~/.zshrc"
    echo "  Backup saved to ~/.zshrc.backup"
fi

# Ask about data removal
echo ""
echo "[4/4] Data removal..."
read -p "Remove all data (~/.claude-dashboard)? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$HOME/.claude-dashboard"
    echo "  Data removed"
else
    echo "  Data preserved at: $HOME/.claude-dashboard"
fi

echo ""
echo "========================================"
echo "Uninstallation Complete!"
echo "========================================"
echo ""
echo "To reinstall, run: ./install.sh"
echo ""
