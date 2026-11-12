"""Database operations for WhirlpoolBee."""

import os
import re
import asyncio
import sqlite3
import statistics
import aiosqlite
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, date, timedelta
import json


from src.utils import tone_analysis


class Database:
    """SQLite database manager."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.schema_path = Path(__file__).parent.parent / "schema.sql"
        self.schema_extensions_path = Path(__file__).parent.parent / "schema_extensions.sql"
        self.schema_v3_path = Path(__file__).parent.parent / "schema_v3.sql"
        self.schema_v4_path = Path(__file__).parent.parent / "schema_v4.sql"

    def initialize(self):
        """Initialize database with schema."""
        conn = sqlite3.connect(self.db_path)

        # Read and execute main schema
        with open(self.schema_path, 'r') as f:
            schema = f.read()

        conn.executescript(schema)
        conn.commit()

        # Read and execute schema extensions
        if self.schema_extensions_path.exists():
            with open(self.schema_extensions_path, 'r') as f:
                schema_extensions = f.read()
            conn.executescript(schema_extensions)
            conn.commit()

        # Read and execute v3 schema (tagging, anomalies, NLQ log, Azure DevOps)
        if self.schema_v3_path.exists():
            with open(self.schema_v3_path, 'r') as f:
                schema_v3 = f.read()
            conn.executescript(schema_v3)
            conn.commit()

        # Read and execute v4 schema (DB-driven login providers + local accounts)
        if self.schema_v4_path.exists():
            with open(self.schema_v4_path, 'r') as f:
                schema_v4 = f.read()
            conn.executescript(schema_v4)
            conn.commit()

        # Auto-migrate: add logged_in_user column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)").fetchall()}
        if "logged_in_user" not in cols:
            conn.execute("ALTER TABLE machines ADD COLUMN logged_in_user TEXT")
            conn.commit()
        if "agent_version" not in cols:
            conn.execute("ALTER TABLE machines ADD COLUMN agent_version TEXT")
            conn.commit()
        if "agent_token" not in cols:
            # Per-machine write-auth secret, issued once on first
            # /api/client/register and required (X-Agent-Token header) on
            # every subsequent /api/client/* write for that machine_id --
            # closes the previously-unauthenticated agent ingestion gap
            # (CWE-306). NULL means "not yet issued" (pre-upgrade machines);
            # verify_or_issue_agent_token() atomically issues one to the
            # first caller for such a registered machine.
            conn.execute("ALTER TABLE machines ADD COLUMN agent_token TEXT")
            conn.commit()

        self._migrate_model_performance_machine_id(conn)
        self._migrate_groups(conn)
        self._migrate_ai_suggestions_code_columns(conn)
        self._migrate_admin_users(conn)
        self._migrate_super_users(conn)
        self._migrate_ado_pull_request_review_columns(conn)
        self._drop_ado_repo_mappings(conn)
        self._migrate_azure_ad_env_to_auth_provider(conn)
        self._seed_default_content(conn)
        self._seed_default_tags(conn)

        conn.close()

    def _drop_ado_repo_mappings(self, conn: sqlite3.Connection):
        """Drop the now-unused ado_repo_mappings table. Azure DevOps sync
        used to require an admin to manually map each session's local
        working_directory to an ADO project/repository; sync now
        auto-discovers every project/repository directly from the Azure
        DevOps API, and session<->PR linking matches purely on the
        session's logged-in user + time window, so this table (and any
        mappings an admin had configured) is no longer needed."""
        conn.execute("DROP TABLE IF EXISTS ado_repo_mappings")
        conn.commit()

    def _migrate_ado_pull_request_review_columns(self, conn: sqlite3.Connection):
        """Add closed_by/reviewers_json columns to ado_pull_requests for the
        PR 'View Details' report (who completed/merged the PR, and the
        current reviewer/approval state) -- both are already present in
        the Azure DevOps list-pull-requests response the sync loop already
        fetches, so no extra API calls are needed to populate them."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ado_pull_requests)").fetchall()}
        if "closed_by" not in cols:
            conn.execute("ALTER TABLE ado_pull_requests ADD COLUMN closed_by TEXT")
        if "reviewers_json" not in cols:
            conn.execute("ALTER TABLE ado_pull_requests ADD COLUMN reviewers_json TEXT")
        conn.commit()

    def _migrate_ai_suggestions_code_columns(self, conn: sqlite3.Connection):
        """Add code_before/code_after columns to ai_suggestions so the AI
        Suggestions tab can show the actual before/after code snippet for
        each suggestion (previously only a generic context_text column
        existed, which the frontend never populated)."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_suggestions)").fetchall()}
        if "code_before" not in cols:
            conn.execute("ALTER TABLE ai_suggestions ADD COLUMN code_before TEXT")
        if "code_after" not in cols:
            conn.execute("ALTER TABLE ai_suggestions ADD COLUMN code_after TEXT")
        conn.commit()

    def _migrate_admin_users(self, conn: sqlite3.Connection):
        """Create the admin_users table: users who can view every machine's
        data (everyone else is restricted to only the machine(s) they're the
        logged-in user on -- see Database.get_machine_ids_for_user and
        enforce_machine_scope in main.py). Seeded on first run from the
        DASHBOARD_ADMIN_EMAILS env var (comma-separated) so at least one
        admin exists right after a fresh deploy without a manual DB edit;
        admins can add/remove others afterwards via /api/admin/users."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        for email in os.environ.get("DASHBOARD_ADMIN_EMAILS", "").split(","):
            email = email.strip().lower()
            if email:
                conn.execute("INSERT OR IGNORE INTO admin_users (email) VALUES (?)", (email,))
        conn.commit()

    def _migrate_super_users(self, conn: sqlite3.Connection):
        """Create the super_users table: a narrower tier than admin_users,
        used to gate individual sensitive features (currently just the
        Conversation Tone page/API) to a hand-picked few people rather than
        every admin. Being an admin does NOT imply being a super user, and
        vice versa - the two lists are independent. Seeded on first run from
        the DASHBOARD_SUPER_USER_EMAILS env var (comma-separated); can also
        be managed afterwards via /api/admin/super-users (admin-only)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS super_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        for email in os.environ.get("DASHBOARD_SUPER_USER_EMAILS", "").split(","):
            email = email.strip().lower()
            if email:
                conn.execute("INSERT OR IGNORE INTO super_users (email) VALUES (?)", (email,))
        conn.commit()

    def _migrate_azure_ad_env_to_auth_provider(self, conn: sqlite3.Connection):
        """One-time migration for deployments upgrading from the old static,
        env-var-only Azure AD config: if AZURE_AD_CLIENT_ID/CLIENT_SECRET/
        TENANT_ID are all set in the environment AND no 'azure-ad' provider
        row exists yet in auth_providers, create one automatically so
        existing SSO keeps working with zero manual steps. Login providers
        are now managed entirely through the Admin -> Login Providers UI
        (stored in auth_providers) going forward -- these env vars are only
        ever consulted here, once, and never again at runtime."""
        from src.utils.config import Config
        from src.utils.crypto import encrypt_secret

        if not (Config.AZURE_AD_CLIENT_ID and Config.AZURE_AD_CLIENT_SECRET and Config.AZURE_AD_TENANT_ID):
            return
        existing = conn.execute(
            "SELECT 1 FROM auth_providers WHERE provider_key = 'azure-ad'"
        ).fetchone()
        if existing:
            return
        conn.execute(
            """INSERT INTO auth_providers
               (provider_key, provider_type, display_name, client_id,
                client_secret_encrypted, tenant_id, enabled)
               VALUES ('azure-ad', 'azure_ad', 'Microsoft / Windows', ?, ?, ?, 1)""",
            (
                Config.AZURE_AD_CLIENT_ID,
                encrypt_secret(Config.AZURE_AD_CLIENT_SECRET),
                Config.AZURE_AD_TENANT_ID,
            ),
        )
        conn.commit()

    def _migrate_groups(self, conn: sqlite3.Connection):
        """Create the groups table (teams that machines/users can be assigned
        to) and add machines.group_id so any machine can belong to at most
        one group. Groups let users scope every report/filter to a team
        instead of a single machine or the entire fleet."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(machines)").fetchall()}
        if "group_id" not in cols:
            conn.execute("ALTER TABLE machines ADD COLUMN group_id INTEGER REFERENCES groups(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machines_group_id ON machines(group_id)")
        conn.commit()

    def _migrate_model_performance_machine_id(self, conn: sqlite3.Connection):
        """Add a machine_id column to model_performance so per-model stats can
        be scoped to a single machine like every other Optimization metric.
        model_performance previously had no machine linkage at all (rows were
        keyed only by model_name/task_type/date, silently merging every
        machine's reports together) — SQLite can't ALTER a UNIQUE constraint
        in place, so the table is recreated with machine_id folded into the
        uniqueness key. Existing rows are preserved with machine_id = NULL
        (unattributed legacy data — they only surface in the "All Machines"
        aggregate going forward; new reports are always machine-tagged)."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(model_performance)").fetchall()}
        if "machine_id" in cols:
            return
        conn.executescript("""
            CREATE TABLE model_performance_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT,
                model_name TEXT NOT NULL,
                task_type TEXT,
                average_response_time_ms REAL,
                success_rate REAL,
                average_token_efficiency REAL,
                cost_per_task REAL,
                user_satisfaction_score REAL,
                total_tasks INTEGER DEFAULT 0,
                date DATE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(machine_id, model_name, task_type, date)
            );
            INSERT INTO model_performance_new
                (id, model_name, task_type, average_response_time_ms, success_rate,
                 average_token_efficiency, cost_per_task, user_satisfaction_score,
                 total_tasks, date, created_at)
            SELECT id, model_name, task_type, average_response_time_ms, success_rate,
                   average_token_efficiency, cost_per_task, user_satisfaction_score,
                   total_tasks, date, created_at
            FROM model_performance;
            DROP TABLE model_performance;
            ALTER TABLE model_performance_new RENAME TO model_performance;
        """)
        conn.commit()

    def _seed_default_content(self, conn: sqlite3.Connection):
        """Seed the curated reference-content tables (best practices library,
        learning paths, community patterns) with a real starter set on first
        run. These are editorial/reference catalogs, not derived telemetry —
        there's no client-reported "usage" data that could ever populate
        them, so without a seed they'd stay permanently empty. Only seeds
        when the table is completely empty, so it never overwrites/duplicates
        real content added later via the API.
        """
        if conn.execute("SELECT COUNT(*) FROM best_practices").fetchone()[0] == 0:
            conn.executemany(
                """INSERT INTO best_practices
                   (category, title, description, example_prompt, example_code, tags, difficulty_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("prompt_engineering", "Give Claude the exact file paths and error text",
                     "Paste the full error message/stack trace and the file path(s) involved instead of "
                     "describing the bug from memory. Claude can jump straight to the right code instead "
                     "of spending turns exploring the repo to find it.",
                     "Fix this: TypeError: 'NoneType' object is not subscriptable in src/api/main.py line 214",
                     None, '["prompting", "debugging"]', "beginner"),
                    ("prompt_engineering", "State the acceptance criteria up front",
                     "Describe what \"done\" looks like (expected behavior, edge cases, tests that must "
                     "pass) before asking for the implementation. This avoids extra back-and-forth turns "
                     "clarifying scope after the first draft.",
                     "Add input validation to the /signup endpoint: reject empty emails, emails without "
                     "'@', and passwords under 8 chars. Return 400 with a clear error message for each case.",
                     None, '["prompting", "planning"]', "beginner"),
                    ("workflow", "Let Claude read before it writes",
                     "For non-trivial changes, ask Claude to explore the relevant files first (or let it "
                     "use its own search tools) rather than immediately requesting a diff. This produces "
                     "changes that match existing conventions instead of introducing a new style.",
                     "Look at how errors are handled elsewhere in this file, then add the same pattern here.",
                     None, '["workflow", "code-quality"]', "intermediate"),
                    ("debugging", "Ask for the smallest reproducible change",
                     "When investigating a bug, ask Claude to isolate the minimal failing case first "
                     "(one test, one function) instead of a broad refactor. It's faster to verify a fix is "
                     "correct, and easier to review.",
                     "Write a minimal failing test that reproduces this bug before fixing it.",
                     None, '["debugging", "testing"]', "intermediate"),
                    ("code_quality", "Request tests alongside new code",
                     "Explicitly ask for tests covering the new behavior (including edge cases) in the "
                     "same request as the implementation, rather than as a separate follow-up — it keeps "
                     "test coverage from silently lagging behind feature work.",
                     "Implement the rate limiter and add unit tests covering the burst limit, the reset "
                     "window, and concurrent requests.",
                     None, '["testing", "code-quality"]', "intermediate"),
                    ("optimization", "Batch independent questions/edits into one turn",
                     "If you have several unrelated things for Claude to look at or change, ask for all of "
                     "them in a single message instead of one-by-one. It reduces round trips and token "
                     "overhead from re-establishing context on every turn.",
                     "1) Rename `getUser` to `fetchUser` everywhere. 2) Add a docstring to `parse_config`. "
                     "3) Check whether `utils/date.py` is still used anywhere.",
                     None, '["optimization", "workflow"]', "advanced"),
                ]
            )
            conn.commit()

        if conn.execute("SELECT COUNT(*) FROM learning_paths").fetchone()[0] == 0:
            conn.executemany(
                """INSERT INTO learning_paths
                   (name, description, difficulty_level, estimated_duration_hours, steps,
                    prerequisites, completion_reward_points)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("Effective Prompting Fundamentals",
                     "Learn to write prompts that get correct results in fewer turns: providing context, "
                     "stating acceptance criteria, and giving Claude exact file/error references.",
                     "beginner", 2,
                     '[{"step": 1, "title": "Include exact error text and file paths"}, '
                     '{"step": 2, "title": "State acceptance criteria before implementation"}, '
                     '{"step": 3, "title": "Ask for tests alongside new code"}]',
                     '[]', 100),
                    ("Debugging with Claude Code",
                     "A workflow for isolating, reproducing, and fixing bugs efficiently using Claude Code.",
                     "intermediate", 3,
                     '[{"step": 1, "title": "Reproduce with a minimal failing test"}, '
                     '{"step": 2, "title": "Ask Claude to explain the root cause before fixing"}, '
                     '{"step": 3, "title": "Verify the fix against the original failing test"}]',
                     '["Effective Prompting Fundamentals"]', 150),
                    ("Token & Cost Optimization",
                     "Techniques for reducing token usage and cost: batching requests, leveraging prompt "
                     "caching, and avoiding redundant context.",
                     "advanced", 2,
                     '[{"step": 1, "title": "Batch independent questions into one turn"}, '
                     '{"step": 2, "title": "Reuse system/context prompts to benefit from prompt caching"}, '
                     '{"step": 3, "title": "Review token usage stats to spot waste patterns"}]',
                     '["Effective Prompting Fundamentals"]', 200),
                ]
            )
            conn.commit()

        if conn.execute("SELECT COUNT(*) FROM community_patterns").fetchone()[0] == 0:
            conn.executemany(
                """INSERT INTO community_patterns
                   (pattern_type, title, description, content, language, use_case, tags, is_verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("prompt_template", "Bug fix with reproduction",
                     "A template that front-loads the error, file, and expected behavior so Claude can "
                     "act immediately instead of asking clarifying questions.",
                     "Error: {paste exact error/stack trace}\nFile: {file path}\nExpected: {what should "
                     "happen instead}\nReproduce: {minimal steps or a failing test}",
                     None, "bug_fixing", '["prompting", "debugging"]', 1),
                    ("prompt_template", "Scoped refactor request",
                     "A template for requesting a refactor without unintended scope creep.",
                     "Refactor {function/module} to {goal, e.g. 'extract validation logic'}. Keep the "
                     "public API unchanged. Do not modify unrelated files.",
                     None, "refactoring", '["prompting", "refactoring"]', 1),
                    ("workflow", "Read-then-write for unfamiliar codebases",
                     "Ask Claude to summarize the relevant module's conventions before requesting a "
                     "change, so the generated code matches existing style and patterns.",
                     "Before making changes, look at 2-3 similar files in this codebase and follow the "
                     "same patterns for error handling, naming, and structure.",
                     None, "onboarding", '["workflow", "code-quality"]', 1),
                ]
            )
            conn.commit()

        if conn.execute("SELECT COUNT(*) FROM achievements").fetchone()[0] == 0:
            conn.executemany(
                """INSERT INTO achievements
                   (name, description, badge_icon, badge_color, criteria, points, rarity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("First Steps", "Complete your first Claude Code session.",
                     "🚀", "#6366f1", '{"type": "session_count", "min": 1}', 10, "common"),
                    ("Getting Warmed Up", "Complete 10 sessions.",
                     "🔥", "#f59e0b", '{"type": "session_count", "min": 10}', 20, "common"),
                    ("Power User", "Complete 25 sessions.",
                     "⚡", "#8b5cf6", '{"type": "session_count", "min": 25}', 50, "rare"),
                    ("Prompt Engineer", "Reach an average prompt quality score of 70+.",
                     "🎯", "#10b981", '{"type": "prompt_quality", "min": 70}', 60, "rare"),
                    ("Cache Master", "Reach an average cache efficiency score of 80+.",
                     "💾", "#06b6d4", '{"type": "cache_efficiency", "min": 80}', 75, "epic"),
                    ("Code Craftsman", "Modify 50+ distinct files with Claude's help.",
                     "🛠️", "#ec4899", '{"type": "files_modified", "min": 50}', 60, "rare"),
                    ("Project Explorer", "Work across 10+ different projects.",
                     "🗺️", "#f43f5e", '{"type": "projects", "min": 10}', 80, "epic"),
                    ("Token Titan", "Use 1,000,000+ tokens in total.",
                     "👑", "#eab308", '{"type": "tokens_used", "min": 1000000}', 100, "legendary"),
                ]
            )
            conn.commit()

        # Higher-tier achievements, added after the original 8 above went
        # live. Each original achievement criterion has exactly one
        # threshold, so an engaged user earns all the ones they qualify for
        # almost immediately and then their leaderboard points permanently
        # plateau no matter how much more they use Claude Code afterwards
        # (recompute_user_achievements only ever inserts rows for criteria
        # that exist in this table -- it has nothing further to award).
        # These extra tiers give sustained activity somewhere to keep
        # earning points. Unlike the block above (which only seeds a
        # brand-new, empty table), this uses INSERT OR IGNORE keyed on the
        # UNIQUE `name` column so it safely adds the new tiers to
        # already-running/populated databases too, without touching any
        # already-earned achievement or its original id.
        conn.executemany(
            """INSERT OR IGNORE INTO achievements
               (name, description, badge_icon, badge_color, criteria, points, rarity)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("Marathon Coder", "Complete 50 sessions.",
                 "🏃", "#0ea5e9", '{"type": "session_count", "min": 50}', 90, "epic"),
                ("Centurion", "Complete 100 sessions.",
                 "💯", "#7c3aed", '{"type": "session_count", "min": 100}', 140, "epic"),
                ("Claude Devotee", "Complete 250 sessions.",
                 "🏆", "#eab308", '{"type": "session_count", "min": 250}', 220, "legendary"),
                ("Prompt Virtuoso", "Reach an average prompt quality score of 85+.",
                 "🎨", "#10b981", '{"type": "prompt_quality", "min": 85}', 90, "epic"),
                ("Prompt Perfectionist", "Reach an average prompt quality score of 95+.",
                 "💎", "#059669", '{"type": "prompt_quality", "min": 95}', 130, "legendary"),
                ("Cache Wizard", "Reach an average cache efficiency score of 90+.",
                 "🧙", "#0891b2", '{"type": "cache_efficiency", "min": 90}', 100, "epic"),
                ("Cache Sage", "Reach an average cache efficiency score of 97+.",
                 "🔮", "#155e75", '{"type": "cache_efficiency", "min": 97}', 140, "legendary"),
                ("Refactor Master", "Modify 100+ distinct files with Claude's help.",
                 "🔧", "#db2777", '{"type": "files_modified", "min": 100}', 110, "epic"),
                ("Codebase Architect", "Modify 250+ distinct files with Claude's help.",
                 "🏗️", "#9d174d", '{"type": "files_modified", "min": 250}', 180, "legendary"),
                ("Serial Explorer", "Work across 20+ different projects.",
                 "🧭", "#e11d48", '{"type": "projects", "min": 20}', 120, "epic"),
                ("Polyglot Pioneer", "Work across 40+ different projects.",
                 "🌐", "#be123c", '{"type": "projects", "min": 40}', 180, "legendary"),
                ("Token Tycoon", "Use 2,500,000+ tokens in total.",
                 "💰", "#ca8a04", '{"type": "tokens_used", "min": 2500000}', 150, "legendary"),
                ("Token Emperor", "Use 5,000,000+ tokens in total.",
                 "🌟", "#a16207", '{"type": "tokens_used", "min": 5000000}', 220, "legendary"),
                ("Token Legend", "Use 10,000,000+ tokens in total.",
                 "☄️", "#854d0e", '{"type": "tokens_used", "min": 10000000}', 300, "legendary"),
            ]
        )
        conn.commit()

    def _seed_default_tags(self, conn: sqlite3.Connection):
        """Seed the default session-tag taxonomy (bugfix/feature/refactor/etc)
        on first run. is_system=1 marks these as not user-deletable from the
        UI; admins/users can still add their own custom tags alongside them."""
        if conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] > 0:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO tags (name, color, is_system) VALUES (?, ?, 1)",
            [
                ("bugfix", "#ef4444"),
                ("feature", "#10b981"),
                ("refactor", "#6366f1"),
                ("chore", "#6b7280"),
                ("docs", "#0ea5e9"),
                ("test", "#8b5cf6"),
                ("performance", "#f59e0b"),
                ("security", "#dc2626"),
                ("exploration", "#ec4899"),
            ],
        )
        conn.commit()

    def get_connection(self):
        """Get async database connection."""
        return aiosqlite.connect(self.db_path)

    # ========================================================================
    # Session tagging (bugfix/feature/refactor/...)
    # ========================================================================

    # Keyword -> tag name heuristic used by suggest_tags_for_session(). Order
    # matters only in that every match is returned (a session can plausibly
    # be both 'bugfix' and 'test'); checked against the session's first user
    # message and its working_directory.
    _AUTO_TAG_KEYWORDS = {
        "bugfix": ("fix", "bug", "crash", "regression", "broken", "error", "issue"),
        "feature": ("add", "implement", "new feature", "feature:", "create a", "build a"),
        "refactor": ("refactor", "clean up", "cleanup", "restructure", "reorganize", "simplify"),
        "docs": ("document", "readme", "docstring", "comment"),
        "test": ("test", "unit test", "pytest", "coverage"),
        "performance": ("performance", "optimi", "speed up", "slow", "latency"),
        "security": ("security", "vulnerab", "cve", "exploit", "auth bypass", "secret"),
        "chore": ("chore", "bump version", "dependency", "upgrade", "housekeeping"),
    }

    async def get_all_tags(self) -> List[Dict[str, Any]]:
        """All tags with how many sessions currently carry each one."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT t.id, t.name, t.color, t.is_system,
                          COUNT(st.session_id) as session_count
                   FROM tags t
                   LEFT JOIN session_tags st ON st.tag_id = t.id
                   GROUP BY t.id ORDER BY t.is_system DESC, t.name ASC"""
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def create_tag(self, name: str, color: str = None) -> int:
        """Create a custom tag. Raises sqlite3.IntegrityError if it exists."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "INSERT INTO tags (name, color, is_system) VALUES (?, ?, 0)",
                (name.strip().lower(), color or "#6366f1"),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_tags_for_sessions(self, session_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Batch-fetch tags for a list of session_ids -> {session_id: [tag, ...]}."""
        if not session_ids:
            return {}
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" * len(session_ids))
            async with db.execute(
                f"""SELECT st.session_id, t.id, t.name, t.color, st.source
                    FROM session_tags st JOIN tags t ON t.id = st.tag_id
                    WHERE st.session_id IN ({placeholders})""",
                session_ids,
            ) as cursor:
                rows = await cursor.fetchall()
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["session_id"], []).append(
                {"id": row["id"], "name": row["name"], "color": row["color"], "source": row["source"]}
            )
        return result

    async def add_session_tag(self, session_id: str, tag_name: str, source: str = "manual",
                               tagged_by: str = None, confidence: float = None) -> Dict[str, Any]:
        """Tag a session, creating the tag if it doesn't exist yet."""
        tag_name = tag_name.strip().lower()
        if not tag_name:
            raise ValueError("tag name is required")
        async with self.get_connection() as db:
            await db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, name, color FROM tags WHERE name = ?", (tag_name,)) as cursor:
                tag = dict(await cursor.fetchone())
            await db.execute(
                """INSERT OR IGNORE INTO session_tags (session_id, tag_id, source, confidence, tagged_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, tag["id"], source, confidence, tagged_by),
            )
            await db.commit()
        return tag

    async def remove_session_tag(self, session_id: str, tag_id: int) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM session_tags WHERE session_id = ? AND tag_id = ?", (session_id, tag_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def suggest_tags_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Keyword-heuristic auto-tag suggestions for a session, based on its
        first few user messages and working_directory. Purely local string
        matching -- no model call -- so suggestions are a starting point the
        user confirms/edits rather than an authoritative classification."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT content FROM messages
                   WHERE session_id = ? AND role = 'user'
                   ORDER BY timestamp ASC LIMIT 5""",
                (session_id,),
            ) as cursor:
                messages = [r["content"] or "" for r in await cursor.fetchall()]
        text = " ".join(messages).lower()
        suggestions = []
        for tag_name, keywords in self._AUTO_TAG_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits:
                confidence = round(min(50 + hits * 15, 95), 1)
                suggestions.append({"name": tag_name, "confidence": confidence})
        suggestions.sort(key=lambda s: -s["confidence"])
        return suggestions

    # ========================================================================
    # Anomaly detection (token/cost spikes per user or machine)
    # ========================================================================

    # z-score thresholds for flagging a day's usage as an anomaly, and for
    # grading its severity once flagged.
    _ANOMALY_Z_THRESHOLD = 2.5
    _ANOMALY_Z_HIGH = 4.0
    _ANOMALY_Z_MEDIUM = 3.0
    _ANOMALY_MIN_BASELINE_DAYS = 5
    _ANOMALY_MIN_ABSOLUTE = {"daily_cost": 1.0, "daily_tokens": 10000, "daily_sessions": 3}

    @staticmethod
    def _z_scores_for_series(day_values: List[tuple]) -> List[Dict[str, Any]]:
        """Given [(date, value), ...] sorted ascending, compute a trailing
        baseline (mean/stddev of up to the preceding 14 days, excluding the
        day itself) for every day that has enough history, and return a
        z-score record for each. Only upward spikes matter here (a quiet day
        isn't a cost/usage risk), so z is left signed but callers only act on
        positive outliers."""
        results = []
        for i, (day, value) in enumerate(day_values):
            history = [v for _, v in day_values[max(0, i - 14):i]]
            if len(history) < Database._ANOMALY_MIN_BASELINE_DAYS:
                continue
            mean = statistics.mean(history)
            stdev = statistics.pstdev(history)
            # Floor stdev so a near-flat history doesn't explode z from
            # tiny absolute noise (e.g. $0.01 -> $0.05 isn't a "spike").
            effective_stdev = max(stdev, mean * 0.25, 0.01)
            z = (value - mean) / effective_stdev
            results.append({
                "date": day, "value": value, "mean": mean, "stdev": stdev, "z": z,
            })
        return results

    async def detect_anomalies(self, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Detect and persist cost/token/session-count spikes per machine and
        per user over the trailing `days` window (computed against up to 14
        days of prior history as the baseline). Upserts into `anomalies` so
        acknowledge/dismiss state survives across requests, and returns the
        full set of anomalies detected for the window (any status)."""
        lookback_days = days + 14  # extra history to seed the baseline
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            extra, mp = self._machine_scope_clause(machine_id, "tu.machine_id")

            async with db.execute(
                f"""SELECT tu.machine_id, date(tu.timestamp) as day,
                           ROUND(COALESCE(SUM(tu.estimated_cost_usd), 0), 4) as cost,
                           COALESCE(SUM(tu.input_tokens + tu.output_tokens), 0) as tokens,
                           COUNT(DISTINCT tu.session_id) as sessions
                    FROM token_usage tu
                    WHERE tu.timestamp >= date('now', ? || ' days'){extra}
                    GROUP BY tu.machine_id, day ORDER BY tu.machine_id, day""",
                [f"-{lookback_days}"] + mp,
            ) as cursor:
                machine_days = await cursor.fetchall()

            extra_u, mp_u = self._machine_scope_clause(machine_id, "m.machine_id")
            async with db.execute(
                f"""SELECT COALESCE(m.logged_in_user, tu.machine_id) as user_key,
                           date(tu.timestamp) as day,
                           ROUND(COALESCE(SUM(tu.estimated_cost_usd), 0), 4) as cost,
                           COALESCE(SUM(tu.input_tokens + tu.output_tokens), 0) as tokens,
                           COUNT(DISTINCT tu.session_id) as sessions
                    FROM token_usage tu
                    LEFT JOIN machines m ON m.machine_id = tu.machine_id
                    WHERE tu.timestamp >= date('now', ? || ' days'){extra_u}
                    GROUP BY user_key, day ORDER BY user_key, day""",
                [f"-{lookback_days}"] + mp_u,
            ) as cursor:
                user_days = await cursor.fetchall()

            display_names = {}
            async with db.execute("SELECT machine_id, machine_name, logged_in_user FROM machines") as cursor:
                for row in await cursor.fetchall():
                    display_names[row["machine_id"]] = self._display_name(row["logged_in_user"], row["machine_name"], row["machine_id"])

            detected = []
            for scope_type, rows, key_col in (("machine", machine_days, "machine_id"), ("user", user_days, "user_key")):
                grouped: Dict[str, List[tuple]] = {}
                for row in rows:
                    grouped.setdefault(row[key_col], []).append(row)
                for scope_id, scope_rows in grouped.items():
                    for metric, field, min_abs in (
                        ("daily_cost", "cost", self._ANOMALY_MIN_ABSOLUTE["daily_cost"]),
                        ("daily_tokens", "tokens", self._ANOMALY_MIN_ABSOLUTE["daily_tokens"]),
                        ("daily_sessions", "sessions", self._ANOMALY_MIN_ABSOLUTE["daily_sessions"]),
                    ):
                        series = [(r["day"], r[field] or 0) for r in scope_rows]
                        # Only score days within the requested window, not the
                        # extra history pulled purely to seed the baseline.
                        cutoff = series[-days:] if len(series) > days else series
                        cutoff_dates = {d for d, _ in cutoff}
                        for z_rec in self._z_scores_for_series(series):
                            if z_rec["date"] not in cutoff_dates:
                                continue
                            if z_rec["z"] < self._ANOMALY_Z_THRESHOLD or z_rec["value"] < min_abs:
                                continue
                            severity = (
                                "high" if z_rec["z"] >= self._ANOMALY_Z_HIGH
                                else "medium" if z_rec["z"] >= self._ANOMALY_Z_MEDIUM
                                else "low"
                            )
                            label = display_names.get(scope_id, scope_id) if scope_type == "machine" else (scope_id or "Unknown")
                            detected.append({
                                "scope_type": scope_type, "scope_id": scope_id, "scope_label": label,
                                "metric": metric, "metric_date": z_rec["date"],
                                "observed_value": z_rec["value"], "baseline_mean": round(z_rec["mean"], 4),
                                "baseline_stddev": round(z_rec["stdev"], 4), "deviation_score": round(z_rec["z"], 2),
                                "severity": severity,
                            })

            now = datetime.utcnow().isoformat()
            for a in detected:
                await db.execute(
                    """INSERT INTO anomalies
                       (detected_at, scope_type, scope_id, scope_label, metric, metric_date,
                        observed_value, baseline_mean, baseline_stddev, deviation_score, severity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(scope_type, scope_id, metric, metric_date) DO UPDATE SET
                         detected_at=excluded.detected_at, scope_label=excluded.scope_label,
                         observed_value=excluded.observed_value, baseline_mean=excluded.baseline_mean,
                         baseline_stddev=excluded.baseline_stddev, deviation_score=excluded.deviation_score,
                         severity=excluded.severity""",
                    (now, a["scope_type"], a["scope_id"], a["scope_label"], a["metric"], a["metric_date"],
                     a["observed_value"], a["baseline_mean"], a["baseline_stddev"], a["deviation_score"], a["severity"]),
                )
            await db.commit()

            # Return the persisted rows (with status/ack info) for exactly the
            # scope/date keys we just (re)computed, newest + most severe first.
            if not detected:
                return []
            keys = {(a["scope_type"], a["scope_id"], a["metric"], a["metric_date"]) for a in detected}
            placeholders = " OR ".join(["(scope_type=? AND scope_id=? AND metric=? AND metric_date=?)"] * len(keys))
            params: List[Any] = []
            for k in keys:
                params.extend(k)
            async with db.execute(
                f"""SELECT * FROM anomalies WHERE {placeholders}
                    ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, metric_date DESC""",
                params,
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_anomalies(self, status: str = None, machine_id: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch previously-detected anomalies without recomputing them."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            clauses, params = [], []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if machine_id and machine_id != "all":
                ids = [m.strip() for m in machine_id.split(",") if m.strip()]
                if ids:
                    clauses.append(
                        f"(scope_id IN ({','.join('?' * len(ids))}))"
                    )
                    params.extend(ids)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            async with db.execute(
                f"""SELECT * FROM anomalies {where}
                    ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                             metric_date DESC LIMIT ?""",
                params,
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def set_anomaly_status(self, anomaly_id: int, status: str, by: str = None) -> bool:
        async with self.get_connection() as db:
            if status == "acknowledged":
                cursor = await db.execute(
                    "UPDATE anomalies SET status=?, acknowledged_by=?, acknowledged_at=? WHERE id=?",
                    (status, by, datetime.utcnow().isoformat(), anomaly_id),
                )
            else:
                cursor = await db.execute("UPDATE anomalies SET status=? WHERE id=?", (status, anomaly_id))
            await db.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Natural-language query execution (deterministic, local-only — see
    # src/utils/nlq.py for the question -> structured-query parser)
    # ------------------------------------------------------------------
    async def nlq_known_teams(self) -> list:
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT name FROM groups ORDER BY name") as cursor:
                return [r["name"] for r in await cursor.fetchall()]

    async def nlq_known_users(self) -> list:
        """Distinct, normalized usernames (lowercased email/username local
        part -- see _identity_local_part) that can be recognized in a
        question. machines.logged_in_user is stored in whatever format the
        client agent captured (e.g. 'Jane Doe <jane.doe@example.com>',
        a bare email, or just 'jdoe'), none of which a user would
        ever literally type out in full -- so the raw value is useless for
        keyword matching and must be normalized first."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT logged_in_user FROM machines WHERE logged_in_user IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()
        identities = {self._identity_local_part(r["logged_in_user"]) for r in rows}
        identities.discard(None)
        return sorted(identities)

    async def _nlq_scope_machine_ids(self, scope_type: str, scope_value: str) -> Optional[list]:
        """Resolve a team name or username to a list of machine_ids. Returns
        None for 'all' scope (no filtering needed)."""
        if scope_type == "all" or not scope_value:
            return None
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            if scope_type == "team":
                async with db.execute(
                    """SELECT m.machine_id FROM machines m JOIN groups g ON m.group_id = g.id
                       WHERE lower(g.name) = lower(?)""",
                    (scope_value,),
                ) as cursor:
                    return [r["machine_id"] for r in await cursor.fetchall()]
            else:  # 'user' or 'me' (scope_value is a normalized local-part username)
                # logged_in_user may be 'Name <email>', a bare email, or a
                # bare username, so this can't be a SQL string-equality
                # match against scope_value -- normalize every row in
                # Python with the same helper used to produce scope_value
                # in the first place (nlq_known_users / the 'me' resolver).
                async with db.execute(
                    "SELECT machine_id, logged_in_user FROM machines WHERE logged_in_user IS NOT NULL"
                ) as cursor:
                    rows = await cursor.fetchall()
                target = scope_value.lower()
                return [r["machine_id"] for r in rows if self._identity_local_part(r["logged_in_user"]) == target]

    async def run_nl_query(self, parsed, machine_id: str = None) -> dict:
        """Execute a parsed natural-language query and return a result dict
        with the headline value plus a short per-day/per-scope breakdown."""
        start_iso, end_iso = parsed.start.isoformat(), parsed.end.isoformat()
        machine_ids = await self._nlq_scope_machine_ids(parsed.scope_type, parsed.scope_value)
        if machine_ids is not None and not machine_ids:
            return {"value": 0, "unit": parsed.metric, "breakdown": [], "no_scope_match": True}

        # Combine the resolved scope's machine_ids with any caller-level
        # machine_id restriction (non-admin users browsing their own data).
        effective_ids = machine_ids
        if machine_id:
            caller_ids = set(machine_id.split(","))
            effective_ids = [m for m in machine_ids if m in caller_ids] if machine_ids is not None else list(caller_ids)

        def ids_clause(column):
            if effective_ids is None:
                return "", []
            if not effective_ids:
                return " AND 1=0", []
            return f" AND {column} IN ({','.join('?' * len(effective_ids))})", list(effective_ids)

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row

            if getattr(parsed, "rank", False) and parsed.metric in ("cost", "tokens", "sessions", "hours"):
                return await self._nlq_leaderboard(db, parsed.metric, start_iso, end_iso, ids_clause)

            if parsed.metric == "cost":
                clause, params = ids_clause("tu.machine_id")
                q = f"""SELECT COALESCE(SUM(tu.estimated_cost_usd), 0) as v,
                               COUNT(DISTINCT date(tu.timestamp)) as days
                        FROM token_usage tu WHERE tu.timestamp >= ? AND tu.timestamp < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": round(row["v"], 2), "unit": "cost", "active_days": row["days"]}

            if parsed.metric == "tokens":
                clause, params = ids_clause("tu.machine_id")
                q = f"""SELECT COALESCE(SUM(tu.total_tokens), 0) as v
                        FROM token_usage tu WHERE tu.timestamp >= ? AND tu.timestamp < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": row["v"], "unit": "tokens"}

            if parsed.metric == "sessions":
                clause, params = ids_clause("s.machine_id")
                q = f"""SELECT COUNT(*) as v FROM sessions s
                        WHERE s.start_time >= ? AND s.start_time < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": row["v"], "unit": "sessions"}

            if parsed.metric == "hours":
                clause, params = ids_clause("s.machine_id")
                q = f"""SELECT COALESCE(SUM(s.duration_seconds), 0) / 3600.0 as v FROM sessions s
                        WHERE s.start_time >= ? AND s.start_time < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": round(row["v"], 1), "unit": "hours"}

            if parsed.metric == "commits":
                clause, params = ids_clause("s.machine_id")
                q = f"""SELECT COUNT(DISTINCT pc.id) as v
                        FROM ado_pr_commits pc
                        JOIN session_pr_links spl ON spl.pr_row_id = pc.pr_row_id
                        JOIN sessions s ON s.session_id = spl.session_id
                        WHERE pc.commit_date >= ? AND pc.commit_date < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": row["v"], "unit": "commits", "note": "Requires Azure DevOps integration to be configured (see Settings)."}

            if parsed.metric == "pull_requests":
                clause, params = ids_clause("s.machine_id")
                q = f"""SELECT COUNT(DISTINCT pr.id) as v
                        FROM ado_pull_requests pr
                        JOIN session_pr_links spl ON spl.pr_row_id = pr.id
                        JOIN sessions s ON s.session_id = spl.session_id
                        WHERE pr.created_date >= ? AND pr.created_date < ?{clause}"""
                async with db.execute(q, [start_iso, end_iso] + params) as cur:
                    row = await cur.fetchone()
                return {"value": row["v"], "unit": "pull_requests", "note": "Requires Azure DevOps integration to be configured (see Settings)."}

            return {"value": None, "unit": None}

    async def _nlq_leaderboard(self, db, metric: str, start_iso: str, end_iso: str,
                                ids_clause, limit: int = 5) -> dict:
        """Top-N per-person breakdown for ranking questions like "who spent
        the most" or "who's the top in the leaderboard", grouped by the
        machine's logged-in user (normalized to its local-part username --
        see _identity_local_part -- so the same person isn't split across
        multiple rows just because their logged_in_user was captured in a
        different raw format on different machines)."""
        if metric == "cost":
            clause, params = ids_clause("tu.machine_id")
            q = f"""SELECT m.logged_in_user as who, COALESCE(SUM(tu.estimated_cost_usd), 0) as v
                    FROM token_usage tu JOIN machines m ON m.machine_id = tu.machine_id
                    WHERE tu.timestamp >= ? AND tu.timestamp < ?{clause}
                    GROUP BY m.logged_in_user"""
        elif metric == "tokens":
            clause, params = ids_clause("tu.machine_id")
            q = f"""SELECT m.logged_in_user as who, COALESCE(SUM(tu.total_tokens), 0) as v
                    FROM token_usage tu JOIN machines m ON m.machine_id = tu.machine_id
                    WHERE tu.timestamp >= ? AND tu.timestamp < ?{clause}
                    GROUP BY m.logged_in_user"""
        elif metric == "hours":
            clause, params = ids_clause("s.machine_id")
            q = f"""SELECT m.logged_in_user as who, COALESCE(SUM(s.duration_seconds), 0) / 3600.0 as v
                    FROM sessions s JOIN machines m ON m.machine_id = s.machine_id
                    WHERE s.start_time >= ? AND s.start_time < ?{clause}
                    GROUP BY m.logged_in_user"""
        else:  # sessions
            clause, params = ids_clause("s.machine_id")
            q = f"""SELECT m.logged_in_user as who, COUNT(*) as v
                    FROM sessions s JOIN machines m ON m.machine_id = s.machine_id
                    WHERE s.start_time >= ? AND s.start_time < ?{clause}
                    GROUP BY m.logged_in_user"""

        async with db.execute(q, [start_iso, end_iso] + params) as cur:
            rows = await cur.fetchall()

        merged: Dict[str, float] = {}
        for r in rows:
            label = self._identity_local_part(r["who"]) or (r["who"] or "unknown")
            merged[label] = merged.get(label, 0) + (r["v"] or 0)

        ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        round_fn = (lambda v: round(v, 2)) if metric == "cost" else ((lambda v: round(v, 1)) if metric == "hours" else (lambda v: round(v)))
        breakdown = [{"label": label, "value": round_fn(v)} for label, v in ranked]
        return {
            "value": breakdown[0]["value"] if breakdown else 0,
            "unit": metric,
            "breakdown": breakdown,
            "is_ranking": True,
        }

    async def log_nl_query(self, question: str, parsed, asked_by: str = None):
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO nl_query_log
                   (asked_by, question, resolved_metric, resolved_scope_type,
                    resolved_scope_value, time_range_label, matched)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (asked_by, question, parsed.metric, parsed.scope_type,
                 parsed.scope_value, parsed.time_label, 1 if parsed.matched else 0),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Azure DevOps integration (session <-> PR/commit linking)
    # ------------------------------------------------------------------
    async def upsert_ado_pull_request(self, project: str, repository: str, pr_data: dict) -> int:
        """Insert/update a PR row from the Azure DevOps REST API shape.
        Returns the local row id (ado_pull_requests.id).

        closed_by/reviewers_json are populated from fields already present
        in this same pr_data payload (closedBy, reviewers) -- no extra API
        calls needed during the bulk sync."""
        from src.integrations.azure_devops import vote_label

        closed_by = (pr_data.get("closedBy") or {}).get("displayName")
        reviewers = []
        for r in (pr_data.get("reviewers") or []):
            vote = r.get("vote", 0)
            reviewers.append({
                "display_name": r.get("displayName"),
                "vote": vote,
                "vote_label": vote_label(vote),
                "is_required": r.get("isRequired", False),
            })
        reviewers_json = json.dumps(reviewers) if reviewers else None

        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO ado_pull_requests
                   (ado_pr_id, ado_project, ado_repository, title, author, status,
                    created_date, closed_date, source_branch, target_branch, url,
                    closed_by, reviewers_json, last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ado_project, ado_repository, ado_pr_id) DO UPDATE SET
                       title=excluded.title, author=excluded.author, status=excluded.status,
                       closed_date=excluded.closed_date, url=excluded.url,
                       closed_by=excluded.closed_by, reviewers_json=excluded.reviewers_json,
                       last_synced_at=excluded.last_synced_at""",
                (
                    pr_data["pullRequestId"], project, repository,
                    pr_data.get("title"),
                    (pr_data.get("createdBy") or {}).get("displayName"),
                    pr_data.get("status"),
                    pr_data.get("creationDate"),
                    pr_data.get("closedDate"),
                    (pr_data.get("sourceRefName") or "").replace("refs/heads/", ""),
                    (pr_data.get("targetRefName") or "").replace("refs/heads/", ""),
                    pr_data.get("url"),
                    closed_by,
                    reviewers_json,
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id FROM ado_pull_requests WHERE ado_project=? AND ado_repository=? AND ado_pr_id=?",
                (project, repository, pr_data["pullRequestId"]),
            ) as cursor:
                row = await cursor.fetchone()
                return row["id"] if row else None

    async def upsert_ado_pr_commit(self, pr_row_id: int, commit_data: dict):
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(pr_row_id, commit_sha) DO NOTHING""",
                (
                    pr_row_id, commit_data.get("commitId"),
                    (commit_data.get("author") or {}).get("email"),
                    (commit_data.get("author") or {}).get("date"),
                    commit_data.get("comment"),
                ),
            )
            await db.commit()

    @staticmethod
    def _identity_local_part(identity: Optional[str]) -> Optional[str]:
        """Extract the comparable "local part" (before '@') from an identity
        string, for matching a machine's logged-in user against a git commit
        author. Handles three shapes:
          - 'Jane Doe <jane.doe@example.com>' (git-style name+email)
          - 'jane.doe@example-corp.com' (bare email)
          - 'jdoe' (bare username, no email at all)
        The domain is deliberately ignored: a user's SSO/login email domain
        (e.g. example.com) commonly differs from their git commit email
        domain (e.g. example-corp.com) for the same person."""
        if not identity:
            return None
        match = re.search(r'<([^>]+)>', identity)
        email_or_user = match.group(1) if match else identity
        local_part = email_or_user.split('@')[0].strip().lower()
        return local_part or None

    async def link_sessions_to_prs(self, buffer_minutes: int = 120) -> int:
        """Heuristically link sessions to PRs: a commit authored by the
        session's logged-in user landed within `buffer_minutes` of the
        session's active window. Matches across every synced Azure DevOps
        project/repository (sync pulls the whole org), since sessions have
        no reliable working-directory -> repo mapping to scope by. Also
        records which specific commit matched in
        ado_commit_session_matches, so get_ado_pull_requests can compute a
        per-PR "AI %" (ai_commits / total_commits), not just a yes/no flag.
        Returns the number of new session<->PR links created."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT s.session_id, s.start_time,
                          COALESCE(s.end_time, s.start_time) as end_time, m.logged_in_user
                   FROM sessions s LEFT JOIN machines m ON m.machine_id = s.machine_id
                   WHERE m.logged_in_user IS NOT NULL"""
            ) as cursor:
                sessions = await cursor.fetchall()

            created = 0
            for s in sessions:
                username = self._identity_local_part(s["logged_in_user"])
                if not username:
                    continue
                window_start = (
                    datetime.fromisoformat(s["start_time"]) - timedelta(minutes=buffer_minutes)
                ).isoformat()
                window_end = (
                    datetime.fromisoformat(s["end_time"]) + timedelta(minutes=buffer_minutes)
                ).isoformat()
                async with db.execute(
                    """SELECT DISTINCT pr_row_id, commit_sha, author_email
                       FROM ado_pr_commits
                       WHERE commit_date BETWEEN ? AND ?
                         AND author_email IS NOT NULL""",
                    (window_start, window_end),
                ) as cur2:
                    candidates = await cur2.fetchall()
                for c in candidates:
                    if self._identity_local_part(c["author_email"]) != username:
                        continue
                    cursor = await db.execute(
                        """INSERT INTO session_pr_links (session_id, pr_row_id, match_method, match_confidence)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(session_id, pr_row_id) DO NOTHING""",
                        (s["session_id"], c["pr_row_id"], "time_window+author", 90.0),
                    )
                    if cursor.rowcount:
                        created += 1
                    await db.execute(
                        """INSERT INTO ado_commit_session_matches (pr_row_id, commit_sha, session_id)
                           VALUES (?, ?, ?)
                           ON CONFLICT(pr_row_id, commit_sha, session_id) DO NOTHING""",
                        (c["pr_row_id"], c["commit_sha"], s["session_id"]),
                    )
            await db.commit()
            return created

    async def get_login_identities(self, machine_id: str = None) -> List[str]:
        """Resolve the distinct Claude Code login identities (lowercased
        email/username local-part, via _identity_local_part) for the
        machine(s) selected through the app's standard machine_id scoping
        param ('all'/None = every known machine). This is what lets the
        Git Pull Requests page show PR/AI stats for 'the currently
        selected user(s)' instead of the entire Azure DevOps org."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            frag, params = self._machine_scope_clause(machine_id, "machine_id")
            async with db.execute(
                f"""SELECT DISTINCT logged_in_user FROM machines
                    WHERE logged_in_user IS NOT NULL{frag}""",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        identities = {self._identity_local_part(r["logged_in_user"]) for r in rows}
        identities.discard(None)
        return sorted(identities)

    @staticmethod
    def _sql_local_part_expr(column: str) -> str:
        """SQL expression for the lowercased local-part (before '@') of a
        plain email column. ado_pr_commits.author_email is always a bare
        email straight from the Azure DevOps API (never the 'Name <email>'
        format machines.logged_in_user can be), so a simple SQL
        SUBSTR/INSTR is sufficient here -- unlike _identity_local_part,
        which also has to handle that richer format in Python."""
        return (f"LOWER(CASE WHEN INSTR({column}, '@') > 0 "
                f"THEN SUBSTR({column}, 1, INSTR({column}, '@') - 1) ELSE {column} END)")

    async def get_ado_pull_requests(self, status: str = None, limit: int = 100, offset: int = 0,
                                     ai_only: bool = False, machine_id: str = None,
                                     project: str = None, author: str = None,
                                     known_users_only: bool = False) -> List[Dict[str, Any]]:
        identities = None
        if machine_id and machine_id != "all":
            identities = await self.get_login_identities(machine_id)
            if not identities:
                return []
        elif known_users_only:
            # User-facing page: even with no specific user/team selected,
            # never show the entire Azure DevOps org -- only PRs authored by
            # a recognized Claude Code user (any machine, org-wide). The
            # unscoped admin page intentionally does NOT set this, since it
            # is meant to show literally every PR regardless of tool usage.
            identities = await self.get_login_identities(None)
            if not identities:
                return []
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            clauses, params = [], []
            if status and status != "all":
                clauses.append("pr.status = ?")
                params.append(status)
            if project and project != "all":
                clauses.append("(pr.ado_project || '/' || pr.ado_repository) = ?")
                params.append(project)
            if author:
                clauses.append("pr.author LIKE ?")
                params.append(f"%{author}%")
            if identities is not None:
                local_part = self._sql_local_part_expr("pc2.author_email")
                placeholders = ",".join("?" * len(identities))
                clauses.append(
                    f"""EXISTS (SELECT 1 FROM ado_pr_commits pc2
                                WHERE pc2.pr_row_id = pr.id AND pc2.author_email IS NOT NULL
                                AND {local_part} IN ({placeholders}))"""
                )
                params.extend(identities)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            having = "HAVING ai_commits > 0" if ai_only else ""
            params.extend([limit, offset])
            async with db.execute(
                f"""SELECT pr.*,
                           COUNT(DISTINCT spl.session_id) as linked_sessions,
                           COUNT(DISTINCT pc.commit_sha) as total_commits,
                           COUNT(DISTINCT csm.commit_sha) as ai_commits
                    FROM ado_pull_requests pr
                    LEFT JOIN session_pr_links spl ON spl.pr_row_id = pr.id
                    LEFT JOIN ado_pr_commits pc ON pc.pr_row_id = pr.id
                    LEFT JOIN ado_commit_session_matches csm ON csm.pr_row_id = pr.id
                    {where}
                    GROUP BY pr.id
                    {having}
                    ORDER BY pr.created_date DESC LIMIT ? OFFSET ?""",
                params,
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
            for r in rows:
                r["ai_commit_pct"] = (
                    round(r["ai_commits"] / r["total_commits"] * 100, 1) if r["total_commits"] else 0
                )
                r["is_ai_assisted"] = r["ai_commits"] > 0
            return rows

    async def get_ado_pr_detail(self, pr_row_id: int) -> Optional[Dict[str, Any]]:
        """Full commit-level detail for a single PR's 'View Details' / AI
        adoption report: the PR row plus every commit with its author,
        message, and whether it was matched to an active Claude Code
        session (and which one). File-level 'code area' data is fetched
        live from Azure DevOps by the API layer and merged in -- too
        expensive to pre-fetch per-commit for the entire org during sync."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM ado_pull_requests WHERE id = ?", (pr_row_id,)) as cur:
                pr_row = await cur.fetchone()
            if not pr_row:
                return None
            pr = dict(pr_row)
            async with db.execute(
                """SELECT pc.commit_sha, pc.author_email, pc.commit_date, pc.comment,
                          GROUP_CONCAT(DISTINCT csm.session_id) as session_ids
                   FROM ado_pr_commits pc
                   LEFT JOIN ado_commit_session_matches csm
                     ON csm.pr_row_id = pc.pr_row_id AND csm.commit_sha = pc.commit_sha
                   WHERE pc.pr_row_id = ?
                   GROUP BY pc.commit_sha
                   ORDER BY pc.commit_date ASC""",
                (pr_row_id,),
            ) as cur:
                commit_rows = await cur.fetchall()
        commits = []
        for r in commit_rows:
            c = dict(r)
            c["session_ids"] = c["session_ids"].split(",") if c["session_ids"] else []
            c["is_ai"] = bool(c["session_ids"])
            commits.append(c)
        total = len(commits)
        ai_count = sum(1 for c in commits if c["is_ai"])
        pr["commits"] = commits
        pr["total_commits"] = total
        pr["ai_commits"] = ai_count
        pr["ai_commit_pct"] = round(ai_count / total * 100, 1) if total else 0
        pr["is_ai_assisted"] = ai_count > 0

        # Co-authors: distinct commit authors other than the PR's primary
        # author (derived from already-synced commit data, no extra calls).
        primary_author = (pr.get("author") or "").strip().lower()
        co_author_emails = []
        seen = set()
        for c in commits:
            email = (c.get("author_email") or "").strip()
            if not email:
                continue
            key = email.lower()
            local_part = key.split("@")[0]
            if local_part == primary_author or key == primary_author:
                continue
            if key not in seen:
                seen.add(key)
                co_author_emails.append(email)
        pr["co_authors"] = co_author_emails

        pr["reviewers"] = json.loads(pr["reviewers_json"]) if pr.get("reviewers_json") else []

        # Time to complete: closed_date - created_date, in hours (None if PR
        # is still active/not closed, or either timestamp is missing).
        pr["time_to_complete_hours"] = self._hours_between(pr.get("created_date"), pr.get("closed_date"))
        return pr

    @staticmethod
    def _hours_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
        """Parse two ISO-ish timestamp strings and return the difference in
        hours, or None if either is missing/unparseable."""
        if not start or not end:
            return None
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return round((e - s).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            return None

    async def get_ado_projects_repos(self) -> List[str]:
        """Distinct 'Project/Repository' combos across all synced PRs, for
        populating the PR report's project/repo filter dropdown."""
        async with self.get_connection() as db:
            async with db.execute(
                """SELECT DISTINCT ado_project, ado_repository FROM ado_pull_requests
                   ORDER BY ado_project, ado_repository"""
            ) as cursor:
                rows = await cursor.fetchall()
            return [f"{p}/{r}" for p, r in rows]

    async def get_ai_assisted_pr_stats(self, days: int = 90, machine_id: str = None,
                                        known_users_only: bool = False) -> Dict[str, Any]:
        """% of synced PRs with at least one linked Claude Code session, plus
        the cost/sessions attributed to AI-assisted PRs. Pass machine_id to
        scope down to the currently selected user(s) (via their machine(s))
        instead of the entire Azure DevOps org. Pass known_users_only=True
        (the user-facing page's default, unscoped view) to still restrict to
        recognized Claude Code users org-wide rather than literally every
        PR in Azure DevOps."""
        identities = None
        if machine_id and machine_id != "all":
            identities = await self.get_login_identities(machine_id)
            if not identities:
                return {
                    "total_prs": 0, "ai_assisted_prs": 0, "ai_assisted_pct": 0,
                    "sessions_involved": 0, "cost_attributed": 0, "days": days,
                }
        elif known_users_only:
            identities = await self.get_login_identities(None)
            if not identities:
                return {
                    "total_prs": 0, "ai_assisted_prs": 0, "ai_assisted_pct": 0,
                    "sessions_involved": 0, "cost_attributed": 0, "days": days,
                }
        identity_clause, identity_params = "", []
        if identities is not None:
            local_part = self._sql_local_part_expr("pc2.author_email")
            placeholders = ",".join("?" * len(identities))
            identity_clause = (
                f""" AND EXISTS (SELECT 1 FROM ado_pr_commits pc2
                                 WHERE pc2.pr_row_id = pr.id AND pc2.author_email IS NOT NULL
                                 AND {local_part} IN ({placeholders}))"""
            )
            identity_params = list(identities)
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT COUNT(*) as total,
                          COUNT(DISTINCT CASE WHEN spl.id IS NOT NULL THEN pr.id END) as ai_assisted
                   FROM ado_pull_requests pr
                   LEFT JOIN session_pr_links spl ON spl.pr_row_id = pr.id
                   WHERE pr.created_date >= date('now', ? || ' days'){identity_clause}""",
                [f"-{days}"] + identity_params,
            ) as cursor:
                counts = await cursor.fetchone()

            async with db.execute(
                f"""SELECT COUNT(DISTINCT spl.session_id) as sessions,
                          COALESCE(SUM(tu.estimated_cost_usd), 0) as cost
                   FROM session_pr_links spl
                   JOIN ado_pull_requests pr ON pr.id = spl.pr_row_id
                   LEFT JOIN token_usage tu ON tu.session_id = spl.session_id
                   WHERE pr.created_date >= date('now', ? || ' days'){identity_clause}""",
                [f"-{days}"] + identity_params,
            ) as cursor:
                attribution = await cursor.fetchone()

        total = counts["total"] or 0
        ai_assisted = counts["ai_assisted"] or 0
        return {
            "total_prs": total,
            "ai_assisted_prs": ai_assisted,
            "ai_assisted_pct": round(ai_assisted / total * 100, 1) if total else 0,
            "sessions_involved": attribution["sessions"] or 0,
            "cost_attributed": round(attribution["cost"] or 0, 2),
            "days": days,
        }

    # Session operations
    async def create_session(self, session_id: str, start_time: datetime,
                            working_directory: str, command_args: str = None,
                            model_name: str = None, machine_id: str = "local") -> int:
        """Create a new session record."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO sessions
                   (session_id, machine_id, start_time, working_directory, command_args, model_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, machine_id, start_time, working_directory, command_args, model_name)
            )
            await db.commit()
            return cursor.lastrowid

    async def end_session(self, session_id: str, end_time: datetime,
                         exit_code: int = None):
        """Update session with end time and duration."""
        async with self.get_connection() as db:
            await db.execute(
                """UPDATE sessions
                   SET end_time = ?,
                       duration_seconds = CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER),
                       exit_code = ?
                   WHERE session_id = ?""",
                (end_time, end_time, exit_code, session_id)
            )
            await db.commit()

    async def get_active_sessions(self, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get all active sessions (no end_time), optionally filtered by machine
        (a single machine_id, or a comma-separated list when a Team/Group scope
        is active)."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = """SELECT s.*, m.machine_name, m.logged_in_user
                       FROM sessions s
                       LEFT JOIN machines m ON s.machine_id = m.machine_id
                       WHERE s.end_time IS NULL"""
            params = []
            frag, fparams = self._machine_scope_clause(machine_id, "s.machine_id")
            query += frag
            params.extend(fparams)
            query += " ORDER BY s.start_time DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_recent_sessions(self, limit: int = 100, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get recent sessions, optionally filtered by machine (a single
        machine_id, or a comma-separated list when a Team/Group scope is
        active)."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = """SELECT s.*, m.machine_name, m.logged_in_user
                       FROM sessions s
                       LEFT JOIN machines m ON s.machine_id = m.machine_id
                       WHERE 1=1"""
            params = []
            frag, fparams = self._machine_scope_clause(machine_id, "s.machine_id")
            query += frag
            params.extend(fparams)
            query += " ORDER BY s.start_time DESC LIMIT ?"
            params.append(limit)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # Launch operations
    async def log_launch(self, session_id: str, timestamp: datetime,
                        working_directory: str, command_line: str,
                        shell_pid: int = None, machine_id: str = "local") -> int:
        """Log a Claude launch event."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO launches
                   (session_id, machine_id, timestamp, working_directory, command_line, shell_pid)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, machine_id, timestamp, working_directory, command_line, shell_pid)
            )
            await db.commit()
            return cursor.lastrowid

    # Process metrics
    async def log_process_metrics(self, timestamp: datetime, pid: int,
                                  session_id: str = None, cpu_percent: float = None,
                                  memory_rss_mb: float = None, memory_vms_mb: float = None,
                                  memory_percent: float = None, num_threads: int = None,
                                  num_fds: int = None, process_status: str = None,
                                  machine_id: str = "local"):
        """Log process metrics."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO process_metrics
                   (timestamp, pid, session_id, machine_id, cpu_percent, memory_rss_mb, memory_vms_mb,
                    memory_percent, num_threads, num_fds, process_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, pid, session_id, machine_id, cpu_percent, memory_rss_mb, memory_vms_mb,
                 memory_percent, num_threads, num_fds, process_status)
            )
            await db.commit()

    # Network metrics
    async def log_network_metrics(self, timestamp: datetime, pid: int,
                                  session_id: str = None, bytes_sent: int = None,
                                  bytes_recv: int = None, packets_sent: int = None,
                                  packets_recv: int = None, num_connections: int = None,
                                  connection_details: List[Dict] = None,
                                  machine_id: str = "local"):
        """Log network metrics."""
        conn_json = json.dumps(connection_details) if connection_details else None

        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO network_metrics
                   (timestamp, pid, session_id, machine_id, bytes_sent, bytes_recv,
                    packets_sent, packets_recv, num_connections, connection_details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, pid, session_id, machine_id, bytes_sent, bytes_recv,
                 packets_sent, packets_recv, num_connections, conn_json)
            )
            await db.commit()

    # Filesystem events
    async def log_filesystem_event(self, timestamp: datetime, file_path: str,
                                   event_type: str, session_id: str = None,
                                   file_size_bytes: int = None, machine_id: str = "local"):
        """Log filesystem event."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO filesystem_events
                   (timestamp, session_id, machine_id, event_type, file_path, file_size_bytes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (timestamp, session_id, machine_id, event_type, file_path, file_size_bytes)
            )
            await db.commit()

    # Token usage
    async def log_token_usage(self, timestamp: datetime, session_id: str,
                             model_name: str = None, input_tokens: int = None,
                             output_tokens: int = None, total_tokens: int = None,
                             estimated_cost_usd: float = None, machine_id: str = "local"):
        """Log token usage."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO token_usage
                   (timestamp, session_id, machine_id, model_name, input_tokens, output_tokens,
                    total_tokens, estimated_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, session_id, machine_id, model_name, input_tokens, output_tokens,
                 total_tokens, estimated_cost_usd)
            )
            await db.commit()

    # Discovery log
    async def log_discovery(self, timestamp: datetime, source_type: str,
                          source_path: str, records_found: int,
                          successfully_parsed: bool, error_message: str = None):
        """Log discovery event."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO discovery_log
                   (timestamp, source_type, source_path, records_found,
                    successfully_parsed, error_message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (timestamp, source_type, source_path, records_found,
                 successfully_parsed, error_message)
            )
            await db.commit()

    # Daily rollups
    async def get_or_create_daily_rollup(self, date_val: date) -> Dict[str, Any]:
        """Get or create daily rollup for a date."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM daily_rollups WHERE date = ?", (date_val,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)

            # Create new rollup
            await db.execute(
                "INSERT INTO daily_rollups (date) VALUES (?)", (date_val,)
            )
            await db.commit()

            async with db.execute(
                "SELECT * FROM daily_rollups WHERE date = ?", (date_val,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row)

    async def update_daily_rollup(self, date_val: date, **kwargs):
        """Update daily rollup fields."""
        if not kwargs:
            return

        set_clauses = [f"{k} = ?" for k in kwargs.keys()]
        values = list(kwargs.values()) + [date_val]

        async with self.get_connection() as db:
            await db.execute(
                f"UPDATE daily_rollups SET {', '.join(set_clauses)} WHERE date = ?",
                values
            )
            await db.commit()

    # Analytics queries
    async def get_stats_overview(self, machine_id: str = None) -> Dict[str, Any]:
        """Get overview statistics."""
        frag, params = self._machine_scope_clause(machine_id)
        token_frag, token_params = self._machine_scope_subquery_clause(machine_id)
        async with self.get_connection() as db:
            # Total sessions
            async with db.execute(f"SELECT COUNT(*) FROM sessions WHERE 1=1{frag}", params) as cursor:
                total_sessions = (await cursor.fetchone())[0]

            # Active sessions
            async with db.execute(
                f"SELECT COUNT(*) FROM sessions WHERE end_time IS NULL{frag}", params
            ) as cursor:
                active_sessions = (await cursor.fetchone())[0]

            # Total tokens
            async with db.execute(f"SELECT SUM(total_tokens) FROM token_usage WHERE 1=1{token_frag}", token_params) as cursor:
                total_tokens = (await cursor.fetchone())[0] or 0

            # Total launches
            async with db.execute(f"SELECT COUNT(*) FROM launches WHERE 1=1{frag}", params) as cursor:
                total_launches = (await cursor.fetchone())[0]

            # Latest session
            async with db.execute(
                f"SELECT start_time FROM sessions WHERE 1=1{frag} ORDER BY start_time DESC LIMIT 1", params
            ) as cursor:
                row = await cursor.fetchone()
                latest_session = row[0] if row else None

            # Total cost
            async with db.execute(f"SELECT SUM(estimated_cost_usd) FROM token_usage WHERE 1=1{token_frag}", token_params) as cursor:
                total_cost = (await cursor.fetchone())[0] or 0

            return {
                "total_sessions": total_sessions,
                "active_sessions": active_sessions,
                "total_tokens": total_tokens,
                "total_launches": total_launches,
                "total_cost": total_cost,
                "latest_session": latest_session
            }

    async def get_sessions_by_date(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get session counts by date."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT DATE(start_time) as date, COUNT(*) as count
                   FROM sessions
                   WHERE start_time >= datetime('now', '-' || ? || ' days')
                   GROUP BY DATE(start_time)
                   ORDER BY date""", (days,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_tokens_by_date(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get token usage by date."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT DATE(timestamp) as date,
                          SUM(input_tokens) as input_tokens,
                          SUM(output_tokens) as output_tokens,
                          SUM(total_tokens) as total_tokens
                   FROM token_usage
                   WHERE timestamp >= datetime('now', '-' || ? || ' days')
                   GROUP BY DATE(timestamp)
                   ORDER BY date""", (days,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_costs_by_month(self) -> List[Dict[str, Any]]:
        """Get costs grouped by month for entire history."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT
                       strftime('%Y-%m', timestamp) as month,
                       strftime('%Y', timestamp) as year,
                       CASE strftime('%m', timestamp)
                           WHEN '01' THEN 'January'
                           WHEN '02' THEN 'February'
                           WHEN '03' THEN 'March'
                           WHEN '04' THEN 'April'
                           WHEN '05' THEN 'May'
                           WHEN '06' THEN 'June'
                           WHEN '07' THEN 'July'
                           WHEN '08' THEN 'August'
                           WHEN '09' THEN 'September'
                           WHEN '10' THEN 'October'
                           WHEN '11' THEN 'November'
                           WHEN '12' THEN 'December'
                       END as month_name,
                       COUNT(DISTINCT session_id) as session_count,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens,
                       SUM(estimated_cost_usd) as total_cost
                   FROM token_usage
                   GROUP BY strftime('%Y-%m', timestamp)
                   ORDER BY month DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_top_projects(self, limit: int = 20, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get top projects (working directories) by session activity."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            q = """SELECT s.working_directory,
                          COUNT(DISTINCT s.session_id) as session_count,
                          ROUND(COALESCE(SUM(s.duration_seconds), 0) / 3600.0, 2) as total_hours,
                          ROUND(COALESCE(SUM(tu.estimated_cost_usd), 0), 4) as total_cost,
                          MAX(s.start_time) as last_active
                   FROM sessions s
                   LEFT JOIN (
                       SELECT session_id, SUM(estimated_cost_usd) AS estimated_cost_usd
                       FROM token_usage GROUP BY session_id
                   ) tu ON tu.session_id = s.session_id
                   WHERE s.working_directory IS NOT NULL AND s.working_directory != ''"""
            params: List[Any] = []
            frag, fparams = self._machine_scope_clause(machine_id, "s.machine_id")
            q += frag
            params.extend(fparams)
            q += """ GROUP BY s.working_directory
                   ORDER BY session_count DESC
                   LIMIT ?"""
            params.append(limit)
            async with db.execute(q, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # Machine operations
    async def register_machine(self, machine_id: str, machine_name: str,
                               ip_address: str = None, os_type: str = None,
                               os_version: str = None, logged_in_user: str = None,
                               agent_version: str = None) -> int:
        """Register or update a machine."""
        now = datetime.utcnow()
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT OR REPLACE INTO machines
                   (machine_id, machine_name, ip_address, os_type, os_version,
                    logged_in_user, agent_version, group_id, agent_token, first_seen, last_seen, is_active)
                   VALUES (
                       ?,
                       ?,
                       ?,
                       ?,
                       ?,
                       COALESCE(?, (SELECT logged_in_user FROM machines WHERE machine_id = ?)),
                       COALESCE(?, (SELECT agent_version FROM machines WHERE machine_id = ?)),
                       (SELECT group_id FROM machines WHERE machine_id = ?),
                       (SELECT agent_token FROM machines WHERE machine_id = ?),
                       COALESCE((SELECT first_seen FROM machines WHERE machine_id = ?), ?),
                       ?,
                       1
                   )""",
                (machine_id, machine_name, ip_address, os_type, os_version,
                 logged_in_user, machine_id,
                 agent_version, machine_id,
                 machine_id,
                 machine_id,
                 machine_id, now, now)
            )
            await db.commit()
            return cursor.lastrowid

    async def verify_or_issue_agent_token(self, machine_id: str, provided_token: Optional[str],
                                          allow_unregistered: bool = False) -> (bool, Optional[str]):
        """Gate for every /api/client/* write. Returns (allowed, token_to_hand_back).

        - Unknown machine_id (not registered yet): allowed only when
          `allow_unregistered` (i.e. /api/client/register, which creates the
          row; the *next* call then issues a token). Every other write is
          denied, otherwise anyone could write under a fresh machine_id
          without ever holding a token.
        - Machine exists but has no agent_token on file yet (pre-upgrade
          legacy machine, or brand new registration this call): issue and
          store a fresh one now, hand it back so the caller can persist it
          and include it on every future request.
        - Machine exists and already has a token: allowed only if
          `provided_token` matches exactly; otherwise denied (someone else
          trying to write under this machine_id without knowing its secret).
        """
        import hmac as _hmac
        import secrets as _secrets
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT agent_token FROM machines WHERE machine_id = ?", (machine_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                return allow_unregistered, None

            stored_token = row[0]
            if stored_token is None:
                # Conditional UPDATE so concurrent first calls can't each
                # issue (and hand back) a different token.
                new_token = _secrets.token_urlsafe(32)
                cursor = await db.execute(
                    "UPDATE machines SET agent_token = ? WHERE machine_id = ? AND agent_token IS NULL",
                    (new_token, machine_id)
                )
                await db.commit()
                if cursor.rowcount == 1:
                    return True, new_token
                async with db.execute(
                    "SELECT agent_token FROM machines WHERE machine_id = ?", (machine_id,)
                ) as cursor:
                    stored_token = (await cursor.fetchone())[0]

            if provided_token and _hmac.compare_digest(provided_token, stored_token):
                return True, None

            return False, None

    async def reset_agent_token(self, machine_id: str) -> bool:
        """Clear a machine's stored agent_token so its next registration
        is treated as a fresh legacy-upgrade and is issued a new token.
        Admin-only remediation for a machine whose local .agent_token was
        lost/reset (or never received due to a server bug) and is now
        permanently rejected by verify_or_issue_agent_token(). Returns
        False if the machine_id doesn't exist."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "UPDATE machines SET agent_token = NULL WHERE machine_id = ?",
                (machine_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_machine_heartbeat(self, machine_id: str, agent_version: str = None):
        """Update last_seen timestamp (and agent_version, if reported) for a machine."""
        now = datetime.utcnow()
        async with self.get_connection() as db:
            if agent_version:
                await db.execute(
                    "UPDATE machines SET last_seen = ?, is_active = 1, agent_version = ? "
                    "WHERE machine_id = ?",
                    (now, agent_version, machine_id)
                )
            else:
                await db.execute(
                    "UPDATE machines SET last_seen = ?, is_active = 1 WHERE machine_id = ?",
                    (now, machine_id)
                )
            await db.commit()

    async def get_all_machines(self) -> List[Dict[str, Any]]:
        """Get all registered machines."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT
                       m.id,
                       m.machine_id,
                       m.machine_name,
                       m.ip_address,
                       m.os_type,
                       m.os_version,
                       m.logged_in_user,
                       m.agent_version,
                       m.group_id,
                       g.name as group_name,
                       REPLACE(m.first_seen, ' ', 'T') || 'Z' as first_seen,
                       REPLACE(m.last_seen, ' ', 'T') || 'Z' as last_seen,
                       m.is_active,
                       REPLACE(m.created_at, ' ', 'T') || 'Z' as created_at
                   FROM machines m
                   LEFT JOIN groups g ON g.id = m.group_id
                   ORDER BY m.is_active DESC, m.last_seen DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                machines = [dict(row) for row in rows]
                for machine in machines:
                    machine["display_name"] = self._display_name(
                        machine.get("logged_in_user"), machine.get("machine_name"), machine.get("machine_id")
                    )
                return machines

    async def get_active_machines(self) -> List[Dict[str, Any]]:
        """Get machines that have been active in the last hour."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM machines
                   WHERE last_seen >= datetime('now', '-1 hour')
                   ORDER BY last_seen DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                machines = [dict(row) for row in rows]
                for machine in machines:
                    machine["display_name"] = self._display_name(
                        machine.get("logged_in_user"), machine.get("machine_name"), machine.get("machine_id")
                    )
                return machines

    async def get_machine_by_id(self, machine_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific machine by ID."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM machines WHERE machine_id = ?", (machine_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                machine = dict(row)
                machine["display_name"] = self._display_name(
                    machine.get("logged_in_user"), machine.get("machine_name"), machine.get("machine_id")
                )
                return machine

    @staticmethod
    def _display_name(logged_in_user: Optional[str], machine_name: Optional[str], machine_id: Optional[str] = None) -> str:
        """Best-effort human-friendly name for whoever uses a machine --
        preferred over the machine's own name/id everywhere in the UI, since
        end users care who a page's data belongs to, not which physical
        machine it came from. Strips client_agent.py's git-derived
        "Name <email>" format down to just the name (or bare email when no
        git user.name is set), falling back to the machine name/id only when
        no user has been detected at all (e.g. a freshly registered
        machine)."""
        if logged_in_user:
            name = logged_in_user.split('<')[0].strip()
            if name:
                return name
        return machine_name or machine_id or "Unknown"

    async def get_machine_ids_for_user(self, email: str) -> List[str]:
        """Best-effort match of a signed-in Azure AD email to the machine(s)
        they run client_agent.py on. client_agent.py populates
        machines.logged_in_user from (in order of preference) the machine's
        git `user.email`/`user.name` (as "Name <email>"), the OS full name,
        or the OS login name -- there's no strict device-to-corporate-email
        pairing, so matching is inherently fuzzy.

        Matching is done on username (the part of an email before "@"), not
        the full email, so the same person can match across domains (e.g.
        signed in as jane.doe@example.com but the machine's git email is
        jane.doe@example-corp.com). Matches when: an email embedded in
        logged_in_user (the git "Name <email>" format) has the same username
        as the signed-in email, or logged_in_user itself equals the
        signed-in email's username (covers a bare OS username like
        "jane.doe")."""
        if not email:
            return []
        local_part = self._local_part(email)
        if not local_part:
            return []
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT machine_id, logged_in_user FROM machines "
                "WHERE logged_in_user IS NOT NULL AND logged_in_user != ''"
            ) as cursor:
                rows = await cursor.fetchall()
        matched = []
        for row in rows:
            logged_in_user = (row["logged_in_user"] or "").strip().lower()
            if not logged_in_user:
                continue
            embedded_email_match = re.search(r"<([^<>@\s]+@[^<>\s]+)>", logged_in_user) \
                or re.search(r"([^<>@\s]+@[^<>\s]+)", logged_in_user)
            if embedded_email_match:
                if self._local_part(embedded_email_match.group(1)) == local_part:
                    matched.append(row["machine_id"])
                    continue
            if logged_in_user == local_part:
                matched.append(row["machine_id"])
        return matched

    # ============================================================================
    # ADMIN USER OPERATIONS
    #
    # admin_users controls who can see every machine's data vs. only their
    # own (see get_machine_ids_for_user above and enforce_machine_scope in
    # main.py). Email is stored lowercased/trimmed so lookups are
    # case-insensitive.
    #
    # Matching is done on the username (the part before "@"), not the full
    # email, because the same person can sign in under different domains
    # (e.g. jane.doe@example-corp.com vs. jane.doe@example.com) --
    # whichever one is stored in admin_users, any domain with that username
    # is treated as the same admin.
    # ============================================================================

    @staticmethod
    def _local_part(email: str) -> str:
        """Return the username portion of an email (before "@"), lowercased."""
        email = (email or "").strip().lower()
        return email.split("@")[0] if "@" in email else email

    async def is_admin_user(self, email: str) -> bool:
        """Return True if `email`'s username matches a registered admin
        user's username, regardless of email domain."""
        if not email:
            return False
        local_part = self._local_part(email)
        if not local_part:
            return False
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT email FROM admin_users"
            ) as cursor:
                rows = await cursor.fetchall()
        return any(self._local_part(row[0]) == local_part for row in rows)

    async def get_admin_users(self) -> List[Dict[str, Any]]:
        """List all admin users, oldest first."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT email, added_at FROM admin_users ORDER BY added_at"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def add_admin_user(self, email: str) -> None:
        """Grant admin access to `email` (no-op if already an admin)."""
        async with self.get_connection() as db:
            await db.execute(
                "INSERT OR IGNORE INTO admin_users (email) VALUES (?)",
                (email.strip().lower(),),
            )
            await db.commit()

    async def remove_admin_user(self, email: str) -> bool:
        """Revoke admin access from `email`. Returns True if a row was removed."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM admin_users WHERE email = ?", (email.strip().lower(),)
            )
            await db.commit()
            return cursor.rowcount > 0

    # ============================================================================
    # SUPER USER OPERATIONS
    #
    # A narrower tier than admin_users, used to gate individual sensitive
    # features (currently just Conversation Tone) to a hand-picked few
    # people. Independent of admin_users - being an admin doesn't make you a
    # super user, and vice versa. Same username-based, domain-flexible
    # matching as admin_users (see _local_part above).
    # ============================================================================

    async def is_super_user(self, email: str) -> bool:
        """Return True if `email`'s username matches a registered super
        user's username, regardless of email domain."""
        if not email:
            return False
        local_part = self._local_part(email)
        if not local_part:
            return False
        async with self.get_connection() as db:
            async with db.execute("SELECT email FROM super_users") as cursor:
                rows = await cursor.fetchall()
        return any(self._local_part(row[0]) == local_part for row in rows)

    async def get_super_users(self) -> List[Dict[str, Any]]:
        """List all super users, oldest first."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT email, added_at FROM super_users ORDER BY added_at"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def add_super_user(self, email: str) -> None:
        """Grant super-user access to `email` (no-op if already one)."""
        async with self.get_connection() as db:
            await db.execute(
                "INSERT OR IGNORE INTO super_users (email) VALUES (?)",
                (email.strip().lower(),),
            )
            await db.commit()

    async def remove_super_user(self, email: str) -> bool:
        """Revoke super-user access from `email`. Returns True if a row was removed."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM super_users WHERE email = ?", (email.strip().lower(),)
            )
            await db.commit()
            return cursor.rowcount > 0

    # ============================================================================
    # LOGIN PROVIDERS (DB-driven SSO/OAuth2 + admin-created local accounts)
    #
    # Fully UI-configurable via Admin -> Login Providers: no environment
    # variables or redeploy needed to add/change/remove a provider. See
    # schema_v4.sql for the auth_providers/local_users table shapes and
    # src/utils/auth.py for how each provider_type's login flow actually
    # works; src/utils/crypto.py for client_secret encryption at rest.
    # ============================================================================

    async def is_auth_required(self) -> bool:
        """True as soon as at least one login provider is enabled or at
        least one local account exists -- the dynamic, DB-driven
        replacement for the old static Config.AUTH_ENABLED. See
        request.state.auth_enabled in main.py (set once per request by the
        auth middleware) for where this is actually consulted."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT EXISTS(SELECT 1 FROM auth_providers WHERE enabled = 1) "
                "OR EXISTS(SELECT 1 FROM local_users)"
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row[0]) if row else False

    @staticmethod
    def _auth_provider_row_to_dict(row: dict, include_secret: bool = False) -> Dict[str, Any]:
        d = dict(row)
        if not include_secret:
            d.pop("client_secret_encrypted", None)
            d["has_secret"] = bool(row.get("client_secret_encrypted"))
        return d

    async def list_auth_providers(self, include_secret: bool = False) -> List[Dict[str, Any]]:
        """List every configured login provider, oldest first. Client
        secrets are masked out by default (include_secret=True is only ever
        used internally, right before starting/completing that provider's
        login flow)."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM auth_providers ORDER BY created_at"
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    self._auth_provider_row_to_dict(dict(row), include_secret)
                    for row in rows
                ]

    async def get_auth_provider(self, provider_key: str, include_secret: bool = True) -> Optional[Dict[str, Any]]:
        """Fetch a single provider by its slug (e.g. 'google', 'azure-ad').
        include_secret defaults to True here since the only caller of this
        specific lookup is the login/callback flow itself, which needs the
        decrypted secret to talk to the provider."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM auth_providers WHERE provider_key = ?", (provider_key,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._auth_provider_row_to_dict(dict(row), include_secret)

    async def create_auth_provider(
        self,
        provider_key: str,
        provider_type: str,
        display_name: str,
        client_id: str,
        client_secret_encrypted: str,
        authorization_url: Optional[str] = None,
        token_url: Optional[str] = None,
        userinfo_url: Optional[str] = None,
        scopes: Optional[str] = None,
        tenant_id: Optional[str] = None,
        enabled: bool = True,
    ) -> int:
        """Add a new login provider. Raises sqlite3.IntegrityError if
        provider_key is already taken."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO auth_providers
                   (provider_key, provider_type, display_name, client_id,
                    client_secret_encrypted, authorization_url, token_url,
                    userinfo_url, scopes, tenant_id, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (provider_key, provider_type, display_name, client_id,
                 client_secret_encrypted, authorization_url, token_url,
                 userinfo_url, scopes, tenant_id, 1 if enabled else 0),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_auth_provider(self, provider_id: int, **fields) -> bool:
        """Update any subset of an existing provider's columns (e.g. just
        display_name, or a freshly-encrypted client_secret_encrypted when an
        admin rotates a secret). client_secret_encrypted is only included by
        the caller when the admin actually typed a new secret -- leaving it
        out keeps the existing one. Returns True if a row was updated."""
        allowed = {
            "display_name", "client_id", "client_secret_encrypted",
            "authorization_url", "token_url", "userinfo_url", "scopes",
            "tenant_id", "enabled",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [provider_id]
        async with self.get_connection() as db:
            cursor = await db.execute(
                f"UPDATE auth_providers SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_auth_provider(self, provider_id: int) -> bool:
        """Remove a login provider entirely. Returns True if a row was removed."""
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM auth_providers WHERE id = ?", (provider_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_local_users(self) -> List[Dict[str, Any]]:
        """List admin-created local accounts (password hashes excluded)."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, email, name, enabled, created_at, updated_at, last_login_at "
                "FROM local_users ORDER BY created_at"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_local_user(self, email: str, name: str, password_hash: str) -> int:
        """Create an admin-provisioned username/password account. Raises
        sqlite3.IntegrityError if the email is already registered."""
        email = email.strip().lower()
        async with self.get_connection() as db:
            cursor = await db.execute(
                "INSERT INTO local_users (email, name, password_hash) VALUES (?, ?, ?)",
                (email, name, password_hash),
            )
            await db.commit()
            return cursor.lastrowid

    async def update_local_user(self, user_id: int, **fields) -> bool:
        """Update a local account's name/enabled flag, or reset its
        password_hash. Returns True if a row was updated."""
        allowed = {"name", "enabled", "password_hash"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        async with self.get_connection() as db:
            cursor = await db.execute(
                f"UPDATE local_users SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_local_user(self, user_id: int) -> bool:
        """Remove a local account entirely. Returns True if a row was removed."""
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM local_users WHERE id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_local_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Fetch a local account (including its password_hash) for login
        verification. Matches on exact lowercased email -- unlike
        admin_users/super_users this is not username-only/domain-flexible,
        since a local account's email IS its login username, not something
        matched against a separately-reported git/OS identity."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM local_users WHERE email = ?", (email.strip().lower(),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def touch_local_user_login(self, user_id: int) -> None:
        """Record a successful login's timestamp."""
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE local_users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user_id,),
            )
            await db.commit()

    # ============================================================================
    # GROUP (TEAM) OPERATIONS
    #
    # Groups let a set of machines/users be scoped together as a "team" for
    # reporting purposes. Every machine belongs to at most one group. Once a
    # group is selected as the active scope, the frontend resolves it to a
    # comma-separated list of that group's machine_ids and passes that list
    # through the existing `machine_id` query param used everywhere already —
    # so no per-page/per-endpoint changes are needed: `_machine_scope_clause()`
    # below transparently accepts either a single machine_id or a comma list.
    # ============================================================================

    async def create_group(self, name: str, description: str = None) -> int:
        """Create a new group (team). Raises sqlite3.IntegrityError if the
        name already exists."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "INSERT INTO groups (name, description) VALUES (?, ?)",
                (name.strip(), description)
            )
            await db.commit()
            return cursor.lastrowid

    async def update_group(self, group_id: int, name: str = None, description: str = None) -> bool:
        """Rename/update a group's description."""
        async with self.get_connection() as db:
            await db.execute(
                """UPDATE groups SET
                       name = COALESCE(?, name),
                       description = COALESCE(?, description)
                   WHERE id = ?""",
                (name.strip() if name else None, description, group_id)
            )
            await db.commit()
            cursor = await db.execute("SELECT changes()")
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    async def delete_group(self, group_id: int) -> bool:
        """Delete a group. Member machines are unassigned (group_id -> NULL),
        not deleted."""
        async with self.get_connection() as db:
            await db.execute("UPDATE machines SET group_id = NULL WHERE group_id = ?", (group_id,))
            await db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            await db.commit()
            cursor = await db.execute("SELECT changes()")
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    async def get_all_groups(self) -> List[Dict[str, Any]]:
        """Get all groups along with how many machines belong to each."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT g.id, g.name, g.description,
                          REPLACE(g.created_at, ' ', 'T') || 'Z' as created_at,
                          COUNT(m.machine_id) as machine_count
                   FROM groups g
                   LEFT JOIN machines m ON m.group_id = g.id
                   GROUP BY g.id
                   ORDER BY g.name"""
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        """Get a single group by id."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_machines_in_group(self, group_id: int) -> List[Dict[str, Any]]:
        """Get all machines belonging to a group."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM machines WHERE group_id = ? ORDER BY machine_name", (group_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def set_machine_group(self, machine_id: str, group_id: Optional[int]) -> bool:
        """Assign (or change/clear, if group_id is None) a machine's group.

        Raises ValueError if group_id is set but doesn't reference an
        existing group — SQLite foreign keys aren't enforced here, so this
        guard is the only thing preventing an orphaned/bogus group_id from
        being written.
        """
        async with self.get_connection() as db:
            if group_id is not None:
                async with db.execute(
                    "SELECT 1 FROM groups WHERE id = ?", (group_id,)
                ) as cursor:
                    if not await cursor.fetchone():
                        raise ValueError(f"Group {group_id} not found")
            await db.execute(
                "UPDATE machines SET group_id = ? WHERE machine_id = ?",
                (group_id, machine_id)
            )
            await db.commit()
            cursor = await db.execute("SELECT changes()")
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    # Tables keyed directly by session_id -- every row for a deleted
    # machine's sessions must be purged from each of these before the
    # sessions themselves (and the machine) can be removed.
    _SESSION_SCOPED_TABLES = (
        "launches", "process_metrics", "network_metrics", "filesystem_events",
        "token_usage", "messages", "session_tags", "session_pr_links",
        "ado_commit_session_matches", "prompt_analysis", "context_insights",
        "cache_analytics", "code_modifications", "code_quality_metrics",
        "refactoring_analysis", "ai_suggestions",
    )

    async def delete_machine_completely(self, machine_id: str) -> Optional[Dict[str, Any]]:
        """Permanently delete a user/machine and every row that references it:
        all of that machine's sessions and everything keyed off those
        session_ids, every machine_id-keyed row, and -- if this was the
        person's only machine -- their logged_in_user-keyed gamification
        data (skills, learning path, achievements, language stats) and
        user-scoped anomaly flags.

        This is irreversible and intended for the admin-only "delete user"
        action in User Management. Returns a summary dict (machine_id,
        logged_in_user, sessions_deleted, user_data_purged) or None if the
        machine doesn't exist.

        Note: `token_waste_patterns` is intentionally left untouched -- it's
        a shared community pattern-detection table (not one row per user)
        whose `sessions_affected` JSON would need parsing to clean
        precisely, and `nl_query_log.asked_by` is an SSO admin email (a
        different identity than the machine's `logged_in_user`), so it
        isn't this user's data to purge either.
        """
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM machines WHERE machine_id = ?", (machine_id,)
            ) as cursor:
                machine = await cursor.fetchone()
            if not machine:
                return None
            logged_in_user = machine["logged_in_user"]

            async with db.execute(
                "SELECT session_id FROM sessions WHERE machine_id = ?", (machine_id,)
            ) as cursor:
                session_ids = [row["session_id"] for row in await cursor.fetchall()]

            if session_ids:
                placeholders = ",".join("?" * len(session_ids))
                for table in self._SESSION_SCOPED_TABLES:
                    await db.execute(
                        f"DELETE FROM {table} WHERE session_id IN ({placeholders})", session_ids
                    )
                await db.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({placeholders})", session_ids
                )

            await db.execute("DELETE FROM sync_log WHERE machine_id = ?", (machine_id,))
            await db.execute("DELETE FROM model_performance WHERE machine_id = ?", (machine_id,))
            await db.execute(
                "DELETE FROM anomalies WHERE scope_type = 'machine' AND scope_id = ?", (machine_id,)
            )

            # Only purge the shared user_id-keyed tables if no other machine
            # still belongs to this same logged_in_user -- otherwise we'd
            # wipe out gamification history that still legitimately belongs
            # to their other, still-present machine.
            user_data_purged = False
            if logged_in_user:
                async with db.execute(
                    "SELECT 1 FROM machines WHERE logged_in_user = ? AND machine_id != ?",
                    (logged_in_user, machine_id),
                ) as cursor:
                    has_other_machine = await cursor.fetchone() is not None
                if not has_other_machine:
                    await db.execute("DELETE FROM user_skills WHERE user_id = ?", (logged_in_user,))
                    await db.execute(
                        "DELETE FROM learning_path_progress WHERE user_id = ?", (logged_in_user,)
                    )
                    await db.execute(
                        "DELETE FROM user_achievements WHERE user_id = ?", (logged_in_user,)
                    )
                    await db.execute("DELETE FROM language_stats WHERE user_id = ?", (logged_in_user,))
                    await db.execute(
                        "DELETE FROM anomalies WHERE scope_type = 'user' AND scope_id = ?",
                        (logged_in_user,),
                    )
                    user_data_purged = True

            await db.execute("DELETE FROM machines WHERE machine_id = ?", (machine_id,))
            await db.commit()

            return {
                "machine_id": machine_id,
                "logged_in_user": logged_in_user,
                "sessions_deleted": len(session_ids),
                "user_data_purged": user_data_purged,
            }

    async def resolve_group_machine_ids(self, group_id: int) -> str:
        """Resolve a group to a comma-separated string of its member
        machine_ids, suitable for passing straight into any existing
        `machine_id` filter param."""
        machines = await self.get_machines_in_group(group_id)
        return ",".join(m["machine_id"] for m in machines)

    @staticmethod
    def _machine_scope_clause(machine_id: Optional[str], column: str = "machine_id") -> tuple:
        """Build a SQL fragment + bound params for filtering rows by machine.

        `machine_id` may be:
          - None / '' / 'all'         -> no filter (fragment is "")
          - a single machine_id       -> "{column} = ?"
          - a comma-separated list    -> "{column} IN (?, ?, ...)" (used when
                                          a Team/Group scope expands to
                                          multiple machines)

        Returns (sql_fragment, params_list). The fragment always starts with
        " AND " so it can be appended directly to an existing WHERE clause.
        """
        if not machine_id or machine_id == "all":
            return "", []
        ids = [m.strip() for m in machine_id.split(",") if m.strip()]
        if not ids:
            return "", []
        if len(ids) == 1:
            return f" AND {column} = ?", [ids[0]]
        placeholders = ",".join("?" * len(ids))
        return f" AND {column} IN ({placeholders})", ids

    @classmethod
    def _machine_scope_subquery_clause(cls, machine_id: Optional[str], id_column: str = "session_id") -> tuple:
        """Same as _machine_scope_clause, but for tables that don't have a
        machine_id column directly and instead need to filter via a
        `session_id IN (SELECT session_id FROM sessions WHERE machine_id ...)`
        subquery."""
        frag, params = cls._machine_scope_clause(machine_id, "machine_id")
        if not frag:
            return "", []
        return f" AND {id_column} IN (SELECT session_id FROM sessions WHERE 1=1{frag})", params


    async def log_sync(self, machine_id: str, source: str = None,
                        items_total: int = 0, items_synced: int = 0,
                        items_failed: int = 0, message: str = None) -> int:
        """Record the result of one client-agent sync cycle."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO sync_log
                   (machine_id, source, items_total, items_synced, items_failed, message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (machine_id, source, items_total, items_synced, items_failed, message)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_sync_log(self, limit: int = 100, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get recent sync log entries, most recent first."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            q = """SELECT sl.id,
                          sl.machine_id,
                          m.machine_name,
                          m.logged_in_user,
                          sl.source,
                          sl.items_total,
                          sl.items_synced,
                          sl.items_failed,
                          sl.message,
                          REPLACE(sl.synced_at, ' ', 'T') || 'Z' as synced_at
                   FROM sync_log sl
                   LEFT JOIN machines m ON m.machine_id = sl.machine_id
                   WHERE 1=1"""
            params: List[Any] = []
            frag, fparams = self._machine_scope_clause(machine_id, "sl.machine_id")
            q += frag
            params.extend(fparams)
            q += " ORDER BY sl.synced_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(q, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ============================================================================
    # LEARNING & DEVELOPMENT METHODS
    # ============================================================================

    async def get_best_practices(self, category: str = None, difficulty: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get best practices, optionally filtered by category and difficulty."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM best_practices WHERE 1=1"
            params = []
            if category:
                query += " AND category = ?"
                params.append(category)
            if difficulty:
                query += " AND difficulty_level = ?"
                params.append(difficulty)
            query += " ORDER BY upvotes DESC, created_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_derived_best_practices(self, machine_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Derive 'best practices' cards directly from this machine's own real
        prompt history (prompt_analysis) instead of the generic static
        catalog: high-quality prompts become real "what you're doing well"
        examples, and lower-quality prompts that have real analyzer-generated
        suggestions become real "try this next time" examples — using the
        machine's own actual prompt text and suggestions, not placeholders.
        Returns [] if this machine has no analyzed prompts yet, so the caller
        can fall back to the static reference catalog."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        half = max(1, limit // 2)
        frag, fparams = self._machine_scope_clause(machine_id, "s.machine_id")
        # frag/fparams always non-empty here since callers only invoke this
        # with a real machine_id or comma-separated group scope (never 'all').

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT pa.id, pa.prompt_text, pa.quality_score, pa.clarity_score,
                          pa.specificity_score
                   FROM prompt_analysis pa JOIN sessions s ON pa.session_id = s.session_id
                   WHERE 1=1{frag} AND pa.quality_score >= 75
                   ORDER BY pa.quality_score DESC LIMIT ?""",
                (*fparams, half)
            ) as cursor:
                good_rows = await cursor.fetchall()

            async with db.execute(
                f"""SELECT pa.id, pa.prompt_text, pa.quality_score, pa.suggestions
                   FROM prompt_analysis pa JOIN sessions s ON pa.session_id = s.session_id
                   WHERE 1=1{frag} AND pa.suggestions IS NOT NULL AND pa.suggestions != '[]'
                   ORDER BY pa.quality_score ASC LIMIT ?""",
                (*fparams, half)
            ) as cursor:
                improve_rows = await cursor.fetchall()

        def excerpt(text, length=220):
            text = (text or "").strip().replace("\n", " ")
            return text if len(text) <= length else text[:length].rstrip() + "..."

        practices = []
        for row in good_rows:
            score = row["quality_score"] or 0
            practices.append({
                "id": f"derived-good-{row['id']}",
                "category": "prompt_engineering",
                "title": f"A prompt you wrote scored {round(score)}/100",
                "description": (
                    f"Clarity {round(row['clarity_score'] or 0)}/100, "
                    f"specificity {round(row['specificity_score'] or 0)}/100 — "
                    "a real example from your own prompt history. Keep using this style."
                ),
                "example_prompt": excerpt(row["prompt_text"]),
                "example_code": None,
                "tags": json.dumps(["your-activity", "high-quality"]),
                "difficulty_level": "advanced" if score >= 85 else "intermediate",
                "upvotes": 0,
            })
        for row in improve_rows:
            suggestions = json.loads(row["suggestions"] or "[]")
            if not suggestions:
                continue
            practices.append({
                "id": f"derived-improve-{row['id']}",
                "category": "prompt_engineering",
                "title": f"A prompt of yours scored {round(row['quality_score'] or 0)}/100 — try this",
                "description": " ".join(suggestions),
                "example_prompt": excerpt(row["prompt_text"]),
                "example_code": None,
                "tags": json.dumps(["your-activity", "improvement"]),
                "difficulty_level": "beginner",
                "upvotes": 0,
            })
        return practices[:limit]

    async def create_best_practice(self, category: str, title: str, description: str,
                                   example_prompt: str = None, example_code: str = None,
                                   tags: str = None, difficulty_level: str = "intermediate"):
        """Create a new best practice."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO best_practices
                   (category, title, description, example_prompt, example_code, tags, difficulty_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (category, title, description, example_prompt, example_code, tags, difficulty_level)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_user_skills(self, user_id: str) -> List[Dict[str, Any]]:
        """Get skill progression for a user."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM user_skills WHERE user_id = ? ORDER BY skill_category",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def update_user_skill(self, user_id: str, skill_category: str, **updates):
        """Update user skill metrics."""
        async with self.get_connection() as db:
            # Check if record exists
            async with db.execute(
                "SELECT id FROM user_skills WHERE user_id = ? AND skill_category = ?",
                (user_id, skill_category)
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                # Update existing
                set_clauses = [f"{k} = ?" for k in updates.keys()]
                values = list(updates.values()) + [user_id, skill_category]
                await db.execute(
                    f"UPDATE user_skills SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND skill_category = ?",
                    values
                )
            else:
                # Create new
                columns = ['user_id', 'skill_category'] + list(updates.keys())
                placeholders = ['?'] * len(columns)
                values = [user_id, skill_category] + list(updates.values())
                await db.execute(
                    f"INSERT INTO user_skills ({', '.join(columns)}) VALUES ({', '.join(placeholders)})",
                    values
                )
            await db.commit()

    async def get_learning_paths(self, difficulty: str = None) -> List[Dict[str, Any]]:
        """Get available learning paths."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM learning_paths WHERE is_active = 1"
            params = []
            if difficulty:
                query += " AND difficulty_level = ?"
                params.append(difficulty)
            query += " ORDER BY difficulty_level, created_at"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_user_learning_progress(self, user_id: str) -> List[Dict[str, Any]]:
        """Get user's progress on learning paths."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT lpp.*, lp.name, lp.description
                   FROM learning_path_progress lpp
                   JOIN learning_paths lp ON lpp.learning_path_id = lp.id
                   WHERE lpp.user_id = ?
                   ORDER BY lpp.last_activity DESC""",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_community_patterns(self, pattern_type: str = None, language: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get community-shared patterns."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM community_patterns WHERE 1=1"
            params = []
            if pattern_type:
                query += " AND pattern_type = ?"
                params.append(pattern_type)
            if language:
                query += " AND language = ?"
                params.append(language)
            query += " ORDER BY upvotes DESC, usage_count DESC LIMIT ?"
            params.append(limit)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_user_achievements(self, user_id: str) -> List[Dict[str, Any]]:
        """Get achievements earned by a user."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT ua.*, a.name, a.description, a.badge_icon, a.badge_color, a.rarity, a.points
                   FROM user_achievements ua
                   JOIN achievements a ON ua.achievement_id = a.id
                   WHERE ua.user_id = ?
                   ORDER BY ua.earned_at DESC""",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def _get_user_activity_stats(self, db, user_id: str) -> Dict[str, Any]:
        """Aggregate real activity data for a user/machine (user_id == machine_id in
        this app) from sessions/token_usage/prompt_analysis/code_modifications/
        cache_analytics. Used to derive skills, learning-path progress, and
        achievements from actual telemetry rather than static placeholders.

        user_id of None/''/'all' means "no specific machine selected" — matches
        the convention used by every other page (Usage/Insights/etc.), where an
        unfiltered/'all' selection aggregates across every machine rather than
        literally filtering on a machine_id equal to the string "all". user_id
        may also be a comma-separated list of machine_ids (a Team/Group scope),
        in which case activity is aggregated across just that group."""

        async def scalar(base_query, column="machine_id"):
            frag, fparams = self._machine_scope_clause(user_id, column)
            async with db.execute(base_query + frag, fparams) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

        sessions_completed = await scalar(
            "SELECT COUNT(*) FROM sessions WHERE end_time IS NOT NULL"
        ) or 0
        tokens_used = await scalar(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM token_usage WHERE 1=1"
        ) or 0
        files_modified = await scalar(
            """SELECT COUNT(DISTINCT cm.file_path) FROM code_modifications cm
               JOIN sessions s ON cm.session_id = s.session_id WHERE 1=1""",
            column="s.machine_id"
        ) or 0
        avg_prompt_quality = await scalar(
            """SELECT AVG(pa.quality_score) FROM prompt_analysis pa
               JOIN sessions s ON pa.session_id = s.session_id WHERE 1=1""",
            column="s.machine_id"
        )
        avg_cache_efficiency = await scalar(
            """SELECT AVG(ca.cache_efficiency_score) FROM cache_analytics ca
               JOIN sessions s ON ca.session_id = s.session_id WHERE 1=1""",
            column="s.machine_id"
        )
        avg_mod_quality = await scalar(
            """SELECT AVG(cm.modification_quality_score) FROM code_modifications cm
               JOIN sessions s ON cm.session_id = s.session_id WHERE 1=1""",
            column="s.machine_id"
        )
        accepted_rate = await scalar(
            """SELECT AVG(CASE WHEN cm.ai_suggestion_accepted THEN 1.0 ELSE 0.0 END)
               FROM code_modifications cm JOIN sessions s ON cm.session_id = s.session_id
               WHERE 1=1""",
            column="s.machine_id"
        )
        distinct_projects = await scalar(
            """SELECT COUNT(DISTINCT working_directory) FROM sessions
               WHERE working_directory IS NOT NULL AND working_directory != ''"""
        ) or 0
        first_activity = await scalar(
            "SELECT MIN(start_time) FROM sessions WHERE 1=1"
        )
        last_activity = await scalar(
            "SELECT MAX(start_time) FROM sessions WHERE 1=1"
        )

        return {
            "sessions_completed": sessions_completed,
            "tokens_used": tokens_used,
            "files_modified": files_modified,
            "avg_prompt_quality": avg_prompt_quality,
            "avg_cache_efficiency": avg_cache_efficiency,
            "avg_mod_quality": avg_mod_quality,
            "accepted_rate": accepted_rate,
            "distinct_projects": distinct_projects,
            "first_activity": first_activity,
            "last_activity": last_activity,
        }

    async def recompute_user_skills(self, user_id: str) -> bool:
        """Derive the 4 skill categories for a user/machine from real activity
        data and upsert into user_skills. Returns True if there was any
        activity to derive from (False leaves the table untouched)."""
        async with self.get_connection() as db:
            stats = await self._get_user_activity_stats(db, user_id)
            if stats["sessions_completed"] == 0:
                return False

            prompt_level = (
                round(stats["avg_prompt_quality"]) if stats["avg_prompt_quality"] is not None
                else min(100, stats["sessions_completed"] * 3)
            )
            if stats["avg_mod_quality"] is not None:
                quality_level = round(stats["avg_mod_quality"])
            else:
                quality_level = round(min(
                    100,
                    (stats["accepted_rate"] or 0) * 60 + min(stats["files_modified"], 40)
                ))
            efficiency_level = (
                round(stats["avg_cache_efficiency"]) if stats["avg_cache_efficiency"] is not None
                else round(min(100, stats["tokens_used"] / 20000))
            )
            collab_level = round(min(
                100,
                stats["distinct_projects"] * 8 + min(stats["sessions_completed"], 40)
            ))

            categories = {
                "prompt_engineering": (prompt_level, stats["avg_prompt_quality"]),
                "code_quality": (quality_level, stats["avg_mod_quality"]),
                "efficiency": (efficiency_level, stats["avg_cache_efficiency"]),
                "collaboration": (collab_level, collab_level),
            }

            for category, (level, avg_quality) in categories.items():
                level = max(0, min(100, int(level)))
                avg_quality = round(float(avg_quality), 1) if avg_quality is not None else 0.0
                await db.execute(
                    """INSERT INTO user_skills
                       (user_id, skill_category, skill_level, sessions_completed, tokens_used,
                        code_files_modified, average_session_quality, last_activity, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(user_id, skill_category) DO UPDATE SET
                         skill_level = excluded.skill_level,
                         sessions_completed = excluded.sessions_completed,
                         tokens_used = excluded.tokens_used,
                         code_files_modified = excluded.code_files_modified,
                         average_session_quality = excluded.average_session_quality,
                         last_activity = excluded.last_activity,
                         updated_at = CURRENT_TIMESTAMP""",
                    (user_id, category, level, stats["sessions_completed"], stats["tokens_used"],
                     stats["files_modified"], avg_quality, stats["last_activity"])
                )
            await db.commit()
            return True

    async def recompute_learning_path_progress(self, user_id: str) -> bool:
        """Derive per-path progress from real activity data and upsert into
        learning_path_progress. Each path has 3 steps; step completion is
        driven by the skill level most relevant to that path's subject."""
        async with self.get_connection() as db:
            stats = await self._get_user_activity_stats(db, user_id)
            if stats["sessions_completed"] == 0:
                return False

            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name FROM learning_paths WHERE is_active = 1"
            ) as cursor:
                paths = await cursor.fetchall()

            prompt_level = (
                stats["avg_prompt_quality"] if stats["avg_prompt_quality"] is not None
                else min(100, stats["sessions_completed"] * 3)
            )
            debugging_level = round(min(
                100, stats["files_modified"] * 2 + stats["sessions_completed"]
            ))
            optimization_level = (
                stats["avg_cache_efficiency"] if stats["avg_cache_efficiency"] is not None
                else min(100, stats["tokens_used"] / 20000)
            )

            # Map each learning path (by name) to the skill level driving its progress.
            path_levels = {
                "Effective Prompting Fundamentals": prompt_level,
                "Debugging with Claude Code": debugging_level,
                "Token & Cost Optimization": optimization_level,
            }

            for path in paths:
                level = path_levels.get(path["name"], prompt_level) or 0
                steps_completed = max(0, min(3, int(level * 3 // 100)))
                progress_pct = round(steps_completed / 3 * 100, 1)
                completed_at = stats["last_activity"] if steps_completed >= 3 else None
                steps_json = json.dumps(list(range(1, steps_completed + 1)))

                await db.execute(
                    """INSERT INTO learning_path_progress
                       (user_id, learning_path_id, current_step, steps_completed,
                        progress_percentage, started_at, completed_at, last_activity)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, learning_path_id) DO UPDATE SET
                         current_step = excluded.current_step,
                         steps_completed = excluded.steps_completed,
                         progress_percentage = excluded.progress_percentage,
                         completed_at = excluded.completed_at,
                         last_activity = excluded.last_activity""",
                    (user_id, path["id"], steps_completed, steps_json, progress_pct,
                     stats["first_activity"], completed_at, stats["last_activity"])
                )
            await db.commit()
            return True

    async def recompute_user_achievements(self, user_id: str) -> bool:
        """Check real activity stats against achievement criteria and record
        newly-earned achievements in user_achievements (never revokes)."""
        async with self.get_connection() as db:
            stats = await self._get_user_activity_stats(db, user_id)
            if stats["sessions_completed"] == 0:
                return False

            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT id, criteria FROM achievements") as cursor:
                achievements = await cursor.fetchall()

            metric_values = {
                "session_count": stats["sessions_completed"],
                "prompt_quality": stats["avg_prompt_quality"] or 0,
                "cache_efficiency": stats["avg_cache_efficiency"] or 0,
                "files_modified": stats["files_modified"],
                "projects": stats["distinct_projects"],
                "tokens_used": stats["tokens_used"],
            }

            earned_any = False
            for achievement in achievements:
                criteria = json.loads(achievement["criteria"])
                metric = metric_values.get(criteria.get("type"))
                if metric is None or metric < criteria.get("min", float("inf")):
                    continue
                await db.execute(
                    """INSERT OR IGNORE INTO user_achievements
                       (user_id, achievement_id, earned_at, progress_data)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, achievement["id"], stats["last_activity"],
                     json.dumps({"metric": criteria.get("type"), "value": metric}))
                )
                earned_any = True
            await db.commit()
            return earned_any

    async def refresh_user_learning_data(self, user_id: str) -> None:
        """Recompute skills, learning-path progress, and achievements for a
        user/machine from live activity data. Cheap enough (a handful of
        aggregate queries + upserts over a modest row count) to run on every
        Learning-page load, so data self-heals without a separate background
        job."""
        await self.recompute_user_skills(user_id)
        await self.recompute_learning_path_progress(user_id)
        await self.recompute_user_achievements(user_id)

    async def get_leaderboard(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """Build an all-machines scoreboard ranking every machine/user by
        total achievement points earned (real activity, not placeholders).

        Refreshes skills + achievements for every machine in scope first
        (same self-healing pattern as `refresh_user_learning_data`), then
        aggregates points/badges/skill level per machine and ranks them.

        `machine_id` follows the same convention used by every other page/
        endpoint (None/''/'all' -> every machine; a single id or a
        comma-separated Team/Group scope -> just those machines), so the
        existing global Team filter narrows the ranked pool automatically.
        """
        all_machines = await self.get_all_machines()
        if machine_id and machine_id != "all":
            scoped_ids = {m.strip() for m in machine_id.split(",") if m.strip()}
            machines = [m for m in all_machines if m["machine_id"] in scoped_ids]
        else:
            machines = all_machines

        total_achievements_row = None
        async with self.get_connection() as db:
            async with db.execute("SELECT COUNT(*), COALESCE(SUM(points), 0) FROM achievements") as cursor:
                total_achievements_row = await cursor.fetchone()
        total_badges_available = total_achievements_row[0] if total_achievements_row else 0
        total_points_available = total_achievements_row[1] if total_achievements_row else 0

        entries = []
        for machine in machines:
            user_id = machine["machine_id"]
            # Self-healing: derive latest skills/achievements from real
            # activity before reading them, so the leaderboard never shows
            # stale data even if the owner hasn't opened the Learning page.
            await self.recompute_user_skills(user_id)
            await self.recompute_user_achievements(user_id)

            achievements = await self.get_user_achievements(user_id)
            skills = await self.get_user_skills(user_id)

            total_points = sum(a.get("points") or 0 for a in achievements)
            avg_skill_level = (
                round(sum(s.get("skill_level") or 0 for s in skills) / len(skills), 1)
                if skills else 0.0
            )

            async with self.get_connection() as db:
                stats = await self._get_user_activity_stats(db, user_id)

            top_badges = sorted(achievements, key=lambda a: a.get("points") or 0, reverse=True)

            if stats["sessions_completed"] == 0 and total_points == 0:
                # Skip machines with zero recorded activity entirely - nothing
                # to rank, and they'd just clutter the board at 0 points.
                continue

            entries.append({
                "machine_id": machine["machine_id"],
                "machine_name": machine.get("machine_name") or machine["machine_id"],
                "logged_in_user": machine.get("logged_in_user"),
                "display_name": machine.get("display_name"),
                "os_type": machine.get("os_type"),
                "group_id": machine.get("group_id"),
                "group_name": machine.get("group_name"),
                "is_active": machine.get("is_active"),
                "total_points": total_points,
                "total_points_available": total_points_available,
                "badge_count": len(achievements),
                "total_badges_available": total_badges_available,
                "avg_skill_level": avg_skill_level,
                "sessions_completed": stats["sessions_completed"],
                "tokens_used": stats["tokens_used"],
                "files_modified": stats["files_modified"],
                "distinct_projects": stats["distinct_projects"],
                "last_activity": stats["last_activity"],
                "badges": [
                    {
                        "name": a["name"],
                        "badge_icon": a.get("badge_icon"),
                        "badge_color": a.get("badge_color"),
                        "rarity": a.get("rarity"),
                        "points": a.get("points"),
                        "earned_at": a.get("earned_at"),
                    }
                    for a in top_badges
                ],
            })

        # Rank by points desc, then skill level, then session count as tiebreakers.
        entries.sort(
            key=lambda e: (e["total_points"], e["avg_skill_level"], e["sessions_completed"]),
            reverse=True
        )
        for idx, entry in enumerate(entries, start=1):
            entry["rank"] = idx

        return {
            "entries": entries,
            "total_badges_available": total_badges_available,
            "total_points_available": total_points_available,
        }

    # Human-readable label for each achievement criteria "type" - used to
    # build the plain-English explanation in get_achievement_details()
    # (e.g. "your completed sessions reached 94" rather than exposing the
    # raw internal metric key "session_count").
    ACHIEVEMENT_METRIC_LABELS = {
        "session_count": "completed sessions",
        "prompt_quality": "average prompt quality score",
        "cache_efficiency": "average cache efficiency score",
        "files_modified": "distinct files modified",
        "projects": "distinct projects worked across",
        "tokens_used": "tokens used",
    }

    async def get_achievement_details(self, machine_id: str) -> Dict[str, Any]:
        """Drill-down behind one machine's leaderboard badges/points: every
        achievement - earned or still locked - together with the real,
        live-recomputed activity number behind it and a plain-English
        explanation of why it was (or wasn't yet) awarded. This is the
        "how did I get this badge / what do I need for the next one"
        transparency view, mirroring get_tone_details()'s drill-down
        pattern on the Conversation Tone page.

        Always recomputes achievements first (self-healing, same as
        get_leaderboard()) so this reflects the very latest activity even
        if the owner hasn't visited the Leaderboard/Learning page since."""
        machine = await self.get_machine_by_id(machine_id)
        if not machine:
            return {"machine_id": machine_id, "total_points": 0, "earned": [], "locked": []}

        await self.recompute_user_skills(machine_id)
        await self.recompute_user_achievements(machine_id)

        async with self.get_connection() as db:
            stats = await self._get_user_activity_stats(db, machine_id)

            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, name, description, badge_icon, badge_color, criteria, points, rarity
                   FROM achievements ORDER BY points"""
            ) as cursor:
                achievements = [dict(row) for row in await cursor.fetchall()]

            async with db.execute(
                "SELECT achievement_id, earned_at FROM user_achievements WHERE user_id = ?",
                (machine_id,)
            ) as cursor:
                earned_map = {row["achievement_id"]: row["earned_at"] for row in await cursor.fetchall()}

        metric_values = {
            "session_count": stats["sessions_completed"],
            "prompt_quality": stats["avg_prompt_quality"] or 0,
            "cache_efficiency": stats["avg_cache_efficiency"] or 0,
            "files_modified": stats["files_modified"],
            "projects": stats["distinct_projects"],
            "tokens_used": stats["tokens_used"],
        }

        earned_list, locked_list = [], []
        for a in achievements:
            criteria = json.loads(a["criteria"])
            metric_type = criteria.get("type")
            threshold = criteria.get("min", 0)
            current_value = metric_values.get(metric_type, 0)
            # Round the two average-score metrics for display; the rest are
            # already whole numbers (counts/token totals).
            display_value = round(current_value, 1) if isinstance(current_value, float) else current_value
            progress_pct = round(min(100, (current_value / threshold) * 100), 1) if threshold else 100.0
            label = self.ACHIEVEMENT_METRIC_LABELS.get(metric_type, metric_type or "activity")
            is_earned = a["id"] in earned_map

            if is_earned:
                explanation = f"Earned — your {label} reached {display_value} (needed {threshold}+)."
            else:
                remaining = round(threshold - current_value, 1) if isinstance(current_value, float) else threshold - current_value
                explanation = (
                    f"Locked — your {label} is currently {display_value}, "
                    f"{remaining:g} short of the {threshold}+ needed."
                    if remaining > 0 else
                    f"Locked — your {label} is {display_value} (needed {threshold}+); "
                    "will unlock the next time your activity is refreshed."
                )

            entry = {
                "id": a["id"],
                "name": a["name"],
                "description": a["description"],
                "badge_icon": a["badge_icon"],
                "badge_color": a["badge_color"],
                "rarity": a["rarity"],
                "points": a["points"],
                "criteria_type": metric_type,
                "criteria_min": threshold,
                "current_value": display_value,
                "progress_pct": progress_pct,
                "earned_at": earned_map.get(a["id"]),
                "explanation": explanation,
            }
            (earned_list if is_earned else locked_list).append(entry)

        # Earned badges: most valuable first. Locked badges: closest to
        # unlocking first, so the most actionable "almost there" ones are
        # what the user sees at the top.
        earned_list.sort(key=lambda e: -e["points"])
        locked_list.sort(key=lambda e: -e["progress_pct"])

        return {
            "machine_id": machine_id,
            "machine_name": machine.get("machine_name") or machine_id,
            "display_name": machine.get("display_name"),
            "total_points": sum(e["points"] for e in earned_list),
            "earned": earned_list,
            "locked": locked_list,
        }

    # Hard cap on how many of a machine's most-recent user messages get
    # analyzed per request -- mirrors the safety-cap pattern used elsewhere
    # in this file (e.g. MAX_SESSION_DURATION_SECONDS in the API layer):
    # tone analysis runs live on every request rather than being
    # pre-computed/stored, so a machine with tens of thousands of messages
    # shouldn't be able to make this endpoint arbitrarily slow.
    TONE_ANALYSIS_MESSAGE_LIMIT = 500

    async def get_tone_analysis(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-user "tone of conversation" scoreboard: profanity, politeness,
        spelling, and grammar signals derived live from each machine's own
        chat prompts (role='user' rows in `messages`) -- never the
        assistant's replies. See src/utils/tone_analysis.py for how each
        signal is computed and why (fully local, no network/Java deps).

        `machine_id` follows the same convention as every other page/
        endpoint (None/''/'all' -> every machine; a single id or
        comma-separated Team/Group scope -> just those machines).
        """
        all_machines = await self.get_all_machines()
        if machine_id and machine_id != "all":
            scoped_ids = {m.strip() for m in machine_id.split(",") if m.strip()}
            machines = [m for m in all_machines if m["machine_id"] in scoped_ids]
        else:
            machines = all_machines

        # Fetch the raw message text for every machine first (cheap, I/O
        # bound) while still on the event loop, then hand the actual
        # analysis off to a worker thread. analyze_text()/aggregate() are
        # synchronous, CPU-bound (regex + dictionary lookups per word) and
        # can take multiple seconds across hundreds of messages -- running
        # that inline on the event loop would stall every other concurrent
        # request on this single-worker server (this previously caused
        # unrelated endpoints like /api/groups to appear "stuck" while a
        # tone-analysis request was in flight).
        contents_by_machine: Dict[str, List[str]] = {}
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            for machine in machines:
                mid = machine["machine_id"]
                async with db.execute(
                    """SELECT content FROM messages
                       WHERE machine_id = ? AND role = 'user'
                       ORDER BY timestamp DESC LIMIT ?""",
                    (mid, self.TONE_ANALYSIS_MESSAGE_LIMIT)
                ) as cursor:
                    rows = await cursor.fetchall()
                if rows:
                    contents_by_machine[mid] = [row["content"] for row in rows]

        entries = await asyncio.to_thread(
            self._compute_tone_entries, machines, contents_by_machine
        )

        # Rank "best tone" first: most polite, fewest profanity/rude hits,
        # fewest spelling/grammar issues as tiebreakers.
        entries.sort(
            key=lambda e: (
                -e["politeness_rate_pct"],
                e["profanity_rate_pct"],
                e["rude_phrase_hits"],
                e["spelling_error_rate_pct"],
                e["grammar_issues"],
            )
        )
        for idx, entry in enumerate(entries, start=1):
            entry["rank"] = idx

        return {"entries": entries}

    @staticmethod
    def _compute_tone_entries(
        machines: List[Dict[str, Any]], contents_by_machine: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        """The synchronous, CPU-bound half of get_tone_analysis(). Runs off
        the event loop via asyncio.to_thread - see the caller's comment."""
        entries = []
        for machine in machines:
            mid = machine["machine_id"]
            contents = contents_by_machine.get(mid)
            if not contents:
                continue  # nothing to analyze - skip, same as leaderboard

            results = [tone_analysis.analyze_text(text) for text in contents]
            summary = tone_analysis.aggregate(results)
            tone = tone_analysis.overall_tone_label(summary)

            entries.append({
                "machine_id": mid,
                "machine_name": machine.get("machine_name") or mid,
                "logged_in_user": machine.get("logged_in_user"),
                "display_name": machine.get("display_name"),
                "os_type": machine.get("os_type"),
                "group_id": machine.get("group_id"),
                "group_name": machine.get("group_name"),
                "tone_label": tone["label"],
                "tone_emoji": tone["emoji"],
                **summary,
            })
        return entries

    # Cap on how many example messages are returned per category in the
    # detail drill-down (profanity/rude/misspelling/grammar) - keeps the
    # response small and avoids dumping someone's entire message history.
    TONE_DETAIL_EXAMPLE_LIMIT = 25

    async def get_tone_details(self, machine_id: str) -> Dict[str, Any]:
        """Drill-down behind a single machine's tone summary: the actual
        flagged messages/words, for "why was I scored this way" transparency.
        Only ever called for one machine at a time (the caller-selected
        row in the tone-analysis table) - non-admins can only ever end up
        requesting their own machine_id, since `enforce_auth`
        already forces that upstream."""
        machine = await self.get_machine_by_id(machine_id)
        if not machine:
            return {"machine_id": machine_id, "messages_analyzed": 0, "categories": {}}

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT content, timestamp FROM messages
                   WHERE machine_id = ? AND role = 'user'
                   ORDER BY timestamp DESC LIMIT ?""",
                (machine_id, self.TONE_ANALYSIS_MESSAGE_LIMIT)
            ) as cursor:
                rows = await cursor.fetchall()

        messages = [(row["content"], row["timestamp"]) for row in rows]

        # CPU-bound (regex + dictionary lookups per message) - offload to a
        # worker thread so it can't block the event loop, same reasoning as
        # get_tone_analysis().
        details = await asyncio.to_thread(
            self._compute_tone_details, messages, self.TONE_DETAIL_EXAMPLE_LIMIT
        )
        details["machine_id"] = machine_id
        details["machine_name"] = machine.get("machine_name") or machine_id
        details["display_name"] = machine.get("display_name")
        return details

    @staticmethod
    def _compute_tone_details(
        messages: List[tuple], example_limit: int
    ) -> Dict[str, Any]:
        """The synchronous, CPU-bound half of get_tone_details(). Runs off
        the event loop via asyncio.to_thread - see the caller's comment."""
        def _snippet(text: str, max_len: int = 220) -> str:
            text = " ".join(text.split())  # collapse newlines/whitespace
            return text if len(text) <= max_len else text[:max_len].rstrip() + "…"

        profanity_examples = []
        rude_examples = []
        polite_examples = []
        misspelling_examples = []
        grammar_examples = []

        for content, timestamp in messages:
            result = tone_analysis.analyze_text(content)
            snippet = _snippet(content)

            if result.profanity_hits and len(profanity_examples) < example_limit:
                profanity_examples.append({"text": snippet, "timestamp": timestamp})
            if result.rude_hits and len(rude_examples) < example_limit:
                rude_examples.append({"text": snippet, "timestamp": timestamp})
            if result.polite_hits and len(polite_examples) < example_limit:
                polite_examples.append({"text": snippet, "timestamp": timestamp})
            if result.misspelled_words and len(misspelling_examples) < example_limit:
                misspelling_examples.append({
                    "words": result.misspelled_words,
                    "text": snippet,
                    "timestamp": timestamp,
                })
            if result.grammar_labels and len(grammar_examples) < example_limit:
                grammar_examples.append({
                    "issues": result.grammar_labels,
                    "text": snippet,
                    "timestamp": timestamp,
                })

        return {
            "messages_analyzed": len(messages),
            "categories": {
                "profanity": profanity_examples,
                "rude": rude_examples,
                "polite": polite_examples,
                "misspellings": misspelling_examples,
                "grammar": grammar_examples,
            },
        }

    # ============================================================================
    # AI USAGE OPTIMIZATION METHODS
    # ============================================================================

    async def prompt_analysis_exists(self, session_id: str, prompt_text: str) -> bool:
        """Dedup guard: client agents may re-parse the same JSONL file across
        restarts, so skip re-inserting analysis for a prompt already recorded
        for this session."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT id FROM prompt_analysis WHERE session_id = ? AND prompt_text = ? LIMIT 1",
                (session_id, prompt_text)
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def create_prompt_analysis(self, session_id: str, prompt_text: str, **metrics):
        """Create a prompt quality analysis record."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO prompt_analysis
                   (session_id, prompt_text, quality_score, clarity_score, specificity_score,
                    context_efficiency_score, token_efficiency, suggestions, improved_prompt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, prompt_text, metrics.get('quality_score'), metrics.get('clarity_score'),
                 metrics.get('specificity_score'), metrics.get('context_efficiency_score'),
                 metrics.get('token_efficiency'), metrics.get('suggestions'), metrics.get('improved_prompt'))
            )
            await db.commit()
            return cursor.lastrowid

    async def get_prompt_analysis(self, session_id: str = None, min_quality: float = None, limit: int = 50, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get prompt analysis records."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM prompt_analysis WHERE 1=1"
            params = []
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            if min_quality:
                query += " AND quality_score >= ?"
                params.append(min_quality)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY analyzed_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_context_insight(self, session_id: str, **metrics):
        """Create context optimization insight."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO context_insights
                   (session_id, total_context_tokens, unique_context_tokens, redundant_tokens,
                    optimal_tokens, waste_percentage, recommendations, estimated_cost_savings)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, metrics.get('total_context_tokens'), metrics.get('unique_context_tokens'),
                 metrics.get('redundant_tokens'), metrics.get('optimal_tokens'),
                 metrics.get('waste_percentage'), metrics.get('recommendations'),
                 metrics.get('estimated_cost_savings'))
            )
            await db.commit()
            return cursor.lastrowid

    async def get_context_insights(self, session_id: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get context optimization insights."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM context_insights WHERE analyzed_at >= datetime('now', '-' || ? || ' days')"
            params = [days]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY analyzed_at DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def cache_analytics_exists(self, session_id: str) -> bool:
        """Dedup guard: cache totals are computed once per completed session,
        so skip if this session's summary row was already recorded."""
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT id FROM cache_analytics WHERE session_id = ? LIMIT 1",
                (session_id,)
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def create_cache_analytics(self, session_id: str, **metrics):
        """Create cache analytics record."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO cache_analytics
                   (session_id, cache_read_tokens, cache_write_tokens, cache_hit_rate,
                    cache_efficiency_score, cost_saved_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, metrics.get('cache_read_tokens'), metrics.get('cache_write_tokens'),
                 metrics.get('cache_hit_rate'), metrics.get('cache_efficiency_score'),
                 metrics.get('cost_saved_usd'))
            )
            await db.commit()
            return cursor.lastrowid

    async def get_cache_analytics(self, session_id: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get cache analytics."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM cache_analytics WHERE analyzed_at >= datetime('now', '-' || ? || ' days')"
            params = [days]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY analyzed_at DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_model_performance(self, model_name: str = None, task_type: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get model performance metrics, optionally scoped to one machine."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM model_performance WHERE date >= date('now', '-' || ? || ' days')"
            params = [days]
            if model_name:
                query += " AND model_name = ?"
                params.append(model_name)
            if task_type:
                query += " AND task_type = ?"
                params.append(task_type)
            if machine_id and machine_id != "all":
                frag, fparams = self._machine_scope_clause(machine_id, "machine_id")
                query += frag
                params.extend(fparams)
            query += " ORDER BY date DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def upsert_model_performance(self, model_name: str, task_type: str, date_val,
                                        response_time_ms: float = None, success: bool = True,
                                        token_efficiency: float = None, cost: float = 0.0,
                                        machine_id: str = None):
        """Record one real task outcome into that day's (machine_id, model,
        task_type) rolling aggregate. Averages are updated incrementally
        (weighted by the existing total_tasks count) rather than overwritten,
        so this is safe to call once per completed session without losing
        prior data for the same day. machine_id is required to keep this
        data machine-specific; legacy rows reported before this field
        existed have machine_id = NULL and only surface in the "All
        Machines" aggregate view."""
        async with self.get_connection() as db:
            # Serialize the read/modify/write, including legacy NULL-machine keys.
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT total_tasks, average_response_time_ms, success_rate,
                          average_token_efficiency, cost_per_task
                   FROM model_performance
                   WHERE model_name = ? AND task_type IS ? AND date = ? AND machine_id IS ?""",
                (model_name, task_type, date_val, machine_id)
            ) as cursor:
                existing = await cursor.fetchone()

            success_pct = 100.0 if success else 0.0

            if existing:
                n = existing[0] or 0
                new_n = n + 1

                def _weighted_avg(old_val, new_val):
                    if old_val is None:
                        return new_val
                    return ((old_val * n) + new_val) / new_n

                new_response_time = _weighted_avg(existing[1], response_time_ms) if response_time_ms is not None else existing[1]
                new_success_rate = _weighted_avg(existing[2], success_pct)
                new_token_eff = _weighted_avg(existing[3], token_efficiency) if token_efficiency is not None else existing[3]
                new_cost_per_task = _weighted_avg(existing[4], cost)

                await db.execute(
                    """UPDATE model_performance
                       SET total_tasks = ?, average_response_time_ms = ?, success_rate = ?,
                           average_token_efficiency = ?, cost_per_task = ?
                       WHERE model_name = ? AND task_type IS ? AND date = ? AND machine_id IS ?""",
                    (new_n, new_response_time, new_success_rate, new_token_eff, new_cost_per_task,
                     model_name, task_type, date_val, machine_id)
                )
            else:
                await db.execute(
                    """INSERT INTO model_performance
                       (machine_id, model_name, task_type, average_response_time_ms, success_rate,
                        average_token_efficiency, cost_per_task, total_tasks, date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (machine_id, model_name, task_type, response_time_ms, success_pct,
                     token_efficiency, cost, date_val)
                )
            await db.commit()

    async def get_token_waste_patterns(self, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get token waste patterns, optionally scoped to one machine or a
        comma-separated list of machines (a pattern's sessions_affected JSON
        array is checked for overlap with those machines' own session_ids)."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM token_waste_patterns WHERE detected_at >= datetime('now', '-' || ? || ' days') ORDER BY total_wasted_tokens DESC",
                (days,)
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

            if not machine_id or machine_id == "all":
                return rows

            frag, fparams = self._machine_scope_clause(machine_id)
            async with db.execute(
                f"SELECT session_id FROM sessions WHERE 1=1{frag}", fparams
            ) as cursor:
                machine_sessions = {row[0] for row in await cursor.fetchall()}

        filtered = []
        for row in rows:
            try:
                affected = set(json.loads(row.get("sessions_affected") or "[]"))
            except (TypeError, ValueError):
                affected = set()
            if affected & machine_sessions:
                filtered.append(row)
        return filtered

    # ============================================================================
    # CODE ANALYTICS METHODS
    # ============================================================================

    async def code_modification_exists(self, session_id: str, file_path: str, timestamp: str) -> bool:
        """Check whether a modification for this exact tool call was already recorded.

        Client agents may re-parse the same JSONL session file across
        restarts (in-memory dedup state isn't persisted), so this natural
        key (session + file + the tool call's own timestamp) prevents
        duplicate rows from piling up on every resync.
        """
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT id FROM code_modifications WHERE session_id = ? AND file_path = ? AND timestamp = ? LIMIT 1",
                (session_id, file_path, timestamp)
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def create_code_modification(self, session_id: str, file_path: str, **metrics):
        """Log a code modification."""
        async with self.get_connection() as db:
            if metrics.get('timestamp'):
                cursor = await db.execute(
                    """INSERT INTO code_modifications
                       (session_id, file_path, language, modification_type, lines_added, lines_deleted,
                        lines_modified, complexity_before, complexity_after, ai_suggestion_accepted,
                        modification_quality_score, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, file_path, metrics.get('language'), metrics.get('modification_type'),
                     metrics.get('lines_added'), metrics.get('lines_deleted'), metrics.get('lines_modified'),
                     metrics.get('complexity_before'), metrics.get('complexity_after'),
                     metrics.get('ai_suggestion_accepted'), metrics.get('modification_quality_score'),
                     metrics.get('timestamp'))
                )
            else:
                cursor = await db.execute(
                    """INSERT INTO code_modifications
                       (session_id, file_path, language, modification_type, lines_added, lines_deleted,
                        lines_modified, complexity_before, complexity_after, ai_suggestion_accepted,
                        modification_quality_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, file_path, metrics.get('language'), metrics.get('modification_type'),
                     metrics.get('lines_added'), metrics.get('lines_deleted'), metrics.get('lines_modified'),
                     metrics.get('complexity_before'), metrics.get('complexity_after'),
                     metrics.get('ai_suggestion_accepted'), metrics.get('modification_quality_score'))
                )
            await db.commit()
            return cursor.lastrowid

    async def get_code_modifications(self, session_id: str = None, language: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get code modifications."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM code_modifications WHERE timestamp >= datetime('now', '-' || ? || ' days')"
            params = [days]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            if language:
                query += " AND language = ?"
                params.append(language)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY timestamp DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_refactoring_impact(self, days: int = 30, machine_id: str = None, limit: int = 15) -> List[Dict[str, Any]]:
        """Derive real refactoring-impact rows from code_modifications' own
        complexity_before/complexity_after fields (computed client-side from
        the actual before/after code of each Edit/MultiEdit) — no separate
        fabricated 'refactoring_analysis' table needed. Only modifications
        where both values are known and complexity genuinely dropped are
        returned, sorted by biggest real improvement first."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = """SELECT session_id, file_path, language, modification_type,
                              lines_added, lines_deleted, complexity_before, complexity_after,
                              modification_quality_score, timestamp
                       FROM code_modifications
                       WHERE timestamp >= datetime('now', '-' || ? || ' days')
                         AND complexity_before IS NOT NULL AND complexity_after IS NOT NULL
                         AND complexity_before > complexity_after"""
            params = [days]
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            async with db.execute(query, params) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

        results = []
        for row in rows:
            before, after = row["complexity_before"], row["complexity_after"]
            improvement = before - after
            row["complexity_improvement"] = round(improvement, 1)
            row["improvement_percentage"] = round(100.0 * improvement / before, 1) if before else 0
            results.append(row)
        results.sort(key=lambda r: r["improvement_percentage"], reverse=True)
        return results[:limit]

    async def get_language_stats(self, user_id: str) -> List[Dict[str, Any]]:
        """Get language usage statistics for a user."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM language_stats WHERE user_id = ? ORDER BY proficiency_score DESC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def update_language_stats(self, user_id: str, language: str, **updates):
        """Update language statistics for a user."""
        async with self.get_connection() as db:
            # Check if record exists
            async with db.execute(
                "SELECT id FROM language_stats WHERE user_id = ? AND language = ?",
                (user_id, language)
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                # Update existing
                set_clauses = [f"{k} = ?" for k in updates.keys()]
                values = list(updates.values()) + [user_id, language]
                await db.execute(
                    f"UPDATE language_stats SET {', '.join(set_clauses)} WHERE user_id = ? AND language = ?",
                    values
                )
            else:
                # Create new
                columns = ['user_id', 'language'] + list(updates.keys())
                placeholders = ['?'] * len(columns)
                values = [user_id, language] + list(updates.values())
                await db.execute(
                    f"INSERT INTO language_stats ({', '.join(columns)}) VALUES ({', '.join(placeholders)})",
                    values
                )
            await db.commit()

    async def recompute_language_stats_for_session(self, session_id: str):
        """Recompute language_stats rows touched by a session's code modifications.

        Recomputes (rather than increments) directly from code_modifications
        joined through sessions -> machines to resolve the logged_in_user, so
        it's naturally idempotent if a session's modifications are ever
        re-synced (no double-counting risk from incremental counters).
        """
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT m.logged_in_user FROM sessions s "
                "JOIN machines m ON s.machine_id = m.machine_id "
                "WHERE s.session_id = ? LIMIT 1",
                (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
            user_id = row[0] if row and row[0] else None
            if not user_id:
                return

            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT cm.language,
                          COUNT(*) as files_modified,
                          COALESCE(SUM(cm.lines_added + cm.lines_deleted), 0) as lines_written,
                          COUNT(DISTINCT cm.session_id) as sessions_count,
                          MAX(cm.timestamp) as last_used
                   FROM code_modifications cm
                   JOIN sessions s ON cm.session_id = s.session_id
                   JOIN machines mach ON s.machine_id = mach.machine_id
                   WHERE mach.logged_in_user = ? AND cm.language IS NOT NULL
                   GROUP BY cm.language""",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()

            for r in rows:
                await self.update_language_stats(
                    user_id, r['language'],
                    files_modified=r['files_modified'],
                    lines_written=r['lines_written'],
                    sessions_count=r['sessions_count'],
                    last_used=r['last_used']
                )

    async def create_code_quality_metrics(self, session_id: str, **metrics):
        """Create code quality metrics record."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO code_quality_metrics
                   (session_id, file_path, language, test_coverage_percentage, cyclomatic_complexity,
                    maintainability_index, code_duplication_percentage, security_issues_count,
                    performance_issues_count, best_practices_score, overall_quality_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, metrics.get('file_path'), metrics.get('language'),
                 metrics.get('test_coverage_percentage'), metrics.get('cyclomatic_complexity'),
                 metrics.get('maintainability_index'), metrics.get('code_duplication_percentage'),
                 metrics.get('security_issues_count'), metrics.get('performance_issues_count'),
                 metrics.get('best_practices_score'), metrics.get('overall_quality_score'))
            )
            await db.commit()
            return cursor.lastrowid

    async def get_code_quality_metrics(self, session_id: str = None, language: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get code quality metrics."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM code_quality_metrics WHERE analyzed_at >= datetime('now', '-' || ? || ' days')"
            params = [days]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            if language:
                query += " AND language = ?"
                params.append(language)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY analyzed_at DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_ai_suggestion(self, session_id: str, suggestion_type: str, suggestion_text: str, **metrics):
        """Log an AI suggestion."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO ai_suggestions
                   (session_id, suggestion_type, suggestion_text, context_text, was_accepted,
                    was_modified, acceptance_time_seconds, file_path, language, code_before, code_after)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, suggestion_type, suggestion_text, metrics.get('context_text'),
                 metrics.get('was_accepted'), metrics.get('was_modified'),
                 metrics.get('acceptance_time_seconds'), metrics.get('file_path'),
                 metrics.get('language'), metrics.get('code_before'), metrics.get('code_after'))
            )
            await db.commit()
            return cursor.lastrowid

    async def get_ai_suggestions(self, session_id: str = None, suggestion_type: str = None, days: int = 30, machine_id: str = None) -> List[Dict[str, Any]]:
        """Get AI suggestions."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM ai_suggestions WHERE timestamp >= datetime('now', '-' || ? || ' days')"
            params = [days]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            if suggestion_type:
                query += " AND suggestion_type = ?"
                params.append(suggestion_type)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            query += " ORDER BY timestamp DESC"
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_ai_suggestion_acceptance_rate(self, days: int = 30, suggestion_type: str = None, machine_id: str = None) -> Dict[str, Any]:
        """Calculate AI suggestion acceptance rate."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = """SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN was_accepted = 1 THEN 1 ELSE 0 END) as accepted,
                        SUM(CASE WHEN was_modified = 1 THEN 1 ELSE 0 END) as modified,
                        AVG(acceptance_time_seconds) as avg_acceptance_time
                       FROM ai_suggestions
                       WHERE timestamp >= datetime('now', '-' || ? || ' days')"""
            params = [days]
            if suggestion_type:
                query += " AND suggestion_type = ?"
                params.append(suggestion_type)
            frag, fparams = self._machine_scope_subquery_clause(machine_id)
            query += frag
            params.extend(fparams)
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                if row:
                    result = dict(row)
                    result['acceptance_rate'] = (result['accepted'] / max(result['total'], 1)) * 100 if result['total'] else 0
                    result['modification_rate'] = (result['modified'] / max(result['accepted'], 1)) * 100 if result['accepted'] else 0
                    return result
                return {"total": 0, "accepted": 0, "acceptance_rate": 0, "modification_rate": 0, "avg_acceptance_time": 0}

    async def get_project_health(self, working_directory: str) -> Optional[Dict[str, Any]]:
        """Get project health metrics."""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM project_health WHERE working_directory = ?",
                (working_directory,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
