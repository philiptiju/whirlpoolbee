-- Claude Local Dashboard Database Schema
-- SQLite 3.x

-- Groups table: teams that machines/users can be assigned to, so reports
-- can be scoped to a team instead of a single machine or the whole fleet.
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Machines table: tracks client machines reporting to server
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT UNIQUE NOT NULL, -- UUID or hostname-based unique ID
    machine_name TEXT NOT NULL, -- hostname
    ip_address TEXT,
    os_type TEXT, -- 'darwin', 'linux', 'windows'
    os_version TEXT,
    logged_in_user TEXT,            -- username or email of the logged-in user
    agent_version TEXT,             -- client_agent.py version this machine is running
    group_id INTEGER REFERENCES groups(id), -- team this machine/user belongs to, if any
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_machines_machine_id ON machines(machine_id);
CREATE INDEX IF NOT EXISTS idx_machines_last_seen ON machines(last_seen);
CREATE INDEX IF NOT EXISTS idx_machines_is_active ON machines(is_active);
-- idx_machines_group_id is created in Database._migrate_groups() instead of
-- here: on an existing (pre-groups) database this script's CREATE TABLE is a
-- no-op (table already exists without group_id), so an index on group_id
-- here would fail with "no such column" until the ALTER TABLE migration runs.


-- Sync log table: one row per sync cycle reported by each client agent, so
-- the dashboard can show what actually happened on each sync request
-- (items found/synced/failed) rather than just the machine's last_seen time.
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    source TEXT,                    -- 'sqlite' or 'jsonl'
    items_total INTEGER DEFAULT 0,  -- items found at the source this cycle
    items_synced INTEGER DEFAULT 0, -- new items actually sent to the server
    items_failed INTEGER DEFAULT 0,
    message TEXT,                   -- short human-readable summary
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_log_machine_id ON sync_log(machine_id, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_log_synced_at ON sync_log(synced_at);

-- Sessions table: tracks Claude Code sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    machine_id TEXT NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds INTEGER,
    working_directory TEXT,
    command_args TEXT,
    model_name TEXT,
    exit_code INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_working_dir ON sessions(working_directory);
CREATE INDEX IF NOT EXISTS idx_sessions_machine_id ON sessions(machine_id);

-- Launches table: every time claude is invoked
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    machine_id TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    working_directory TEXT,
    command_line TEXT,
    shell_pid INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_launches_timestamp ON launches(timestamp);
CREATE INDEX IF NOT EXISTS idx_launches_session ON launches(session_id);
CREATE INDEX IF NOT EXISTS idx_launches_machine_id ON launches(machine_id);

-- Process metrics: CPU, memory, etc.
CREATE TABLE IF NOT EXISTS process_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    pid INTEGER NOT NULL,
    session_id TEXT,
    machine_id TEXT NOT NULL,
    cpu_percent REAL,
    memory_rss_mb REAL,
    memory_vms_mb REAL,
    memory_percent REAL,
    num_threads INTEGER,
    num_fds INTEGER,
    process_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_process_metrics_timestamp ON process_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_process_metrics_session ON process_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_process_metrics_machine_id ON process_metrics(machine_id);

-- Network metrics: bytes sent/received, connections
CREATE TABLE IF NOT EXISTS network_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    pid INTEGER NOT NULL,
    session_id TEXT,
    machine_id TEXT NOT NULL,
    bytes_sent BIGINT,
    bytes_recv BIGINT,
    packets_sent BIGINT,
    packets_recv BIGINT,
    num_connections INTEGER,
    connection_details TEXT, -- JSON array of connections
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_network_metrics_timestamp ON network_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_network_metrics_session ON network_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_network_metrics_machine_id ON network_metrics(machine_id);

-- Filesystem events: files read/written
CREATE TABLE IF NOT EXISTS filesystem_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    session_id TEXT,
    machine_id TEXT NOT NULL,
    event_type TEXT, -- 'read', 'write', 'create', 'delete'
    file_path TEXT NOT NULL,
    file_size_bytes BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_filesystem_events_timestamp ON filesystem_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_filesystem_events_session ON filesystem_events(session_id);
CREATE INDEX IF NOT EXISTS idx_filesystem_events_path ON filesystem_events(file_path);
CREATE INDEX IF NOT EXISTS idx_filesystem_events_machine_id ON filesystem_events(machine_id);

-- Token usage: track token consumption
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    session_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    model_name TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_session ON token_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_machine_id ON token_usage(machine_id);

-- Daily rollups: aggregated daily statistics
CREATE TABLE IF NOT EXISTS daily_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE UNIQUE NOT NULL,
    total_sessions INTEGER DEFAULT 0,
    total_launches INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_duration_seconds INTEGER DEFAULT 0,
    avg_cpu_percent REAL,
    avg_memory_mb REAL,
    max_memory_mb REAL,
    total_bytes_sent BIGINT DEFAULT 0,
    total_bytes_recv BIGINT DEFAULT 0,
    unique_directories INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_rollups_date ON daily_rollups(date);

-- Configuration table: store app settings
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Discovery log: track what data sources were found
CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    source_type TEXT, -- 'config', 'log', 'transcript', 'sqlite', 'history'
    source_path TEXT,
    records_found INTEGER,
    successfully_parsed BOOLEAN,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_log_timestamp ON discovery_log(timestamp);

-- Messages table: conversation history for sessions
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    role TEXT NOT NULL, -- 'user' or 'assistant'
    content TEXT NOT NULL,
    tokens INTEGER,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_machine_id ON messages(machine_id);
