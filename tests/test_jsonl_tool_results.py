import json

from src.parsers.jsonl_sessions import JsonlSessionParser


def test_tool_results_follow_ids_across_turns_and_orders():
    records = [
        {"type": "user", "message": {"content": "Start"}},
        {"type": "assistant", "uuid": "a", "message": {"content": [
            {"type": "tool_use", "id": "first", "name": "A", "input": {}},
            {"type": "tool_use", "id": "second", "name": "B", "input": {}},
        ]}},
        {"type": "user", "toolUseResult": "raw A", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "first", "content": "A"},
        ]}},
        {"type": "user", "message": {"content": "Next turn"}},
        {"type": "user", "toolUseResult": "raw B", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "second", "content": "B", "is_error": True},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "unknown", "content": "unmatched"},
        ]}},
    ]
    session = JsonlSessionParser().parse_content("\n".join(map(json.dumps, records)))
    assert [t.result for t in session.turns[0].tool_calls] == ["raw A", "raw B"]
    assert session.turns[0].tool_calls[1].is_error
    assert len(session.turns[0].tool_results) == 2
    assert session.turns[1].tool_results[0]["content"] == "unmatched"


def test_multiple_results_use_individual_content_not_shared_raw_result():
    records = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "A"},
            {"type": "tool_use", "id": "b", "name": "B"},
        ]}},
        {"type": "user", "toolUseResult": "ambiguous", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "result A"},
            {"type": "tool_result", "tool_use_id": "b", "content": "result B"},
        ]}},
    ]
    session = JsonlSessionParser().parse_content("\n".join(map(json.dumps, records)))
    assert [t.result for t in session.turns[0].tool_calls] == ["result A", "result B"]
