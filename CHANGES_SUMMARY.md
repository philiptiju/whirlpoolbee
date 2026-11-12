# Multi-Machine Support - Changes Summary

## What Was Done

Claude Local Dashboard supports a **multi-machine monitoring system** where you can:
- Run the dashboard server on one machine
- Install client agents on multiple machines (Machine 1, Machine 2, etc.)
- View all machines' Claude Code usage in one central dashboard
- Click on any machine in the sidebar to filter data for that specific machine

## Key Changes Made

### 1. Database Schema Updates
- ✅ Added `machines` table to track all client machines
- ✅ Added `machine_id` column to all data tables (sessions, token_usage, messages, etc.)
- ✅ Added a migration script for existing databases
- ✅ Migration associates existing data with the machine running the script

### 2. Server API Endpoints
Added new endpoints for client machines to send data:
- `POST /api/client/register` - Register a new machine
- `POST /api/client/heartbeat` - Keep machine marked as active
- `POST /api/client/session` - Send session data
- `POST /api/client/token_usage` - Send token usage
- `POST /api/client/message` - Send conversation messages
- `POST /api/client/git_metrics` - Send git commit data
- `GET /api/machines` - List all registered machines
- `GET /api/machines/active` - List currently active machines

### 3. Client Agent Script
Created `client_agent.py`:
- Runs on each client machine
- Automatically registers the machine with server
- Syncs Claude Code data every 5 minutes (configurable)
- Sends heartbeat to mark machine as active

### 4. UI Enhancements
- ✅ Added **machine selector sidebar** on the left side of every page
- Shows all registered machines with:
  - Machine name (hostname)
  - Machine ID
  - Active/Inactive status (green/red badge)
- Click "All Machines" to see aggregate data from all machines
- Click a specific machine to filter data for that machine only
- Responsive design - sidebar collapses on smaller screens

### 5. Documentation
Created comprehensive guides:
- `MULTI_MACHINE_SETUP.md` - Complete setup instructions
- `start_server.sh` - Easy server startup script

## How to Use It

### Running on One Machine Only
Start the dashboard with `./start.sh`, then open it:
```bash
# Access the dashboard
open http://127.0.0.1:8765
```

Once registered, your machine appears under its hostname in the sidebar.

### Setting Up Multi-Machine Monitoring

#### On This Machine (Server):
```bash
# Start server with network access
./start_server.sh

# Or manually:
./venv/bin/python3 -m uvicorn src.api.main:app --host 0.0.0.0 --port 8765
```

Server will be accessible at:
- **Local**: http://127.0.0.1:8765
- **Network**: `http://<your-server-ip>:8765` — replace the placeholder with your server's address

#### On Each Client Machine:
```bash
# 1. Copy client_agent.py to the machine
# 2. Install dependencies
pip install httpx aiosqlite

# 3. Run the agent (replace with your server IP)
python client_agent.py --server "http://<your-server-ip>:8765"
```

The client will:
- Register itself automatically
- Appear in the sidebar within seconds
- Start syncing data every 5 minutes

## Files Created/Modified

### New Files:
- `client_agent.py` - Client agent for remote machines
- `migrate_to_multi_machine.py` - Database migration script for existing installations
- `start_server.sh` - Server startup script
- `MULTI_MACHINE_SETUP.md` - Detailed setup guide
- `CHANGES_SUMMARY.md` - This file

### Modified Files:
- `schema.sql` - Added machines table and machine_id columns
- `src/database.py` - Added machine management methods
- `src/api/main.py` - Added client API endpoints and machine endpoints
- `src/web/templates/base.html` - Added machine selector sidebar and JavaScript
- `src/web/static/css/style.css` - Added sidebar styles

## What You See in the Dashboard Now

### Machine Sidebar (Left Side):
```
╔══════════════════╗
║    Machines      ║
╠══════════════════╣
║ ● All Machines   ║  ← Click to see all data
║                  ║
║ ● server        ║  ← Example server hostname
║   Active         ║    Click to filter
╚══════════════════╝
```

### After Adding More Machines:
```
╔══════════════════╗
║    Machines      ║
╠══════════════════╣
║ ● All Machines   ║
║                  ║
║ ● server        ║
║   Active         ║
║                  ║
║ ● laptop_darwin  ║  ← New machine
║   Active         ║
║                  ║
║ ● desktop_linux  ║  ← Another machine
║   Inactive       ║
╚══════════════════╝
```

## Testing It Out

1. **View your current machine**:
   - Open http://127.0.0.1:8765
   - You should see the machine sidebar
   - Your registered machine should be listed under its hostname
   - Click it to filter data

2. **Add another machine** (optional for testing):
   - Open a new terminal
   - Run: `python client_agent.py --server http://127.0.0.1:8765`
   - Refresh the dashboard
   - You should see the new machine appear in the sidebar

## Network Diagram

```
Example Single-Machine Setup:
┌─────────────────────────────────┐
│  Dashboard Host (server)        │
│                                 │
│  ┌──────────┐   ┌────────────┐ │
│  │ Dashboard│   │   Sample   │ │
│  │  Server  │   │    Data    │ │
│  │  :8765   │   │            │ │
│  └──────────┘   └────────────┘ │
└─────────────────────────────────┘

After Adding Clients:
┌─────────────┐      ┌─────────────┐
│  Machine 1  │      │  Machine 2  │
│  (client)   │      │  (client)   │
└──────┬──────┘      └──────┬──────┘
       │                    │
       │   HTTP POST        │
       └────────┬───────────┘
                │
         ┌──────▼──────┐
         │   Server    │
         │  Dashboard  │
         │   :8765     │
         └─────────────┘
              ▲
              │ View in browser
              │
         ┌────┴─────┐
         │   You    │
         └──────────┘
```

## Important Notes

1. **Existing data is preserved** - Migration associates existing sessions, tokens, and messages with the machine running the script.

2. **Network access is configurable** - Start with `./start_server.sh` to listen on all interfaces, then point clients at your server's address.

3. **No breaking changes** - The dashboard works exactly the same when viewing "All Machines"

4. **Security** - This is designed for trusted local networks. See MULTI_MACHINE_SETUP.md for production security considerations.

## Next Steps

1. ✅ Multi-machine infrastructure is ready
2. ⏭️ Install client agent on other machines (optional)
3. ⏭️ Set up clients as background services (optional)
4. ⏭️ Configure firewall if needed (optional)

## Questions?

- How to set up clients? → See `MULTI_MACHINE_SETUP.md`
- How does it work? → See architecture diagram above
- Troubleshooting? → See troubleshooting section in `MULTI_MACHINE_SETUP.md`

---

**Status**: ✅ All features implemented and tested
**Server address and registered machines**: Depend on your deployment.
