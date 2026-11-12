-- Schema Extensions for Learning & Development, AI Optimization, and Code Analytics
-- Claude Local Dashboard v2.0

-- ============================================================================
-- LEARNING & DEVELOPMENT
-- ============================================================================

-- Best Practices Library
CREATE TABLE IF NOT EXISTS best_practices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, -- 'prompt_engineering', 'code_quality', 'workflow', 'debugging'
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    example_prompt TEXT,
    example_code TEXT,
    tags TEXT, -- JSON array
    difficulty_level TEXT, -- 'beginner', 'intermediate', 'advanced'
    upvotes INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_best_practices_category ON best_practices(category);
CREATE INDEX IF NOT EXISTS idx_best_practices_difficulty ON best_practices(difficulty_level);

-- User Skills Progression
CREATE TABLE IF NOT EXISTS user_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, -- From machines.logged_in_user
    skill_category TEXT NOT NULL, -- 'prompt_engineering', 'code_quality', 'efficiency', 'collaboration'
    skill_level INTEGER DEFAULT 0, -- 0-100 score
    sessions_completed INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    code_files_modified INTEGER DEFAULT 0,
    average_session_quality REAL DEFAULT 0.0, -- 0-100 score
    last_activity TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, skill_category)
);

CREATE INDEX IF NOT EXISTS idx_user_skills_user ON user_skills(user_id);
CREATE INDEX IF NOT EXISTS idx_user_skills_category ON user_skills(skill_category);

-- Learning Paths
CREATE TABLE IF NOT EXISTS learning_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    difficulty_level TEXT, -- 'beginner', 'intermediate', 'advanced'
    estimated_duration_hours INTEGER,
    steps TEXT NOT NULL, -- JSON array of step objects
    prerequisites TEXT, -- JSON array of skill names
    completion_reward_points INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User Progress on Learning Paths
CREATE TABLE IF NOT EXISTS learning_path_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    learning_path_id INTEGER NOT NULL,
    current_step INTEGER DEFAULT 0,
    steps_completed TEXT, -- JSON array of completed step IDs
    progress_percentage REAL DEFAULT 0.0,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    last_activity TIMESTAMP,
    FOREIGN KEY (learning_path_id) REFERENCES learning_paths(id),
    UNIQUE(user_id, learning_path_id)
);

CREATE INDEX IF NOT EXISTS idx_learning_progress_user ON learning_path_progress(user_id);

-- Community Patterns (Anonymized Sharing)
CREATE TABLE IF NOT EXISTS community_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL, -- 'prompt_template', 'code_snippet', 'workflow'
    title TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    language TEXT, -- Programming language if applicable
    use_case TEXT,
    success_rate REAL, -- How often this pattern led to successful outcomes
    usage_count INTEGER DEFAULT 0,
    upvotes INTEGER DEFAULT 0,
    downvotes INTEGER DEFAULT 0,
    tags TEXT, -- JSON array
    is_verified BOOLEAN DEFAULT 0, -- Verified by moderators
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_community_patterns_type ON community_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_community_patterns_language ON community_patterns(language);

-- Achievements/Badges
CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    badge_icon TEXT, -- Icon name or URL
    badge_color TEXT, -- Hex color code
    criteria TEXT NOT NULL, -- JSON object defining achievement criteria
    points INTEGER DEFAULT 0,
    rarity TEXT, -- 'common', 'rare', 'epic', 'legendary'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User Achievements
CREATE TABLE IF NOT EXISTS user_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    achievement_id INTEGER NOT NULL,
    earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    progress_data TEXT, -- JSON object with progress details
    FOREIGN KEY (achievement_id) REFERENCES achievements(id),
    UNIQUE(user_id, achievement_id)
);

CREATE INDEX IF NOT EXISTS idx_user_achievements_user ON user_achievements(user_id);

-- ============================================================================
-- AI USAGE OPTIMIZATION
-- ============================================================================

-- Prompt Quality Analysis
CREATE TABLE IF NOT EXISTS prompt_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER, -- If linked to a specific message
    prompt_text TEXT NOT NULL,
    quality_score REAL, -- 0-100 score
    clarity_score REAL, -- 0-100
    specificity_score REAL, -- 0-100
    context_efficiency_score REAL, -- 0-100
    token_efficiency REAL, -- Tokens used vs tokens needed
    suggestions TEXT, -- JSON array of improvement suggestions
    improved_prompt TEXT, -- AI-suggested better version
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_prompt_analysis_session ON prompt_analysis(session_id);
CREATE INDEX IF NOT EXISTS idx_prompt_analysis_quality ON prompt_analysis(quality_score);

-- Context Optimization Insights
CREATE TABLE IF NOT EXISTS context_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    total_context_tokens INTEGER,
    unique_context_tokens INTEGER,
    redundant_tokens INTEGER,
    optimal_tokens INTEGER,
    waste_percentage REAL,
    recommendations TEXT, -- JSON array of optimization recommendations
    estimated_cost_savings REAL, -- USD savings if optimized
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_context_insights_session ON context_insights(session_id);

