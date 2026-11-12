#!/bin/bash
set -e

# Claude Dashboard Client Agent - One-Line Installer
# Usage: curl -fsSL https://your-dashboard-server/static/client-install.sh | bash -s -- --server https://your-dashboard-server
# Or:    curl -fsSL https://your-dashboard-server/client-install.sh | bash -s -- --server http://custom-server:8765
# Add --debug to run the agent in the foreground (Ctrl+C works) instead of
# installing it as a background service.

INSTALL_DIR="$HOME/.claude-dashboard-agent"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Parse arguments (done first, before any output, so we know up front
# whether to run verbosely or quietly).
SERVER_URL=""
DEBUG_MODE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --server)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "Error: --server requires a dashboard URL." >&2
                exit 1
            fi
            SERVER_URL="$2"
            shift 2
            ;;
        --debug)
            DEBUG_MODE=true
            shift
            ;;
        *)
            shift
            ;;
    esac
done

SERVER_URL="${SERVER_URL:-${CLAUDE_DASHBOARD_SERVER:-}}"
if [[ ! "$SERVER_URL" =~ ^https?://[^[:space:]]+$ ]]; then
    echo "Error: specify a dashboard URL with --server or CLAUDE_DASHBOARD_SERVER (http:// or https://)." >&2
    exit 1
fi
SERVER_URL="${SERVER_URL%/}"

# ── Stop any previously-installed agent and wipe the install directory for
# a fully clean install ─────────────────────────────────────────────────
# This runs BEFORE we decide where logging goes (below), specifically so
# the wipe never deletes the install.log we're actively writing to out
# from under ourselves. Every reinstall/update therefore starts from a
# completely empty $INSTALL_DIR — no leftover client_agent.py.bak.* files,
# no ever-growing agent.log/agent.error.log, no stale venv, no orphaned
# watchdog.sh/start.sh from a previous OS/config — only the running
# process is stopped first, everything else is deleted and rebuilt fresh.
stop_existing_agent() {
    # Asking the service manager nicely (launchctl unload / schtasks //End /
    # systemctl stop) normally sends SIGTERM — but the agent *intentionally*
    # ignores SIGTERM/SIGINT/SIGHUP in background mode so a user can't kill
    # it by accident (Ctrl+C, closing the terminal, a stray `kill`). That
    # means the graceful stop below is NOT expected to actually terminate
    # it; it just stops the service manager from tracking/auto-restarting
    # the old instance. The real kill happens via `kill -9` on the recorded
    # PID, which cannot be ignored by any process.
    if [[ "$OSTYPE" == "darwin"* ]]; then
        launchctl unload "$HOME/Library/LaunchAgents/com.claude.dashboard.agent.plist" &>/dev/null || true
    elif [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]] || [[ "$OSTYPE" == "win32"* ]]; then
        schtasks //End //TN "ClaudeDashboardAgent" &>/dev/null || true
        schtasks //End //TN "ClaudeDashboardAgentWatchdog" &>/dev/null || true
    elif [[ -d "/etc/systemd/system" ]] || [[ -d "$HOME/.config/systemd/user" ]]; then
        systemctl --user stop claude-dashboard-agent &>/dev/null || true
    fi
    if [ -f "$INSTALL_DIR/agent.pid" ]; then
        OLD_PID="$(cat "$INSTALL_DIR/agent.pid" 2>/dev/null || true)"
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            kill -9 "$OLD_PID" 2>/dev/null || true
        fi
    fi
    # Extra safety net for macOS/Linux: catch any stray instance of THIS
    # install's agent even if agent.pid was stale/missing (e.g. an old
    # install predating the PID-file mechanism, or a crash that left it
    # behind).
    if command -v pgrep &>/dev/null; then
        for stray_pid in $(pgrep -f "$INSTALL_DIR/client_agent.py" 2>/dev/null || true); do
            kill -9 "$stray_pid" 2>/dev/null || true
        done
    fi
}

if [ -d "$INSTALL_DIR" ]; then
    if $DEBUG_MODE; then
        echo -e "${BLUE}Stopping any existing agent and cleaning up previous installation...${NC}"
    fi
    stop_existing_agent
    rm -rf "$INSTALL_DIR"
    if $DEBUG_MODE; then
        echo -e "${GREEN}✓${NC} Removed previous installation"
    fi
fi
mkdir -p "$INSTALL_DIR"

# By default (no --debug) the installer only prints three possible
# messages: "Installing Claude Service", then either "Successfully
# installed" or "Failed installing". Full step-by-step logs are only shown
# with --debug; otherwise they're captured in $INSTALL_DIR/install.log for
# troubleshooting.
echo "Installing Claude Service"

if $DEBUG_MODE; then
    echo -e "${BLUE}╔═══════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║   Claude Dashboard Client Agent Installer            ║${NC}"
    echo -e "${BLUE}╚═══════════════════════════════════════════════════════╝${NC}"
    echo
else
    LOG_FILE="$INSTALL_DIR/install.log"
    # Save the real stdout/stderr (fds 3/4) so the exit trap can report the
    # final status on the terminal even though everything else is
    # redirected to the log file below.
    exec 3>&1 4>&2
    exec >"$LOG_FILE" 2>&1

    report_result() {
        local exit_code=$?
        exec >&3 2>&4
        if [ $exit_code -eq 0 ]; then
            echo "Successfully installed"
            echo "  Log file: $LOG_FILE"
        else
            echo "Failed installing"
            echo "  See $LOG_FILE for details"
        fi
    }
    trap report_result EXIT
fi

# Check Python
echo -e "${BLUE}[1/6]${NC} Checking Python installation..."

# On Windows, `python`/`python3` may resolve to a fake "App Execution Alias"
# stub (in %LOCALAPPDATA%\Microsoft\WindowsApps) that exists on PATH even
# when Python isn't really installed. Running it prints a Microsoft Store
# nag message instead of a real version string, so `command -v` alone is
# not enough — we must verify `--version` actually returns a parseable
# "Python X.Y.Z" string.
PYTHON_CMD=""
PYTHON_VERSION=""
for candidate in python3 python; do
    if command -v "$candidate" &> /dev/null; then
        ver_output=$("$candidate" --version 2>&1 || true)
        if [[ "$ver_output" =~ Python\ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
            PYTHON_CMD="$candidate"
            PYTHON_VERSION="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
            PYTHON_MAJOR="${BASH_REMATCH[1]}"
            PYTHON_MINOR="${BASH_REMATCH[2]}"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}✗ Python 3.8+ is not installed (or not runnable).${NC}"
    echo "  Please install Python 3.8+ from https://www.python.org/"
    echo
    echo -e "${YELLOW}Windows users:${NC} if you've already installed Python but see a"
    echo "  \"Python was not found; run without arguments to install from the"
    echo "  Microsoft Store...\" message, disable the fake python/python3"
    echo "  shortcuts at: Settings > Apps > Advanced app settings > App"
    echo "  execution aliases (turn OFF \"python.exe\" and \"python3.exe\"),"
    echo "  then restart your terminal."
    exit 1
fi

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]); then
    echo -e "${RED}✗ Python 3.8+ required, found $PYTHON_VERSION${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION found"

