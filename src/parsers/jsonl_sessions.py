"""
Windows JSONL Session Parser for Claude Code.

On Windows, Claude Code stores session data as JSONL files at:
    %USERPROFILE%\\.claude\\projects\\<project-slug>\\<session-uuid>.jsonl

Each line is a JSON object with a 'type' field. Key record types:
  - queue-operation: session start/end markers
  - user: user messages (human text, tool results, commands)
  - assistant: assistant responses (text, thinking, tool_use)
  - attachment: metadata (tools, agents, skills, token reminders)
  - ai-title: AI-generated session title
  - last-prompt: last user prompt text
  - file-history-snapshot / file-history-delta: file edit tracking

This parser extracts structured session data from these JSONL files.
"""

import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A single tool invocation by the assistant."""
    tool_use_id: str
    name: str
    input_args: Dict[str, Any]
    timestamp: str = ""
    result: Optional[str] = None
    is_error: bool = False
    duration_ms: Optional[int] = None


@dataclass
class ConversationTurn:
    """One turn in the conversation (user prompt + assistant response)."""
    turn_index: int
    user_text: str = ""
    user_timestamp: str = ""
    user_uuid: str = ""
    assistant_text: str = ""
    assistant_timestamp: str = ""
    assistant_uuid: str = ""
    model: str = ""
    stop_reason: str = ""
    thinking_text: str = ""
    thinking_tokens: int = 0
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    is_meta: bool = False


@dataclass
class AgentInvocation:
    """A sub-agent spawned during the session."""
    agent_type: str
    description: str
    prompt: str
    agent_id: str = ""
    status: str = ""
    result_summary: str = ""
    model: str = ""
    total_tokens: int = 0
    tool_use_count: int = 0
    duration_ms: int = 0


@dataclass
class ParsedSession:
    """Fully parsed session from a JSONL file."""
    session_id: str
    file_path: str
    title: str = ""
    cwd: str = ""
    git_branch: str = ""
    entrypoint: str = ""
    version: str = ""
    model: str = ""

    # Timestamps
    started_at: str = ""
    ended_at: str = ""

    # Conversation
    turns: List[ConversationTurn] = field(default_factory=list)

    # Token totals
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_thinking_tokens: int = 0

    # Tools & agents
    tool_names_used: List[str] = field(default_factory=list)
    available_tools: List[str] = field(default_factory=list)
    available_agents: List[str] = field(default_factory=list)
    available_skills: List[str] = field(default_factory=list)
    agent_invocations: List[AgentInvocation] = field(default_factory=list)

    # Metadata
    last_prompt: str = ""
    file_size_bytes: int = 0
    record_count: int = 0
    error_count: int = 0


class JsonlSessionParser:
    """Parse Claude Code Windows JSONL session files."""

    def parse_file(self, file_path: str) -> Optional[ParsedSession]:
        """Parse a JSONL session file into structured data."""
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return None

        records = self._read_records(path)
        if not records:
            return None

        session_id = self._extract_session_id(records, path)
        session = ParsedSession(
            session_id=session_id,
            file_path=str(path),
            file_size_bytes=path.stat().st_size,
            record_count=len(records),
        )

        self._extract_metadata(records, session)
        self._extract_conversation(records, session)
        self._extract_tools_and_agents(records, session)
        self._compute_totals(session)

        return session

    def parse_content(self, content: str, source_name: str = "upload") -> Optional[ParsedSession]:
        """Parse JSONL content from a string (for uploads / pastes)."""
        records = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip the PowerShell header line if present
            if line.startswith("PS ") or line.startswith("Get-Content"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not records:
            return None

        session_id = self._extract_session_id(records, Path(source_name))
        session = ParsedSession(
            session_id=session_id,
            file_path=source_name,
            file_size_bytes=len(content.encode('utf-8')),
            record_count=len(records),
        )

        self._extract_metadata(records, session)
        self._extract_conversation(records, session)
        self._extract_tools_and_agents(records, session)
        self._compute_totals(session)

        return session

    # ── Private helpers ──────────────────────────────────────────────

    def _read_records(self, path: Path) -> List[Dict[str, Any]]:
        """Read and parse all JSONL records from a file."""
        records = []
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return records

    def _extract_session_id(self, records: List[Dict], path: Path) -> str:
        """Get session ID from records or filename."""
        for r in records:
            sid = r.get('sessionId')
            if sid:
                return sid
        # Fall back to filename (UUID.jsonl)
        stem = path.stem
        if re.match(r'^[0-9a-f]{8}-', stem):
            return stem
        return f"jsonl-{path.name}"

    def _extract_metadata(self, records: List[Dict], session: ParsedSession):
        """Extract session-level metadata from records."""
        for r in records:
            rtype = r.get('type')

            if rtype == 'ai-title':
                session.title = r.get('aiTitle', '') or session.title

            elif rtype == 'last-prompt':
                session.last_prompt = r.get('lastPrompt', '')

            elif rtype == 'queue-operation':
                ts = r.get('timestamp', '')
                op = r.get('operation', '')
                if op == 'enqueue' and not session.started_at:
                    session.started_at = ts
                elif op == 'dequeue':
                    session.ended_at = ts

            # Extract cwd / branch / model from any user or assistant record
            elif rtype in ('user', 'assistant'):
                if not session.cwd and r.get('cwd'):
                    session.cwd = r['cwd']
                if not session.git_branch and r.get('gitBranch'):
                    session.git_branch = r['gitBranch']
                if not session.entrypoint and r.get('entrypoint'):
                    session.entrypoint = r['entrypoint']
                if not session.version and r.get('version'):
                    session.version = r['version']

                # Update branch if it changes mid-session
                if r.get('gitBranch'):
                    session.git_branch = r['gitBranch']

                if rtype == 'assistant':
                    msg = r.get('message', {})
                    model = msg.get('model', '')
                    if model:
                        session.model = model

        # Derive timestamps from records if queue-operation is missing
        if not session.started_at:
            for r in records:
                ts = r.get('timestamp')
                if ts:
                    session.started_at = ts
                    break
        if not session.ended_at:
            for r in reversed(records):
                ts = r.get('timestamp')
                if ts:
                    session.ended_at = ts
                    break

    def _extract_conversation(self, records: List[Dict], session: ParsedSession):
        """Build the conversation turn list from user/assistant records."""
        turns: List[ConversationTurn] = []
        current_turn: Optional[ConversationTurn] = None
        turn_index = 0

        # Track assistant content fragments (multiple records per message)
        seen_assistant_uuids = set()
        tool_calls_by_id = {}

        for r in records:
            rtype = r.get('type')

            if rtype == 'user':
                msg = r.get('message', {})
                content = msg.get('content', '')
                is_meta = r.get('isMeta', False)
                origin = r.get('origin', {})

                # Extract user text
                text = self._extract_text_content(content)
                tool_results = self._extract_tool_results(content)

                if tool_results and current_turn:
                    raw_result = r.get('toolUseResult')
                    for tr in tool_results:
                        match = tool_calls_by_id.get(tr['tool_use_id'])
                        owner = match[1] if match else current_turn
                        owner.tool_results.append(tr)
                        if match:
                            tc = match[0]
                            result = raw_result if len(tool_results) == 1 and raw_result is not None else tr['content']
                            tc.result = (json.dumps(result) if isinstance(result, dict) else str(result))[:2000]
                            tc.is_error = tr['is_error']
                elif text and not is_meta:
                    # New human message → start a new turn
                    if origin.get('kind') == 'human' or not tool_results:
                        current_turn = ConversationTurn(
                            turn_index=turn_index,
                            user_text=text,
                            user_timestamp=r.get('timestamp', ''),
                            user_uuid=r.get('uuid', ''),
                            is_meta=is_meta,
                        )
                        turns.append(current_turn)
                        turn_index += 1

            elif rtype == 'assistant':
                msg = r.get('message', {})
                uuid = r.get('uuid', '')

                if not current_turn:
                    # Orphan assistant message
                    current_turn = ConversationTurn(turn_index=turn_index)
                    turns.append(current_turn)
                    turn_index += 1

                # Accumulate assistant content (may span multiple records)
                content_items = msg.get('content', [])
                for item in content_items:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get('type')

                    if item_type == 'text':
                        text = item.get('text', '')
                        if text:
                            if current_turn.assistant_text:
                                current_turn.assistant_text += '\n' + text
                            else:
                                current_turn.assistant_text = text

                    elif item_type == 'thinking':
                        thinking = item.get('thinking', '')
                        if thinking:
                            current_turn.thinking_text = thinking

                    elif item_type == 'tool_use':
                        tc = ToolCall(
                            tool_use_id=item.get('id', ''),
                            name=item.get('name', ''),
                            input_args=item.get('input', {}),
                            timestamp=r.get('timestamp', ''),
                        )
                        current_turn.tool_calls.append(tc)
                        if tc.tool_use_id:
                            tool_calls_by_id[tc.tool_use_id] = (tc, current_turn)

                # Extract usage from first occurrence of this message
                if uuid not in seen_assistant_uuids:
                    seen_assistant_uuids.add(uuid)
                    usage = msg.get('usage', {})
                    if usage:
                        current_turn.input_tokens += usage.get('input_tokens', 0)
                        current_turn.input_tokens += usage.get('cache_creation_input_tokens', 0)
                        current_turn.input_tokens += usage.get('cache_read_input_tokens', 0)
                        current_turn.output_tokens += usage.get('output_tokens', 0)
                        current_turn.cache_creation_tokens += usage.get('cache_creation_input_tokens', 0)
                        current_turn.cache_read_tokens += usage.get('cache_read_input_tokens', 0)

                        details = usage.get('output_tokens_details', {})
                        current_turn.thinking_tokens += details.get('thinking_tokens', 0)

                current_turn.model = msg.get('model', '') or current_turn.model
                current_turn.stop_reason = msg.get('stop_reason', '') or current_turn.stop_reason
                current_turn.assistant_timestamp = r.get('timestamp', '') or current_turn.assistant_timestamp
                current_turn.assistant_uuid = uuid or current_turn.assistant_uuid

        session.turns = turns

    def _extract_tools_and_agents(self, records: List[Dict], session: ParsedSession):
        """Extract available tools, agents, skills, and agent invocations."""
        tool_names_used = set()

        for r in records:
            rtype = r.get('type')

            if rtype == 'attachment':
                att = r.get('attachment', {})
                att_type = att.get('type')

                if att_type == 'deferred_tools_delta':
                    names = att.get('addedNames', [])
                    session.available_tools = list(set(session.available_tools + names))

                elif att_type == 'agent_listing_delta':
                    agent_types = att.get('addedTypes', [])
                    session.available_agents = list(set(session.available_agents + agent_types))

                elif att_type == 'skill_listing':
                    skill_names = att.get('names', [])
                    session.available_skills = list(set(session.available_skills + skill_names))

            elif rtype == 'assistant':
                msg = r.get('message', {})
                for item in msg.get('content', []):
                    if isinstance(item, dict) and item.get('type') == 'tool_use':
                        tool_names_used.add(item.get('name', ''))

            # Detect agent invocations from tool_use + tool_result pairs
            if rtype == 'user':
                raw = r.get('toolUseResult', {})
                if isinstance(raw, dict) and raw.get('agentType'):
                    inv = AgentInvocation(
                        agent_type=raw.get('agentType', ''),
                        description=raw.get('prompt', '')[:200],
                        prompt=raw.get('prompt', ''),
                        agent_id=raw.get('agentId', ''),
                        status=raw.get('status', ''),
                        model=raw.get('resolvedModel', ''),
                        total_tokens=raw.get('totalTokens', 0),
                        tool_use_count=raw.get('totalToolUseCount', 0),
                        duration_ms=raw.get('totalDurationMs', 0),
                    )
                    # Extract result summary from content
                    content_list = raw.get('content', [])
                    for c in content_list:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            inv.result_summary = c.get('text', '')[:500]
                    session.agent_invocations.append(inv)

        session.tool_names_used = sorted(tool_names_used)

    def _compute_totals(self, session: ParsedSession):
        """Sum up token counts across all turns."""
        for turn in session.turns:
            session.total_input_tokens += turn.input_tokens
            session.total_output_tokens += turn.output_tokens
            session.total_cache_creation_tokens += turn.cache_creation_tokens
            session.total_cache_read_tokens += turn.cache_read_tokens
            session.total_thinking_tokens += turn.thinking_tokens

    def _extract_text_content(self, content) -> str:
        """Extract plain text from message content (string or list)."""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get('type') == 'text' and 'text' in item:
                        parts.append(item['text'])
                elif isinstance(item, str):
                    parts.append(item)
            return '\n'.join(parts).strip()
        return ''

    def _extract_tool_results(self, content) -> List[Dict[str, Any]]:
        """Extract tool_result items from message content."""
        results = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'tool_result':
                    results.append({
                        'tool_use_id': item.get('tool_use_id', ''),
                        'content': self._truncate(
                            item.get('content', ''), 2000
                        ),
                        'is_error': item.get('is_error', False),
                    })
        return results

    @staticmethod
    def _truncate(text, max_len: int) -> str:
        if isinstance(text, str):
            return text[:max_len] + ('…' if len(text) > max_len else '')
        return str(text)[:max_len]


# ── Utility: discover JSONL files on Windows ────────────────────────

def discover_windows_jsonl_sessions(
    base_path: Optional[str] = None,
) -> List[Path]:
    """
    Find all Claude Code JSONL session files.

    Default search paths (Windows):
        %USERPROFILE%\\.claude\\projects\\*\\*.jsonl

    Also works on macOS/Linux:
        ~/.claude/projects/*/*.jsonl
    """
    if base_path:
        roots = [Path(base_path)]
    else:
        home = Path.home()
        roots = [
            home / ".claude" / "projects",
        ]

    found = []
    for root in roots:
        if not root.exists():
            continue
        for jsonl_file in root.glob("**/*.jsonl"):
            if jsonl_file.is_file() and jsonl_file.stat().st_size > 100:
                found.append(jsonl_file)

    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def session_to_dict(session: ParsedSession) -> Dict[str, Any]:
    """Convert a ParsedSession to a JSON-serializable dict."""
    return {
        'session_id': session.session_id,
        'file_path': session.file_path,
        'title': session.title,
        'cwd': session.cwd,
        'git_branch': session.git_branch,
        'entrypoint': session.entrypoint,
        'version': session.version,
        'model': session.model,
        'started_at': session.started_at,
        'ended_at': session.ended_at,
        'total_input_tokens': session.total_input_tokens,
        'total_output_tokens': session.total_output_tokens,
        'total_cache_creation_tokens': session.total_cache_creation_tokens,
        'total_cache_read_tokens': session.total_cache_read_tokens,
        'total_thinking_tokens': session.total_thinking_tokens,
        'tool_names_used': session.tool_names_used,
        'available_tools': session.available_tools,
        'available_agents': session.available_agents,
        'available_skills': session.available_skills,
        'last_prompt': session.last_prompt,
        'file_size_bytes': session.file_size_bytes,
        'record_count': session.record_count,
        'turn_count': len(session.turns),
        'agent_invocation_count': len(session.agent_invocations),
        'turns': [
            {
                'turn_index': t.turn_index,
                'user_text': t.user_text,
                'user_timestamp': t.user_timestamp,
                'assistant_text': t.assistant_text,
                'assistant_timestamp': t.assistant_timestamp,
                'model': t.model,
                'stop_reason': t.stop_reason,
                'thinking_text': t.thinking_text[:500] if t.thinking_text else '',
                'thinking_tokens': t.thinking_tokens,
                'input_tokens': t.input_tokens,
                'output_tokens': t.output_tokens,
                'cache_creation_tokens': t.cache_creation_tokens,
                'cache_read_tokens': t.cache_read_tokens,
                'tool_calls': [
                    {
                        'name': tc.name,
                        'input': _summarize_tool_input(tc),
                        'result_preview': (tc.result or '')[:300],
                        'is_error': tc.is_error,
                    }
                    for tc in t.tool_calls
                ],
                'is_meta': t.is_meta,
            }
            for t in session.turns
        ],
        'agent_invocations': [
            {
                'agent_type': a.agent_type,
                'description': a.description,
                'status': a.status,
                'model': a.model,
                'total_tokens': a.total_tokens,
                'tool_use_count': a.tool_use_count,
                'duration_ms': a.duration_ms,
                'result_summary': a.result_summary,
            }
            for a in session.agent_invocations
        ],
    }


def _summarize_tool_input(tc: ToolCall) -> str:
    """Create a short summary of tool call inputs."""
    args = tc.input_args
    if not args:
        return ''

    if tc.name in ('Read', 'read'):
        return args.get('file_path', args.get('path', ''))
    elif tc.name in ('Edit', 'Write', 'edit', 'write', 'create'):
        return args.get('file_path', args.get('path', ''))
    elif tc.name in ('Grep', 'grep'):
        pattern = args.get('pattern', '')
        path = args.get('path', '')
        return f'/{pattern}/ in {path}' if path else f'/{pattern}/'
    elif tc.name in ('Glob', 'glob'):
        return args.get('pattern', '')
    elif tc.name in ('Bash', 'bash'):
        cmd = args.get('command', '')
        return cmd[:120]
    elif tc.name == 'Agent':
        return args.get('description', args.get('prompt', ''))[:100]
    else:
        # Generic: show first key=value pairs
        pairs = [f'{k}={str(v)[:60]}' for k, v in list(args.items())[:3]]
        return ', '.join(pairs)