-- Model Performance Tracking
CREATE TABLE IF NOT EXISTS model_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT, -- Which machine reported this; NULL means unattributed legacy data
    model_name TEXT NOT NULL,
    task_type TEXT, -- 'code_generation', 'debugging', 'refactoring', 'explanation'
    average_response_time_ms REAL,
    success_rate REAL, -- 0-100
    average_token_efficiency REAL,
    cost_per_task REAL,
    user_satisfaction_score REAL, -- 0-100
    total_tasks INTEGER DEFAULT 0,
    date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(machine_id, model_name, task_type, date)
);

CREATE INDEX IF NOT EXISTS idx_model_performance_model ON model_performance(model_name);
CREATE INDEX IF NOT EXISTS idx_model_performance_date ON model_performance(date);

-- Caching Analytics
CREATE TABLE IF NOT EXISTS cache_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cache_hit_rate REAL, -- 0-100
    cache_efficiency_score REAL, -- 0-100
    cost_saved_usd REAL,
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_cache_analytics_session ON cache_analytics(session_id);

-- Token Waste Patterns
CREATE TABLE IF NOT EXISTS token_waste_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL, -- 'excessive_context', 'repetitive_prompts', 'inefficient_code_gen'
    description TEXT NOT NULL,
    sessions_affected TEXT, -- JSON array of session IDs
    total_wasted_tokens INTEGER,
    estimated_cost_wasted REAL,
    recommendation TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_token_waste_type ON token_waste_patterns(pattern_type);

-- ============================================================================
-- CODE ANALYTICS
-- ============================================================================

-- File Modifications Tracking (Enhanced)
CREATE TABLE IF NOT EXISTS code_modifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT, -- Auto-detected programming language
    modification_type TEXT, -- 'create', 'modify', 'delete', 'refactor'
    lines_added INTEGER DEFAULT 0,
    lines_deleted INTEGER DEFAULT 0,
    lines_modified INTEGER DEFAULT 0,
    complexity_before REAL, -- Cyclomatic complexity
    complexity_after REAL,
    ai_suggestion_accepted BOOLEAN, -- Whether user kept AI changes
    modification_quality_score REAL, -- 0-100 based on best practices
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_code_mods_session ON code_modifications(session_id);
CREATE INDEX IF NOT EXISTS idx_code_mods_language ON code_modifications(language);
CREATE INDEX IF NOT EXISTS idx_code_mods_file ON code_modifications(file_path);

-- Language Usage Statistics
CREATE TABLE IF NOT EXISTS language_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    language TEXT NOT NULL,
    files_modified INTEGER DEFAULT 0,
    lines_written INTEGER DEFAULT 0,
    sessions_count INTEGER DEFAULT 0,
    total_time_seconds INTEGER DEFAULT 0,
    proficiency_score REAL DEFAULT 0.0, -- 0-100 based on code quality
    last_used TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, language)
);

CREATE INDEX IF NOT EXISTS idx_language_stats_user ON language_stats(user_id);
CREATE INDEX IF NOT EXISTS idx_language_stats_language ON language_stats(language);

-- Code Quality Metrics
CREATE TABLE IF NOT EXISTS code_quality_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    file_path TEXT,
    language TEXT,
    test_coverage_percentage REAL,
    cyclomatic_complexity REAL,
    maintainability_index REAL, -- 0-100
    code_duplication_percentage REAL,
    security_issues_count INTEGER DEFAULT 0,
    performance_issues_count INTEGER DEFAULT 0,
    best_practices_score REAL, -- 0-100
    overall_quality_score REAL, -- 0-100
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_code_quality_session ON code_quality_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_code_quality_language ON code_quality_metrics(language);

-- Refactoring Impact Analysis
CREATE TABLE IF NOT EXISTS refactoring_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    refactoring_type TEXT, -- 'extract_function', 'rename', 'simplify', 'optimize'
    before_snapshot TEXT, -- JSON with before metrics
    after_snapshot TEXT, -- JSON with after metrics
    impact_score REAL, -- -100 to 100 (negative = worse, positive = better)
    complexity_improvement REAL,
    readability_improvement REAL,
    performance_improvement REAL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_refactoring_session ON refactoring_analysis(session_id);

-- AI Suggestion Acceptance Tracking
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    suggestion_type TEXT, -- 'code_completion', 'bug_fix', 'refactoring', 'optimization'
    suggestion_text TEXT NOT NULL,
    context_text TEXT, -- Code context where suggestion was made
    was_accepted BOOLEAN,
    was_modified BOOLEAN, -- User modified before accepting
    acceptance_time_seconds INTEGER, -- How long user took to accept/reject
    file_path TEXT,
    language TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_suggestions_session ON ai_suggestions(session_id);
CREATE INDEX IF NOT EXISTS idx_ai_suggestions_type ON ai_suggestions(suggestion_type);
CREATE INDEX IF NOT EXISTS idx_ai_suggestions_accepted ON ai_suggestions(was_accepted);

-- Project Health Tracking
CREATE TABLE IF NOT EXISTS project_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    working_directory TEXT UNIQUE NOT NULL,
    overall_health_score REAL, -- 0-100
    code_quality_score REAL, -- 0-100
    test_coverage REAL,
    documentation_coverage REAL,
    security_score REAL, -- 0-100
    technical_debt_score REAL, -- 0-100 (higher = more debt)
    active_contributors INTEGER,
    total_sessions INTEGER,
    last_analyzed TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_project_health_dir ON project_health(working_directory);
CREATE INDEX IF NOT EXISTS idx_project_health_score ON project_health(overall_health_score);
