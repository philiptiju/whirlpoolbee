"""Deterministic natural-language query parser for the Ask page.

WhirlpoolBee is positioned as a fully local/offline analytics tool, so this
module intentionally does NOT call any external LLM API. Instead it uses a
small regex/keyword-based intent parser to translate questions like
"How much did Team Alpha spend last week?" into a structured query
(metric, scope, time range) that is then executed against the existing
local SQLite database via parameterized SQL. This keeps the feature
private-by-default and dependency-free, at the cost of only supporting a
bounded vocabulary of phrasings (documented in the Ask page's example
chips).
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Metric vocabulary: keyword -> canonical metric name
# ---------------------------------------------------------------------------
METRIC_KEYWORDS = {
    "cost": "cost", "spend": "cost", "spent": "cost", "spending": "cost",
    "money": "cost", "$": "cost", "budget": "cost",
    "token": "tokens", "tokens": "tokens",
    "session": "sessions", "sessions": "sessions",
    "hour": "hours", "hours": "hours", "time": "hours",
    "commit": "commits", "commits": "commits",
    "pr": "pull_requests", "prs": "pull_requests",
    "pull request": "pull_requests", "pull requests": "pull_requests",
}

METRIC_LABELS = {
    "cost": "cost", "tokens": "tokens used", "sessions": "sessions",
    "hours": "hours", "commits": "commits", "pull_requests": "pull requests",
}

# Ordered so longer/more-specific phrases are tried first
TIME_PATTERNS = [
    (r"\btoday\b", lambda now: (now.replace(hour=0, minute=0, second=0, microsecond=0), now, "today")),
    (r"\byesterday\b", lambda now: (
        (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        "yesterday",
    )),
    (r"\bthis week\b", lambda now: (now - timedelta(days=now.weekday()), now, "this week")),
    (r"\blast week\b", lambda now: (
        now - timedelta(days=now.weekday() + 7),
        now - timedelta(days=now.weekday()),
        "last week",
    )),
    (r"\bthis month\b", lambda now: (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now, "this month")),
    (r"\blast month\b", lambda now: _last_month_range(now)),
    (r"\blast (\d+) days?\b", lambda now, m: (now - timedelta(days=int(m.group(1))), now, f"last {m.group(1)} days")),
    (r"\bpast (\d+) days?\b", lambda now, m: (now - timedelta(days=int(m.group(1))), now, f"past {m.group(1)} days")),
    (r"\blast 24 hours?\b", lambda now: (now - timedelta(hours=24), now, "last 24 hours")),
    (r"\ball time\b", lambda now: (datetime(2000, 1, 1), now, "all time")),
]


def _last_month_range(now: datetime):
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev_month = first_of_this_month - timedelta(seconds=1)
    first_of_prev_month = last_of_prev_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_of_prev_month, first_of_this_month, "last month"


# Words that signal the user wants a ranking/breakdown ("who spent the
# most", "leaderboard", "top users") rather than a single aggregate total.
RANK_PATTERN = re.compile(r"\b(who|top|leaderboard|highest|most|ranking|rank)\b")


@dataclass
class ParsedQuery:
    metric: Optional[str] = None
    scope_type: str = "all"  # 'all' | 'team' | 'user' | 'me'
    scope_value: Optional[str] = None
    scope_label: Optional[str] = None
    start: datetime = field(default_factory=lambda: datetime.utcnow() - timedelta(days=30))
    end: datetime = field(default_factory=datetime.utcnow)
    time_label: str = "last 30 days"
    rank: bool = False
    matched: bool = True
    unmatched_reason: Optional[str] = None


def parse_query(question: str, teams: list, users: list) -> ParsedQuery:
    """Parse a free-text question into a structured query plan.

    `teams` is a list of known group/team names; `users` is a list of known
    *normalized* usernames -- the lowercased email/username local-part (see
    Database._identity_local_part), not the raw 'Name <email>' strings
    stored on machines.logged_in_user, so a literal mention of someone's
    name/username in the question can actually match (both gathered from
    the live DB by the caller so new machines/groups are recognized
    without code changes).
    """
    q = question.strip()
    ql = q.lower()
    now = datetime.utcnow()
    result = ParsedQuery(start=now - timedelta(days=30), end=now, time_label="last 30 days")

    # --- Ranking intent ("who spent the most", "leaderboard", "top users") ---
    result.rank = bool(RANK_PATTERN.search(ql))

    # --- Metric ---
    metric = None
    for kw in sorted(METRIC_KEYWORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(kw)}\b", ql):
            metric = METRIC_KEYWORDS[kw]
            break
    # A ranking question doesn't always name an explicit metric ("who's the
    # top in the leaderboard?") -- default to cost, the most common framing
    # for "who's using/spending the most" style questions.
    if metric is None and result.rank:
        metric = "cost"
    result.metric = metric

    # --- Time range (first matching pattern wins) ---
    for pattern, builder in TIME_PATTERNS:
        m = re.search(pattern, ql)
        if m:
            try:
                start, end, label = builder(now, m) if m.groups() else builder(now)
            except TypeError:
                start, end, label = builder(now)
            result.start, result.end, result.time_label = start, end, label
            break

    # --- Scope: team name, "me", or a known username ---
    team_match = re.search(r"\bteam\s+([a-z0-9][\w\- ]*?)(?:\s+(?:spend|spent|cost|use|used|do|doing|team)\b|[?.,]|$)", ql)
    if team_match:
        candidate = team_match.group(1).strip()
        matched_team = next((t for t in teams if t.lower() == candidate or candidate in t.lower() or t.lower() in candidate), None)
        result.scope_type = "team"
        result.scope_value = matched_team or candidate
        result.scope_label = matched_team or candidate.title()
        if not matched_team:
            result.matched = False
            result.unmatched_reason = f"No team named \"{candidate}\" was found."
    elif re.search(r"\b(i|my|me)\b", ql.split("team")[0] if "team" in ql else ql):
        result.scope_type = "me"
        result.scope_label = "you"
    else:
        # Fallback: a known team name mentioned without the literal word
        # "team" in front of it (e.g. "How much did BB spend last week?").
        fallback_team = next((t for t in teams if t and re.search(rf"\b{re.escape(t.lower())}\b", ql)), None)
        if fallback_team:
            result.scope_type = "team"
            result.scope_value = fallback_team
            result.scope_label = fallback_team
        else:
            for u in sorted(users, key=len, reverse=True):
                if u and re.search(rf"\b{re.escape(u.lower())}\b", ql):
                    result.scope_type = "user"
                    result.scope_value = u
                    result.scope_label = u
                    break

    if metric is None:
        result.matched = False
        result.unmatched_reason = (
            result.unmatched_reason
            or "Couldn't tell which metric you're asking about (try cost, tokens, sessions, hours, commits, or PRs)."
        )

    return result
