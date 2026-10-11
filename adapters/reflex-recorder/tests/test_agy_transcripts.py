"""agy_transcripts: hand-encoded protobuf mirroring the inferred schema."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import agy_transcripts as ag  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "agy"


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def vi(f: int, n: int) -> bytes:
    return varint(f << 3) + varint(n)


def ld(f: int, b) -> bytes:
    if isinstance(b, str):
        b = b.encode()
    return varint(f << 3 | 2) + varint(len(b)) + b


def ts(sec: int) -> bytes:
    return ld(1, vi(1, sec) + vi(2, 5))


SCHEMA = """
CREATE TABLE trajectory_meta (trajectory_id text, cascade_id text, trajectory_type integer, source integer);
CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer NOT NULL DEFAULT 0,
  status integer NOT NULL DEFAULT 0, has_subtrajectory numeric NOT NULL DEFAULT false,
  metadata blob, error_details blob, permissions blob, task_details blob, render_info blob,
  step_payload blob, step_format integer NOT NULL DEFAULT 0);
CREATE TABLE gen_metadata (idx integer PRIMARY KEY, data blob, size integer NOT NULL DEFAULT 0);
CREATE TABLE trajectory_metadata_blob (id text DEFAULT 'main', data blob);
"""

T0 = 1_780_000_000


def make_db(path: Path, steps, workspace="file:///work/proj", model="MODEL_PLACEHOLDER_M9"):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for s in steps:
        con.execute("INSERT INTO steps(idx,step_type,status,metadata,error_details,step_payload)"
                    " VALUES (?,?,?,?,?,?)", s)
    kv = ld(20, ld(1, "model_enum") + ld(2, model))
    con.execute("INSERT INTO gen_metadata(idx,data) VALUES (0,?)", (ld(1, kv),))
    con.execute("INSERT INTO trajectory_metadata_blob(data) VALUES (?)",
                (ld(1, ld(1, workspace) + ld(2, workspace)),))
    con.commit()
    con.close()


def ts_meta(sec, extra=b""):
    return ts(sec) + vi(3, 2) + extra


def tool_meta(name, args, sec):
    return ts_meta(sec, ld(4, ld(2, name) + ld(3, args) + ld(9, name)))


def build(tmp: Path) -> Path:
    root = tmp / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)
    usage = ld(9, vi(1, 1196) + vi(2, 10102) + vi(3, 477) + vi(5, 8154) + vi(9, 396) + vi(10, 81))
    steps = [
        (0, 14, 3, ts_meta(T0), None, vi(1, 14) + ld(19, ld(2, "hello user"))),
        (1, 15, 3, ts_meta(T0 + 1, usage), None, vi(1, 15) + ld(20, ld(7, ld(2, "view_file")))),
        (2, 21, 3, tool_meta("run_command", json.dumps({"CommandLine": "ls SENTINEL_CMD"}), T0 + 2),
         None, vi(1, 21) + ld(28, b"xxxx")),
        (3, 132, 7, tool_meta("manage_task", "{}", T0 + 3), b"\x01", vi(1, 132) + ld(140, b"zz")),
        (4, 23, 3, ts_meta(T0 + 4), None, vi(1, 23) + ld(30, b"q")),
        (5, 8, 3, b"\xff\xff\xff", None, b"\x80"),          # truncated varint -> unknown
        (6, 8, 3, ld(4, b"\x0a\xff\x7f"), None, None),       # bad inner len -> unknown
    ]
    make_db(root / "conversations" / "c1.db", steps)
    (root / "conversations" / "c2.pb").write_bytes(bytes(range(256)) * 4)   # opaque
    (root / "history.jsonl").write_text(json.dumps(
        {"conversationId": "c2", "workspace": "/hist/ws", "timestamp": 1, "display": "d"}) + "\n")
    return root


def test_cli_conversations_and_steps(tmp_path):
    root = build(tmp_path)
    convs = {c.id: c for c in ag.iter_conversations([str(root)])}
    c1, c2 = convs["c1"], convs["c2"]
    assert c1.source == ag.SRC_CLI and c1.decodable
    assert c1.workspace == "/work/proj" and c1.model == "MODEL_PLACEHOLDER_M9"
    assert c1.started == "2026-05-28T20:26:40Z" and c1.ended > c1.started
    assert not c2.decodable and c2.workspace == "/hist/ws"
    assert list(ag.iter_steps(c2)) == []

    steps = list(ag.iter_steps(c1, with_text=True))
    kinds = [s.kind for s in steps]
    assert kinds == ["user", "model", "tool_call", "tool_call", "other", "unknown", "unknown"]
    assert steps[0].text == "hello user"
    m = steps[1]
    assert m.n_tool_calls == 1
    assert m.tokens == {"model_enum": 1196, "input": 10102, "output": 477,
                        "reasoning": 396, "response": 81, "cache_read": 8154}
    assert m.tokens["output"] == m.tokens["reasoning"] + m.tokens["response"]
    t = steps[2]
    assert (t.tool, t.command, t.status, t.error) == ("run_command", "ls SENTINEL_CMD", 3, False)
    assert steps[3].tool == "manage_task" and steps[3].error and steps[3].command is None
    assert steps[3].payload_bytes == 2
    assert ag.tool_histogram(c1) == {"run_command": 1, "manage_task": 1}


def test_ide_overview_and_opaque_and_dedupe(tmp_path):
    for d in ("antigravity", "antigravity-ide"):
        r = tmp_path / d
        (r / "conversations").mkdir(parents=True)
        (r / "conversations" / "aa.pb").write_bytes(b"\x00" * 64)
        (r / "conversations" / "bb.pb").write_bytes(b"\x01" * 64)
        logs = r / "brain" / "aa" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        shutil.copy(FIX / "overview.txt", logs / "overview.txt")
    convs = list(ag.iter_conversations([str(tmp_path / "antigravity"), str(tmp_path / "antigravity-ide")]))
    assert sorted(c.id for c in convs) == ["aa", "bb"]       # deduped across both roots
    aa = next(c for c in convs if c.id == "aa")
    bb = next(c for c in convs if c.id == "bb")
    assert aa.source == ag.SRC_IDE and aa.decodable and not bb.decodable
    steps = list(ag.iter_steps(aa))
    assert [s.kind for s in steps] == ["user", "model", "tool_call", "tool_call", "unknown", "other"]
    assert [s.tool for s in steps if s.kind == "tool_call"] == ["run_command", "view_file"]
    assert steps[2].command == "echo SENTINEL_CMD"
    assert aa.started == "2026-05-12T02:34:09Z" and aa.ended == "2026-05-12T02:35:00Z"


def test_summary_counts_and_never_leaks_content(tmp_path, capsys):
    root = build(tmp_path)
    assert ag.main(["summary", "--json", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "SENTINEL_CMD" not in out and "hello user" not in out
    s = json.loads(out)["agy-cli"]
    assert s["conversations"] == 2 and s["opaque_conversations"] == 1
    assert s["steps"] == 7 and s["steps_by_kind"]["unknown"] == 2
    assert s["tokens"]["output"] == 477 and s["tokens"]["model_steps_with_usage"] == 1
    assert {"tool": "run_command", "count": 1} in s["top_tools"]


def test_garbage_db_never_crashes(tmp_path):
    root = tmp_path / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)
    (root / "conversations" / "bad.db").write_bytes(b"not sqlite" * 50)
    convs = list(ag.iter_conversations([str(root)]))
    assert len(convs) == 1
    assert list(ag.iter_steps(convs[0])) == []
