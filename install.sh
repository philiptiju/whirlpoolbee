#!/bin/bash
# Claude Local Dashboard - Installation Script
# macOS only

set -e

echo "========================================"
echo "Claude Local Dashboard - Installer"
echo "========================================"
echo ""

# Check if running on macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo "Error: This installer is for macOS only"
    exit 1
fi

# Get installation directory
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$HOME/.claude-dashboard"
LOG_DIR="$DATA_DIR/logs"
VENV_DIR="$INSTALL_DIR/venv"

echo "Installation directory: $INSTALL_DIR"
echo "Data directory: $DATA_DIR"
echo ""

# Create directories
echo "[1/8] Creating directories..."
mkdir -p "$DATA_DIR"
mkdir -p "$LOG_DIR"
chmod 700 "$DATA_DIR"
chmod 700 "$LOG_DIR"

# Check Python version
echo "[2/8] Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "Found Python $PYTHON_VERSION"

# Create virtual environment
echo "[3/8] Creating virtual environment..."
if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment already exists, removing..."
    rm -rf "$VENV_DIR"
fi
python3 -m venv "$VENV_DIR"

# Install dependencies
echo "[4/8] Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip > /dev/null
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# Initialize database
echo "[5/8] Initializing database..."
"$VENV_DIR/bin/python3" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
from src.database import Database
from src.utils.config import Config

Config.ensure_directories()
db = Database(str(Config.DB_PATH))
db.initialize()
print('Database initialized at:', Config.DB_PATH)
"

# Install launchd agents
echo "[6/8] Installing launchd agents..."
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCHD_DIR"

for plist in "$INSTALL_DIR"/launchd/*.plist; do
    if [ -f "$plist" ]; then
        filename=$(basename "$plist")
        dest="$LAUNCHD_DIR/$filename"

        # Replace placeholders
        sed -e "s|INSTALL_PATH|$INSTALL_DIR|g" \
            -e "s|VENV_PATH|$VENV_DIR|g" \
            -e "s|LOG_PATH|$LOG_DIR|g" \
            "$plist" > "$dest"

        echo "  Installed: $filename"
    fi
done

# Load launchd agents
echo "[7/8] Loading launchd agents..."
for plist in "$LAUNCHD_DIR"/com.local.claude.*.plist; do
    if [ -f "$plist" ]; then
        launchctl load "$plist" 2>/dev/null || true
        echo "  Loaded: $(basename "$plist")"
    fi
done

# Setup shell integration
echo "[8/8] Setting up shell integration..."
ZSHRC="$HOME/.zshrc"

# Check if already integrated
if grep -q "# Claude Dashboard wrapper" "$ZSHRC" 2>/dev/null; then
    echo "  Shell integration already configured"
else
    echo "" >> "$ZSHRC"
    echo "# Claude Dashboard wrapper - Auto-generated" >> "$ZSHRC"
    echo "# DO NOT EDIT THIS SECTION MANUALLY" >> "$ZSHRC"
    cat >> "$ZSHRC" << 'EOF'
claude() {
    local session_id="claude-session-$$-$RANDOM"
    local start_time=$(date +%s)
    local cwd="$PWD"

    # Run actual claude command
    command claude "$@"
    local exit_code=$?

    # Log session (async, don't wait)
    (
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))

        # Could log to a simple file or make API call
        # For now, just placeholder
    ) &

    return $exit_code
}
EOF
    echo "  Shell integration added to ~/.zshrc"
    echo "  Run 'source ~/.zshrc' to activate"
fi

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Start the dashboard: ./start.sh"
echo "  2. Open browser to: http://127.0.0.1:8765"
echo "  3. View logs: tail -f $LOG_DIR/*.log"
echo ""
echo "Management commands:"
echo "  Start:   ./start.sh"
echo "  Stop:    ./stop.sh"
echo "  Uninstall: ./uninstall.sh"
echo ""
echo "Database: $DATA_DIR/claude.db"
echo "Logs:     $LOG_DIR/"
echo ""
