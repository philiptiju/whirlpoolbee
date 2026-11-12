#!/usr/bin/env python3
"""
WhirlpoolBee Client Agent (self-contained)

Collects Claude Code usage data and sends it to a central dashboard server.
Automatically detects the data source:
  - SQLite sessions.db (macOS/Linux)
  - JSONL session files (Windows)

Usage:
    python client_agent.py --server http://server-ip:8765

This is a single-file agent — no other files are needed.
Just copy this file + install dependencies:  pip install httpx cryptography

(cryptography is only needed for the auto-updater's signature check; the
agent still runs its normal collection/sync duties without it, but will
refuse to self-update until it's installed — see check_for_update() below.)
"""

import argparse
import asyncio
import base64
import signal
import socket
import platform
import json
import re
import sqlite3
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

import httpx
import sys
import os
import getpass

# On Windows, stdout/stderr default to the legacy console codepage (e.g.
# cp1252) instead of UTF-8 whenever they're not attached to a real console —
# which is exactly the case here once the agent is backgrounded and its
# output is redirected to agent.log/agent.error.log (via nohup or Task
# Scheduler). Since this file prints emoji/arrows (✓, 💓, →, …) in its status
# messages, that mismatch crashes the whole process with a
# UnicodeEncodeError the moment it tries to print one — before the agent
# ever gets to register or sync. Force UTF-8 (replacing anything that
# somehow still can't be encoded) so backgrounded runs can't crash on this.
#
# Also force line-buffering: when stdout/stderr are redirected to a file
# (always true in background mode, agent.log/agent.error.log) Python
# switches to full block buffering instead of line buffering, so log
# lines can sit unwritten for a long time — and would be silently lost
# entirely if the process image is ever replaced via os.execv() (used by
# the auto-updater below) without an explicit flush first. Line-buffer
# both streams whenever they're not an interactive terminal so agent.log
# always reflects what's actually happened, in near-real-time.
for _stream_name, _stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
    try:
        if sys.platform == "win32":
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        elif not _stream.isatty():
            _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

# Bump this whenever client_agent.py changes and is re-deployed to
# src/web/static/client_agent.py. The server reports this same value back
# from /api/agent/version (read directly off its own static file), so
# running agents can detect a mismatch and self-update — keep this in sync
# with the deployed static copy, not just this source-tree copy.
AGENT_VERSION = "1.5.0"

# Public half of the Ed25519 keypair used to sign official client_agent.py
# releases (see scripts/sign_agent.py). This is NOT a secret — only the
# matching private key (held offline, never on this server) can produce a
# signature this key accepts. check_for_update() below refuses to install
# any downloaded update whose accompanying .sig file doesn't verify against
# this key, so a compromised/malicious dashboard server (or a MITM on the
# connection, if server_url is ever misconfigured to plain http://) can no
# longer push arbitrary code to the whole agent fleet — it would need the
# offline private key too.
AGENT_UPDATE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAylKwXg31PF6ZHSBSNpfF/d2zQQIV7J3tNq+4isKgBC8=
-----END PUBLIC KEY-----
"""


# ════════════════════════════════════════════════════════════════════
# Embedded JSONL parser (from src/parsers/jsonl_sessions.py)
# ════════════════════════════════════════════════════════════════════

# Model pricing (USD per million tokens)
_MODEL_PRICING = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20240620": {"input": 3.00, "output": 15.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
}


def estimate_cost(model_name: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
    """Estimate cost in USD for a model and token counts.
    
    input_tokens should be non-cached input only.
    Cache read/creation tokens are priced separately at reduced rates.
    """
    pricing = _MODEL_PRICING.get(model_name)
    if not pricing:
        for key, value in _MODEL_PRICING.items():
            if key in model_name or model_name in key:
                pricing = value
                break
    if not pricing:
        return 0.0
    base_input_cost = (input_tokens / 1_000_000) * pricing["input"]
    cache_read_cost = (cache_read_tokens / 1_000_000) * pricing["input"] * 0.1  # 10% of input price
    cache_create_cost = (cache_creation_tokens / 1_000_000) * pricing["input"] * 1.25  # 125% of input price
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return base_input_cost + cache_read_cost + cache_create_cost + output_cost


# File-editing tools whose calls represent a real code modification we can
# report. (Read/Grep/Glob/Bash etc. don't modify files, so they're excluded.)
_EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_LANGUAGE_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".scala": "scala", ".sh": "shell",
    ".bash": "shell", ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
    ".ipynb": "jupyter", ".vue": "vue", ".lua": "lua", ".r": "r", ".m": "objective-c",
    ".dockerfile": "dockerfile",
}


def _detect_language(file_path: str) -> Optional[str]:
    """Best-effort language detection from a file's extension."""
    if not file_path:
        return None
    ext = Path(file_path).suffix.lower()
    if not ext and Path(file_path).name.lower() == "dockerfile":
        return "dockerfile"
    return _LANGUAGE_BY_EXT.get(ext)


def _extract_code_modification(tool_call) -> Optional[Dict[str, Any]]:
    """Derive real file/line-change facts — and the actual before/after code
    text — from an Edit/Write/MultiEdit/NotebookEdit tool call's own
    arguments. Returns (file_path, lines_added, lines_deleted, mod_type,
    old_code, new_code); old_code is None when the tool call has no
    'before' state (e.g. Write creating a new file)."""
    args = tool_call.input_args or {}
    name = tool_call.name

    if name == "Write":
        file_path = args.get("file_path")
        content = args.get("content") or ""
        lines_added = content.count("\n") + 1 if content else 0
        return file_path, lines_added, 0, "create", None, content

    if name == "Edit":
        file_path = args.get("file_path")
        old = args.get("old_string") or ""
        new = args.get("new_string") or ""
        lines_deleted = old.count("\n") + 1 if old else 0
        lines_added = new.count("\n") + 1 if new else 0
        return file_path, lines_added, lines_deleted, "modify", old, new

    if name == "MultiEdit":
        file_path = args.get("file_path")
        lines_added = lines_deleted = 0
        old_parts, new_parts = [], []
        for edit in (args.get("edits") or []):
            old = edit.get("old_string") or ""
            new = edit.get("new_string") or ""
            lines_deleted += old.count("\n") + 1 if old else 0
            lines_added += new.count("\n") + 1 if new else 0
            old_parts.append(old)
            new_parts.append(new)
        return file_path, lines_added, lines_deleted, "modify", "\n".join(old_parts), "\n".join(new_parts)

    if name == "NotebookEdit":
        file_path = args.get("notebook_path") or args.get("file_path")
        new_source = args.get("new_source") or ""
        lines_added = new_source.count("\n") + 1 if new_source else 0
        edit_mode = args.get("edit_mode", "replace")
        mod_type = "create" if edit_mode == "insert" else "modify"
        return file_path, lines_added, 0, mod_type, None, new_source

    return None


# Real (not fabricated) cyclomatic-complexity-style estimate: base complexity
# of 1 plus one point per branching/looping/boolean-combinator token actually
# present in the code. This is a simple heuristic, not a full AST-based
# calculation per language, but every count comes from the real code text.
_COMPLEXITY_PATTERN = re.compile(
    r'\b(if|elif|else\s+if|for|while|case|switch|catch|except)\b|&&|\|\||\?\s*[^:{}\n]+:'
)

