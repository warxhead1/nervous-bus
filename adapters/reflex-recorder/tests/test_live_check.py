"""live_check: transcript detectors on one session's runs, each hit printed once."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import live_check
from detectors.base import ensure_detector_schema
from detectors.foreground_wait import ForegroundWaitDetector
from transcript_steps import parse_transcript

FIX = Path(__file__).parent / "fixtures" / "transcripts"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, session_id TEXT,
                           started TEXT, ended TEXT, event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    for rid, sess, name in (("RW", "lead-1", "start_grid_waits"), ("RX", "other", "start_grid_waits"),
                            ("RN", "lead-1", "podbot_sweep")):
        fx = FIX / f"{name}.jsonl"
        st = parse_transcript(fx)
        conn.execute("INSERT INTO runs VALUES (?, 'demo', ?, ?, ?, ?)",
                     (rid, sess, _iso(st[0].t0), _iso(st[-1].t0), len(st)))
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES (?, 1, ?, ?)",
                     (rid, _iso(st[0].t0), json.dumps({"data": {"transcript_path": str(fx)}})))
    conn.commit()
    conn.close()
    return path


def test_scoped_to_the_given_runs_and_deduped(tmp_path):
    conn = sqlite3.connect(_db(tmp_path / "runs.db"))
    seen: set[str] = set()
    hits = live_check.live_hits(conn, ["RW", "RN"], seen, detectors=(ForegroundWaitDetector,), roots=[])
    assert [(h["run_id"], h["detector"]) for h in hits] == [("RW", "foreground_wait")]
    assert live_check.live_hits(conn, ["RW", "RN"], seen, detectors=(ForegroundWaitDetector,),
                                roots=[]) == []
    assert conn.execute("SELECT COUNT(*) FROM detector_hits").fetchone()[0] == 0


def test_session_selects_its_runs_inside_the_window(tmp_path):
    conn = sqlite3.connect(_db(tmp_path / "runs.db"))
    assert live_check.session_runs(conn, "lead-1", since_hours=24 * 365 * 30) == ["RN", "RW"]  # by start
    assert live_check.session_runs(conn, "lead-1", since_hours=1) == []


def test_text_output_groups_runs_per_signature():
    hits = [{"run_id": r, "project": "p", "detector": "foreground_wait", "signature": "p:fw",
             "remediation": None} for r in ("A", "B", "C")]
    assert live_check.format_hits(hits) == ["[reflex] foreground_wait on run C and 2 more run(s) (p): p:fw"]


def test_cli_prints_a_hit_once_across_calls(tmp_path, capsys, monkeypatch):
    db = _db(tmp_path / "runs.db")
    monkeypatch.setattr(live_check, "LIVE_DETECTORS", (ForegroundWaitDetector,))
    monkeypatch.setattr(ForegroundWaitDetector, "transcript_roots", [])
    argv = ["--db", str(db), "--state", str(tmp_path / "seen.json"), "--run-id", "RW"]
    assert live_check.main(argv) == 0
    first = capsys.readouterr().out
    assert first.startswith("[reflex] foreground_wait on run RW (demo):")
    assert live_check.main(argv) == 0
    assert capsys.readouterr().out == ""


def test_publish_sends_each_new_hit_once_in_schema_shape(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[3] / "schemas"
                         / "reflex.live.hit.v1.json").read_text())
    sent = []
    monkeypatch.setattr(live_check.bus_publish, "publish",
                        lambda ch, p, **kw: sent.append((ch, p)) or True)
    db = _db(tmp_path / "runs.db")
    argv = ["--db", str(db), "--state", str(tmp_path / "seen.json"), "--session", "lead-1",
            "--since-hours", str(24 * 365 * 30), "--publish"]
    live_check.main(argv)
    first = len(sent)
    assert first
    live_check.main(argv)
    assert len(sent) == first
    assert {c for c, _ in sent} == {"reflex.live.hit.v1"}
    for _, payload in sent:
        Draft202012Validator(schema).validate(payload)
        assert payload["session_id"] == "lead-1"


def test_hook_hands_back_earlier_scan_output_and_spawns_the_next(tmp_path):
    import live_hook
    calls = []

    def spawn(cmd, **kw):
        calls.append(cmd)

    ev = {"tool_name": "Agent", "session_id": "s"}
    ctx = lambda e: live_hook.context_for(e, spawn=spawn, pending=tmp_path)  # noqa: E731
    assert ctx({"tool_name": "Bash", "session_id": "s"}) == ""
    assert ctx({"tool_name": "Agent"}) == ""
    assert calls == []
    assert ctx(ev) == ""                      # first call: nothing yet, scan spawned
    assert "--session" in calls[0] and "s" in calls[0] and "--publish" in calls[0]
    out = Path(calls[0][calls[0].index("--out") + 1])
    assert ctx(ev) == "" and len(calls) == 1  # scan still running: no second spawn
    out.with_name(out.name + ".part").unlink()
    out.write_text("[reflex] foreground_wait on run RW (demo): x\n")
    assert ctx(ev).startswith("[reflex]")     # delivered once, next scan spawned
    assert len(calls) == 2
    assert ctx({"tool_name": "Task", "session_id": "s"}) == ""


def test_hook_is_fast_on_a_slow_scan(tmp_path):
    import time
    import live_hook
    t0 = time.perf_counter()
    live_hook.context_for({"tool_name": "Agent", "session_id": "s"},
                          spawn=lambda *a, **k: time.sleep(0), pending=tmp_path)
    assert time.perf_counter() - t0 < 1.0


def test_unchanged_runs_are_not_rescanned(tmp_path, capsys, monkeypatch):
    db = _db(tmp_path / "runs.db")
    scans = []
    real = live_check.live_hits
    monkeypatch.setattr(live_check, "live_hits",
                        lambda conn, ids, seen, **kw: scans.append(list(ids)) or real(conn, ids, seen, **kw))
    monkeypatch.setattr(live_check, "LIVE_DETECTORS", (ForegroundWaitDetector,))
    monkeypatch.setattr(ForegroundWaitDetector, "transcript_roots", [])
    argv = ["--db", str(db), "--state", str(tmp_path / "seen.json"), "--run-id", "RW"]
    live_check.main(argv)
    live_check.main(argv)
    assert scans == [["RW"], []]
    sqlite3.connect(db).execute("UPDATE runs SET event_count = event_count + 1").connection.commit()
    live_check.main(argv)
    assert scans[-1] == ["RW"]


def test_out_file_appears_only_when_there_are_lines(tmp_path, monkeypatch):
    db = _db(tmp_path / "runs.db")
    monkeypatch.setattr(live_check, "LIVE_DETECTORS", (ForegroundWaitDetector,))
    monkeypatch.setattr(ForegroundWaitDetector, "transcript_roots", [])
    out = tmp_path / "o.out"
    argv = ["--db", str(db), "--state", str(tmp_path / "seen.json"), "--run-id", "RW", "--out", str(out)]
    live_check.main(argv)
    assert out.read_text().startswith("[reflex] foreground_wait")
    out.unlink()
    live_check.main(argv)
    assert not out.exists() and not (tmp_path / "o.out.part").exists()


def test_hook_main_prints_hook_json(monkeypatch, capsys):
    import io
    import live_hook
    monkeypatch.setattr(live_hook, "context_for", lambda event, **kw: "[reflex] line")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name":"Agent","session_id":"s"}'))
    assert live_hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] == "[reflex] line"
