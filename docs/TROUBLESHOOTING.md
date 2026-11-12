## Troubleshooting Guide

### Dashboard Won't Start

**Problem**: Running `./start.sh` fails or dashboard is not accessible

**Solutions**:

1. Check if port 8765 is already in use:
   ```bash
   lsof -i :8765
   ```
   If something is using it, either stop that process or edit `src/utils/config.py` to use a different port.

2. Check dashboard logs:
   ```bash
   tail -f ~/.claude-dashboard/logs/dashboard.log
   ```

3. Verify Python environment:
   ```bash
   ./venv/bin/python3 --version
   ```

4. Reinstall dependencies:
   ```bash
   ./venv/bin/pip install -r requirements.txt
   ```

### No Data Appearing

**Problem**: Dashboard loads but shows no metrics

**Solutions**:

1. Check if launchd agents are loaded:
   ```bash
   launchctl list | grep com.local.claude
   ```

2. Manually run a collector to test:
   ```bash
   ./venv/bin/python3 scripts/collect_process_metrics.py
   ```

3. Check collector logs:
   ```bash
   tail -f ~/.claude-dashboard/logs/*.log
   ```

4. Verify database was created:
   ```bash
   ls -lh ~/.claude-dashboard/claude.db
   ```

5. Check database contents:
   ```bash
   sqlite3 ~/.claude-dashboard/claude.db "SELECT COUNT(*) FROM sessions;"
   ```

### Collectors Not Running

**Problem**: Background collectors aren't collecting data

**Solutions**:

1. Reload launchd agents:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.local.claude.*.plist
   launchctl load ~/Library/LaunchAgents/com.local.claude.*.plist
   ```

2. Check agent status:
   ```bash
   launchctl list | grep claude
   ```

3. View specific agent logs:
   ```bash
   tail -f ~/.claude-dashboard/logs/process-metrics.log
   ```

4. Test collector manually:
   ```bash
   cd claude-local-dashboard
   ./venv/bin/python3 scripts/collect_process_metrics.py
   ```

### High CPU Usage

**Problem**: Collectors using too much CPU

**Solutions**:

1. Adjust collection intervals in launchd plists:
   - Edit files in `~/Library/LaunchAgents/`
   - Increase `StartInterval` values
   - Reload agents

2. Disable specific collectors:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.local.claude.network-metrics.plist
   ```

### Database Too Large

**Problem**: SQLite database growing too large

**Solutions**:

1. Check database size:
   ```bash
   du -h ~/.claude-dashboard/claude.db
   ```

2. Clear old data (be careful!):
   ```bash
   sqlite3 ~/.claude-dashboard/claude.db
   ```
   ```sql
   DELETE FROM process_metrics WHERE timestamp < datetime('now', '-30 days');
   DELETE FROM network_metrics WHERE timestamp < datetime('now', '-30 days');
   VACUUM;
   ```

3. Archive and start fresh:
   ```bash
   mv ~/.claude-dashboard/claude.db ~/.claude-dashboard/claude.db.backup
   ./venv/bin/python3 -c "from src.database import Database; from src.utils.config import Config; Config.ensure_directories(); Database(str(Config.DB_PATH)).initialize()"
   ```

### Shell Integration Not Working

**Problem**: Claude commands not being logged

**Solutions**:

1. Verify shell integration in `~/.zshrc`:
   ```bash
   grep "Claude Dashboard wrapper" ~/.zshrc
   ```

2. Reload shell configuration:
   ```bash
   source ~/.zshrc
   ```

3. Test wrapper function:
   ```bash
   type claude
   ```
   Should show it's a function, not just a command.

### Permission Errors

**Problem**: Permission denied errors in logs

**Solutions**:

1. Fix data directory permissions:
   ```bash
   chmod 700 ~/.claude-dashboard
   chmod 700 ~/.claude-dashboard/logs
   ```

2. Fix database permissions:
   ```bash
   chmod 600 ~/.claude-dashboard/claude.db
   ```

3. Ensure collector scripts are executable:
   ```bash
   chmod +x scripts/*.py
   ```

### Python Import Errors

**Problem**: "ModuleNotFoundError" in collector logs

**Solutions**:

1. Verify virtual environment:
   ```bash
   which python3
   ./venv/bin/python3 --version
   ```

2. Reinstall dependencies:
   ```bash
   ./venv/bin/pip install -r requirements.txt
   ```

3. Check PYTHONPATH in launchd plists

### Charts Not Loading

**Problem**: Dashboard loads but charts are blank

**Solutions**:

1. Check browser console for JavaScript errors (F12)

2. Verify API endpoints work:
   ```bash
   curl http://127.0.0.1:8765/api/stats/overview
   ```

3. Clear browser cache and reload

4. Try a different browser

### Reset Everything

**Nuclear option**: Complete reset

```bash
# Stop everything
./stop.sh

# Uninstall
./uninstall.sh

# Remove all data
rm -rf ~/.claude-dashboard

# Reinstall
./install.sh

# Start fresh
./start.sh
```

### Getting Help

1. Check logs first:
   ```bash
   ls -lh ~/.claude-dashboard/logs/
   ```

2. Run collectors manually to see errors:
   ```bash
   ./venv/bin/python3 scripts/collect_process_metrics.py
   ```

3. Check database integrity:
   ```bash
   sqlite3 ~/.claude-dashboard/claude.db "PRAGMA integrity_check;"
   ```

4. Verify system requirements:
   - macOS 10.15 or later
   - Python 3.11+
   - At least 100MB free disk space

## Common Issues

### "Address already in use"

Port 8765 is in use. Either:
- Stop the other process
- Change port in `src/utils/config.py`

### "Database is locked"

Multiple processes accessing the database. Either:
- Wait a moment and retry
- Restart collectors
- Check for zombie processes: `ps aux | grep python`

### No Claude processes found

If you haven't run Claude yet:
- The dashboard will show no data
- Run some Claude sessions first
- Wait for collectors to run (or run manually)