# Create installation directory
echo -e "${BLUE}[2/6]${NC} Creating installation directory..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo -e "${GREEN}✓${NC} Directory: $INSTALL_DIR"

# Download client_agent.py from the server that served this script
echo -e "${BLUE}[3/6]${NC} Downloading client agent..."

DOWNLOAD_SERVER="$SERVER_URL"

# No backup-then-overwrite needed here: $INSTALL_DIR was already wiped
# clean of any previous install above, so there's nothing to back up.

# Try to download from the dashboard server
if command -v curl &> /dev/null; then
    if curl -fsSL "${DOWNLOAD_SERVER}/static/client_agent.py" -o client_agent.py 2>/dev/null && [ -s "client_agent.py" ]; then
        echo -e "${GREEN}✓${NC} Downloaded from $DOWNLOAD_SERVER"
    else
        echo -e "${RED}✗ Failed to download from ${DOWNLOAD_SERVER}/static/client_agent.py${NC}"
        echo "  Please ensure the dashboard server is running and accessible"
        exit 1
    fi
elif command -v wget &> /dev/null; then
    if wget -q "${DOWNLOAD_SERVER}/static/client_agent.py" -O client_agent.py 2>/dev/null && [ -s "client_agent.py" ]; then
        echo -e "${GREEN}✓${NC} Downloaded from $DOWNLOAD_SERVER"
    else
        echo -e "${RED}✗ Failed to download from ${DOWNLOAD_SERVER}/static/client_agent.py${NC}"
        echo "  Please ensure the dashboard server is running and accessible"
        exit 1
    fi
else
    echo -e "${RED}✗ Neither curl nor wget is available${NC}"
    exit 1
fi

