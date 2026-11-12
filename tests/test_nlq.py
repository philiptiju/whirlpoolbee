"""Unit tests for the Ask page's deterministic NL query parser.

These are regression tests for a reported bug: "most questions are not
working". Root cause was that machines.logged_in_user is stored as raw
identity strings (e.g. 'Jane Doe <jane.doe@example.com>'), but
scope resolution ('me'/username matching) compared that raw string
directly against question text / the authenticated caller's email local
part, which essentially never matched. Callers must now pass *normalized*
(lowercased local-part) usernames into parse_query, matching what
Database.nlq_known_users() returns post-fix.
"""
from src.utils.nlq import parse_query

TEAMS = ["BB", "Arch", "Palette"]
USERS = ["jane.doe", "john.smith", "bob"]


def test_self_scope_detected_for_i_my_me():
    parsed = parse_query("How many sessions did I have this month?", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.metric == "sessions"
    assert parsed.scope_type == "me"
    assert parsed.time_label == "this month"


def test_explicit_team_keyword_matches_known_team():
    parsed = parse_query("How much did Team BB spend last week?", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.metric == "cost"
    assert parsed.scope_type == "team"
    assert parsed.scope_value == "BB"
    assert parsed.time_label == "last week"


def test_unknown_team_name_is_reported_as_unmatched():
    parsed = parse_query("How much did Team Alpha spend last week?", TEAMS, USERS)
    assert parsed.matched is False
    assert "alpha" in parsed.unmatched_reason.lower()


def test_team_name_recognized_without_literal_team_keyword():
    """Regression test: 'How much did BB spend last week?' (no literal word
    'team') should still resolve to the BB team, not silently fall through
    to the all-everyone scope."""
    parsed = parse_query("How much did BB spend last week?", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.scope_type == "team"
    assert parsed.scope_value == "BB"


def test_known_username_mentioned_in_question_resolves_to_user_scope():
    parsed = parse_query("How many tokens did john.smith use yesterday?", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.scope_type == "user"
    assert parsed.scope_value == "john.smith"
    assert parsed.time_label == "yesterday"


def test_ranking_intent_detected_for_who_questions():
    parsed = parse_query("Who is spending more money", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.rank is True
    assert parsed.metric == "cost"


def test_ranking_intent_without_explicit_metric_defaults_to_cost():
    """Regression test: 'Who is the top in the leaderboard' has no metric
    keyword at all (no 'cost'/'tokens'/etc.), so previously this failed
    with 'could not tell which metric'. A bare ranking question should
    default to cost instead of failing outright."""
    parsed = parse_query("Who is the top in the leaderboard", TEAMS, USERS)
    assert parsed.matched is True
    assert parsed.rank is True
    assert parsed.metric == "cost"


def test_non_ranking_question_does_not_set_rank():
    parsed = parse_query("How many hours this week?", TEAMS, USERS)
    assert parsed.rank is False


def test_no_recognizable_metric_is_unmatched():
    parsed = parse_query("What's the weather like today?", TEAMS, USERS)
    assert parsed.matched is False
    assert "metric" in parsed.unmatched_reason
