#!/bin/bash
# Start Claude Dashboard Server with network access for multi-machine support

echo "Starting Claude Dashboard Server..."
echo ""
echo "Server will be accessible at:"
echo "  - Local:   http://127.0.0.1:8765"
echo "  - Network: http://$(ipconfig getifaddr en0 2>/dev/null || hostname):8765"
echo ""
echo "Press Ctrl+C to stop"
echo ""

./venv/bin/python3 -m uvicorn src.api.main:app \
    --host 0.0.0.0 \
    --port 8765 \
    --log-level info