# Verify it's a valid Python file
if ! head -n 1 client_agent.py | grep -q "python"; then
    echo -e "${RED}✗ Downloaded file is not a valid Python script${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} client_agent.py downloaded successfully"

# Create virtual environment
echo -e "${BLUE}[4/6]${NC} Setting up Python virtual environment..."
if [ -d "venv" ]; then
    echo -e "${YELLOW}⚠${NC}  Removing existing venv..."
    rm -rf venv
fi

"$PYTHON_CMD" -m venv venv

# Windows venvs use venv/Scripts/activate; Unix uses venv/bin/activate.
if [ -f "venv/Scripts/activate" ]; then
    VENV_ACTIVATE="venv/Scripts/activate"
elif [ -f "venv/bin/activate" ]; then
    VENV_ACTIVATE="venv/bin/activate"
else
    echo -e "${RED}✗ Virtual environment creation failed (no activate script found)${NC}"
    exit 1
fi
source "$VENV_ACTIVATE"
echo -e "${GREEN}✓${NC} Virtual environment created"

# Install dependencies
echo -e "${BLUE}[5/6]${NC} Installing dependencies..."
# Use `python -m pip` rather than the `pip` executable directly — on
# Windows, the pip shim in venv/Scripts/ isn't always directly executable
# from Git Bash (permission errors), while running pip as a module through
# the venv's python interpreter always works.
python -m pip install --quiet --upgrade pip
python -m pip install --quiet httpx cryptography
echo -e "${GREEN}✓${NC} Dependencies installed (httpx)"

echo -e "${BLUE}[6/6]${NC} Server configuration"
echo -e "${GREEN}✓${NC} Server: $SERVER_URL"

# Create launcher script
echo -e "${BLUE}Creating launcher script...${NC}"
cat > "$INSTALL_DIR/start.sh" <<EOF
#!/bin/bash

# On Windows, bash.exe launched non-interactively (e.g. by Task Scheduler)
# doesn't source Git Bash's profile scripts, so usr/bin -- where nohup.exe
# lives -- can be missing from PATH even though it's part of the same Git
# install that's running this script. Add the common Git-for-Windows bin
# dirs defensively; harmless on macOS/Linux since they simply won't exist.
if [[ "\$OSTYPE" == "msys"* ]] || [[ "\$OSTYPE" == "cygwin"* ]] || [[ "\$OSTYPE" == "win32"* ]]; then
    export PATH="/usr/bin:/mingw64/bin:/bin:\$PATH"
fi

