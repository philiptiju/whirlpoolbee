# Privacy & Security

## Data Collection Philosophy

Claude Local Dashboard is designed with **privacy first**:

- **100% Local**: All data stays on your Mac
- **No External Servers**: Zero external API calls
- **Offline Capable**: Works without internet
- **Open Source**: All code is inspectable

## What Data is Collected

### Session Data
- **Timestamps**: When Claude was launched
- **Working Directory**: Where Claude was run
- **Command Arguments**: Flags passed to Claude
- **Exit Code**: How the session ended
- **Duration**: How long the session ran

### System Metrics
- **CPU Usage**: Per Claude process
- **Memory Usage**: RSS and VMS memory
- **Thread Count**: Number of active threads
- **File Descriptors**: Open file handles

### Network Data
- **Connection Count**: Number of active connections
- **Local Addresses**: Localhost ports used
- **Remote Addresses**: External IPs contacted
- **Bytes Transferred**: Total network I/O

### Development Data
- **Git Commits**: In working directories
- **Files Changed**: During sessions
- **Insertions/Deletions**: Code changes
- **Repository Paths**: Project locations

### Filesystem Data
- **Files Accessed**: Read/write operations
- **File Sizes**: Bytes on disk
- **Modification Times**: When files changed
- **File Paths**: Locations accessed

### Token Usage
- **Input Tokens**: Sent to Claude
- **Output Tokens**: Received from Claude
- **Model Name**: Which model was used
- **Estimated Cost**: Based on public pricing

## What is NOT Collected

- **Actual Code**: Your source code is not stored
- **Prompt Content**: Prompts not saved
- **Clipboard Data**: Never accessed
- **Keystrokes**: No keylogging
- **Screenshots**: No screen capture
- **External Sites**: No web tracking
- **Analytics**: No telemetry sent anywhere

## Secret Redaction

Automatic redaction of sensitive data using regex patterns:

### Detected Patterns
- API Keys (Anthropic, GitHub, AWS, OpenAI)
- Bearer Tokens
- JWT Tokens
- OAuth Tokens
- Private Keys (PEM format)
- Passwords in environment variables
- Client Secrets
- Consumer Secrets

### Redaction Rules

Located in `src/utils/secrets.py`:

```python
SECRET_PATTERNS = [
    (r'sk-[a-zA-Z0-9]{20,}', 'ANTHROPIC_KEY'),
    (r'ghp_[a-zA-Z0-9]{36}', 'GITHUB_TOKEN'),
    (r'AKIA[0-9A-Z]{16}', 'AWS_ACCESS_KEY'),
    # ... more patterns
]
```

Redacted values appear as `[REDACTED_TYPE]` in any output.

### Customizing Redaction

Add your own patterns to `SECRET_PATTERNS` in `src/utils/secrets.py`.

## Network Security

### Local-Only Binding

The web server binds **only** to `127.0.0.1`:

```python
# src/utils/config.py
HOST = "127.0.0.1"  # NOT "0.0.0.0"
PORT = 8765
```

This means:
- **Not accessible** from other computers
- **Not accessible** from your local network
- **Not accessible** from the internet
- Only accessible from **your Mac**

### No Outbound Traffic

Background collectors make **zero** external network requests:

- No analytics beacons
- No update checks
- No telemetry
- No crash reports

The only network activity is:
- Monitoring Claude's own network usage
- Local HTTP server for dashboard

## File System Security

### Data Directory Permissions

```bash
~/.claude-dashboard/
  Mode: 0700 (drwx------)
  Owner: Your user only
  Group: Your group only
```

Only **you** can read/write the data.

### Database Encryption

SQLite database is **not encrypted** by default (it's on an encrypted disk).

For additional security:
1. Enable FileVault (full-disk encryption)
2. Use SQLCipher for encrypted SQLite (requires code changes)

## Shell Integration

The shell wrapper function logs:
- Command invocation time
- Working directory
- Exit code
- Duration

It does **not** log:
- Actual arguments (to avoid leaking secrets)
- Environment variables
- Standard output/error
- Interactive input

## Audit Trail

All data collection is logged:

```bash
~/.claude-dashboard/logs/
  process-metrics.log
  network-metrics.log
  git-metrics.log
  filesystem.log
  daily-rollup.log
  dashboard.log
```

Check these logs to see exactly what was collected.

## Data Retention

### Default Retention
- **Forever**: All data is kept indefinitely
- **No Auto-Cleanup**: Manual deletion required

### Manual Cleanup

Delete old data:

```bash
sqlite3 ~/.claude-dashboard/claude.db
```

```sql
-- Delete data older than 30 days
DELETE FROM process_metrics WHERE timestamp < datetime('now', '-30 days');
DELETE FROM network_metrics WHERE timestamp < datetime('now', '-30 days');
DELETE FROM git_metrics WHERE timestamp < datetime('now', '-30 days');
DELETE FROM filesystem_events WHERE timestamp < datetime('now', '-30 days');

-- Reclaim space
VACUUM;
```

### Complete Data Removal

```bash
rm -rf ~/.claude-dashboard
```

## Third-Party Code

Dependencies used:

- **FastAPI**: Web framework (MIT License)
- **uvicorn**: ASGI server (BSD License)
- **psutil**: System monitoring (BSD License)
- **aiosqlite**: Async SQLite (MIT License)
- **Jinja2**: Templating (BSD License)
- **Chart.js**: Charting (MIT License)

All dependencies are **open source** and **inspectable**.

No tracking libraries or analytics SDKs.

## Threat Model

### Protected Against
- External data leaks
- Network snooping (local-only)
- Unauthorized access (file permissions)
- Accidental secret exposure (redaction)

### Not Protected Against
- **Local malware**: With your user access
- **Physical access**: To your unlocked Mac
- **Keyloggers**: If already installed
- **Screen capture**: By other apps

### Recommendations
1. Use FileVault for disk encryption
2. Lock your Mac when away
3. Keep macOS updated
4. Use a firewall
5. Review collected data periodically

## Compliance

### GDPR
- **Data Controller**: You
- **Data Processor**: You
- **Data Storage**: Your device
- **Data Transfer**: None
- **Right to Deletion**: `rm -rf ~/.claude-dashboard`

Since all data is local, GDPR obligations are on **you**, not this software.

### CCPA
- No data is "sold" (because it never leaves your Mac)
- No data is "shared" with third parties
- You have full control and access

## Uninstallation

Complete data removal:

```bash
./uninstall.sh
# Answer "y" when prompted to remove data

# Verify removal
ls -la ~/.claude-dashboard  # Should not exist
```

## Questions?

This software is provided as-is with no warranties. Review the code yourself:

- Database schema: `schema.sql`
- Collection logic: `src/collectors/`
- Secret redaction: `src/utils/secrets.py`
- Network binding: `src/utils/config.py`

**When in doubt, audit the code.**
