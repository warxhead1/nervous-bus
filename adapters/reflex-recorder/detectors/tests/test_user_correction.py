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
    classify_turn,
    match_themes,
    _strip_envelope,
    scan_transcripts,
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


def _queued_line(prompt, ts, kind="human", mode="prompt"):
    return {
        "type": "attachment",
        "timestamp": ts,
        "attachment": {
            "type": "queued_command", "prompt": prompt, "commandMode": mode,
            "origin": {"kind": kind}, "timestamp": ts,
        },
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

    def test_over_explained_output_fires_on_ui_copy_complaint(self):
        themes = match_themes("Why did you put subtitles on every card, you keep over-explaining")
        self.assertIn("over_explained_output", themes)

    def test_over_explained_output_ignores_verbose_flag(self):
        themes = match_themes("Why did you drop the --verbose flag from the build command")
        self.assertNotIn("over_explained_output", themes)

    def test_over_explained_output_ignores_leftover_file_complaint(self):
        themes = match_themes("Why did you leave those log files, they don't need to be there")
        self.assertNotIn("over_explained_output", themes)

    def test_idle_dispatch_fires_on_stalling_complaints(self):
        for text in (
            "The build is still going, so why do you keep stopping?",
            "why are you waiting, the service is back up",
            "you keep pausing when there is other work to pick up",
        ):
            self.assertIn("idle_dispatch", match_themes(text), text)

    def test_idle_dispatch_ignores_a_program_that_keeps_waiting(self):
        themes = match_themes("Why did you let the tests keep waiting on the socket")
        self.assertNotIn("idle_dispatch", themes)

    def test_multiple_themes_can_fire_together(self):
        themes = match_themes(
            "Why did you use sudo again, don't touch root access without asking"
        )
        self.assertIn("sudo", themes)


class TestClassifyTurn(unittest.TestCase):
    def test_cue_without_theme_is_unthemed(self):
        self.assertEqual(classify_turn("That's not what I asked for, read the spec again"),
                         ["unthemed"])

    def test_themed_turn_is_not_also_unthemed(self):
        self.assertEqual(classify_turn("Why did you use sudo for that"), ["sudo"])

    def test_no_cue_is_nothing(self):
        self.assertEqual(classify_turn("Please add a chart to the dashboard"), [])

    def test_turn_after_an_interrupt_counts_whatever_it_says(self):
        self.assertEqual(classify_turn("use the other file", after_interrupt=True),
                         ["interrupted"])

    def test_asking_for_a_go_ahead_is_ask_operator(self):
        for text in ("why do you need my go ahead, just run it",
                     "Why are you asking again, you don't need my permission"):
            self.assertIn("ask_operator_for_automatable", classify_turn(text), text)


class TestRealTurnShapes(unittest.TestCase):
    """Synthetic turns shaped like corrections measured in live transcripts
    on 2026-11-26 that the first cue vocabulary missed."""

    def test_corrections_land_on_the_rule_themes(self):
        cases = {
            "you killed my other sessions, never do that again": "destructive_op",
            "stop stating things that are measurably false": "stale_claim",
            "You didn't set your monitor correctly, the job already finished": "monitor_accuracy",
            "stop making me do redundant setup, you can do it yourself": "ask_operator_for_automatable",
            "how the hell did you stop yourself, nothing is happening": "idle_dispatch",
            "I told you to just launch the agent, don't ask me": "ask_operator_for_automatable",
            "you said this was fixed but the login page still shows the old error": "stale_claim",
        }
        for text, theme in cases.items():
            self.assertIn(theme, classify_turn(text), text)

    def test_generic_stop_complaint_is_unthemed_not_dropped(self):
        self.assertEqual(classify_turn("Please stop interrupting me for trivia"), ["unthemed"])

    def test_near_misses_stay_silent(self):
        for text in (
            "we don't want to stop until the benchmark is green",
            "the log says the worker was killed by the OOM reaper at 12:00",
            "Add a monitor setting to the dashboard header",
            "the doc says the report is measurably false-positive free",
            "rm -rf build/ is the cleanup step in the Makefile",
        ):
            self.assertEqual(classify_turn(text), [], text)

    def test_injected_envelopes_and_pasted_logs_never_reach_the_classifier(self):
        wrapped = "<system-reminder>stop using sudo, you killed it</system-reminder>"
        self.assertEqual(_strip_envelope(wrapped), "")
        relay = "Another claude session sent a message: you killed my worktree, stop it"
        self.assertEqual(_strip_envelope(relay), "")


class TestScanTranscripts(unittest.TestCase):
    def test_counts_signatures_without_any_run(self):
        root = Path(tempfile.mkdtemp())
        _write_transcript(root, "s1", [
            _user_line("you killed my session, never do that again", "2026-11-26T00:00:00Z"),
            _user_line("<system-reminder>stop it, you killed it</system-reminder>", "2026-11-26T00:01:00Z"),
            _user_line("add a chart", "2026-11-26T00:02:00Z"),
        ])
        self.assertEqual(scan_transcripts(root), {"destructive_op": 1})


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

    def test_interrupt_marks_the_next_turn_and_image_turns_are_read(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line([{"type": "text", "text": "[Request interrupted by user for tool use]"}],
                       "2026-09-20T00:10:00Z"),
            _user_line("no, edit the config file instead", "2026-09-20T00:10:30Z"),
            _user_line([{"type": "image", "source": {}},
                        {"type": "text", "text": "That's not what I asked, look at the screenshot"}],
                       "2026-09-20T00:20:00Z"),
        ])
        by_sig = {c.signature: c for c in self._detector().run()}
        self.assertEqual(set(by_sig), {"interrupted", "unthemed"})
        self.assertEqual(by_sig["interrupted"].run_ids, ["run-1"])

    def test_queued_human_turn_is_the_mid_flight_channel(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _queued_line("the dashboard is still blank after the restart",
                         "2026-09-20T00:10:00Z"),
            _queued_line("What is the best open model right now?", "2026-09-20T00:11:00Z"),
            _queued_line("You have 1 orchestration message. Run `orca orchestration check`.",
                         "2026-09-20T00:12:00Z"),
            _queued_line("<task-notification>again and again</task-notification>",
                         "2026-09-20T00:13:00Z", kind="task-notification",
                         mode="task-notification"),
        ])
        by_sig = {c.signature: c for c in self._detector().run()}
        self.assertEqual(set(by_sig), {"mid_flight"})
        self.assertEqual(by_sig["mid_flight"].run_ids, ["run-1"])

    def test_orchestrated_minimax_prompts_are_not_human_turns(self):
        _insert_run(
            self.conn, "run-1", "synthetic", "claude-code-minimax", "sess-1",
            "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z",
        )
        _write_transcript(self.transcript_root, "sess-1", [
            _user_line("You are the nightly audit. Why did you stop? Never do that again.",
                       "2026-09-20T00:10:00Z"),
        ])
        self.assertEqual(self._detector().run(), [])

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