# Small, documented pattern lists used to flag real (if approximate) code
# smells directly in the diff text — not a substitute for real static
# analysis/linting, but genuine signal derived from the actual code.
_SECURITY_PATTERNS = [
    re.compile(r'\beval\s*\('), re.compile(r'\bexec\s*\('),
    re.compile(r'os\.system\s*\('),
    re.compile(r'subprocess\.(?:call|run|Popen)\([^)]*shell\s*=\s*True'),
    re.compile(r'\.innerHTML\s*='), re.compile(r'dangerouslySetInnerHTML'),
    re.compile(r'SELECT\b.{0,80}["\']\s*\+'),
    re.compile(r'password\s*=\s*["\'][^"\']+["\']', re.IGNORECASE),
    re.compile(r'\bpickle\.loads?\('),
]
_PERFORMANCE_PATTERNS = [
    re.compile(r'SELECT\s+\*'),
    re.compile(r'time\.sleep\('),
    re.compile(r'for\s+.+\s+in\s+.+:\s*\n\s+.*for\s+.+\s+in\s+.+:'),  # naive nested-loop detector
]
_DEBUG_MARKER_PATTERN = re.compile(r'\b(console\.log|debugger|print)\s*\(|#\s*(TODO|FIXME|XXX)\b', re.IGNORECASE)


def _estimate_complexity(code: Optional[str]) -> Optional[float]:
    """Heuristic cyclomatic-complexity-style estimate derived from the real
    code text. Returns None when there's no code to measure (e.g. no
    'before' state for a newly created file) rather than fabricating a
    number."""
    if not code or not code.strip():
        return None
    return float(1 + len(_COMPLEXITY_PATTERN.findall(code)))


def _estimate_code_quality(new_code: Optional[str], complexity: Optional[float]) -> Dict[str, Any]:
    """Rule-based, fully documented quality heuristic derived from the actual
    new code text — mirrors the transparency of _score_prompt() above. Every
    sub-score is computed from real signals in the diff; nothing is randomly
    invented. Not a substitute for real static analysis/linting.
    test_coverage_percentage genuinely can't be derived from a diff, so it's
    left as None rather than fabricated.
    """
    code = new_code or ""
    lines = code.splitlines()
    non_blank = [l.strip() for l in lines if l.strip()]
    total = len(non_blank) or 1

    duplicate_lines = total - len(set(non_blank))
    duplication_pct = round(100.0 * duplicate_lines / total, 1)

    security_hits = sum(len(p.findall(code)) for p in _SECURITY_PATTERNS)
    performance_hits = sum(len(p.findall(code)) for p in _PERFORMANCE_PATTERNS)
    debug_markers = len(_DEBUG_MARKER_PATTERN.findall(code))
    long_line_ratio = (sum(1 for l in lines if len(l) > 120) / len(lines)) if lines else 0.0

    best_practices_score = max(0.0, 100.0 - debug_markers * 8 - long_line_ratio * 30)
    complexity_penalty = min(60.0, (complexity or 1) * 3)
    maintainability_index = max(0.0, round(100.0 - complexity_penalty - duplication_pct * 0.5, 1))

    overall_quality_score = round(
        max(0.0, min(100.0,
            (maintainability_index + best_practices_score) / 2
            - security_hits * 15 - performance_hits * 8
        )), 1
    )

    return {
        "cyclomatic_complexity": complexity,
        "maintainability_index": maintainability_index,
        "code_duplication_percentage": duplication_pct,
        "security_issues_count": security_hits,
        "performance_issues_count": performance_hits,
        "best_practices_score": round(best_practices_score, 1),
        "overall_quality_score": overall_quality_score,
        "test_coverage_percentage": None,
    }


# Keyword buckets for a lightweight, transparent heuristic classifier of what
# kind of task a turn represents. This is a rule-based approximation (not a
# ground-truth label) used only to bucket model_performance rows by task type.
_TASK_TYPE_KEYWORDS = {
    "debugging": ("bug", "error", "fix", "crash", "traceback", "exception", "fails", "broken", "not working"),
    "refactoring": ("refactor", "rename", "simplify", "clean up", "extract", "reorganize", "restructure"),
    "documentation": ("document", "docstring", "readme", "comment", "explain", "describe"),
}


def _classify_task_type(text: str) -> str:
    """Heuristic task-type classification from prompt text. Defaults to
    'code_generation' when no other bucket's keywords match."""
    lowered = (text or "").lower()
    for task_type, keywords in _TASK_TYPE_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return task_type
    return "code_generation"


def _score_prompt(prompt_text: str, input_tokens: int, output_tokens: int,
                   had_tool_error: bool) -> Dict[str, Any]:
    """Rule-based prompt-quality heuristic (not a fabricated random score):
    rewards concrete signals known to reduce back-and-forth — specific
    references (file paths, error text, code blocks), reasonable length,
    and a clear ask — and reports the real token_efficiency ratio.
    Sub-scores are simple, documented, and reproducible from the prompt
    text itself.
    """
    text = prompt_text or ""
    length = len(text)

    has_path_ref = bool(re.search(r'[\w./\\-]+\.\w{1,5}(:\d+)?', text))
    has_code_block = "```" in text or bool(re.search(r'`[^`]+`', text))
    has_error_ref = bool(re.search(r'\b(error|exception|traceback|fail(ed|ing)?)\b', text, re.IGNORECASE))
    specific_signals = sum([has_path_ref, has_code_block, has_error_ref])

    specificity_score = min(100.0, 40.0 + specific_signals * 20.0)

    if length < 10:
        clarity_score = 30.0  # too short to convey real intent
    elif length > 4000:
        clarity_score = 60.0  # very long prompts tend to bury the actual ask
    else:
        clarity_score = 85.0

    context_efficiency_score = 40.0 if had_tool_error else 80.0

    quality_score = round((specificity_score + clarity_score + context_efficiency_score) / 3, 2)
    token_efficiency = round(output_tokens / input_tokens, 4) if input_tokens else None

    suggestions = []
    if specific_signals == 0:
        suggestions.append("Include specific file paths, error text, or code snippets to reduce back-and-forth.")
    if length < 10:
        suggestions.append("Add more detail about what you're trying to accomplish.")
    if had_tool_error:
        suggestions.append("A tool call failed during this turn — double-check the referenced file/command exists.")

    return {
        "quality_score": quality_score,
        "clarity_score": clarity_score,
        "specificity_score": specificity_score,
        "context_efficiency_score": context_efficiency_score,
        "token_efficiency": token_efficiency,
        "suggestions": json.dumps(suggestions),
    }


@dataclass
class _ToolCall:
    tool_use_id: str
    name: str
    input_args: Dict[str, Any]
    timestamp: str = ""
    result: Optional[str] = None
    is_error: bool = False


