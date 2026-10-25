"""token_ledger tool times + token_report lanes / tool_time sections."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402
import token_report as tr  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(hours=2)


def _ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _asst(mid, sec, blocks, ctx=40_000):
    return json.dumps({"type": "assistant", "timestamp": _ts(sec), "sessionId": "s1",
                       "agentId": "aLANE1", "cwd": "/home/u/data2/worktrees/proj/wt",
                       "message": {"id": mid, "model": "m1", "content": blocks,
                                   "usage": {"input_tokens": 1, "cache_read_input_tokens": ctx,
                                             "cache_creation_input_tokens": 0, "output_tokens": 9}}})


def _use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _res(tid, sec, text="ok"):
    return json.dumps({"type": "user", "timestamp": _ts(sec), "message": {
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}})


def _blob(tid):
    # api-request-blob replays results with no timestamp; must not clobber them
    return json.dumps({"type": "api-request-blob", "message": {
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x" * 999}]}})


LANE = [
    _asst("m1", 0, [_use("t1", "Read", file_path="/a.rs")]),
    _res("t1", 2),
    _asst("m2", 10, [_use("t2", "Bash", command="until grep -q DONE log; do sleep 10; done")]),
    _res("t2", 310),
    _blob("t2"),
    _asst("m3", 320, [_use("t3", "Bash", command="for s in 1 2 3; do ./target/release/bench $s; done")]),
    _res("t3", 500),
    _asst("m4", 510, [_use("t4", "Edit", file_path="/a.rs", old_string="1", new_string="2")]),
    _res("t4", 511),
    _asst("m5", 520, [_use("t5", "Bash", command="bash scripts/check.sh --fast 2>&1 | tail")]),
    _res("t5", 600),
    _asst("m6", 610, [], ctx=90_000),
]


def _ledger(tmp_path, lines=LANE):
    root = tmp_path / "root" / "-proj" / "s1" / "subagents"
    root.mkdir(parents=True)
    f = root / "agent-aLANE1.jsonl"
    f.write_text("\n".join(lines) + "\n")
    (root / "agent-aLANE1.meta.json").write_text(json.dumps(
        {"agentType": "worker", "model": "sonnet", "description": "SECRET lane description"}))
    db = tmp_path / "tokens.db"
    tl.run(db, roots=[("claude", tmp_path / "root")])
    return db


def test_time_classes():
    tc = tl.time_class
    assert tc("Read") == "Read"
    assert tc("Bash", "until grep -q X log; do sleep 10; done") == "wait"
    assert tc("Bash", "for sd in 1 2 3; do S0=$sd ./target/release/examples/explore a; done") == "loop"
    assert tc("Bash", "for f in *.rs; do echo $f; done") == "other"     # loop, no program run
    assert tc("Bash", "bash scripts/check.sh --fast") == "gate"
    assert tc("Bash", "cd w && cargo nextest run -p x") == "test"
    assert tc("Bash", "cargo build --release") == "build"
    assert tc("Bash", "git -C /w commit -m x") == "commit"
    assert tc("Bash", "cd /w && git status --short") == "git"
    assert tc("Bash", "sed -n 1,40p a.rs; grep -n x b.rs") == "read"


def test_ingest_records_durations_and_classes(tmp_path):
    c = sqlite3.connect(_ledger(tmp_path))
    rows = dict(((i, (cls, round(d or -1))) for i, cls, d in
                 c.execute("SELECT id, time_class, duration_s FROM tool_calls")))
    assert rows == {"t1": ("Read", 2), "t2": ("wait", 300), "t3": ("loop", 180),
                    "t4": ("Edit", 1), "t5": ("gate", 80)}
    # the timestamp-less replay did not overwrite the real result
    assert c.execute("SELECT result_chars FROM tool_calls WHERE id='t2'").fetchone()[0] == 2


def test_backfill_fills_rows_from_before_the_columns(tmp_path):
    db = _ledger(tmp_path)
    c = sqlite3.connect(db)
    c.execute("UPDATE tool_calls SET result_ts=NULL, duration_s=NULL, time_class=NULL")
    c.commit()
    files = tl.discover([("claude", tmp_path / "root")])
    st = tl.backfill_tool_times(c, files, project="proj")
    assert st == {"sessions": 1, "files_read": 1, "missing": 0}
    assert c.execute("SELECT time_class, ROUND(duration_s) FROM tool_calls WHERE id='t2'"
                     ).fetchone() == ("wait", 300)


def test_untimed_lane_reports_the_split_as_unknown(tmp_path):
    conn = sqlite3.connect(_ledger(tmp_path))
    conn.execute("UPDATE tool_calls SET duration_s = NULL")
    since = (T0 - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    [lane] = tr.lanes(conn, since)
    assert lane["tool_min"] is None and lane["model_min"] is None
    assert lane["untimed_calls"] == 5 and lane["wall_min"] > 0


def test_old_db_gets_the_columns_added(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(tl.SCHEMA)
    c.close()
    cols = {r[1] for r in tl.connect(db).execute("PRAGMA table_info(tool_calls)")}
    assert {"result_ts", "duration_s", "time_class"} <= cols


def test_lanes_and_tool_time(tmp_path):
    conn = sqlite3.connect(_ledger(tmp_path))
    since = (T0 - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    [lane] = tr.lanes(conn, since, project="proj", with_description=True)
    assert lane["lane"] == "aLANE1" and lane["turns"] == 6 and lane["model"] == "m1"
    assert lane["wall_min"] == round(610 / 60, 1)
    assert lane["tool_min"] == round((2 + 300 + 180 + 1 + 80) / 60, 1)
    assert lane["turns_to_first_edit"] == 3
    assert (lane["ctx_first"], lane["ctx_last"]) == (40_001, 90_001)
    assert lane["calls_over_60s"] == 3 and list(lane["tool_min_by_class"])[:2] == ["wait", "loop"]
    assert lane["description"] == "SECRET lane description"
    tt = {r["class"]: r for r in tr.tool_time(conn, since)}
    assert tt["wait"]["minutes"] == 5.0 and tt["loop"]["calls_over_60s"] == 1


def test_digest_carries_lanes_but_not_descriptions_or_commands(tmp_path):
    conn = sqlite3.connect(_ledger(tmp_path))
    rep = tr.build(conn, days=1)
    assert rep["lanes"] and rep["tool_time"]
    blob = json.dumps(rep) + tr.to_markdown(rep)
    assert "SECRET" not in blob
    assert "DONE log" not in blob and "release/bench" not in blob
    assert "## Lanes (subagents) by wall time" in tr.to_markdown(rep)