cd "$INSTALL_DIR"
if [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
else
    source venv/bin/activate
fi

# Always run in the background unless --debug is passed, no matter how this
# script is invoked (installer, manually, cron, etc.). CLAUDE_AGENT_BG is set
# by the relaunch below (and by the launchd/systemd service definitions) so
# an already-backgrounded/supervised process doesn't fork again.
DEBUG=false
for arg in "\$@"; do
    if [ "\$arg" = "--debug" ]; then
        DEBUG=true
    fi
done

if ! \$DEBUG && [ -z "\$CLAUDE_AGENT_BG" ]; then
    export CLAUDE_AGENT_BG=1
    nohup "\$0" "\$@" >> "$INSTALL_DIR/agent.log" 2>> "$INSTALL_DIR/agent.error.log" < /dev/null &
    disown
    echo \$! > "$INSTALL_DIR/agent.pid"
    echo "Agent started in background (PID: \$(cat "$INSTALL_DIR/agent.pid")). Logs: $INSTALL_DIR/agent.log"
    exit 0
fi

# Record the PID that will actually run the agent so a future reinstall (or
# the watchdog) can find and stop it. This line is reached both when this
# script re-forked itself just above, and when a service manager
# (launchd/schtasks/systemd) already set CLAUDE_AGENT_BG and invoked this
# script directly (no fork happens in that case) — \$\$ is this shell's own
# PID, and \`exec\` below replaces this process image with python's while
# keeping that same PID, so the recorded value stays valid the whole time
# the agent runs, however it was launched.
echo \$\$ > "$INSTALL_DIR/agent.pid"
exec python "$INSTALL_DIR/client_agent.py" --server "$SERVER_URL" "\$@"
EOF

chmod +x "$INSTALL_DIR/start.sh"

# Create systemd service (Linux) or launchd plist (macOS)
echo
echo -e "${BLUE}Setting up auto-start...${NC}"

# Tracks whether the OS service manager (launchd/systemd) is now running the
# agent, so we know whether a manual background launch is still needed.
SERVICE_STARTED=false

if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS - create LaunchAgent
    PLIST_FILE="$HOME/Library/LaunchAgents/com.claude.dashboard.agent.plist"
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.dashboard.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/start.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_AGENT_BG</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/agent.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/agent.error.log</string>
</dict>
</plist>
EOF

    echo -e "${GREEN}✓${NC} LaunchAgent created: $PLIST_FILE"
    echo
    echo -e "${YELLOW}Auto-start commands:${NC}"
    echo "  Enable:  launchctl load ~/Library/LaunchAgents/com.claude.dashboard.agent.plist"
    echo "  Disable: launchctl unload ~/Library/LaunchAgents/com.claude.dashboard.agent.plist"
    echo "  Start:   launchctl start com.claude.dashboard.agent"
    echo "  Stop:    launchctl stop com.claude.dashboard.agent"
    echo "  Logs:    tail -f $INSTALL_DIR/agent.log"

    if ! $DEBUG_MODE; then
        launchctl unload "$PLIST_FILE" &>/dev/null || true
        if launchctl load -w "$PLIST_FILE" &>/dev/null; then
            launchctl start com.claude.dashboard.agent &>/dev/null || true
            SERVICE_STARTED=true
            echo -e "${GREEN}✓${NC} LaunchAgent loaded — agent is running in the background (auto-restarts via KeepAlive)"
        fi
    fi

elif [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]] || [[ "$OSTYPE" == "win32"* ]]; then
    # Windows (Git Bash/MSYS2/Cygwin). There's no POSIX process model here:
    # nohup + disown only detaches from bash's own job table, but the whole
    # process tree is still tied to the console Windows created for this
    # `curl | bash` invocation — closing that window/terminal kills every
    # child in it (Python's SIGINT/SIGTERM handling can't intercept that,
    # since it isn't a real POSIX signal on Windows). Task Scheduler runs
    # start.sh in its own session with no console attached at all, so the
    # nohup-detached agent it spawns is no longer tied to anything that can
    # be closed, and it survives reboots/logoffs too.
    TASK_NAME="ClaudeDashboardAgent"
    WATCHDOG_TASK_NAME="ClaudeDashboardAgentWatchdog"

    WIN_BASH_EXE=$(cygpath -w "$(command -v bash)" 2>/dev/null || command -v bash)
    WIN_START_SCRIPT=$(cygpath -w "$INSTALL_DIR/start.sh" 2>/dev/null || echo "$INSTALL_DIR/start.sh")
    TASK_ACTION="\"$WIN_BASH_EXE\" \"$WIN_START_SCRIPT\""

    echo -e "${GREEN}✓${NC} Windows Task Scheduler will be used for auto-start"
    echo
    echo -e "${YELLOW}Auto-start commands:${NC}"
    echo "  (uses // instead of / -- Git Bash mangles single-slash args into fake paths)"
    echo "  Status:  schtasks //query //tn $TASK_NAME //v //fo list"
    echo "  Stop:    schtasks //end //tn $TASK_NAME"
    echo "  Disable: schtasks //change //tn $TASK_NAME //disable"
    echo "  Enable:  schtasks //change //tn $TASK_NAME //enable"
    echo "  Logs:    tail -f $INSTALL_DIR/agent.log"

    if ! $DEBUG_MODE; then
        # Main task: start the agent whenever the user logs on.
        if schtasks //Create //F //TN "$TASK_NAME" //TR "$TASK_ACTION" //SC ONLOGON //RL LIMITED >/dev/null 2>&1; then
            # Watchdog task: re-check every 5 minutes (matches the agent's own
            # sync cadence). start.sh's own nohup guard only forks a new
            # process when none is running under CLAUDE_AGENT_BG yet — but
            # since each watchdog tick is a fresh bash.exe invocation with no
            # memory of that env var, it would refork every 5 minutes even
            # while healthy. Guard on the PID file instead so this only
            # relaunches the agent if it has actually died.
            cat > "$INSTALL_DIR/watchdog.sh" <<WDEOF
#!/bin/bash
PID_FILE="$INSTALL_DIR/agent.pid"
if [ -f "\$PID_FILE" ] && kill -0 "\$(cat "\$PID_FILE")" 2>/dev/null; then
    exit 0
