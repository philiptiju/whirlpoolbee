#!/bin/bash
# Claude Local Dashboard - Stop Script

PID_FILE="$HOME/.claude-dashboard/dashboard.pid"

echo "Stopping Claude Local Dashboard..."

if [ ! -f "$PID_FILE" ]; then
    echo "Dashboard is not running (no PID file found)"
    exit 0
fi

PID=$(cat "$PID_FILE")

if ps -p "$PID" > /dev/null 2>&1; then
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Dashboard stopped (PID: $PID)"
else
    echo "Dashboard was not running"
    rm -f "$PID_FILE"
fi
