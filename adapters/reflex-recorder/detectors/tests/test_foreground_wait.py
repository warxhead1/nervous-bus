"""foreground_wait: known answer on a real (redacted) lane + negative controls.

start_grid_waits is a lane that polled its own detached gate runs in the
foreground (`until grep -q "CHECK PASS" log; do sleep 10; done`). podbot_sweep
spent hours in foreground harness calls but never polled, so it is the
negative control that separates "long foreground call" from "foreground wait".
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from detectors.base import ensure_detector_schema  # noqa: E402
from detectors.foreground_wait import (  # noqa: E402
    MIN_WAIT_MIN, ForegroundWaitDetector, fires, wait_calls, wait_minutes,
)
from transcript_steps import Step, is_poll_wait, parse_transcript  # noqa: E402

FIX = _ENGINE / "tests" / "fixtures" / "transcripts"


def _calls(name):
    return wait_calls(parse_transcript(FIX / f"{name}.jsonl"))


def test_fires_on_the_lane_that_polled_its_gates():
    c = _calls("start_grid_waits")
    assert fires(c)
    assert len(c) == 8 and round(wait_minutes(c), 1) == 23.9
    assert max(s.duration for s in c) / 60 > 7


def test_silent_on_long_foreground_work_that_never_polls():
    for name in ("podbot_sweep", "playable", "rocks_off_line", "yaw_settle"):
        c = _calls(name)
        assert not fires(c), name
        assert wait_minutes(c) < MIN_WAIT_MIN / 2


def test_poll_wait_shapes():
    assert is_poll_wait('until command grep -aq "CHECK PASS" log; do sleep 10; done; tail log')
    assert is_poll_wait("for i in $(seq 1 55); do if grep -q X L; then break; fi; sleep 10; done")
    assert is_poll_wait("n=0; while [ $n -lt 9 ]; do\n  sleep 5\n  n=$((n+1))\ndone")
    assert is_poll_wait("sleep 120 && tail -5 log")
    assert is_poll_wait("cargo build; sleep 2m; cat out")
    assert not is_poll_wait("sleep 2 && curl -s localhost:5173")          # short settle
    assert not is_poll_wait("for s in 1 2 3; do ./bench $s; done")          # serial, not a wait
    assert not is_poll_wait("python3 - <<'EOF'\nimport time\n# sleep 60\nEOF")
    assert not is_poll_wait("grep -n 'sleep 30' scripts/wait.sh")


def _bash(cmd, t0, dur, bg=False):
    inp = {"command": cmd}
    if bg:
        inp["run_in_background"] = True
    return Step(id=str(t0), tool="Bash", input=inp, t0=t0, t1=t0 + dur)


def test_background_waits_do_not_count():
    loop = "until grep -q DONE log; do sleep 10; done"
    assert fires(wait_calls([_bash(loop, 0, 600)]))
    assert wait_calls([_bash(loop, 0, 600, bg=True)]) == []


def test_detector_through_the_store():
    fx = FIX / "start_grid_waits.jsonl"
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, started TEXT, ended TEXT,
                           event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    for rid, path in (("RW", fx), ("RN", FIX / "podbot_sweep.jsonl")):
        st = parse_transcript(path)
        conn.execute("INSERT INTO runs VALUES (?, 'demo', ?, ?, ?)",
                     (rid, iso(st[0].t0), iso(st[-1].t0), len(st)))
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) "
                     "VALUES (?, 1, ?, ?)",
                     (rid, iso(st[0].t0), json.dumps({"data": {"transcript_path": str(path)}})))
    det = ForegroundWaitDetector(conn)
    det.transcript_roots = []
    cands = det.run(conn)
    assert [c.run_ids for c in cands] == [["RW"]]
    c = cands[0]
    assert c.extra["wait_calls"] == 8 and c.extra["wait_min"] == 23.9
    assert c.signature == "demo:foreground_wait"
    assert any("foreground poll-wait calls" in e for e in c.evidence)
