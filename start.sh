#!/bin/bash
# Claude Local Dashboard - Start Script

set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$INSTALL_DIR/venv"
PID_FILE="$HOME/.claude-dashboard/dashboard.pid"

echo "Starting Claude Local Dashboard..."

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "Dashboard is already running (PID: $PID)"
        echo "Visit: http://127.0.0.1:8765"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

# Start the dashboard
nohup "$VENV_DIR/bin/python3" "$INSTALL_DIR/src/api/main.py" \
    > "$HOME/.claude-dashboard/logs/dashboard.log" 2>&1 &

PID=$!
echo $PID > "$PID_FILE"

# Wait a moment for startup
sleep 2

# Check if it started successfully
if ps -p "$PID" > /dev/null 2>&1; then
    echo "Dashboard started successfully!"
    echo ""
    echo "Dashboard URL: http://127.0.0.1:8765"
    echo "PID: $PID"
    echo "Logs: tail -f $HOME/.claude-dashboard/logs/dashboard.log"
    echo ""
    echo "To stop: ./stop.sh"
else
    echo "Failed to start dashboard"
    cat "$HOME/.claude-dashboard/logs/dashboard.log"
    exit 1
fi
