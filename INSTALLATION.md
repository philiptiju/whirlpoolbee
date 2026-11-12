# Installation Guide

This guide covers how to install and run the Claude Local Dashboard in different scenarios.

## Table of Contents

1. [Quick Start (Single Machine)](#quick-start-single-machine)
2. [Multi-Machine Setup](#multi-machine-setup)
3. [Manual Installation](#manual-installation)
4. [Troubleshooting](#troubleshooting)

---

## Quick Start (Single Machine)

For monitoring Claude Code usage on a single machine with local dashboard access.

### Prerequisites

- macOS (tested on macOS 10.15+)
- Python 3.11 or higher
- Git (for repository metrics)

### Step 1: Clone or Copy the Project

```bash
# If you have the project folder already, navigate to it
cd claude-local-dashboard

# Or clone from repository (if applicable)
# git clone <repo-url> claude-local-dashboard
# cd claude-local-dashboard
```

### Step 2: Run the Installer

```bash
# Make scripts executable
chmod +x install.sh start.sh stop.sh

# Run the installer
./install.sh
```

The installer will:
- Create data directory at `~/.claude-dashboard`
- Set up Python virtual environment
- Install all dependencies
- Initialize SQLite database
- Set up background collectors (optional)

### Step 3: Start the Dashboard

```bash
./start.sh
```

### Step 4: Open the Dashboard

Open your browser and go to: **http://127.0.0.1:8765**

### Stop the Dashboard

```bash
./stop.sh
```

---

## Multi-Machine Setup

For monitoring Claude Code usage across multiple machines from a central dashboard.

### Architecture

```
Client Machine 1 ──┐
Client Machine 2 ──┼──> Central Server Dashboard
Client Machine N ──┘
```

### A. Server Installation (Central Dashboard)

**Prerequisites:**
- Python 3.11+
- Network access between machines

**Step 1: Install the Dashboard**

```bash
cd claude-local-dashboard

# Run installer (creates venv and database)
chmod +x install.sh start_server.sh
./install.sh
```

**Step 2: Run Migration (if upgrading from single-machine)**

```bash
# Only needed if you have existing data
chmod +x migrate_to_multi_machine.py
./venv/bin/python3 migrate_to_multi_machine.py
```

**Step 3: Start Server with Network Access**

```bash
# Start server accessible from other machines
./start_server.sh
```

Or manually:

```bash
./venv/bin/python3 -m uvicorn src.api.main:app \
    --host 0.0.0.0 \
    --port 8765 \
    --log-level info
```

**Step 4: Find Your Server IP**

```bash
# macOS/Linux
ifconfig | grep "inet " | grep -v 127.0.0.1

# Windows
ipconfig
```

Note your server's IP address (e.g., `192.168.1.100`)

**Step 5: Open Firewall (if needed)**

```bash
# macOS
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $(which python3)

# Linux (ufw)
sudo ufw allow 8765
```

**Step 6: Access Dashboard**

Open browser: `http://SERVER_IP:8765`

### B. Client Installation (Each Machine to Monitor)

**Step 1: Copy Client Files**

Copy these files to each client machine:
- `client_agent.py`
- `requirements.txt` (optional, for fresh install)

**Step 2: Install Dependencies**

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install required packages
pip install httpx aiosqlite
```

**Step 3: Run Client Agent**

```bash
# Replace SERVER_IP with your server's IP address
python client_agent.py --server http://SERVER_IP:8765

# Example:
python client_agent.py --server http://192.168.1.100:8765

# Custom sync interval (default: 300 seconds)
python client_agent.py --server http://192.168.1.100:8765 --interval 60
```

**Step 4: Verify Connection**

You should see:
```
✓ Registered machine: <machine_name> (<machine_id>)
✓ Synced X sessions, X token usage records
```

Check the server dashboard - your machine should appear in the left sidebar.

### C. Run Client as Background Service (Optional)

**macOS (launchd):**

Create `~/Library/LaunchAgents/com.claude.dashboard.client.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.dashboard.client</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python</string>
        <string>/path/to/client_agent.py</string>
        <string>--server</string>
        <string>http://SERVER_IP:8765</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-client.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-client.error.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.claude.dashboard.client.plist
```

**Linux (systemd):**

Create `/etc/systemd/system/claude-client.service`:

```ini
[Unit]
Description=Claude Dashboard Client Agent
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/dashboard
ExecStart=/path/to/venv/bin/python client_agent.py --server http://SERVER_IP:8765
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable claude-client
sudo systemctl start claude-client
```

---

## Manual Installation

If you prefer manual control over the installation process.

### Step 1: Create Virtual Environment

```bash
cd claude-local-dashboard
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Initialize Database

```bash
python3 -c "from src.database import Database; import asyncio; asyncio.run(Database('claude.db').init_schema())"
```

### Step 4: Start the Server

**Local-only (single machine):**
```bash
./venv/bin/python3 -m uvicorn src.api.main:app \
    --host 127.0.0.1 \
    --port 8765
```

**Network-accessible (multi-machine):**
```bash
./venv/bin/python3 -m uvicorn src.api.main:app \
    --host 0.0.0.0 \
    --port 8765
```

### Step 5: Open Dashboard

- **Local:** http://127.0.0.1:8765
- **Network:** http://YOUR_IP:8765

---

## Using the Dashboard

### Single Machine Mode

- Dashboard shows all your local Claude Code activity
- No sidebar (shows all data by default)
- Data stored in local SQLite database

### Multi-Machine Mode

- **Left Sidebar**: Shows all connected machines
- **"All Machines"**: View combined data from all machines
- **Individual Machine**: Click to filter data for that machine
- **Status Badges**:
  - 🟢 **Active**: Machine sent data in last hour
  - 🔴 **Inactive**: No data received in last hour

### Dashboard Pages

1. **Overview** - Real-time stats, active sessions
2. **Sessions** - Session history and details
3. **Usage** - Token usage and costs
4. **Projects** - Git repository activity
5. **System** - CPU, memory, network metrics

---

## Troubleshooting

### Port Already in Use

If you see: `ERROR: [Errno 48] address already in use`

```bash
# Find and kill the process using port 8765
lsof -ti:8765 | xargs kill -9

# Restart server
./start_server.sh
```

### Client Can't Connect to Server

**Check server is running:**
```bash
curl http://SERVER_IP:8765/api/health
```

**Check firewall:**
```bash
# Test from client machine
telnet SERVER_IP 8765
# or
nc -zv SERVER_IP 8765
```

**Check network connectivity:**
```bash
ping SERVER_IP
```

### Machine Not Showing in Sidebar

**Check client agent is running:**
```bash
ps aux | grep client_agent
```

**Check server received registration:**
```bash
curl http://SERVER_IP:8765/api/machines
```

**Check client logs:**
- Look for "✓ Registered machine" message
- Look for error messages

### Database Issues

**Reset database (WARNING: deletes all data):**
```bash
rm claude.db
./venv/bin/python3 migrate_to_multi_machine.py
```

**Check database:**
```bash
sqlite3 claude.db "SELECT * FROM machines;"
sqlite3 claude.db "SELECT COUNT(*) FROM sessions;"
```

### View Logs

**Server logs:**
```bash
# If running with start.sh
tail -f ~/.claude-dashboard/logs/dashboard.log

# If running manually
# Logs appear in terminal
```

**Client logs:**
```bash
# Client logs appear in terminal
# Or check background service logs:

# macOS
tail -f /tmp/claude-client.log

# Linux
sudo journalctl -u claude-client -f
```

---

## Uninstallation

### Single Machine

```bash
./uninstall.sh
```

This will:
- Stop the dashboard
- Remove background collectors
- Optionally remove all data

### Multi-Machine

**Server:**
```bash
# Stop server
lsof -ti:8765 | xargs kill -9

# Optional: remove data
rm -rf ~/.claude-dashboard
rm claude.db
```

**Clients:**
```bash
# Stop client agent
pkill -f client_agent.py

# Remove background service (if configured)
# macOS:
launchctl unload ~/Library/LaunchAgents/com.claude.dashboard.client.plist
rm ~/Library/LaunchAgents/com.claude.dashboard.client.plist

# Linux:
sudo systemctl stop claude-client
sudo systemctl disable claude-client
sudo rm /etc/systemd/system/claude-client.service
```

---

## Environment Variables

Optional configuration via environment variables:

```bash
# Database path (default: claude.db)
export CLAUDE_DB_PATH=/path/to/database.db

# Server port (default: 8765)
export CLAUDE_DASHBOARD_PORT=8080

# Log level (default: info)
export CLAUDE_LOG_LEVEL=debug
```

---

## Security Notes

### Local Network (Default)

- Server binds to `0.0.0.0:8765`
- Accessible from local network
- No authentication
- **Use only on trusted networks**

### Internet Exposure (Advanced)

If exposing over internet, add:
- **HTTPS/TLS** - Use reverse proxy (nginx, Caddy)
- **Authentication** - Add API tokens
- **Firewall** - Restrict access by IP
- **VPN** - Use VPN tunnel

Example nginx config:
```nginx
server {
    listen 443 ssl;
    server_name dashboard.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Next Steps

After installation:

1. ✅ Start using Claude Code normally
2. ✅ Dashboard automatically collects metrics
3. ✅ View analytics at http://127.0.0.1:8765
4. ✅ (Multi-machine) Install client on other machines
5. ✅ (Multi-machine) Monitor all machines from central dashboard

For detailed multi-machine setup, see **MULTI_MACHINE_SETUP.md**

For troubleshooting, see logs and check the Troubleshooting section above.
