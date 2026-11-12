# Quick Start Guide

Get your Claude Local Dashboard running in under 5 minutes!

## Prerequisites Check

```bash
# Check Python version (need 3.11+)
python3 --version

# Check macOS (need 10.15+)
sw_vers

# Check available disk space (need ~100MB)
df -h ~
```

## Installation (One Command)

```bash
cd claude-local-dashboard
./install.sh
```

This will:
- ✓ Create `~/.claude-dashboard` directory
- ✓ Set up Python virtual environment
- ✓ Install all dependencies
- ✓ Initialize SQLite database
- ✓ Configure background collectors
- ✓ Add shell integration

## Start Dashboard

```bash
./start.sh
```

Then open: **http://127.0.0.1:8765**

## First Look

The dashboard will be **empty** until you:

1. Run some Claude Code sessions
2. Wait for collectors to gather data (or run manually)

### Run Collectors Manually (Optional)

```bash
cd claude-local-dashboard

# Collect process metrics
./venv/bin/python3 scripts/collect_process_metrics.py

# Run discovery
./venv/bin/python3 -c "
import asyncio
import sys
sys.path.insert(0, '.')
from src.database import Database
from src.collectors.discovery import DataDiscovery
from src.utils.config import Config

async def discover():
    Config.ensure_directories()
    db = Database(str(Config.DB_PATH))
    discovery = DataDiscovery(db)
    results = await discovery.discover_all()
    print(f'Discovered {results[\"total_records\"]} records')

asyncio.run(discover())
"
```

## Verify Installation

```bash
# Check dashboard is running
curl http://127.0.0.1:8765/api/health

# Check database exists
ls -lh ~/.claude-dashboard/claude.db

# Check launchd agents
launchctl list | grep claude

# View logs
tail -f ~/.claude-dashboard/logs/dashboard.log
```

## Common First-Time Issues

### "Port already in use"

Another service is using port 8765. Either:
```bash
# Find what's using it
lsof -i :8765

# Or change the port in src/utils/config.py
PORT = 8766
```

### "No data appearing"

This is normal! Run some Claude sessions first:
```bash
# Example Claude session (if you have Claude Code installed)
claude --help
```

Then manually run collectors:
```bash
./venv/bin/python3 scripts/collect_process_metrics.py
```

Refresh the dashboard.

### "Permission denied"

Fix permissions:
```bash
chmod 700 ~/.claude-dashboard
chmod +x install.sh start.sh stop.sh uninstall.sh
chmod +x scripts/*.py
```

## Next Steps

1. **Browse the Dashboard**: Explore all 6 pages
2. **Run Claude Sessions**: The more you use Claude, the more data
3. **Check Metrics**: Collectors run automatically in background
4. **Customize**: Edit `src/utils/config.py` for your needs

## Daily Usage

```bash
# Start dashboard (once)
./start.sh

# Use Claude normally
# Data is collected automatically

# View dashboard
open http://127.0.0.1:8765

# Stop dashboard (when done)
./stop.sh
```

## Files to Know

| File | Purpose |
|------|---------|
| `README.md` | Full documentation |
| `TROUBLESHOOTING.md` | Fix common issues |
| `PRIVACY.md` | What data is collected |
| `install.sh` | One-time setup |
| `start.sh` | Start dashboard |
| `stop.sh` | Stop dashboard |
| `uninstall.sh` | Remove everything |

## Getting Help

1. Check `docs/TROUBLESHOOTING.md`
2. View logs: `~/.claude-dashboard/logs/`
3. Test database: `sqlite3 ~/.claude-dashboard/claude.db`
4. Run collectors manually to see errors

## Useful Commands

```bash
# Show all sessions
sqlite3 ~/.claude-dashboard/claude.db "SELECT * FROM sessions ORDER BY start_time DESC LIMIT 10;"

# Count total tokens
sqlite3 ~/.claude-dashboard/claude.db "SELECT SUM(total_tokens) FROM token_usage;"

# View recent launches
sqlite3 ~/.claude-dashboard/claude.db "SELECT * FROM launches ORDER BY timestamp DESC LIMIT 10;"

# Check collector status
launchctl list | grep claude

# Restart a collector
launchctl unload ~/Library/LaunchAgents/com.local.claude.process-metrics.plist
launchctl load ~/Library/LaunchAgents/com.local.claude.process-metrics.plist
```

## What to Expect

### First 5 Minutes
- Dashboard shows minimal/no data
- Run a few Claude sessions
- Collectors start gathering metrics

### After 1 Hour
- Session data appears
- Process metrics collected
- Charts start populating

### After 1 Day
- Full metrics history
- Git activity tracked
- Token usage visible
- Daily rollups calculated

### After 1 Week
- Trends become visible
- Top repositories identified
- Usage patterns clear
- Historical comparisons available

## Pro Tips

1. **Leave it Running**: Background collectors work best when always on
2. **Check Weekly**: Review your usage patterns weekly
3. **Archive Old Data**: Delete data >90 days if database grows too large
4. **Customize Intervals**: Edit launchd plists to collect more/less frequently
5. **Export Data**: Use API endpoints to export JSON for analysis

## Uninstall

Changed your mind? Easy removal:

```bash
./uninstall.sh
# Answer 'y' to remove all data
```

## Support

Read the docs:
- `README.md` - Complete documentation
- `docs/TROUBLESHOOTING.md` - Common issues
- `docs/PRIVACY.md` - Privacy & security

Check logs:
```bash
ls -lh ~/.claude-dashboard/logs/
tail -f ~/.claude-dashboard/logs/*.log
```

## That's It!

You're ready to track your Claude Code usage locally and privately.

**Happy coding with Claude!** 🚀
