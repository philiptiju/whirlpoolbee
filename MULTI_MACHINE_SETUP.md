# Multi-Machine Setup Guide

This guide explains how to set up Claude Local Dashboard to collect data from multiple client machines and display it on a central server.

## Architecture Overview

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  Client Machine │      │  Client Machine │      │  Client Machine │
│    (Machine 1)  │      │    (Machine 2)  │      │    (Machine N)  │
│                 │      │                 │      │                 │
│  client_agent.py│      │  client_agent.py│      │  client_agent.py│
└────────┬────────┘      └────────┬────────┘      └────────┬────────┘
         │                        │                        │
         │         HTTP POST      │         HTTP POST      │
         └────────────────────────┼────────────────────────┘
                                  │
                          ┌───────▼────────┐
                          │  Dashboard     │
                          │  Server        │
                          │  (This Machine)│
                          │                │
                          │  Port 8765     │
                          └────────────────┘
```

## Server Setup (One Machine)

The server machine hosts the dashboard and receives data from all client machines.

### 1. Start the Dashboard Server

On the server machine:

```bash
# Make sure you're in the dashboard directory
cd /path/to/claude-local-dashboard

# Start the server (accessible from other machines)
./venv/bin/python3 -m uvicorn src.api.main:app \
    --host 0.0.0.0 \
    --port 8765 \
    --log-level info
```

**Important**: Using `--host 0.0.0.0` makes the server accessible from other machines on your network.

### 2. Find Your Server IP Address

```bash
# On macOS/Linux
ifconfig | grep "inet " | grep -v 127.0.0.1

# On Windows
ipconfig
```

Note your server's IP address (e.g., `192.168.1.100`)

### 3. Open Firewall (if needed)

Make sure port 8765 is open in your firewall:

```bash
# macOS - add firewall rule
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /path/to/python

# Linux (ufw)
sudo ufw allow 8765

# Windows - use Windows Firewall settings
```

## Client Setup (Each Remote Machine)

Run the client agent on each machine you want to monitor.

### 1. Copy Files to Client Machine

Copy these files to each client machine:
- `client_agent.py`
- `requirements.txt` (if setting up fresh virtual environment)

### 2. Install Dependencies

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install required packages
pip install httpx aiosqlite
```

### 3. Run the Client Agent

```bash
# Replace SERVER_IP with your server's IP address
python client_agent.py --server http://SERVER_IP:8765

# Example:
python client_agent.py --server http://192.168.1.100:8765
```

### Optional: Custom Sync Interval

```bash
# Sync every 60 seconds (default is 300)
python client_agent.py --server http://192.168.1.100:8765 --interval 60
```

### 4. Run as Background Service (Optional)

**macOS/Linux:**

Create a systemd service or launchd plist:

```bash
# Example systemd service
sudo nano /etc/systemd/system/claude-agent.service
```

Add:
```ini
[Unit]
Description=Claude Dashboard Client Agent
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/dashboard
ExecStart=/path/to/dashboard/venv/bin/python client_agent.py --server http://SERVER_IP:8765
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable claude-agent
sudo systemctl start claude-agent
```

## Viewing Multi-Machine Data

### 1. Access the Dashboard

Open your browser and go to:
```
http://SERVER_IP:8765
```

### 2. Machine Selector Sidebar

- The left sidebar shows all registered machines
- Click **"All Machines"** to see aggregate data from all machines
- Click a specific machine name to view data only from that machine
- Active machines show a green "Active" badge
- Inactive machines (no heartbeat in last hour) show a red "Inactive" badge

### 3. Machine Information

Each machine shows:
- **Machine Name**: Hostname of the client machine
- **Machine ID**: Unique identifier (hostname_os)
- **Status**: Active (green) or Inactive (red)

## How It Works

### Data Flow

1. **Client Agent**:
   - Runs on each client machine
   - Monitors local Claude Code usage (reads from `~/.config/claude/sessions.db`)
   - Sends data to server via HTTP POST every 5 minutes (configurable)
   - Sends heartbeat to mark machine as active

2. **Server**:
   - Receives data from all clients
   - Stores in central database with machine_id
   - Serves web dashboard
   - Provides machine filtering via sidebar

### Data Sent from Clients

- Machine registration (on startup)
- Heartbeat (every sync interval)
- Claude sessions
- Token usage
- Messages/conversations
- Git commits

### Security Considerations

**For Local Networks:**
- Current setup is designed for trusted local networks
- No authentication/encryption by default
- All machines should be on the same private network

**For Production/Internet:**
If you need to expose this over the internet, add:
- HTTPS/TLS encryption
- API authentication (bearer tokens)
- Rate limiting
- Firewall rules

## Troubleshooting

### Client Can't Connect to Server

1. **Check server is running:**
   ```bash
   curl http://SERVER_IP:8765/api/health
   ```

2. **Check firewall:**
   - Make sure port 8765 is open on server
   - Test from client: `telnet SERVER_IP 8765`

3. **Check network:**
   - Client and server on same network?
   - Can you ping the server?

### Machine Not Showing in Sidebar

1. **Check client agent is running:**
   ```bash
   ps aux | grep client_agent
   ```

2. **Check client logs:**
   - Look for "✓ Registered machine" message
   - Look for error messages

3. **Check server received registration:**
   ```bash
   curl http://SERVER_IP:8765/api/machines
   ```

### Machine Shows as Inactive

- Machine hasn't sent heartbeat in last hour
- Check if client agent is still running
- Check network connectivity
- Restart client agent

## Example Network Setup

### Simple Home Network

```
Router: 192.168.1.1
├── Server:    192.168.1.100 (Dashboard)
├── Laptop:    192.168.1.101 (Client 1)
├── Desktop:   192.168.1.102 (Client 2)
└── Work Mac:  192.168.1.103 (Client 3)
```

Server command:
```bash
./venv/bin/python3 -m uvicorn src.api.main:app --host 0.0.0.0 --port 8765
```

Client commands (on each machine):
```bash
# Laptop
python client_agent.py --server http://192.168.1.100:8765

# Desktop
python client_agent.py --server http://192.168.1.100:8765

# Work Mac
python client_agent.py --server http://192.168.1.100:8765
```

## API Endpoints

### Client Endpoints (for agents to send data)

- `POST /api/client/register` - Register machine
- `POST /api/client/heartbeat` - Update last_seen
- `POST /api/client/session` - Create session
- `POST /api/client/token_usage` - Log token usage
- `POST /api/client/message` - Log message
- `POST /api/client/git_metrics` - Log git commit

### Machine Management

- `GET /api/machines` - List all machines
- `GET /api/machines/active` - List active machines

### Dashboard Data (with machine filtering)

All existing endpoints accept optional `?machine_id=xxx` parameter:
- `GET /api/stats/overview?machine_id=xxx`
- `GET /api/stats/sessions?machine_id=xxx`
- `GET /api/stats/tokens?machine_id=xxx`
- etc.

## Migration from Local-Only Setup

If you were using the local-only version:

1. **Run migration script** (already done):
   ```bash
   ./venv/bin/python3 migrate_to_multi_machine.py
   ```
   This adds machine_id to all existing data and creates a default machine for your current data.

2. **Restart server** with network access:
   ```bash
   ./venv/bin/python3 -m uvicorn src.api.main:app --host 0.0.0.0 --port 8765
   ```

3. **Your existing data** is now associated with your server machine and visible in the dashboard.

## Next Steps

1. Deploy server on a dedicated machine or VM
2. Install client agent on all your development machines
3. Set up clients to run as background services
4. Monitor all your Claude Code usage from one central dashboard!
