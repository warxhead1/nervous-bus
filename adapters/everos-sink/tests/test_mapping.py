"""Known-answer tests: a Claude transcript in, the exact EverOS MessageItems out."""
import importlib.util
import json
import os

HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location("everos_sink_mapping", os.path.join(os.path.dirname(HERE), "mapping.py"))
mapping = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mapping)


def _line(**rec):
    return json.dumps(rec)


def _asst(mid, ts, *blocks):
    return _line(type="assistant", timestamp=ts, message={"id": mid, "role": "assistant", "content": list(blocks)})


def _user(ts, content, **extra):
    return _line(type="user", timestamp=ts, message={"role": "user", "content": content}, **extra)


TS = "2026-11-14T10:00:0{}Z"
MS = 1794650400000          # 2026-11-14T10:00:00Z

TRANSCRIPT = [
    _line(type="summary", summary="ignored"),
    _user(TS.format(0), "fix the failing test"),
    _user(TS.format(0), "<command-name>/clear</command-name>", isMeta=True),
    _asst("m1", TS.format(1), {"type": "thinking", "thinking": "hmm"}),
    _asst("m1", TS.format(1), {"type": "text", "text": "Reading it."}),
    _asst("m1", TS.format(1), {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/r/a.py"}}),
    _user(TS.format(2), [{"type": "tool_result", "tool_use_id": "t1", "content": "def f(): pass"}]),
    _asst("m2", TS.format(3), {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest"}},
          {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "git status"}}),
    _user(TS.format(4), [{"type": "tool_result", "tool_use_id": "t2",
                          "content": [{"type": "text", "text": "1 failed"}], "is_error": True},
                         {"type": "tool_result", "tool_use_id": "t3", "content": "clean"}]),
    _user(TS.format(5), [{"type": "tool_result", "tool_use_id": "gone", "content": "orphan"}]),
    _asst("m3", TS.format(6), {"type": "text", "text": "Fixed."}),
    "not json",
]


def test_known_answer():
    items = mapping.claude_messages(TRANSCRIPT)
    assert items == [
        {"sender_id": "operator", "role": "user", "timestamp": MS, "content": "fix the failing test"},
        {"sender_id": "claude-code", "role": "assistant", "timestamp": MS + 1000, "content": "Reading it.",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "Read", "arguments": '{"file_path": "/r/a.py"}'}}]},
        {"sender_id": "claude-code", "role": "tool", "timestamp": MS + 2000, "content": "def f(): pass",
         "tool_call_id": "t1"},
        {"sender_id": "claude-code", "role": "assistant", "timestamp": MS + 3000, "content": "",
         "tool_calls": [
             {"id": "t2", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "pytest"}'}},
             {"id": "t3", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "git status"}'}}]},
        {"sender_id": "claude-code", "role": "tool", "timestamp": MS + 4000, "content": "[error] 1 failed",
         "tool_call_id": "t2"},
        {"sender_id": "claude-code", "role": "tool", "timestamp": MS + 4000, "content": "clean",
         "tool_call_id": "t3"},
        {"sender_id": "claude-code", "role": "assistant", "timestamp": MS + 6000, "content": "Fixed."},
    ]
    assert mapping.tool_rounds(items) == 2


def test_case_gate_needs_three_answered_rounds():
    lines = [_user(TS.format(0), "go")]
    for i in range(3):
        lines.append(_asst(f"m{i}", TS.format(i + 1), {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                        "input": {"command": "ls"}}))
        if i < 2:
            lines.append(_user(TS.format(i + 1), [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}]))
    items = mapping.claude_messages(lines)
    assert mapping.tool_rounds(items) == 2       # the third call never got an answer


def test_long_tool_output_is_capped():
    big = "y" * 10_000
    items = mapping.claude_messages([
        _asst("m", TS.format(1), {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}),
        _user(TS.format(2), [{"type": "tool_result", "tool_use_id": "t", "content": big}])], tool_cap=100)
    assert items[1]["content"].startswith("y" * 100)
    assert "9900 chars cut" in items[1]["content"]


def test_batches_never_split_a_call_from_its_results():
    items = [{"role": "user"}, {"role": "assistant", "tool_calls": [{"id": "a"}]},
             {"role": "tool", "tool_call_id": "a"}, {"role": "tool", "tool_call_id": "a"}, {"role": "user"}]
    chunks = list(mapping.batches(items, size=3))
    assert [len(c) for c in chunks] == [1, 3, 1]
    assert chunks[1][0]["role"] == "assistant"
    assert sum(len(c) for c in chunks) == len(items)


def test_schema_example_validates():
    jsonschema = __import__("pytest").importorskip("jsonschema")
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    with open(os.path.join(root, "schemas", "memory.agent.skill.proposed.v1.json")) as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator(schema).validate({
        "app_id": "nervous-bus", "project_id": "default", "agent_id": "claude-code",
        "skill_name": "check-base-commit", "md_path": "nervous-bus/default/agents/claude-code/skills/skill_check-base-commit/SKILL.md",
        "case_count": 4, "session_ids": ["s1"], "proposed_at": "2026-11-14T10:00:00Z"})