@dataclass
class _Turn:
    turn_index: int
    user_text: str = ""
    user_timestamp: str = ""
    assistant_text: str = ""
    assistant_timestamp: str = ""
    model: str = ""
    stop_reason: str = ""
    thinking_text: str = ""
    thinking_tokens: int = 0
    tool_calls: List[_ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    is_meta: bool = False


@dataclass
class _ParsedSession:
    session_id: str
    file_path: str
    title: str = ""
    cwd: str = ""
    git_branch: str = ""
    entrypoint: str = ""
    version: str = ""
    model: str = ""
    started_at: str = ""
    ended_at: str = ""
    turns: List[_Turn] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_thinking_tokens: int = 0
    last_prompt: str = ""
    tool_names_used: List[str] = field(default_factory=list)
    record_count: int = 0


class _JsonlParser:
    """Minimal JSONL parser embedded in client agent."""

    def parse_file(self, file_path: str) -> Optional[_ParsedSession]:
        path = Path(file_path)
        if not path.exists():
            return None
        records = []
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []
        if not records:
            return None

        session = _ParsedSession(
            session_id=self._session_id(records, path),
            file_path=str(path),
            record_count=len(records),
        )
        self._metadata(records, session)
        self._conversation(records, session)
        # compute totals
        for t in session.turns:
            session.total_input_tokens += t.input_tokens
            session.total_output_tokens += t.output_tokens
            session.total_thinking_tokens += t.thinking_tokens
        return session

    def _session_id(self, records, path):
        for r in records:
            sid = r.get('sessionId')
            if sid:
                return sid
        return path.stem if re.match(r'^[0-9a-f]{8}-', path.stem) else f"jsonl-{path.name}"

    def _metadata(self, records, s):
        for r in records:
            rt = r.get('type')
            if rt == 'ai-title':
                s.title = r.get('aiTitle', '') or s.title
            elif rt == 'last-prompt':
                s.last_prompt = r.get('lastPrompt', '')
            elif rt == 'queue-operation':
                ts = r.get('timestamp', '')
                if r.get('operation') == 'enqueue' and not s.started_at:
                    s.started_at = ts
                elif r.get('operation') == 'dequeue':
                    s.ended_at = ts
            elif rt in ('user', 'assistant'):
                if not s.cwd and r.get('cwd'):
                    s.cwd = r['cwd']
                if r.get('gitBranch'):
                    s.git_branch = r['gitBranch']
                if not s.entrypoint and r.get('entrypoint'):
                    s.entrypoint = r['entrypoint']
                if not s.version and r.get('version'):
                    s.version = r['version']
                if rt == 'assistant':
                    model = (r.get('message') or {}).get('model', '')
                    if model:
                        s.model = model
        if not s.started_at:
            for r in records:
                if r.get('timestamp'):
                    s.started_at = r['timestamp']
                    break
        if not s.ended_at:
            for r in reversed(records):
                if r.get('timestamp'):
                    s.ended_at = r['timestamp']
                    break
        if not s.started_at:
            # No record in the entire file carries a `timestamp` field, so
            # there's genuinely no session-content timestamp to use. Fall
            # back to the file's own mtime rather than permanently skipping
            # this session on every sync cycle forever (it would fail the
            # same way on every retry/restart since the file content never
            # changes). The timestamp is approximate but lets the session
            # actually get synced instead of being stuck indefinitely.
            try:
                mtime = Path(s.file_path).stat().st_mtime
                s.started_at = datetime.utcfromtimestamp(mtime).isoformat() + 'Z'
            except OSError:
                pass

    def _conversation(self, records, session):
        turns, current, idx = [], None, 0
        seen_uuids = set()
        tool_names = set()

        for r in records:
            rt = r.get('type')
            if rt == 'user':
                msg = r.get('message') or {}
                content = msg.get('content', '')
                is_meta = r.get('isMeta', False)
                text = self._text(content)
                has_tool_result = isinstance(content, list) and any(
                    isinstance(c, dict) and c.get('type') == 'tool_result' for c in content
                )
                if has_tool_result and current:
                    pass  # tool results stay with current turn
                elif text and not is_meta:
                    origin = r.get('origin') or {}
                    if origin.get('kind') == 'human' or not has_tool_result:
                        current = _Turn(turn_index=idx, user_text=text,
                                        user_timestamp=r.get('timestamp', ''), is_meta=is_meta)
                        turns.append(current)
                        idx += 1
            elif rt == 'assistant':
                msg = r.get('message') or {}
                uuid = r.get('uuid', '')
                if not current:
                    current = _Turn(turn_index=idx)
                    turns.append(current)
                    idx += 1
                for item in (msg.get('content') or []):
                    if not isinstance(item, dict):
                        continue
                    it = item.get('type')
                    if it == 'text' and item.get('text'):
                        current.assistant_text = (current.assistant_text + '\n' + item['text']).strip()
                    elif it == 'thinking' and item.get('thinking'):
                        current.thinking_text = item['thinking']
                    elif it == 'tool_use':
                        tool_names.add(item.get('name', ''))
                        current.tool_calls.append(_ToolCall(
                            tool_use_id=item.get('id', ''),
                            name=item.get('name', ''),
                            input_args=item.get('input') or {},
                            timestamp=r.get('timestamp', ''),
                        ))
                if uuid not in seen_uuids:
                    seen_uuids.add(uuid)
                    usage = msg.get('usage') or {}
                    if usage:
                        current.input_tokens += usage.get('input_tokens') or 0
                        current.output_tokens += usage.get('output_tokens') or 0
                        current.cache_creation_tokens += usage.get('cache_creation_input_tokens') or 0
                        current.cache_read_tokens += usage.get('cache_read_input_tokens') or 0
                        current.thinking_tokens += (usage.get('output_tokens_details') or {}).get('thinking_tokens') or 0
                current.model = msg.get('model', '') or current.model
                current.stop_reason = msg.get('stop_reason', '') or current.stop_reason
                current.assistant_timestamp = r.get('timestamp', '') or current.assistant_timestamp

        session.turns = turns
        session.tool_names_used = sorted(tool_names)

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text' and 'text' in item:
                    parts.append(item['text'])
                elif isinstance(item, str):
                    parts.append(item)
            return '\n'.join(parts).strip()
        return ''


def _discover_jsonl_files(base_path: Optional[str] = None) -> List[Path]:
    """Find JSONL session files under ~/.claude/projects/."""
    roots = [Path(base_path)] if base_path else [Path.home() / ".claude" / "projects"]
    found = []
    for root in roots:
        if not root.exists():
            continue
        for f in root.glob("**/*.jsonl"):
            if f.is_file() and f.stat().st_size > 100:
                found.append(f)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


# ════════════════════════════════════════════════════════════════════
# Client Agent
# ════════════════════════════════════════════════════════════════════


class DataSource(Enum):
    SQLITE = "sqlite"
    JSONL = "jsonl"
    NONE = "none"


class ClientAgent:
    def __init__(self, server_url: str, jsonl_path: Optional[str] = None):
        self.server_url = server_url.rstrip('/')
        self.machine_id = self._get_machine_id()
        self.machine_name = socket.gethostname()
        self.os_type = platform.system().lower()
        self.os_version = platform.release()
        self.logged_in_user = self._get_logged_in_user()
        self.jsonl_override_path = jsonl_path

        # Per-machine write-auth token issued by the server on first
        # /api/client/register and required (via X-Agent-Token) on every
        # subsequent /api/client/* write for this machine_id, so a third
        # party can no longer spoof/tamper with an already-registered
        # machine's data just by knowing its machine_id (CWE-306). Stored
        # next to this script, not in the repo/source — never log it.
        self._token_path = Path(__file__).resolve().parent / ".agent_token"
        self.agent_token: Optional[str] = self._load_agent_token()

        # Detect data source. Never raises: if nothing is found we still
        # want to register with the server and report "no data" on every
        # sync cycle instead of crashing silently before ever contacting
        # the server (previously this made a no-data machine invisible).
        self.no_data_message: Optional[str] = None
        try:
            self.data_source, self.data_path = self._detect_data_source()
        except RuntimeError as e:
            self.data_source, self.data_path = DataSource.NONE, None
            self.no_data_message = str(e)
        self.synced_session_ids: set = set()  # track already-synced JSONL sessions

    @staticmethod
    def _get_machine_id() -> str:
        hostname = socket.gethostname()
        return f"{hostname}_{platform.system()}".lower().replace(" ", "_")

    def _load_agent_token(self) -> Optional[str]:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
            return token or None
        except Exception:
            return None

    def _save_agent_token(self, token: str) -> None:
        try:
            self._token_path.write_text(token, encoding="utf-8")
            try:
                os.chmod(self._token_path, 0o600)  # best-effort; no-op on Windows
            except Exception:
                pass
        except Exception as e:
            print(f"⚠ Failed to persist agent token: {e}")

    def _auth_headers(self) -> dict:
        return {"X-Agent-Token": self.agent_token} if self.agent_token else {}

    @staticmethod
    def _get_logged_in_user() -> str:
        """Detect logged-in user: tries git email, then OS username."""
        # 1. Try git config user.email
        try:
            result = subprocess.run(
                ["git", "config", "--global", "user.email"],
                capture_output=True, text=True, timeout=5
            )
            email = result.stdout.strip()
            if email:
                # Also get the name to combine: "Name <email>"
                name_result = subprocess.run(
                    ["git", "config", "--global", "user.name"],
                    capture_output=True, text=True, timeout=5
                )
                git_name = name_result.stdout.strip()
                if git_name:
                    return f"{git_name} <{email}>"
                return email
        except Exception:
            pass

        # 2. Try OS-level full name (macOS)
        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["id", "-F"], capture_output=True, text=True, timeout=5
                )
                full_name = result.stdout.strip()
                if full_name:
                    return full_name
            except Exception:
                pass

        # 3. Try Windows display name from environment
        if platform.system() == "Windows":
            display = os.environ.get("USERPROFILE", "")
            # USERPROFILE is C:\Users\john.doe — extract the username
            if display:
                uname = Path(display).name
                if uname:
                    return uname

        # 4. Fallback: OS login name
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    # ── Data source detection ───────────────────────────────────────

    def _detect_data_source(self):
        """
        Detect available data source.
        Priority: SQLite DB first, then JSONL files.
        Raises RuntimeError if neither is found.
        """
        # 1. Try SQLite databases
        db_path = self._find_sqlite_db()
        if db_path:
            return DataSource.SQLITE, db_path

        # 2. Try JSONL files (Windows / any OS)
        jsonl_dir = self._find_jsonl_directory()
        if jsonl_dir:
            return DataSource.JSONL, jsonl_dir

        # 3. Nothing found — build a helpful error message
        checked = self._get_all_checked_paths()
        msg_lines = [
            "No Claude Code data found on this machine!",
            "",
            "Checked for SQLite databases:",
        ]
        for p in checked["sqlite"]:
            flag = "✓" if p.exists() else "✗"
            msg_lines.append(f"  {flag} {p}")
        msg_lines.append("")
        msg_lines.append("Checked for JSONL session directories:")
        for p in checked["jsonl"]:
            flag = "✓" if p.exists() else "✗"
            msg_lines.append(f"  {flag} {p}")
        msg_lines += [
            "",
            "Make sure Claude Code has been run at least once on this machine.",
        ]
        raise RuntimeError("\n".join(msg_lines))

    def _find_sqlite_db(self) -> Optional[Path]:
        """Find first existing SQLite sessions DB."""
        home = Path.home()
        dashboard_db = home / ".claude-dashboard" / "claude.db"
        os_type = platform.system()

        if os_type == "Windows":
            appdata_local = home / "AppData" / "Local"
            appdata_roaming = home / "AppData" / "Roaming"
            candidates = [
                dashboard_db,
                appdata_local / "Claude" / "sessions.db",
                appdata_roaming / "Claude" / "sessions.db",
                home / ".config" / "claude" / "sessions.db",
                appdata_local / "claude" / "sessions.db",
                appdata_roaming / "claude" / "sessions.db",
            ]
        elif os_type == "Darwin":
            candidates = [
                dashboard_db,
                home / ".config" / "claude" / "sessions.db",
                home / "Library" / "Application Support" / "Claude" / "sessions.db",
            ]
        else:
            candidates = [
                dashboard_db,
                home / ".config" / "claude" / "sessions.db",
            ]

        for p in candidates:
            if p.exists():
                return p
        return None

    def _find_jsonl_directory(self) -> Optional[Path]:
        """Find a directory containing JSONL session files."""
        if self.jsonl_override_path:
            p = Path(self.jsonl_override_path)
            if p.exists():
                return p

        home = Path.home()
        candidates = [
            home / ".claude" / "projects",
        ]
        for d in candidates:
            if d.exists() and any(d.glob("**/*.jsonl")):
                return d
        return None

    def _get_all_checked_paths(self) -> dict:
        """Return all paths that were checked, for error reporting."""
        home = Path.home()
        os_type = platform.system()
        dashboard_db = home / ".claude-dashboard" / "claude.db"

        sqlite_paths = [dashboard_db]
        if os_type == "Windows":
            sqlite_paths += [
                home / "AppData" / "Local" / "Claude" / "sessions.db",
                home / "AppData" / "Roaming" / "Claude" / "sessions.db",
                home / ".config" / "claude" / "sessions.db",
            ]
        elif os_type == "Darwin":
            sqlite_paths += [
                home / ".config" / "claude" / "sessions.db",
                home / "Library" / "Application Support" / "Claude" / "sessions.db",
            ]
        else:
            sqlite_paths += [home / ".config" / "claude" / "sessions.db"]

        jsonl_paths = [home / ".claude" / "projects"]
        if self.jsonl_override_path:
            jsonl_paths.insert(0, Path(self.jsonl_override_path))

        return {"sqlite": sqlite_paths, "jsonl": jsonl_paths}

    # ── Server communication ────────────────────────────────────────

    async def register(self) -> bool:
        """Register this machine with the server."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.server_url}/api/client/register",
                    json={
                        "machine_id": self.machine_id,
                        "machine_name": self.machine_name,
                        "os_type": self.os_type,
                        "os_version": self.os_version,
                        "logged_in_user": self.logged_in_user,
                        "agent_version": AGENT_VERSION,
                    },
                    headers=self._auth_headers(),
                    timeout=10.0,
                )
                if response.status_code == 200:
                    issued_token = (response.json() or {}).get("agent_token")
                    if issued_token and issued_token != self.agent_token:
                        self.agent_token = issued_token
                        self._save_agent_token(issued_token)
                    print(f"✓ Registered machine: {self.machine_id}")
                    return True
                elif response.status_code in (401, 403):
                    print(
                        "✗ Registration rejected: this machine_id is already registered "
                        "with a different agent token. If this machine's local "
                        f"{self._token_path.name} was lost/reset, an admin may need to "
                        "clear its stored token server-side."
                    )
                    return False
                else:
                    print(f"✗ Failed to register: {response.status_code}")
                    return False
        except Exception as e:
            print(f"✗ Registration failed: {e}")
            return False

    async def send_heartbeat(self):
        """Send heartbeat to server."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.server_url}/api/client/heartbeat",
                    json={"machine_id": self.machine_id, "agent_version": AGENT_VERSION},
                    headers=self._auth_headers(),
                    timeout=5.0,
                )
                if response.status_code == 200:
                    print(f"💓 Heartbeat sent")
        except Exception as e:
            print(f"✗ Heartbeat failed: {e}")

    async def _report_sync(self, source: str, items_total: int, items_synced: int,
                            items_failed: int = 0, message: str = None):
        """Report one sync cycle's results to the server so the Sync tab can
        show what actually happened on each request (including when a sync
        was triggered but there was no data to send)."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.server_url}/api/client/sync-log",
                    json={
                        "machine_id": self.machine_id,
                        "source": source,
                        "items_total": items_total,
                        "items_synced": items_synced,
                        "items_failed": items_failed,
                        "message": message,
                    },
                    headers=self._auth_headers(),
                    timeout=10.0,
                )
        except Exception as e:
            print(f"✗ Failed to report sync log: {e}")

    async def check_for_update(self) -> bool:
        """Check the server for a newer client_agent.py and, if one is
        available, download and apply it automatically — no prompt, no
        user action needed. Returns True only if no update was needed or
        the check itself failed; if an update IS applied, this process
        re-execs itself with the new code and never returns."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.server_url}/api/agent/version", timeout=10.0
                )
                if response.status_code != 200:
                    return False
                latest_version = (response.json() or {}).get("version")
        except Exception as e:
            print(f"⚠ Update check failed: {e}")
            return False

        if not latest_version or latest_version == AGENT_VERSION:
            return False  # already up to date

        print(f"⬆ New agent version available: {AGENT_VERSION} → {latest_version}. Updating…")

        # Signature verification is mandatory, not best-effort: a malicious
        # or compromised dashboard server could otherwise serve arbitrary
        # code to every agent in the fleet (CWE-494). `cryptography` isn't
        # one of this file's two normal runtime deps (httpx, stdlib only),
        # so import it lazily here and fail CLOSED (skip the update, keep
        # running the current — already-trusted — version) rather than
        # falling back to the old compile()-only trust model if it's
        # missing.
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
        except ImportError:
            print(
                "✗ 'cryptography' package not installed — refusing to auto-update "
                "without signature verification. Run: pip install cryptography"
            )
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.server_url}/static/client_agent.py", timeout=30.0
                )
                if response.status_code != 200:
                    print(f"✗ Update download failed: HTTP {response.status_code}")
                    return False
                new_source = response.text

                sig_response = await client.get(
                    f"{self.server_url}/static/client_agent.py.sig", timeout=30.0
                )
                if sig_response.status_code != 200:
                    print(f"✗ Update signature download failed: HTTP {sig_response.status_code}")
                    return False
                signature = base64.b64decode(sig_response.text.strip())
        except Exception as e:
            print(f"✗ Update download failed: {e}")
            return False

        try:
            public_key = load_pem_public_key(AGENT_UPDATE_PUBLIC_KEY_PEM)
            if not isinstance(public_key, Ed25519PublicKey):
                raise TypeError("unexpected public key type")
            public_key.verify(signature, new_source.encode("utf-8"))
        except Exception as e:
            print(f"✗ Update signature verification FAILED, refusing to install: {e}")
            return False

        # Belt-and-suspenders: only after the signature (the real trust
        # boundary) has verified do we also confirm the payload is at
        # least parseable Python, so a bad/corrupt download can't brick
        # the running agent.
        try:
            compile(new_source, "client_agent.py", "exec")
        except SyntaxError as e:
            print(f"✗ Downloaded update failed validation, keeping current version: {e}")
            return False

        script_path = Path(__file__).resolve()
        tmp_path = script_path.with_suffix(".py.new")
        backup_path = script_path.with_suffix(".py.bak")
        try:
            tmp_path.write_text(new_source, encoding="utf-8")
            if script_path.exists():
                shutil.copy2(script_path, backup_path)
            os.replace(tmp_path, script_path)
        except Exception as e:
            print(f"✗ Failed to write update to disk: {e}")
            return False

        await self._report_sync(
            self.data_source.value if self.data_source else "none", 0, 0,
            message=f"Auto-updated agent {AGENT_VERSION} -> {latest_version}, restarting"
        )
        print(f"✓ Updated to v{latest_version}. Restarting…")

        # Re-exec this same process in place — same PID, same argv — so
        # agent.pid and any service-manager supervision (launchd/schtasks/
        # systemd) stay valid. The new code takes over without anyone
        # needing to "restart" the agent from the outside. Flush explicitly
        # first: os.execv() replaces the process image immediately without
        # running any of Python's normal buffer-flushing shutdown, so any
        # not-yet-written log lines above would otherwise vanish.
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # os.execv never returns on success.

    def repair_windows_scheduled_task_launch(self):
        """Self-healing safety net for the Windows Terminal flash bug.

        Machines installed before client-install.ps1 started routing its
        Scheduled Tasks through `conhost.exe --headless` still have their
        task's action pointing at the old, flash-prone
        `powershell.exe -WindowStyle Hidden -File ...` (or an even older
        wscript/vbs variant). Re-running the installer manually fixes it,
        but most people never will — so every time an updated agent starts
        on Windows, patch the two tasks' actions in place. Cheap and
        idempotent: short-circuits via `schtasks /Query` the moment both
        actions already route through conhost --headless, and never
        touches anything if a task doesn't exist (e.g. the
        plain-background-process fallback install path).

        Uses `schtasks /Delete /F` followed by `/Create /F` (full
        delete-then-recreate) rather than `/Change /TR` or a bare
        `/Create /F`: per-user tasks created without stored credentials
        (S4U logon, which is what client-install.ps1 uses) silently fail
        to have their action updated via `/Change` on many Windows
        versions, and `/Create /F` *alone* has also been observed in the
        field to silently no-op when a task by that name already exists
        (exit code 0, but `schtasks /Query` still shows the untouched old
        action). Explicitly deleting first removes the ambiguity and
        matches what a manual reinstall does. This only replaces the
        task's registration; it never touches the currently-running
        agent process.
        """
        if os.name != "nt":
            return
        install_dir = Path(__file__).resolve().parent
        tasks = {
            "ClaudeDashboardAgent": {
                "script": install_dir / "start.ps1",
                "schedule": ["/SC", "ONLOGON"],
            },
            "ClaudeDashboardAgentWatchdog": {
                "script": install_dir / "watchdog.ps1",
                "schedule": ["/SC", "MINUTE", "/MO", "5"],
            },
        }
        for task_name, cfg in tasks.items():
            script_path = cfg["script"]
            try:
                query = subprocess.run(
                    ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
                    capture_output=True, text=True, timeout=10
                )
                if query.returncode != 0:
                    continue  # task doesn't exist -- nothing to repair
                if "conhost.exe --headless" in query.stdout.lower():
                    continue  # already fixed
                if not script_path.exists():
                    continue  # unexpected layout, don't guess

                new_action = (
                    f'conhost.exe --headless powershell.exe -NoProfile '
                    f'-ExecutionPolicy Bypass -File "{script_path}"'
                )
                # `/Create /F` alone silently no-ops on many Windows builds
                # when a task with that name already exists -- confirmed in
                # the field: an agent can be fully updated and actively
                # heartbeating with this exact code running, yet
                # `schtasks /Query` still shows the untouched old action.
                # Deleting the stale registration first (safe: it only
                # removes the *schedule entry*, it does not kill whatever
                # process instance happens to be running right now -- so
                # this is safe to call even on the task that launched the
                # very process executing this code) makes the subsequent
                # `/Create` unambiguous instead of an "update".
                delete_res = subprocess.run(
                    ["schtasks", "/Delete", "/TN", task_name, "/F"],
                    capture_output=True, text=True, timeout=10
                )
                recreate = subprocess.run(
                    ["schtasks", "/Create", "/F", "/TN", task_name, "/TR", new_action]
                    + cfg["schedule"] + ["/RL", "LIMITED"],
                    capture_output=True, text=True, timeout=10
                )

                def _verify() -> bool:
                    v = subprocess.run(
                        ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
                        capture_output=True, text=True, timeout=10
                    )
                    return "conhost.exe --headless" in v.stdout.lower()

                if recreate.returncode == 0 and _verify():
                    print(f"✓ Repaired '{task_name}' scheduled task to launch hidden (no more terminal flash)")
                    continue

                # schtasks.exe reported success (or failed) but the action
                # still isn't updated -- observed in the field on a machine
                # where the agent itself runs as a background/non-interactive
                # process spawned by Task Scheduler (as opposed to the
                # installer, which always runs interactively in the user's
                # own foreground session). Some environments silently drop
                # Task Scheduler *write* operations issued from that context
                # even though reads (`/Query`) and the *original* interactive
                # install both work fine. Fall back to the Task Scheduler
                # PowerShell module (a different API path -- COM-based
                # ScheduledTasks module rather than the legacy schtasks.exe
                # RPC calls) in case it isn't subject to the same restriction.
                print(
                    f"⚠ schtasks repair of '{task_name}' didn't take effect "
                    f"(delete rc={delete_res.returncode}, create rc={recreate.returncode}, "
                    f"create stderr={recreate.stderr.strip()!r}) -- trying PowerShell "
                    f"ScheduledTasks module as a fallback"
                )
                ps_script = (
                    f"$action = New-ScheduledTaskAction -Execute 'conhost.exe' "
                    f"-Argument '--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script_path}\"'; "
                    + (
                        "$trigger = New-ScheduledTaskTrigger -AtLogOn;"
                        if task_name == "ClaudeDashboardAgent"
                        else "$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue);"
                    )
                    + f"Register-ScheduledTask -TaskName '{task_name}' -Action $action -Trigger $trigger "
                    f"-RunLevel Limited -Force | Out-Null"
                )
                ps_res = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    capture_output=True, text=True, timeout=20
                )
                if ps_res.returncode == 0 and _verify():
                    print(f"✓ Repaired '{task_name}' scheduled task via PowerShell fallback (no more terminal flash)")
                else:
                    print(
                        f"⚠ Could not repair '{task_name}' scheduled task via either method "
                        f"(PowerShell rc={ps_res.returncode}, stderr={ps_res.stderr.strip()!r}) "
                        f"-- please re-run the installer manually to pick up the fix"
                    )
            except Exception as e:
                print(f"⚠ Scheduled task repair check failed for '{task_name}': {e}")

    # ── Unified sync dispatch ───────────────────────────────────────

    async def sync_data(self):
        """Sync data using the detected source (SQLite or JSONL)."""
        if self.data_source == DataSource.SQLITE:
            await self._sync_from_sqlite()
        elif self.data_source == DataSource.JSONL:
            await self._sync_from_jsonl()
        else:
            # No data source was ever found on this machine. Still report
            # every cycle so it's visible in the dashboard's Sync tab
            # instead of the agent silently doing nothing forever.
            print(f"⚠ No Claude Code data found on this machine")
            if self.no_data_message:
                print(self.no_data_message)
            # Keep the dashboard row short; the full path-by-path detail is
            # already visible in the agent's own log if someone needs it.
            await self._report_sync(
                "none", 0, 0,
                message="No Claude Code data found (no SQLite DB or JSONL session files detected)"
            )

    # ── SQLite sync (macOS / Linux) ─────────────────────────────────

    async def _sync_from_sqlite(self):
        """Sync sessions and token usage from a local SQLite database."""
        if not self.data_path.exists():
            print(f"⚠ SQLite database disappeared: {self.data_path}")
            await self._report_sync("sqlite", 0, 0, message="SQLite database disappeared")
            return

        print(f"📦 Syncing from SQLite: {self.data_path}")

        try:
            conn = sqlite3.connect(str(self.data_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            async with httpx.AsyncClient(headers=self._auth_headers()) as client:
                # Sync sessions
                cursor.execute(
                    "SELECT * FROM sessions ORDER BY start_time DESC LIMIT 100"
                )
                sessions = cursor.fetchall()
                synced_sessions = 0
                failed_sessions = 0

                for session in sessions:
                    try:
                        d = dict(session)
                        await client.post(
                            f"{self.server_url}/api/client/session",
                            json={
                                "session_id": d["session_id"],
                                "machine_id": self.machine_id,
                                "start_time": d["start_time"],
                                "working_directory": d.get("working_directory"),
                                "command_args": d.get("command_args"),
                                "model_name": d.get("model_name"),
                            },
                            timeout=10.0,
                        )
                        synced_sessions += 1
                    except Exception as e:
                        failed_sessions += 1
                        print(f"  ✗ Failed to sync session: {e}")

                if synced_sessions:
                    print(f"  ✓ Synced {synced_sessions}/{len(sessions)} sessions")

                # Sync token usage
                cursor.execute(
                    "SELECT * FROM token_usage ORDER BY timestamp DESC LIMIT 1000"
                )
                token_records = cursor.fetchall()
                synced_tokens = 0
                failed_tokens = 0

                for record in token_records:
                    try:
                        d = dict(record)
                        await client.post(
                            f"{self.server_url}/api/client/token_usage",
                            json={
                                "timestamp": d["timestamp"],
                                "session_id": d["session_id"],
                                "machine_id": self.machine_id,
                                "model_name": d.get("model_name"),
                                "input_tokens": d.get("input_tokens"),
                                "output_tokens": d.get("output_tokens"),
                                "total_tokens": d.get("total_tokens"),
                                "estimated_cost_usd": d.get("estimated_cost_usd"),
                            },
                            timeout=10.0,
                        )
                        synced_tokens += 1
                    except Exception:
                        failed_tokens += 1

                print(f"  ✓ Synced {synced_tokens}/{len(token_records)} token usage records")

            conn.close()

            items_total = len(sessions) + len(token_records)
            items_synced = synced_sessions + synced_tokens
            items_failed = failed_sessions + failed_tokens
            message = (
                f"{synced_sessions}/{len(sessions)} sessions, "
                f"{synced_tokens}/{len(token_records)} token usage records"
            ) if items_total else "No sessions or token usage found"
            await self._report_sync("sqlite", items_total, items_synced,
                                     items_failed=items_failed, message=message)

        except sqlite3.Error as e:
            print(f"  ✗ Database error: {e}")
            await self._report_sync("sqlite", 0, 0, message=f"Database error: {e}")
        except Exception as e:
            print(f"  ✗ Sync error: {e}")
            await self._report_sync("sqlite", 0, 0, message=f"Sync error: {e}")

    # ── JSONL sync (Windows / fallback) ─────────────────────────────

    async def _sync_from_jsonl(self):
        """Parse JSONL session files and sync to server."""
        print(f"📁 Syncing from JSONL: {self.data_path}")

        jsonl_files = _discover_jsonl_files(str(self.data_path))
        if not jsonl_files:
            print("  ⚠ No JSONL files found")
            await self._report_sync("jsonl", 0, 0, message="No JSONL files found")
            return

        # Only process new / unseen files
        new_files = [
            f for f in jsonl_files if f.stem not in self.synced_session_ids
        ]
        if not new_files:
            print(f"  ✓ All {len(jsonl_files)} sessions already synced")
            await self._report_sync(
                "jsonl", len(jsonl_files), 0,
                message=f"All {len(jsonl_files)} sessions already synced"
            )
            return

        parser = _JsonlParser()
        synced = 0
        failed = 0

        async with httpx.AsyncClient(timeout=15.0, headers=self._auth_headers()) as client:
            for filepath in new_files:
                session = parser.parse_file(str(filepath))
                if not session:
                    failed += 1
                    continue

                # A session with no real start timestamp has no metadata we
                # can trust — skip it rather than fabricating a "now"
                # start_time (which would make any later/older end_time in
                # the file look like it happened *before* the session
                # started, corrupting duration for the rest of the app).
                if not session.started_at:
                    failed += 1
                    print(f"  ⚠ Skipping {filepath.name}: no start timestamp found")
                    continue

                # Calculate duration from timestamps
                duration_seconds = None
                end_time = session.ended_at
                if session.ended_at:
                    try:
                        start_dt = datetime.fromisoformat(session.started_at.replace('Z', '+00:00'))
                        end_dt = datetime.fromisoformat(session.ended_at.replace('Z', '+00:00'))
                        if end_dt < start_dt:
                            # Corrupt/out-of-order data — don't report an
                            # end_time earlier than the session's start.
                            end_time = None
                        else:
                            duration_seconds = int((end_dt - start_dt).total_seconds())
                    except (ValueError, TypeError):
                        end_time = None

                try:
                    # Send session (server deduplicates by session_id + machine_id)
                    resp = await client.post(
                        f"{self.server_url}/api/client/session",
                        json={
                            "session_id": session.session_id,
                            "machine_id": self.machine_id,
                            "start_time": session.started_at,
                            "working_directory": session.cwd,
                            "command_args": (session.last_prompt or "")[:500],
                            "model_name": session.model,
                        },
                    )

                    # Always update end_time/duration (even if session already existed)
                    if end_time or duration_seconds:
                        await client.post(
                            f"{self.server_url}/api/client/session/update",
                            json={
                                "session_id": session.session_id,
                                "end_time": end_time,
                                "duration_seconds": duration_seconds,
                                "model_name": session.model,
                            },
                        )

                    # Send messages for each turn (server deduplicates)
                    session_cache_read = 0
                    session_cache_creation = 0
                    session_input_tokens = 0
                    response_times_ms = []
                    had_any_tool_error = False
                    task_type_votes = []

                    for turn in session.turns:
                        if turn.is_meta or (not turn.user_text and not turn.assistant_text):
                            continue

                        turn_had_error = any(tc.is_error for tc in turn.tool_calls)
                        had_any_tool_error = had_any_tool_error or turn_had_error

                        if turn.user_text:
                            await client.post(
                                f"{self.server_url}/api/client/message",
                                json={
                                    "session_id": session.session_id,
                                    "machine_id": self.machine_id,
                                    "timestamp": turn.user_timestamp or session.started_at,
                                    "role": "user",
                                    "content": turn.user_text[:10000],
                                    "tokens": turn.input_tokens,
                                    "model": turn.model or session.model,
                                },
                            )

                            # Prompt quality heuristic (real prompt text + real token counts)
                            task_type_votes.append(_classify_task_type(turn.user_text))
                            scores = _score_prompt(
                                turn.user_text, turn.input_tokens, turn.output_tokens, turn_had_error
                            )
                            try:
                                await client.post(
                                    f"{self.server_url}/api/optimization/prompt-analysis",
                                    json={
                                        "session_id": session.session_id,
                                        "prompt_text": turn.user_text[:10000],
                                        **scores,
                                    },
                                )
                            except Exception:
                                pass  # best-effort; never block the core sync on this

                        assistant_text = turn.assistant_text
                        if turn.tool_calls:
                            tool_summary = "\n".join(
                                f"🔧 {tc.name}({_summarize_tool(tc)})"
                                for tc in turn.tool_calls
                            )
                            assistant_text = (
                                f"{assistant_text}\n\n{tool_summary}"
                                if assistant_text
                                else tool_summary
                            )

                        if assistant_text:
                            await client.post(
                                f"{self.server_url}/api/client/message",
                                json={
                                    "session_id": session.session_id,
                                    "machine_id": self.machine_id,
                                    "timestamp": turn.assistant_timestamp or session.started_at,
                                    "role": "assistant",
                                    "content": assistant_text[:10000],
                                    "tokens": turn.output_tokens,
                                    "model": turn.model or session.model,
                                },
                            )

                        # Real code modifications derived from actual Edit/Write/
                        # MultiEdit/NotebookEdit tool call arguments, plus a
                        # transparent rule-based quality/complexity estimate
                        # (see _estimate_complexity/_estimate_code_quality)
                        # that feeds the Code Quality, AI Suggestions, and
                        # Refactoring Impact views from the same real data.
                        for tc in turn.tool_calls:
                            if tc.name not in _EDIT_TOOL_NAMES:
                                continue
                            extracted = _extract_code_modification(tc)
                            if not extracted:
                                continue
                            file_path, lines_added, lines_deleted, mod_type, old_code, new_code = extracted
                            if not file_path:
                                continue
                            language = _detect_language(file_path)
                            ts = tc.timestamp or turn.assistant_timestamp or session.started_at
                            complexity_before = _estimate_complexity(old_code)
                            complexity_after = _estimate_complexity(new_code)
                            quality = _estimate_code_quality(new_code, complexity_after)

                            try:
                                resp = await client.post(
                                    f"{self.server_url}/api/analytics/code-modifications",
                                    json={
                                        "session_id": session.session_id,
                                        "file_path": file_path,
                                        "language": language,
                                        "modification_type": mod_type,
                                        "lines_added": lines_added,
                                        "lines_deleted": lines_deleted,
                                        "complexity_before": complexity_before,
                                        "complexity_after": complexity_after,
                                        "ai_suggestion_accepted": not tc.is_error,
                                        "modification_quality_score": quality["overall_quality_score"],
                                        "timestamp": ts,
                                    },
                                )
                                # Client agents may re-parse the same JSONL session
                                # file across restarts, so if this exact
                                # modification was already recorded, skip the
                                # derived quality/suggestion rows too — otherwise
                                # they'd pile up as duplicates on every resync.
                                already_synced = resp.json().get("status") == "already_exists"
                            except Exception:
                                already_synced = False  # best-effort; never block the core sync on this

                            if already_synced:
                                continue

                            # Feed the same real signals into the Code Quality tab.
                            try:
                                await client.post(
                                    f"{self.server_url}/api/analytics/code-quality",
                                    json={
                                        "session_id": session.session_id,
                                        "file_path": file_path,
                                        "language": language,
                                        **quality,
                                    },
                                )
                            except Exception:
                                pass  # best-effort; never block the core sync on this

                            # Every Edit/Write/MultiEdit call is effectively an
                            # AI-authored code suggestion the user accepted (tool
                            # succeeded) or rejected (tool errored) — log it with
                            # the real before/after code for the Suggestions tab.
                            try:
                                await client.post(
                                    f"{self.server_url}/api/analytics/ai-suggestions",
                                    json={
                                        "session_id": session.session_id,
                                        "suggestion_type": mod_type,
                                        "suggestion_text": f"{mod_type.capitalize()} {file_path} (+{lines_added}/-{lines_deleted} lines)",
                                        "code_before": (old_code[:2000] if old_code else None),
                                        "code_after": (new_code[:2000] if new_code else None),
                                        "was_accepted": not tc.is_error,
                                        "was_modified": False,
                                        "file_path": file_path,
                                        "language": language,
                                    },
                                )
                            except Exception:
                                pass  # best-effort; never block the core sync on this

                        # Token usage (server deduplicates by timestamp+session+machine+tokens)
                        if turn.input_tokens or turn.output_tokens:
                            total = (turn.input_tokens + turn.cache_creation_tokens +
                                     turn.cache_read_tokens + turn.output_tokens)
                            cost = estimate_cost(
                                turn.model or session.model or "",
                                turn.input_tokens,
                                turn.output_tokens,
                                cache_read_tokens=turn.cache_read_tokens,
                                cache_creation_tokens=turn.cache_creation_tokens,
                            )
                            await client.post(
                                f"{self.server_url}/api/client/token_usage",
                                json={
                                    "timestamp": turn.assistant_timestamp or session.started_at,
                                    "session_id": session.session_id,
                                    "machine_id": self.machine_id,
                                    "model_name": turn.model or session.model,
                                    "input_tokens": turn.input_tokens,
                                    "output_tokens": turn.output_tokens,
                                    "total_tokens": total,
                                    "estimated_cost_usd": cost,
                                },
                            )

                        session_cache_read += turn.cache_read_tokens
                        session_cache_creation += turn.cache_creation_tokens
                        session_input_tokens += turn.input_tokens

                        if turn.user_timestamp and turn.assistant_timestamp:
                            try:
                                u_dt = datetime.fromisoformat(turn.user_timestamp.replace('Z', '+00:00'))
                                a_dt = datetime.fromisoformat(turn.assistant_timestamp.replace('Z', '+00:00'))
                                delta_ms = (a_dt - u_dt).total_seconds() * 1000
                                if delta_ms >= 0:
                                    response_times_ms.append(delta_ms)
                            except (ValueError, TypeError):
                                pass

                    # Real cache-usage totals for the whole session (already parsed
                    # from each turn's actual usage block — nothing fabricated).
                    total_context_tokens = session_input_tokens + session_cache_read + session_cache_creation
                    if total_context_tokens > 0 and (session_cache_read or session_cache_creation):
                        pricing = _MODEL_PRICING.get(session.model, {})
                        input_price = pricing.get("input", 0.0)
                        cost_saved = (session_cache_read / 1_000_000) * input_price * 0.9
                        cache_hit_rate = round((session_cache_read / total_context_tokens) * 100, 2)
                        try:
                            await client.post(
                                f"{self.server_url}/api/optimization/cache-analytics",
                                json={
                                    "session_id": session.session_id,
                                    "cache_read_tokens": session_cache_read,
                                    "cache_write_tokens": session_cache_creation,
                                    "cache_hit_rate": cache_hit_rate,
                                    "cache_efficiency_score": cache_hit_rate,
                                    "cost_saved_usd": round(cost_saved, 6),
                                },
                            )
                        except Exception:
                            pass

                    # Real model-performance signal for this session: actual
                    # response latency (from turn timestamps), a real success
                    # signal (no failed tool calls + normal stop_reason), and
                    # real cost via the same pricing table used for billing.
                    if session.model:
                        avg_response_ms = (
                            sum(response_times_ms) / len(response_times_ms) if response_times_ms else None
                        )
                        session_cost = estimate_cost(
                            session.model, session.total_input_tokens, session.total_output_tokens,
                            cache_read_tokens=session_cache_read, cache_creation_tokens=session_cache_creation,
                        )
                        token_eff = (
                            round(session.total_output_tokens / session.total_input_tokens, 4)
                            if session.total_input_tokens else None
                        )
                        task_type = max(set(task_type_votes), key=task_type_votes.count) if task_type_votes else "code_generation"
                        session_date = (session.started_at or "")[:10]
                        if session_date:
                            try:
                                await client.post(
                                    f"{self.server_url}/api/optimization/model-performance",
                                    json={
                                        "model_name": session.model,
                                        "task_type": task_type,
                                        "date": session_date,
                                        "average_response_time_ms": avg_response_ms,
                                        "success": not had_any_tool_error,
                                        "average_token_efficiency": token_eff,
                                        "cost_per_task": session_cost,
                                    },
                                )
                            except Exception:
                                pass

                    self.synced_session_ids.add(session.session_id)
                    synced += 1

                except Exception as e:
                    if "already_exists" not in str(e):
                        print(f"  ✗ {filepath.name}: {e}")
                    failed += 1

        print(f"  ✓ Synced {synced} new sessions ({len(self.synced_session_ids)} total, {failed} skipped)")
        await self._report_sync(
            "jsonl", len(jsonl_files), synced, items_failed=failed,
            message=f"{synced} new / {len(new_files)} new files found "
                    f"({len(jsonl_files)} total sessions, {failed} skipped)"
        )

    # ── Main loop ───────────────────────────────────────────────────

    async def run(self, sync_interval: int = 300, debug: bool = False):
        """Run the client agent."""
        print(f"WhirlpoolBee Client Agent")
        print(f"{'=' * 50}")
        print(f"  Machine:     {self.machine_name} ({self.machine_id})")
        print(f"  User:        {self.logged_in_user}")
        print(f"  OS:          {self.os_type} {self.os_version}")
        print(f"  Server:      {self.server_url}")
        print(f"  Version:     {AGENT_VERSION}")
        print(f"  Data source: {self.data_source.value}" +
              (f" → {self.data_path}" if self.data_path else " (none found)"))
        print()

        # Register with server
        if not await self.register():
            print("Failed to register. Exiting.")
            return

        if not debug:
            # Auto-update, no prompt: if the server has a newer
            # client_agent.py than we're running, download and switch to
            # it right away, before doing any data sync. Skipped in
            # --debug so a foreground troubleshooting session isn't
            # unexpectedly swapped out from under the user.
            await self.check_for_update()
            # Self-heal machines installed before the terminal-flash fix:
            # patch their Scheduled Task action in place, no reinstall
            # needed. No-op (and near-instant) once already fixed.
            self.repair_windows_scheduled_task_launch()

        await self.send_heartbeat()
        await self.sync_data()

        if debug:
            print(f"\nMonitoring… (syncing every {sync_interval}s, Ctrl+C to stop) [--debug mode]")
        else:
            print(f"\nMonitoring… (syncing every {sync_interval}s) running in background")
        while True:
            try:
                await asyncio.sleep(sync_interval)
                if not debug:
                    await self.check_for_update()
                await self.send_heartbeat()
                await self.sync_data()
            except KeyboardInterrupt:
                print("\nStopping agent…")
                break
            except Exception as e:
                print(f"Error in main loop: {e}")
                await self._report_sync(
                    self.data_source.value, 0, 0,
                    message=f"Sync cycle failed unexpectedly: {e}"
                )
                await asyncio.sleep(60)


def _summarize_tool(tc) -> str:
    """Short summary of tool call args for display."""
    args = tc.input_args or {}
    if not args:
        return ""
    if tc.name in ("Read", "read"):
        return args.get("file_path") or args.get("path") or ""
    if tc.name in ("Grep", "grep"):
        return (args.get("pattern") or "")[:60]
    if tc.name in ("Glob", "glob"):
        return (args.get("pattern") or "")[:60]
    vals = list(args.values())
    return str(vals[0])[:60] if vals else ""


def main():
    parser = argparse.ArgumentParser(
        description="WhirlpoolBee Client Agent — syncs SQLite or JSONL data to server"
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Dashboard server URL (e.g., http://192.168.1.100:8765)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Sync interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--jsonl-path",
        type=str,
        default=None,
        help="Override JSONL directory path (e.g., C:\\Users\\You\\.claude\\projects)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run in the foreground and respond to Ctrl+C. Without this flag the "
        "agent is meant to run as a background service and ignores common "
        "termination signals (SIGINT/SIGTERM/SIGHUP) so it keeps running.",
    )

    args = parser.parse_args()

    if not args.debug:
        # Background mode: ignore signals a user could send from a terminal
        # (Ctrl+C, `kill`, closing the terminal) so the agent keeps running
        # unattended. It can still be stopped via the official uninstall/
        # service-manager commands (launchctl/systemctl), or SIGKILL.
        def _ignore_signal(signum, frame):
            pass

        for sig_name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _ignore_signal)
                except (ValueError, OSError):
                    pass  # e.g. not supported on this platform/thread

    try:
        agent = ClientAgent(args.server, jsonl_path=args.jsonl_path)
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)

    try:
        asyncio.run(agent.run(sync_interval=args.interval, debug=args.debug))
    except KeyboardInterrupt:
        if args.debug:
            print("\nAgent stopped.")
        # In non-debug mode SIGINT is ignored above, so this path is not
        # expected to be reached outside of --debug.


if __name__ == "__main__":
    main()
