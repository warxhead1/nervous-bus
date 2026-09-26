"""tests/test_user_correction.py — Unit tests for UserCorrectionDetector.

All fixture transcripts/history are SYNTHETIC (this repo is public — never a
real user prompt, per the module's own evidence-only policy). Covers:

  - Positive: a Claude Code transcript turn with BOTH a correction cue AND a
    theme keyword -> fires, attributed to the enclosing run by timestamp.
  - Negative: correction cue alone (no theme keyword) -> no fire.
  - Negative: theme keyword alone (no correction cue) -> no fire.
  - Envelope stripping: a line that is ENTIRELY a harness wrapper tag
    (<bash-input>...</bash-input>) is dropped even if it happens to contain
    a theme keyword.
  - isMeta / list-content / isCompactSummary rows are never treated as
    genuine turns.
  - Codex path: ~/.codex/history.jsonl joined by session_id -> fires with the
    same contract.
  - since_ts bounds the runs scanned (issue #32 API).
  - Signature is the bare theme name (no project prefix, no run_id) — the
    query.py `remediation` contract.
  - record_hit dedups: one hit per (run_id, theme) even if the same theme
    recurs across multiple turns in the same run.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema
from detectors.user_correction import (
    UserCorrectionDetector,
    match_themes,
    _strip_envelope,
)


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    project    TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    session_id TEXT,
    started    TEXT NOT NULL,
    ended      TEXT NOT NULL
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project, agent_kind, session_id, started, ended):
    conn.execute(
        "INSERT INTO runs (run_id, project, agent_kind, session_id, started, ended) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, project, agent_kind, session_id, started, ended),
    )


def _write_transcript(root: Path, session_id: str, lines: list[dict]) -> Path:
    d = root / "-home-eric-projects-synthetic"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _user_line(text_or_list, ts, is_meta=False, is_compact=False):
    return {
        "type": "user",
        "isMeta": is_meta,
        "isCompactSummary": is_compact,
        "message": {"content": text_or_list},
        "timestamp": ts,
    }


class TestStripEnvelope(unittest.TestCase):
    def test_pure_wrapper_strips_to_empty(self):
        self.assertEqual(_strip_envelope("<bash-input>ls -la</bash-input>"), "")

    def test_compact_continuation_strips_to_empty(self):
        self.assertEqual(
            _strip_envelope("This session is being continued from a previous conversation..."),
            "",
        )

    def test_genuine_text_survives(self):
        self.assertEqual(_strip_envelope("Stop doing that, it broke the build"),
                          "Stop doing that, it broke the build")

    def test_mixed_wrapper_plus_text_keeps_text(self):
        stripped = _strip_envelope(
            "<system-reminder>ignore this</system-reminder>Stop using that hack"
        )
        self.assertIn("Stop using that hack", stripped)
        self.assertNotIn("system-reminder", stripped)


class TestMatchThemes(unittest.TestCase):
    def test_cue_and_keyword_both_present_fires(self):
        themes = match_themes("Why did you use rm -rf on that directory, you deleted my work")
        self.assertIn("destructive_op", themes)

    def test_cue_alone_no_keyword_does_not_fire(self):
        themes = match_themes("Why did you do that")
        self.assertEqual(themes, [])

    def test_keyword_alone_no_cue_does_not_fire(self):
        themes = match_themes("Let's use sudo to install the package tomorrow")
        self.assertEqual(themes, [])

    def test_allcaps_counts_as_cue(self):
        themes = match_themes("STOP using a band-aid fix here, find the root cause")
        self.assertIn("band_aid_fix", themes)

    def test_multiple_themes_can_fire_together(self):
        themes = match_themes(
            "Why did you use sudo again, don't touch root access without asking"
        )
        self.assertIn("sudo", themes)


class TestDetectorClaudeSource(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.tmp = tempfile.mkdtemp()
        self.transcript_root = Path(self.tmp) / "transcripts"
        self.transcript_root.mkdir()

    def _detector(self):
        det = UserCorrectionDetector(self.conn)
        det.transcript_root = self.transcript_root
        det.codex_history_path = Path(self.tmp) / "no-such-history.jsonl"
        return det

    def test_positive_fire_attributed_to_enclosing_run(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line("<bash-input>ls</bash-input>", "2026-09-20T00:10:00Z"),
            _user_line([{"type": "tool_result", "content": "ok"}], "2026-09-20T00:11:00Z"),
            _user_line("hook context", "2026-09-20T00:12:00Z", is_meta=True),
            _user_line(
                "Why did you delete that file, you keep destroying my work with rm -rf",
                "2026-09-20T00:20:00Z",
            ),
        ])
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        self.assertIn("destructive_op", by_sig)
        cand = by_sig["destructive_op"]
        self.assertEqual(cand.project, "synthetic")
        self.assertEqual(cand.run_ids, ["run-1"])
        # Signature must be the bare theme — no project prefix, no run_id.
        self.assertEqual(cand.signature, "destructive_op")

    def test_no_matching_turns_yields_no_candidates(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line("Please add a new feature to the dashboard", "2026-09-20T00:10:00Z"),
        ])
        det = self._detector()
        self.assertEqual(det.run(), [])

    def test_compact_summary_flag_excluded(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line(
                "Why did you delete that, you keep destroying my work with rm -rf",
                "2026-09-20T00:20:00Z",
                is_compact=True,
            ),
        ])
        det = self._detector()
        self.assertEqual(det.run(), [])

    def test_dedup_one_hit_per_run_theme_across_multiple_turns(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line(
                "Why did you delete that, you keep destroying my work with rm -rf",
                "2026-09-20T00:10:00Z",
            ),
            _user_line(
                "STOP with the rm -rf, you deleted it again, destroyed everything",
                "2026-09-20T00:20:00Z",
            ),
        ])
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        self.assertEqual(by_sig["destructive_op"].run_ids, ["run-1"])
        hit_count = self.conn.execute(
            "SELECT COUNT(*) FROM detector_hits WHERE detector='user_correction' "
            "AND signature='destructive_op' AND run_id='run-1'"
        ).fetchone()[0]
        self.assertEqual(hit_count, 1)

    def test_since_ts_excludes_earlier_runs(self):
        _insert_run(
            self.conn, "run-old", "synthetic", "claude-code", "sess-old",
            "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-old", [
            _user_line(
                "Why did you delete that, you keep destroying my work with rm -rf",
                "2026-01-01T00:10:00Z",
            ),
        ])
        det = self._detector()
        candidates = det.run(since_ts="2026-06-01T00:00:00Z")
        self.assertEqual(candidates, [])


class TestDetectorCodexSource(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.tmp = tempfile.mkdtemp()
        self.transcript_root = Path(self.tmp) / "transcripts"
        self.transcript_root.mkdir()
        self.history_path = Path(self.tmp) / "history.jsonl"

    def _write_history(self, lines: list[dict]) -> None:
        with open(self.history_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def _detector(self):
        det = UserCorrectionDetector(self.conn)
        det.transcript_root = self.transcript_root
        det.codex_history_path = self.history_path
        return det

    def test_codex_turn_fires_via_session_join(self):
        _insert_run(
            self.conn, "run-2", "synthetic-codex", "codex-cli", "sess-codex-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        import datetime
        ts = datetime.datetime(2026, 9, 20, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
        self._write_history([
            {"session_id": "sess-other", "ts": ts, "text": "why did you use sudo, stop"},
            {"session_id": "sess-codex-1", "ts": ts,
             "text": "Why are you asking me for approval, just do it and automate this"},
        ])
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        self.assertIn("ask_operator_for_automatable", by_sig)
        self.assertEqual(by_sig["ask_operator_for_automatable"].run_ids, ["run-2"])
        self.assertEqual(by_sig["ask_operator_for_automatable"].project, "synthetic-codex")
        # sess-other never joined to any in-window run -> must not appear anywhere.
        self.assertNotIn("sudo", by_sig)


if __name__ == "__main__":
    unittest.main()
