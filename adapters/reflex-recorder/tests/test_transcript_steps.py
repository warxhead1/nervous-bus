"""transcript_steps: transcript -> steps, run -> transcript, and edit extraction."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcript_steps import (  # noqa: E402
    EditTracker, Step, bash_edits, is_numeric_only, mask_numbers, numbers_in,
    parse_transcript_lines, parse_ts, resolve_transcript,
)


def _rec(kind, ts, blocks):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"role": kind, "content": blocks}})


def test_pairs_tool_use_with_result_and_skips_noise_records():
    lines = [
        json.dumps({"type": "attachment", "content": "x" * 50}),
        json.dumps({"type": "api-request", "body": {}}),
        _rec("assistant", "2026-10-24T01:00:00Z",
             [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}},
              {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/x.rs"}}]),
        _rec("user", "2026-10-24T01:00:02.5Z",
             [{"type": "tool_result", "tool_use_id": "b", "content": [{"type": "text", "text": "body"}]}]),
        _rec("user", "2026-10-24T01:00:10Z",
             [{"type": "tool_result", "tool_use_id": "a", "content": "out", "is_error": True}]),
        "not json",
    ]
    steps = parse_transcript_lines(lines)
    assert [s.id for s in steps] == ["a", "b"]
    a, b = steps
    assert a.command == "ls" and a.duration == 10.0 and a.output == "out" and a.is_error
    assert b.file_path == "/x.rs" and b.duration == 2.5 and b.output == "body"


def test_parse_ts_accepts_nanosecond_bus_stamps():
    assert parse_ts("2026-10-24T01:00:00.123456789Z") == parse_ts("2026-10-24T01:00:00.123456Z")
    assert parse_ts("") is None and parse_ts(None) is None


def test_resolve_subagent_transcript_under_a_mirror_root(tmp_path):
    live, mirror = tmp_path / "live", tmp_path / "mirror"
    host = live / "-proj" / "sess1.jsonl"
    sub = mirror / "-proj" / "sess1" / "subagents" / "agent-abc.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text("")
    import transcript_steps as ts
    old = (ts.LIVE_ROOT, ts.MIRROR_ROOT)
    ts.LIVE_ROOT, ts.MIRROR_ROOT = live, mirror
    try:
        got = resolve_transcript(str(host), "sess1", "abc", roots=[live, mirror])
        assert got == sub
        assert resolve_transcript(str(host), "sess1", "", roots=[live, mirror]) is None
    finally:
        ts.LIVE_ROOT, ts.MIRROR_ROOT = old


def test_number_masking_ignores_digits_inside_identifiers():
    assert numbers_in("const T63_MAX: f64 = 0.35;") == ["0.35"]
    assert numbers_in("w12 = 3e-4_f32 + 7") == ["3e-4_f32", "7"]
    assert mask_numbers("x < 1.0") == mask_numbers("x < 3.0")
    assert is_numeric_only("let k = 1.0;", "let k = 2.5;")
    assert not is_numeric_only("let k = 1.0;", "let j = 1.0;")
    assert not is_numeric_only(None, "let k = 1.0;")


def test_sed_python_and_append_edits():
    sed = bash_edits("sed -i 's/K: f64 = 1.0;/K: f64 = 2.0;/; s/a/b/' crates/x/src/lib.rs")
    assert [(e.old, e.new, e.path, e.via) for e in sed][:1] == \
        [("K: f64 = 1.0;", "K: f64 = 2.0;", "crates/x/src/lib.rs", "sed")]
    heredoc = bash_edits(
        "python3 - <<'EOF'\np='crates/x/tests/t.rs'\ns=open(p).read()\n"
        "s=s.replace('MAX: f64 = 0.35','MAX: f64 = 0.45')\nopen(p,'w').write(s)\nEOF")
    assert [(e.path, e.old, e.new) for e in heredoc] == \
        [("crates/x/tests/t.rs", "MAX: f64 = 0.35", "MAX: f64 = 0.45")]
    helper = bash_edits(
        "python3 - <<'PY'\np='a/b.ts'\ns=open(p).read()\n"
        "def rep(a, b):\n    global s\n    s = s.replace(a, b)\nrep('tol = 1e-6', 'tol = 1e-3')\nPY")
    assert [(e.old, e.new) for e in helper] == [("tol = 1e-6", "tol = 1e-3")]
    inline = bash_edits("python3 -c \"import re; open('x.py').read().replace('n = 1', 'n = 2')\"")
    assert [(e.path, e.old, e.new) for e in inline] == [("x.py", "n = 1", "n = 2")]
    app = bash_edits("cat >> scripts/check.budgets <<'EOF'\nweb 170\nEOF")
    assert [(e.path, e.old) for e in app] == [("scripts/check.budgets", None)]


def test_write_is_diffed_against_the_last_body_written():
    tr = EditTracker()
    w = lambda body, t: Step(id=str(t), tool="Write", t0=t,
                             input={"file_path": "/r/t.rs", "content": body})
    first = tr.edits(w("a\nconst MAX: f64 = 1.0;\nb", 0))
    assert [(f.old, f.via) for f in first] == [(None, "Write")]
    second = tr.edits(w("a\nconst MAX: f64 = 2.0;\nb", 1))
    assert [(f.old, f.new) for f in second] == [("const MAX: f64 = 1.0;", "const MAX: f64 = 2.0;")]
