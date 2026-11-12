-- Schema v3: Session Tagging, Anomaly Detection, Natural-Language Query Log,
-- and Azure DevOps Integration.
-- Claude Local Dashboard (WhirlpoolBee) — additive only, loaded after
-- schema.sql and schema_extensions.sql. All statements use IF NOT EXISTS so
-- this file is safe to re-run against an existing database.

-- ============================================================================
-- SESSION TAGGING (bugfix/feature/refactor/etc)
-- ============================================================================

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    color TEXT DEFAULT '#6366f1',
    is_system BOOLEAN DEFAULT 0, -- seeded default tag vs user-created
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Many-to-many: a session can carry multiple tags (e.g. 'bugfix' + 'test').
CREATE TABLE IF NOT EXISTS session_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    source TEXT DEFAULT 'manual', -- 'manual' or 'auto' (keyword-suggested)
    confidence REAL, -- set when source='auto'
    tagged_by TEXT, -- email of whoever applied it (manual only)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (tag_id) REFERENCES tags(id),
    UNIQUE(session_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags(session_id);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag_id);

-- ============================================================================
-- ANOMALY DETECTION (token/cost spikes per user or machine)
-- ============================================================================

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TIMESTAMP NOT NULL,
    scope_type TEXT NOT NULL, -- 'machine' (per-device) or 'user' (aggregated across a user's machines)
    scope_id TEXT NOT NULL, -- machine_id, or logged_in_user for scope_type='user'
    scope_label TEXT, -- display name cached at detection time
    metric TEXT NOT NULL, -- 'daily_cost', 'daily_tokens', 'daily_sessions'
    metric_date DATE NOT NULL,
    observed_value REAL NOT NULL,
    baseline_mean REAL,
    baseline_stddev REAL,
    deviation_score REAL, -- z-score of observed_value vs baseline
    severity TEXT NOT NULL, -- 'low', 'medium', 'high'
    status TEXT DEFAULT 'open', -- 'open', 'acknowledged', 'dismissed'
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope_type, scope_id, metric, metric_date)
);

CREATE INDEX IF NOT EXISTS idx_anomalies_scope ON anomalies(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_status ON anomalies(status);
CREATE INDEX IF NOT EXISTS idx_anomalies_date ON anomalies(metric_date);

-- ============================================================================
-- NATURAL-LANGUAGE QUERY LOG ("How much did Team X spend last week?")
-- ============================================================================

-- Logged for transparency/audit and to improve the keyword matcher over
-- time; the parsing itself is 100% local (regex/keyword matching over
-- existing tables), never sent to an external service.
CREATE TABLE IF NOT EXISTS nl_query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_by TEXT,
    question TEXT NOT NULL,
    resolved_metric TEXT,
    resolved_scope_type TEXT, -- 'team', 'user', 'machine', 'all'
    resolved_scope_value TEXT,
    time_range_label TEXT,
    matched BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nl_query_log_created ON nl_query_log(created_at);

-- ============================================================================
-- AZURE DEVOPS INTEGRATION (PRs/commits linked to sessions)
-- ============================================================================

CREATE TABLE IF NOT EXISTS ado_pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ado_pr_id INTEGER NOT NULL,
    ado_project TEXT NOT NULL,
    ado_repository TEXT NOT NULL,
    title TEXT,
    author TEXT,
    status TEXT, -- 'active', 'completed', 'abandoned'
    created_date TIMESTAMP,
    closed_date TIMESTAMP,
    source_branch TEXT,
    target_branch TEXT,
    url TEXT,
    closed_by TEXT, -- displayName of who completed/abandoned the PR
    reviewers_json TEXT, -- JSON list of {display_name, vote, vote_label} snapshotted at last sync
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ado_project, ado_repository, ado_pr_id)
);

CREATE INDEX IF NOT EXISTS idx_ado_prs_status ON ado_pull_requests(status);
CREATE INDEX IF NOT EXISTS idx_ado_prs_created ON ado_pull_requests(created_date);

CREATE TABLE IF NOT EXISTS ado_pr_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_row_id INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    author_email TEXT,
    commit_date TIMESTAMP,
    comment TEXT,
    FOREIGN KEY (pr_row_id) REFERENCES ado_pull_requests(id),
    UNIQUE(pr_row_id, commit_sha)
);

CREATE INDEX IF NOT EXISTS idx_ado_pr_commits_pr ON ado_pr_commits(pr_row_id);

-- Links a local Claude Code session to an Azure DevOps PR when a commit in
-- that PR, authored by the session's logged-in user, falls inside the
-- session's active time window (+/- buffer).
CREATE TABLE IF NOT EXISTS session_pr_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    pr_row_id INTEGER NOT NULL,
    match_method TEXT, -- 'time_window', 'time_window+author'
    match_confidence REAL, -- 0-100
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (pr_row_id) REFERENCES ado_pull_requests(id),
    UNIQUE(session_id, pr_row_id)
);

CREATE INDEX IF NOT EXISTS idx_session_pr_links_session ON session_pr_links(session_id);
CREATE INDEX IF NOT EXISTS idx_session_pr_links_pr ON session_pr_links(pr_row_id);

-- Per-commit attribution: which specific commit in a PR was made during an
-- active Claude Code session (same author, commit timestamp inside the
-- session's time window +/- buffer). This is what "AI %" per PR is computed
-- from (ai_commits / total_commits for that PR) -- a commit-level signal,
-- not a line-of-code diff (Azure DevOps doesn't expose line-level diffs
-- cheaply at the scale of an entire org sync).
CREATE TABLE IF NOT EXISTS ado_commit_session_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_row_id INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pr_row_id) REFERENCES ado_pull_requests(id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    UNIQUE(pr_row_id, commit_sha, session_id)
);

CREATE INDEX IF NOT EXISTS idx_ado_commit_matches_pr ON ado_commit_session_matches(pr_row_id);

