#Requires -Version 5.1
<#
.SYNOPSIS
    Claude Dashboard Client Agent - Native Windows Installer (no Git Bash / WSL required)

.DESCRIPTION
    Downloads client_agent.py, sets it up in its own virtual environment, and
    registers it as a Scheduled Task (per-user, no admin rights needed) so it
    keeps running in the background and auto-starts at logon. Functionally
    equivalent to client-install.sh, but implemented entirely in PowerShell +
    schtasks.exe so it needs nothing beyond what Windows already ships with.

.PARAMETER ServerUrl
    Dashboard server URL used for both download and telemetry. Required unless
    $env:CLAUDE_DASHBOARD_SERVER is set; piped scripts cannot infer their origin.

.PARAMETER DebugMode
    Run the agent in the foreground (Ctrl+C works) instead of installing it
    as a background Scheduled Task.

.EXAMPLE
    & ([scriptblock]::Create((irm https://<dashboard-host>/static/client-install.ps1))) -ServerUrl https://<dashboard-host>

.EXAMPLE
    & ([scriptblock]::Create((irm https://<dashboard-host>/static/client-install.ps1))) -ServerUrl http://custom:8765
#>
param(
    [string]$ServerUrl = "",
    [switch]$DebugMode
)

$ErrorActionPreference = "Stop"
# PowerShell 7.3+ defaults $PSNativeCommandUseErrorActionPreference to $true,
# which makes ANY native .exe's non-zero exit code a terminating error
# (honoring $ErrorActionPreference above) even when its output is redirected
# to $null. That breaks the common "stop/delete a task that may not exist
# yet" pattern below: schtasks exits non-zero when a task isn't found, which
# is the expected, harmless case on a first-time install. Restore the
# classic behavior (native exit codes are informational, not terminating)
# so those best-effort cleanup calls don't abort the whole script. This
# variable doesn't exist on Windows PowerShell 5.1 -- setting it there is a
# harmless no-op.
$PSNativeCommandUseErrorActionPreference = $false

$InstallDir = Join-Path $env:USERPROFILE ".claude-dashboard-agent"
if (-not $ServerUrl) { $ServerUrl = $env:CLAUDE_DASHBOARD_SERVER }
$serverUri = $null
if (-not [Uri]::TryCreate($ServerUrl, [UriKind]::Absolute, [ref]$serverUri) -or
    $serverUri.Scheme -notin @("http", "https")) {
    throw "Specify an http:// or https:// dashboard URL with -ServerUrl or CLAUDE_DASHBOARD_SERVER."
}
$ServerUrl = $ServerUrl.TrimEnd("/")
$TaskName = "ClaudeDashboardAgent"
$WatchdogTaskName = "ClaudeDashboardAgentWatchdog"
$LogFile = Join-Path $InstallDir "install.log"

# By default (no -DebugMode) the console only ever shows "Installing Claude
# Service" then either "Successfully installed" or "Failed installing" --
# matching client-install.sh. Every step-by-step message below still goes
# somewhere: to the console with -DebugMode, or appended to $LogFile
# otherwise, so install.log always has full detail for troubleshooting.
function Write-Step($msg) {
    if ($DebugMode) { Write-Host $msg -ForegroundColor Cyan }
    else { Add-Content -Path $LogFile -Value $msg -ErrorAction SilentlyContinue }
}
function Write-Ok($msg) {
    if ($DebugMode) { Write-Host "OK  $msg" -ForegroundColor Green }
    else { Add-Content -Path $LogFile -Value "OK  $msg" -ErrorAction SilentlyContinue }
}
function Write-Warn2($msg) {
    if ($DebugMode) { Write-Host "!!  $msg" -ForegroundColor Yellow }
    else { Add-Content -Path $LogFile -Value "!!  $msg" -ErrorAction SilentlyContinue }
}
function Write-Err2($msg) {
    if ($DebugMode) { Write-Host "XX  $msg" -ForegroundColor Red }
    else { Add-Content -Path $LogFile -Value "XX  $msg" -ErrorAction SilentlyContinue }
}
# For supplementary detail lines that aren't a "step" (plain instructional
# text, banners, command hints) -- same show-or-log rule as above.
function Write-Detail($msg) {
    if ($DebugMode) { Write-Host $msg }
    else { Add-Content -Path $LogFile -Value $msg -ErrorAction SilentlyContinue }
}

# ── Stop any previously-installed agent and wipe the install directory for
# a fully clean install, exactly like client-install.sh does. ──────────────
function Stop-ExistingAgent {
    # Wrap each call in try/catch: PowerShell converts native stderr output
    # into an ErrorRecord (independent of exit codes), and under
    # $ErrorActionPreference = "Stop" that becomes a terminating exception
    # even with output redirected via *> $null -- this happens on both
    # Windows PowerShell 5.1 and PowerShell 7+. schtasks legitimately writes
    # to stderr and exits non-zero when a task isn't found, which is the
    # expected, harmless case on a first-time install, so swallow it here.
    try { schtasks /End /TN $TaskName *> $null } catch { }
    try { schtasks /End /TN $WatchdogTaskName *> $null } catch { }
    try { schtasks /Delete /TN $TaskName /F *> $null } catch { }
    try { schtasks /Delete /TN $WatchdogTaskName /F *> $null } catch { }

    $pidFile = Join-Path $InstallDir "agent.pid"
    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($oldPid) {
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        }
    }
    # Extra safety net: catch any stray python process still running THIS
    # install's client_agent.py even if agent.pid was stale/missing.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*client_agent.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

Write-Host "Installing Claude Service"

if (Test-Path $InstallDir) {
    Write-Step "Stopping any existing agent and cleaning up previous installation..."
    Stop-ExistingAgent
    Remove-Item -Recurse -Force $InstallDir
    Write-Ok "Removed previous installation"
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

try {
    # ── [1/6] Check Python ──────────────────────────────────────────────
    Write-Step "[1/6] Checking Python installation..."

    # `python`/`python3` on a fresh Windows box may resolve to a fake "App
    # Execution Alias" stub that exists on PATH even when Python isn't really
    # installed — running it just opens the Microsoft Store instead of
    # printing a version. Verify `--version` actually returns a parseable
    # "Python X.Y.Z" string before trusting any candidate.
    $pythonCmd = $null
    $pythonVersion = $null
    foreach ($candidate in @("python", "py")) {
        $exists = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($exists) {
            try {
                $verOutput = & $candidate --version 2>&1 | Out-String
            } catch { $verOutput = "" }
            if ($verOutput -match "Python (\d+)\.(\d+)\.(\d+)") {
                $pythonCmd = $candidate
                $pythonVersion = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
                $pythonMajor = [int]$Matches[1]
                $pythonMinor = [int]$Matches[2]
                break
            }
        }
    }

    if (-not $pythonCmd) {
        Write-Err2 "Python 3.8+ is not installed (or not runnable)."
        Write-Detail "  Please install Python 3.8+ from https://www.python.org/"
        Write-Detail ""
        Write-Warn2 "If you've already installed Python but see a `"Python was not found;`""
        Write-Detail "  run without arguments to install from the Microsoft Store...`" message,"
        Write-Detail "  disable the fake python/python3 shortcuts at: Settings > Apps >"
        Write-Detail "  Advanced app settings > App execution aliases (turn OFF `"python.exe`""
        Write-Detail "  and `"python3.exe`"), then restart your terminal."
        throw "Python not found"
    }
    if ($pythonMajor -lt 3 -or ($pythonMajor -eq 3 -and $pythonMinor -lt 8)) {
        Write-Err2 "Python 3.8+ required, found $pythonVersion"
        throw "Python too old"
    }
    Write-Ok "Python $pythonVersion found ($pythonCmd)"

    # ── [2/6] Installation directory ───────────────────────────────────
    Write-Step "[2/6] Creating installation directory..."
    Set-Location $InstallDir
    Write-Ok "Directory: $InstallDir"

    # ── [3/6] Download client_agent.py ─────────────────────────────────
    Write-Step "[3/6] Downloading client agent..."
    $downloadServer = $ServerUrl
    $agentPath = Join-Path $InstallDir "client_agent.py"
    try {
        Invoke-WebRequest -Uri "$downloadServer/static/client_agent.py" -OutFile $agentPath -UseBasicParsing
        if (-not (Test-Path $agentPath) -or (Get-Item $agentPath).Length -eq 0) { throw "empty download" }
        Write-Ok "Downloaded from $downloadServer"
    } catch {
        Write-Err2 "Failed to download from $downloadServer/static/client_agent.py"
        Write-Detail "  Please ensure the dashboard server is running and accessible"
        throw
    }
    if (-not (Get-Content $agentPath -TotalCount 1) -match "python") {
        Write-Err2 "Downloaded file is not a valid Python script"
        throw "Invalid download"
    }
    Write-Ok "client_agent.py downloaded successfully"

    # ── [4/6] Virtual environment ───────────────────────────────────────
    Write-Step "[4/6] Setting up Python virtual environment..."
    $venvDir = Join-Path $InstallDir "venv"
    if (Test-Path $venvDir) {
        Write-Warn2 "Removing existing venv..."
        Remove-Item -Recurse -Force $venvDir
    }
    if ($DebugMode) { & $pythonCmd -m venv $venvDir } else { & $pythonCmd -m venv $venvDir *>> $LogFile }
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Err2 "Virtual environment creation failed (no venv\Scripts\python.exe found)"
        throw "venv creation failed"
    }
    Write-Ok "Virtual environment created"

    # ── [5/6] Dependencies ───────────────────────────────────────────────
    Write-Step "[5/6] Installing dependencies..."
    if ($DebugMode) {
        & $venvPython -m pip install --quiet --upgrade pip
        & $venvPython -m pip install --quiet httpx cryptography
    } else {
        & $venvPython -m pip install --quiet --upgrade pip *>> $LogFile
        & $venvPython -m pip install --quiet httpx cryptography *>> $LogFile
    }
    Write-Ok "Dependencies installed (httpx)"

    # ── [6/6] Server configuration ──────────────────────────────────────
    Write-Step "[6/6] Server configuration"
    Write-Ok "Server: $ServerUrl"

    # ── Launcher script ──────────────────────────────────────────────────
    # start.ps1 is what the Scheduled Task actually runs. It launches the
    # agent via conhost.exe --headless + cmd.exe with `>>` redirection so
    # logs append across restarts (Start-Process's -RedirectStandard*
    # params always overwrite instead), fully window-free, detached from
    # whatever spawned it, and records the PID so a future reinstall or the
    # watchdog can find and stop it.
    $startScript = Join-Path $InstallDir "start.ps1"
    $startScriptContent = @"
`$InstallDir = "$InstallDir"
`$VenvPython = "$venvPython"
`$ServerUrl = "$ServerUrl"
`$LogOut = Join-Path `$InstallDir "agent.log"
`$LogErr = Join-Path `$InstallDir "agent.error.log"
`$PidFile = Join-Path `$InstallDir "agent.pid"

`$debug = `$args -contains "-DebugMode"
Set-Location `$InstallDir

if (`$debug) {
    & `$VenvPython "`$InstallDir\client_agent.py" --server `$ServerUrl --debug
} else {
    # conhost.exe --headless runs cmd.exe attached to a genuinely headless
    # console session -- unlike a plain cmd.exe launch with -WindowStyle
    # Hidden, this can't be intercepted/flashed by Windows Terminal's
    # default-terminal-app handoff (which otherwise briefly shows a tab for
    # any new console-subsystem process regardless of window style).
    `$cmdArgs = '--headless cmd.exe /c ""' + `$VenvPython + '" "' + `$InstallDir + '\client_agent.py" --server "' + `$ServerUrl + '" >> "' + `$LogOut + '" 2>> "' + `$LogErr + '""'
    `$proc = Start-Process -FilePath "conhost.exe" -ArgumentList `$cmdArgs -WorkingDirectory `$InstallDir -WindowStyle Hidden -PassThru
    `$proc.Id | Out-File -FilePath `$PidFile -Encoding ascii -NoNewline
}
"@
    Set-Content -Path $startScript -Value $startScriptContent -Encoding UTF8
    Write-Ok "Launcher script created: $startScript"

    # Watchdog: rechecks every 5 minutes (matches the agent's own sync
    # cadence) and only relaunches if the recorded PID is no longer alive,
    # so it doesn't refork a perfectly healthy agent.
    $watchdogScript = Join-Path $InstallDir "watchdog.ps1"

    # Task Scheduler launching `powershell.exe -WindowStyle Hidden` directly
    # still flashes a window -- classic conhost briefly allocates one before
    # the hidden style applies, and on Windows 11 with Windows Terminal set
    # as the default terminal, its terminal-handoff mechanism can flash a
    # Terminal tab too, bypassing -WindowStyle Hidden entirely.
    # `conhost.exe --headless <cmd>` (an undocumented but long-standing,
    # native-since-Windows-7 conhost switch) runs the target attached to a
    # genuinely headless console session that neither classic conhost nor
    # Windows Terminal's handoff ever renders a window for -- so every
    # hidden PowerShell launch below goes through it instead of a plain
    # `-WindowStyle Hidden` invocation.
    $watchdogContent = @"
`$InstallDir = "$InstallDir"
`$PidFile = Join-Path `$InstallDir "agent.pid"
`$alive = `$false
if (Test-Path `$PidFile) {
    `$existingPid = Get-Content `$PidFile -ErrorAction SilentlyContinue
    if (`$existingPid -and (Get-Process -Id `$existingPid -ErrorAction SilentlyContinue)) { `$alive = `$true }
}
if (-not `$alive) {
    & conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$startScript"
}
"@
    Set-Content -Path $watchdogScript -Value $watchdogContent -Encoding UTF8

    # ── Scheduled Tasks (per-user, no admin required) ───────────────────
    Write-Detail ""
    Write-Step "Setting up auto-start..."
    $serviceStarted = $false

    if (-not $DebugMode) {
        $taskAction = "conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
        $watchdogAction = "conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$watchdogScript`""

        # /RU <user> without /RP registers the task with LogonType=S4U instead
        # of the default InteractiveToken. InteractiveToken tasks can ONLY
        # launch while that exact session is logged on AND unlocked/connected
        # -- if the trigger fires while the screen is locked or an RDP
        # session is disconnected (routine on most machines), Task Scheduler
        # can't start the process and leaves the instance sitting as
        # "Queued". Since the watchdog trigger re-fires every 5 minutes
        # forever, queued instances pile up and the task appears
        # permanently stuck in Queue. S4U runs in the background (no UI)
        # regardless of lock/disconnect/logoff state, which is exactly what
        # start.ps1/watchdog.ps1 need since neither touches the desktop, and
        # it still requires no admin rights and stores no password.
        $currentUser = "$env:USERDOMAIN\$env:USERNAME"

        schtasks /Create /F /TN $TaskName /TR $taskAction /SC ONLOGON /RL LIMITED /RU $currentUser *> $null
        if ($LASTEXITCODE -eq 0) {
            schtasks /Create /F /TN $WatchdogTaskName /TR $watchdogAction /SC MINUTE /MO 5 /RL LIMITED /RU $currentUser *> $null
            schtasks /Run /TN $TaskName *> $null
            $serviceStarted = $true
            Write-Ok "Scheduled tasks created - agent is running in the background (auto-restarts)"

            # `schtasks /Run` returning success only means Task Scheduler
            # accepted the request to start an instance -- it does NOT mean
            # the process actually launched. On local/workgroup accounts
            # (no stored password, no true S4U), or under some Group
            # Policy restrictions, the instance can silently sit as
            # "Queued" forever, leaving the install looking successful with
            # nothing actually running. Verify agent.pid points at a live
            # process, and if it doesn't, fall back to launching start.ps1
            # directly so the agent is guaranteed to be running once this
            # script finishes (the scheduled tasks remain in place for
            # future logons/restarts).
            Start-Sleep -Seconds 3
            $pidFile = Join-Path $InstallDir "agent.pid"
            $agentRunning = $false
            if (Test-Path $pidFile) {
                $launchedPid = Get-Content $pidFile -ErrorAction SilentlyContinue
                if ($launchedPid -and (Get-Process -Id $launchedPid -ErrorAction SilentlyContinue)) {
                    $agentRunning = $true
                }
            }
            if (-not $agentRunning) {
                Write-Warn2 "Scheduled task did not start the agent (may be stuck in 'Queued' state); starting it directly as a fallback"
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript *>> $LogFile
                Start-Sleep -Seconds 2
                if (Test-Path $pidFile) {
                    $launchedPid = Get-Content $pidFile -ErrorAction SilentlyContinue
                    if ($launchedPid -and (Get-Process -Id $launchedPid -ErrorAction SilentlyContinue)) {
                        Write-Ok "Agent started directly (PID $launchedPid); scheduled task will keep it running after future logons"
                    } else {
                        Write-Err2 "Agent still failed to start after direct launch attempt"
                    }
                } else {
                    Write-Err2 "Agent still failed to start after direct launch attempt"
                }
            }
        } else {
            Write-Warn2 "Could not create scheduled task; falling back to a plain background process"
            Write-Detail "  (won't survive logging off - re-run with -DebugMode to diagnose)"
        }

        Write-Detail ""
        Write-Warn2 "Auto-start commands:"
        Write-Detail "  Status:  schtasks /query /tn $TaskName /v /fo list"
        Write-Detail "  Stop:    schtasks /end /tn $TaskName"
        Write-Detail "  Disable: schtasks /change /tn $TaskName /disable"
        Write-Detail "  Enable:  schtasks /change /tn $TaskName /enable"
        Write-Detail "  Logs:    Get-Content -Wait `"$InstallDir\agent.log`""
    }

    # ── Done ─────────────────────────────────────────────────────────────
    Write-Detail ""
    Write-Detail "=========================================================="
    Write-Detail "              Installation Complete!"
    Write-Detail "=========================================================="
    Write-Detail ""
    Write-Detail "Installation directory: $InstallDir"
    Write-Detail "Server URL: $ServerUrl"
    Write-Detail ""
    Write-Warn2 "Quick Start:"
    Write-Detail "  Runs in background by default (survives closing this window, auto-restarts)."
    Write-Detail "  Run in foreground (debugging): powershell -ExecutionPolicy Bypass -File `"$startScript`" -DebugMode"
    Write-Detail ""
    Write-Warn2 "Uninstall:"
    Write-Detail "  schtasks /end /tn $TaskName"
    Write-Detail "  schtasks /delete /tn $TaskName /f"
    Write-Detail "  schtasks /delete /tn $WatchdogTaskName /f"
    Write-Detail "  Remove-Item -Recurse -Force `"$InstallDir`""
    Write-Detail ""

    # Invoke via a nested `powershell.exe -ExecutionPolicy Bypass -File`
    # rather than `& $startScript` directly: this whole installer was likely
    # run via `irm ... | iex`, which evaluates in-memory and isn't subject to
    # the system's script execution policy -- but `& $startScript` runs a
    # real .ps1 *file* from disk, which IS subject to it. If the machine's
    # policy is Restricted (a common default), a direct `& $startScript`
    # would fail right here even though everything up to this point worked.
    if ($DebugMode) {
        Write-Detail "Starting agent in the foreground (-DebugMode)..."
        Write-Warn2 "Press Ctrl+C to stop."
        Write-Detail ""
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -DebugMode
    } elseif ($serviceStarted) {
        Write-Detail "Agent is running in the background, managed by Windows Task Scheduler."
        Write-Detail "It will keep running after this window closes and auto-restart if stopped."
    } else {
        Write-Detail "Starting agent in the background..."
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript *>> $LogFile
    }
    Write-Host "Successfully installed"
    Write-Host "  Log file: $LogFile"
} catch {
    Write-Host "Failed installing" -ForegroundColor Red
    Write-Host "  See $LogFile for details"
    if ($DebugMode) {
        Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
    } else {
        Add-Content -Path $LogFile -Value "XX  Error: $($_.Exception.Message)" -ErrorAction SilentlyContinue
    }
    exit 1
}
