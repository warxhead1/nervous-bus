"""param_sweep_thrash: known answers on real (redacted) transcripts + unit cases.

Fixtures: tests/fixtures/transcripts/*.jsonl (provenance in make_fixture.py and
the commit that added them). podbot_sweep is the lane a lead stopped after
three hours of hand sweeps; playable swept too but gated and committed; rocks
and yaw are ordinary lanes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from detectors.base import ensure_detector_schema  # noqa: E402
from detectors.param_sweep_thrash import (  # noqa: E402
    MIN_PARAM_RERUNS, MIN_SERIAL_CALLS, MIN_SPAN_MIN, ParamSweepThrashDetector, analyze,
    fires, harness_runs, is_progress, serial_calls, serial_fires, shell_segments, worst,
)
from transcript_steps import Step, parse_transcript  # noqa: E402

FIX = _ENGINE / "tests" / "fixtures" / "transcripts"


def _worst(name):
    return worst(analyze(parse_transcript(FIX / f"{name}.jsonl")))


# ── known answers on real lanes ──────────────────────────────────────────────

def test_fires_on_the_stopped_podbot_lane():
    w = _worst("podbot_sweep")
    assert fires(w)
    assert len(w.reruns) == 82
    assert round(w.span_min, 1) == 178.5
    assert w.end is None                    # no passing test/gate before the run ended
    assert any(r.why.startswith("numeric edit") for r in w.reruns)
    assert any(r.why.startswith("args:") for r in w.reruns)


def test_silent_on_the_gated_playable_lane():
    w = _worst("playable")
    assert not fires(w)
    assert len(w.reruns) == 6 and round(w.span_min, 1) == 26.4


def test_silent_on_rocks_and_yaw_lanes():
    for name in ("rocks_off_line", "yaw_settle"):
        w = _worst(name)
        assert not fires(w), name
        assert len(w.reruns) <= 2


def test_thresholds_separate_the_reference_lanes():
    pos = _worst("podbot_sweep")
    for name in ("playable", "rocks_off_line", "yaw_settle"):
        neg = _worst(name)
        assert len(neg.reruns) < MIN_PARAM_RERUNS <= len(pos.reruns)
        assert neg.span_min < MIN_SPAN_MIN <= pos.span_min


# ── serial-call trigger (>60 s foreground calls looping a harness) ───────────

def _serial(name):
    return serial_calls(parse_transcript(FIX / f"{name}.jsonl"))


def test_serial_trigger_fires_on_the_podbot_lane():
    ser = _serial("podbot_sweep")
    assert serial_fires(ser)
    assert len(ser) == 35 and round(sum(s.duration for s in ser) / 60, 1) == 88.8


def test_serial_trigger_silent_on_gate_waits_and_ordinary_lanes():
    # start_grid_waits holds 8 foreground waits (23.9 min): those are not serial calls
    for name in ("start_grid_waits", "playable", "rocks_off_line", "yaw_settle"):
        ser = _serial(name)
        assert not serial_fires(ser), name
        assert len(ser) < MIN_SERIAL_CALLS / 2


def test_serial_call_shapes():
    loop = _step("for sd in 1 2 3 4; do S0=$sd ./target/release/examples/explore c 3 1; done",
                 t=0)
    loop.t1 = 200
    chain = _step("S=/x/sweep5.sh; $S 22 0.8 expert 16; $S 17 0.8 expert 16", t=300)
    chain.t1 = 420
    short = _step("for sd in 1 2; do ./bench $sd; done", t=500)
    short.t1 = 530
    single = _step("./bench 3", t=600)
    single.t1 = 900
    wait = _step("until grep -q DONE log; do sleep 10; done", t=1000)
    wait.t1 = 1500
    bg = Step(id="bg", tool="Bash", t0=2000, t1=2600, input={
        "command": "for s in 1 2 3; do ./bench $s; done", "run_in_background": True})
    assert serial_calls([loop, chain, short, single, wait, bg]) == [loop, chain]


# ── through the store (run -> transcript resolution) ─────────────────────────

def _store_with_run(transcript: Path, project="demo") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, started TEXT, ended TEXT,
                           event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    steps = parse_transcript(transcript)
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs VALUES ('R1', ?, ?, ?, ?)",
                 (project, iso(steps[0].t0), iso(steps[-1].t0), len(steps)))
    conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES ('R1', 1, ?, ?)",
                 (iso(steps[0].t0), json.dumps({"data": {"transcript_path": str(transcript),
                                                         "session_id": "s"}})))
    return conn


def test_detector_records_a_hit_through_the_store():
    conn = _store_with_run(FIX / "podbot_sweep.jsonl")
    det = ParamSweepThrashDetector(conn)
    det.transcript_roots = []
    cands = det.run(conn)
    assert len(cands) == 1
    c = cands[0]
    assert c.run_ids == ["R1"] and c.extra["param_reruns"] == 82
    assert c.extra["triggers"] == ["param_reruns", "serial_calls"]
    assert c.extra["serial_calls"] == 35
    assert c.signature.startswith("demo:param_sweep_thrash:")
    assert "R1" not in c.signature
    assert any("param reruns over" in e for e in c.evidence)
    assert conn.execute("SELECT COUNT(*) FROM detector_hits").fetchone()[0] == 1


def test_detector_silent_through_the_store_on_a_negative():
    conn = _store_with_run(FIX / "playable.jsonl")
    det = ParamSweepThrashDetector(conn)
    det.transcript_roots = []
    assert det.run(conn) == []


# ── unit cases ───────────────────────────────────────────────────────────────

def test_shell_segments_keep_quoted_semicolons():
    segs = shell_segments("sed -i 's/a = 1;/a = 2;/' f.rs && ./run.sh 3 | head -2")
    assert segs == ["sed -i 's/a = 1;/a = 2;/' f.rs", "./run.sh 3", "head -2"]


def test_harness_keys():
    k = lambda c: [h.key for h in harness_runs(c)]
    assert k("O_LOOK=6 /x/scratch/stats.sh oval 3 8") == ["stats.sh"]
    assert k("S=/x/sweep5.sh; $S 22 0.8 expert 16; $S 17 0.8 expert 16") == ["sweep5.sh", "sweep5.sh"]
    assert k("cargo build --release && ./target/release/examples/explore oval 3 2 | head") == ["explore"]
    assert k("cd web && node /s/sweep.ts '[{\"kp\":1}]'") == ["sweep.ts"]
    assert k("cargo run --release --example bench -- 5") == ["bench"]
    assert k("cat scripts/check.sh; grep -n x f; git status") == []


def test_numeric_args_are_a_sweep_but_new_words_are_not():
    a = harness_runs("O_DELAY=4 ./stats.sh oval rookie 3 8")[0]
    b = harness_runs("O_DELAY=6 ./stats.sh oval rookie 3 8")[0]
    c = harness_runs("./stats.sh canyon rookie 3 8")[0]
    assert a.sig == b.sig and a.raw != b.raw
    assert a.sig != c.sig


def _step(cmd, out="", t=0.0, err=False):
    return Step(id=f"s{t}", tool="Bash", input={"command": cmd}, t0=t, t1=t + 1, output=out,
                is_error=err)


def test_progress_requires_a_passing_runner_not_a_mention():
    assert is_progress(_step("cargo test -p x", "test result: ok. 3 passed; 0 failed"))
    assert is_progress(_step("bash scripts/check.sh --fast 2>&1 | tail", "CHECK FAST OK (inner)"))
    assert not is_progress(_step("cat scripts/check.sh", "printf 'CHECK PASS'"))
    assert not is_progress(_step("cargo test -- --ignored probe", "test result: ok. 1 passed"))
    assert not is_progress(_step("cargo test", "test result: FAILED. 1 passed; 1 failed"))


def test_structural_edit_between_runs_is_not_a_param_rerun():
    steps = [
        _step("./h.sh 1", t=0),
        _step("sed -i 's/^const K: f64 = 1.0;/const K: f64 = 2.0;/' src/a.rs && ./h.sh 1", t=60),
        _step("sed -i 's/fn a()/fn b()/' src/a.rs && ./h.sh 1", t=120),
    ]
    w = worst(analyze(steps))
    assert len(w.reruns) == 1 and w.reruns[0].why.startswith("numeric edit")


def test_a_passing_gate_closes_the_stretch():
    steps = [_step(f"./h.sh {i}", t=i * 300.0) for i in range(10)]
    steps.insert(5, _step("cargo nextest run", "Summary [ 2.0s] 9 tests run: 9 passed", t=1300))
    sts = analyze(steps)
    assert len(sts) == 2
    assert [len(s.reruns) for s in sts] == [4, 5]