fi
exec "$INSTALL_DIR/start.sh"
WDEOF
            chmod +x "$INSTALL_DIR/watchdog.sh"
            WIN_WATCHDOG_SCRIPT=$(cygpath -w "$INSTALL_DIR/watchdog.sh" 2>/dev/null || echo "$INSTALL_DIR/watchdog.sh")
            schtasks //Create //F //TN "$WATCHDOG_TASK_NAME" //TR "\"$WIN_BASH_EXE\" \"$WIN_WATCHDOG_SCRIPT\"" //SC MINUTE //MO 5 //RL LIMITED >/dev/null 2>&1 || true
            # Start it immediately rather than waiting for the next logon.
            schtasks //Run //TN "$TASK_NAME" >/dev/null 2>&1 || true
            SERVICE_STARTED=true
            echo -e "${GREEN}✓${NC} Scheduled tasks created — agent is running in the background (auto-restarts)"
        else
            echo -e "${YELLOW}⚠${NC}  Could not create scheduled task; falling back to a plain background process"
            echo "  (won't survive closing this terminal — re-run with --debug to diagnose)"
        fi
    fi

elif [[ -d "/etc/systemd/system" ]] || [[ -d "$HOME/.config/systemd/user" ]]; then
    # Linux with systemd
    SERVICE_FILE="$HOME/.config/systemd/user/claude-dashboard-agent.service"
    mkdir -p "$HOME/.config/systemd/user"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Claude Dashboard Client Agent
After=network.target

[Service]
Type=simple
Environment=CLAUDE_AGENT_BG=1
ExecStart=$INSTALL_DIR/start.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

    echo -e "${GREEN}✓${NC} Systemd service created: $SERVICE_FILE"
    echo
    echo -e "${YELLOW}Auto-start commands:${NC}"
    echo "  Enable:  systemctl --user enable claude-dashboard-agent"
    echo "  Disable: systemctl --user disable claude-dashboard-agent"
    echo "  Start:   systemctl --user start claude-dashboard-agent"
    echo "  Stop:    systemctl --user stop claude-dashboard-agent"
    echo "  Status:  systemctl --user status claude-dashboard-agent"
    echo "  Logs:    journalctl --user -u claude-dashboard-agent -f"

    if ! $DEBUG_MODE; then
        systemctl --user daemon-reload &>/dev/null || true
        if systemctl --user enable --now claude-dashboard-agent &>/dev/null; then
            SERVICE_STARTED=true
            echo -e "${GREEN}✓${NC} Systemd service enabled — agent is running in the background (auto-restarts)"
        fi
    fi
fi

# Installation complete
echo
echo -e "${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Installation Complete! ✓                 ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
echo
echo -e "${BLUE}Installation directory:${NC} $INSTALL_DIR"
echo -e "${BLUE}Server URL:${NC} $SERVER_URL"
echo
echo -e "${YELLOW}Quick Start:${NC}"
echo "  Runs in background by default (survives terminal close, auto-restarts)."
echo "  Run in foreground (debugging): $INSTALL_DIR/start.sh --debug"
echo
echo -e "${YELLOW}Uninstall:${NC}"
echo "  rm -rf $INSTALL_DIR"
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "  launchctl unload ~/Library/LaunchAgents/com.claude.dashboard.agent.plist"
    echo "  rm ~/Library/LaunchAgents/com.claude.dashboard.agent.plist"
elif [[ "$OSTYPE" == "msys"* ]] || [[ "$OSTYPE" == "cygwin"* ]] || [[ "$OSTYPE" == "win32"* ]]; then
    echo "  schtasks //end //tn ClaudeDashboardAgent"
    echo "  schtasks //delete //tn ClaudeDashboardAgent //f"
    echo "  schtasks //delete //tn ClaudeDashboardAgentWatchdog //f"
elif [[ -d "/etc/systemd/system" ]] || [[ -d "$HOME/.config/systemd/user" ]]; then
    echo "  systemctl --user stop claude-dashboard-agent"
    echo "  systemctl --user disable claude-dashboard-agent"
    echo "  rm ~/.config/systemd/user/claude-dashboard-agent.service"
fi
echo

if $DEBUG_MODE; then
    echo -e "${GREEN}Starting agent in the foreground (--debug)...${NC}"
    echo -e "${YELLOW}Press Ctrl+C to stop.${NC}"
    echo
    exec "$INSTALL_DIR/start.sh" --debug
elif $SERVICE_STARTED; then
    echo -e "${GREEN}Agent is running in the background, managed by the OS service manager.${NC}"
    echo "It will keep running after this terminal closes and auto-restart if stopped."
else
    # No systemd/launchd available — start.sh backgrounds itself (via
    # nohup + disown) whenever --debug isn't passed, so just invoke it.
    echo -e "${GREEN}Starting agent in the background...${NC}"
    "$INSTALL_DIR/start.sh"
fi
