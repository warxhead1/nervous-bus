"""tests/test_repeated_question.py — Unit tests for RepeatedQuestionDetector.

Covers:
  - Positive: permission_requested event repeated across >= 2 runs → fires.
  - Positive: AskUserQuestion tool call repeated across >= 2 runs → fires.
  - Positive: Bash description ending in "?" repeated across >= 2 runs → fires.
  - Negative: question only in a single run → no fire.
  - Negative: different question classes → no cross-contamination.
  - Signature stability: sig is "<project>:<detector>:<qclass>" with NO run_id,
    NO timestamp.
  - Signature format enforced: sig must not contain run_id, must contain detector
    name, must start with project.
  - Recurrence / dedup path: find_or_create_issue increments across scans.
  - Remediation rung: 'permission' source → 'automate'; stable question → 'automate';
    variable question (path placeholder) → 'inform'.
  - Normalisation: different phrasings mapping to the same class are grouped.
  - Cross-project isolation: same question in different projects produces separate
    candidates.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.repeated_question import (
    RepeatedQuestionDetector,
    _normalize_question,
    _extract_question_text,
    _is_deterministic_question,
)


# ── Minimal schema ────────────────────────────────────────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    worktree      TEXT,
    worktree_slug TEXT,
    git_branch    TEXT,
    bead_id       TEXT,
    outcome       TEXT,
    ended         TEXT NOT NULL,
    close_reason  TEXT
);
"""

_RUN_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    conn.executescript(_RUN_EVENTS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id: str, project: str, outcome: str = "clean") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, outcome, ended) VALUES (?,?,?,?)",
        (run_id, project, outcome, _now_utc()),
    )


def _insert_permission_event(
    conn, run_id: str, seq: int, tool_summary: str
) -> None:
    """Insert a permission_requested event (bus.hearth.session.permission.requested.v1
    stored flat as the data payload)."""
    payload = {
        "tool_summary": tool_summary,
        "project": "myproject",
        "options": ["allow", "deny"],
        "correlation_id": "corr-" + run_id,
        "session_id": run_id,
        "ts": _now_utc(),
        "expires_at": _now_utc(),
        "deep_link": "hearth://approve/corr-" + run_id,
        "project_short": "myproject",
    }
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, _now_utc(), "permission_requested", json.dumps(payload)),
    )


def _insert_ask_user_event(
    conn, run_id: str, seq: int, tool_summary: str
) -> None:
    """Insert a bus.agent.activity.v1 event for an AskUserQuestion tool call."""
    payload = {
        "specversion": "1.0",
        "id": "test-id-" + run_id,
        "source": "/claude-host/myproject",
        "type": "bus.agent.activity.v1",
        "time": _now_utc(),
        "datacontenttype": "application/json",
        "data": {
            "agent": "claude-code",
            "agent_kind": "host_claude_code",
            "event": "tool_call",
            "project": "myproject",
            "tool_name": "AskUserQuestion",
            "tool_summary": tool_summary,
        },
    }
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, _now_utc(), "bus.agent.activity.v1", json.dumps(payload)),
    )


def _insert_bash_question_event(
    conn, run_id: str, seq: int, description: str
) -> None:
    """Insert a bus.agent.activity.v1 Bash event whose description ends with '?'."""
    tool_summary = json.dumps({"command": "echo test", "description": description})
    payload = {
        "specversion": "1.0",
        "id": "bash-id-" + run_id,
        "source": "/claude-host/myproject",
        "type": "bus.agent.activity.v1",
        "time": _now_utc(),
        "datacontenttype": "application/json",
        "data": {
            "agent": "claude-code",
            "agent_kind": "host_claude_code",
            "event": "tool_call",
            "project": "myproject",
            "tool_name": "Bash",
            "tool_summary": tool_summary,
        },
    }
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, _now_utc(), "bus.agent.activity.v1", json.dumps(payload)),
    )


# ── Normalisation unit tests ──────────────────────────────────────────────────

class TestNormalizeQuestion(unittest.TestCase):
    def test_strips_trailing_punctuation(self):
        self.assertEqual(
            _normalize_question("Should I commit?"),
            "should i commit",
        )

    def test_replaces_absolute_path(self):
        norm = _normalize_question("Allow Read /home/eric/projects/foo/bar.py")
        self.assertIn("<path>", norm)
        self.assertNotIn("/home/eric", norm)

    def test_replaces_ulid(self):
        norm = _normalize_question("Run 01ARZ3NDEKTSV4RRFFQ69G5FAV?")
        self.assertIn("<id>", norm)

    def test_replaces_uuid(self):
        norm = _normalize_question("Session de802eda-8fd8-4adb-8993-fd300bbb157a found?")
        self.assertIn("<id>", norm)

    def test_replaces_hex_hash(self):
        norm = _normalize_question("Commit abc123def is missing")
        self.assertIn("<id>", norm)

    def test_replaces_standalone_numbers(self):
        norm = _normalize_question("42 tests failed")
        self.assertIn("<n>", norm)
        self.assertNotIn("42", norm)

    def test_lowercased(self):
        self.assertEqual(_normalize_question("Push to MAIN?"), "push to main")

    def test_stable_same_input(self):
        q = "Should I push to origin?"
        self.assertEqual(_normalize_question(q), _normalize_question(q))

    def test_different_paths_same_class(self):
        q1 = _normalize_question("Allow Read /home/eric/projects/foo.py")
        q2 = _normalize_question("Allow Read /tmp/bar.py")
        self.assertEqual(q1, q2)


class TestIsDeterministicQuestion(unittest.TestCase):
    def test_no_placeholders_is_deterministic(self):
        self.assertTrue(_is_deterministic_question("push to main branch"))

    def test_path_placeholder_is_not_deterministic(self):
        self.assertFalse(_is_deterministic_question("allow read <path>"))

    def test_id_placeholder_is_not_deterministic(self):
        self.assertFalse(_is_deterministic_question("run id <id>"))


class TestExtractQuestionText(unittest.TestCase):
    def test_permission_event_extracts_tool_summary(self):
        raw = json.dumps({"tool_summary": "Read /foo/bar", "options": []})
        result = _extract_question_text("permission_requested", raw)
        self.assertEqual(result, "Read /foo/bar")

    def test_ask_user_question_event_extracts_summary(self):
        raw = json.dumps({
            "data": {
                "tool_name": "AskUserQuestion",
                "tool_summary": "Which branch?",
            }
        })
        result = _extract_question_text("bus.agent.activity.v1", raw)
        self.assertEqual(result, "Which branch?")

    def test_bash_non_question_returns_none(self):
        raw = json.dumps({
            "data": {
                "tool_name": "Bash",
                "tool_summary": json.dumps({"command": "ls", "description": "List files"}),
            }
        })
        result = _extract_question_text("bus.agent.activity.v1", raw)
        self.assertIsNone(result)

    def test_bash_question_description_extracted(self):
        raw = json.dumps({
            "data": {
                "tool_name": "Bash",
                "tool_summary": json.dumps({"command": "ls", "description": "Push to main?"}),
            }
        })
        result = _extract_question_text("bus.agent.activity.v1", raw)
        self.assertEqual(result, "Push to main?")

    def test_non_question_activity_returns_none(self):
        raw = json.dumps({
            "data": {
                "tool_name": "Read",
                "tool_summary": "reading file",
            }
        })
        result = _extract_question_text("bus.agent.activity.v1", raw)
        self.assertIsNone(result)

    def test_invalid_json_returns_none(self):
        result = _extract_question_text("permission_requested", "not-json")
        self.assertIsNone(result)


# ── Positive detection: permission_requested ──────────────────────────────────

class TestPermissionRequestedPositive(unittest.TestCase):
    """permission_requested event repeating >= 2 runs → fires with automate rung."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-001", "myproject")
        _insert_run(self.conn, "run-002", "myproject")
        _insert_permission_event(self.conn, "run-001", 1, "Read /home/eric/projects/foo.py")
        _insert_permission_event(self.conn, "run-002", 1, "Read /tmp/bar.py")

    def test_fires_on_recurring_permission(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.project, "myproject")
        self.assertEqual(c.pattern_name, "repeated_question")
        self.assertEqual(c.detector, "repeated_question")
        self.assertGreaterEqual(c.occurrences, 2)

    def test_remediation_rung_is_automate(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["remediation_rung"], "automate")
        self.assertEqual(candidates[0].extra["source"], "permission")

    def test_proposed_remediation_references_class(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        c = candidates[0]
        self.assertIsNotNone(c.proposed_remediation)
        self.assertIn("allow", c.proposed_remediation.lower())


# ── Positive detection: AskUserQuestion ──────────────────────────────────────

class TestAskUserQuestionPositive(unittest.TestCase):
    """AskUserQuestion repeated across >= 2 runs → fires."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-003", "myproject")
        _insert_run(self.conn, "run-004", "myproject")
        _insert_ask_user_event(self.conn, "run-003", 1, "Which branch should I push to?")
        _insert_ask_user_event(self.conn, "run-004", 1, "Which branch should I push to?")

    def test_fires_on_recurring_ask(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].occurrences, 2)

    def test_remediation_rung_automate_for_stable_question(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        # "which branch should i push to" has no path/id placeholders → automate
        self.assertEqual(candidates[0].extra["remediation_rung"], "automate")


# ── Remediation rung: inform for variable questions ───────────────────────────

class TestRemediationRungInformForVariableQuestion(unittest.TestCase):
    """Question with path placeholders after normalisation → inform rung."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-v1", "myproject")
        _insert_run(self.conn, "run-v2", "myproject")
        # Different paths → same class with <path> placeholder
        _insert_ask_user_event(self.conn, "run-v1", 1, "Should I edit /home/eric/foo.py?")
        _insert_ask_user_event(self.conn, "run-v2", 1, "Should I edit /tmp/bar.py?")

    def test_rung_is_inform_when_question_contains_path_placeholder(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["remediation_rung"], "inform")
        self.assertIn("inform", candidates[0].extra["remediation_rung_justification"].lower())


# ── Negative: single-run question does not fire ───────────────────────────────

class TestSingleRunNoFire(unittest.TestCase):
    """A question that only appears in one run must NOT fire."""

    def test_no_fire_when_single_run(self):
        conn = _make_db()
        _insert_run(conn, "run-single", "myproject")
        _insert_ask_user_event(conn, "run-single", 1, "Unique question only once?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_no_fire_single_permission(self):
        conn = _make_db()
        _insert_run(conn, "run-sp1", "myproject")
        _insert_permission_event(conn, "run-sp1", 1, "Read /unique/path.py")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Negative: no false-fire on non-question events ───────────────────────────

class TestNonQuestionNoFire(unittest.TestCase):
    """Non-question tool calls (Read, Edit, Bash without ?) must not fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-nq1", "myproject")
        _insert_run(self.conn, "run-nq2", "myproject")

    def _insert_bash_no_question(self, run_id: str) -> None:
        payload = {
            "specversion": "1.0",
            "id": "id-" + run_id,
            "source": "/claude-host/myproject",
            "type": "bus.agent.activity.v1",
            "time": _now_utc(),
            "datacontenttype": "application/json",
            "data": {
                "agent": "claude-code",
                "agent_kind": "host_claude_code",
                "event": "tool_call",
                "project": "myproject",
                "tool_name": "Bash",
                "tool_summary": json.dumps({
                    "command": "git status",
                    "description": "Check git status",  # no ?
                }),
            },
        }
        self.conn.execute(
            "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
            (run_id, 1, _now_utc(), "bus.agent.activity.v1", json.dumps(payload)),
        )

    def test_no_fire_on_non_question_bash(self):
        self._insert_bash_no_question("run-nq1")
        self._insert_bash_no_question("run-nq2")
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Signature stability / format ─────────────────────────────────────────────

class TestSignatureFormat(unittest.TestCase):
    """Signature must be <project>:<detector>:<anchor> — no run_id, no timestamp."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-sig1", "sigproject")
        _insert_run(self.conn, "run-sig2", "sigproject")
        _insert_ask_user_event(self.conn, "run-sig1", 1, "Should I push to main?")
        _insert_ask_user_event(self.conn, "run-sig2", 1, "Should I push to main?")

    def test_signature_format(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        sig = candidates[0].signature
        # Must start with project
        self.assertTrue(sig.startswith("sigproject:"), f"sig should start with project: {sig}")
        # Must contain DETECTOR_NAME
        self.assertIn(":repeated_question:", sig)
        # Must have three colon-separated parts minimum: project:detector:anchor
        parts = sig.split(":", 2)
        self.assertEqual(len(parts), 3, f"Signature must have exactly 3 parts: {sig}")
        self.assertEqual(parts[0], "sigproject")
        self.assertEqual(parts[1], "repeated_question")
        self.assertTrue(len(parts[2]) > 0, "Anchor must be non-empty")

    def test_signature_does_not_contain_run_id(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        self.assertNotIn("run-sig1", sig, "Signature must not contain run_id")
        self.assertNotIn("run-sig2", sig, "Signature must not contain run_id")

    def test_signature_does_not_contain_timestamp(self):
        det = RepeatedQuestionDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # No ISO timestamp pattern in signature
        import re
        ts_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIsNone(ts_pattern.search(sig), f"Signature must not contain timestamp: {sig}")

    def test_signature_stable_across_two_detections(self):
        """Running the detector twice on the same data must produce the same sig."""
        det1 = RepeatedQuestionDetector(self.conn)
        c1 = det1.run()
        det2 = RepeatedQuestionDetector(self.conn)
        c2 = det2.run()
        self.assertEqual(c1[0].signature, c2[0].signature)


# ── Recurrence / dedup (Kyoko #5) ────────────────────────────────────────────

class TestRecurrenceDedup(unittest.TestCase):
    """find_or_create_issue increments recurrence_count on repeated scans."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-rec1", "recproject")
        _insert_run(self.conn, "run-rec2", "recproject")
        _insert_ask_user_event(self.conn, "run-rec1", 1, "Should I commit now?")
        _insert_ask_user_event(self.conn, "run-rec2", 1, "Should I commit now?")
        self.question_class = "should i commit now"

    def test_recurrence_count_increments(self):
        sig = f"recproject:repeated_question:{self.question_class}"

        det1 = RepeatedQuestionDetector(self.conn)
        det1.run()
        issue1 = det1.get_issue(sig)
        self.assertIsNotNone(issue1)
        self.assertEqual(issue1["recurrence_count"], 1)

        det2 = RepeatedQuestionDetector(self.conn)
        det2.run()
        issue2 = det2.get_issue(sig)
        self.assertEqual(issue2["recurrence_count"], 2)

    def test_issue_has_correct_detector_name(self):
        det = RepeatedQuestionDetector(self.conn)
        det.run()
        sig = f"recproject:repeated_question:{self.question_class}"
        issue = det.get_issue(sig)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["detector"], "repeated_question")


# ── Remediation rung in extra ─────────────────────────────────────────────────

class TestRemediationRungInExtra(unittest.TestCase):
    """extra dict must always contain remediation_rung key."""

    def test_extra_has_remediation_rung_permission(self):
        conn = _make_db()
        _insert_run(conn, "rp1", "proj")
        _insert_run(conn, "rp2", "proj")
        _insert_permission_event(conn, "rp1", 1, "Write /some/file.txt")
        _insert_permission_event(conn, "rp2", 1, "Write /other/file.txt")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        extra = candidates[0].extra
        self.assertIn("remediation_rung", extra)
        self.assertIn(extra["remediation_rung"], ("eliminate", "automate", "inform"))
        self.assertIn("remediation_rung_justification", extra)

    def test_extra_has_remediation_rung_ask(self):
        conn = _make_db()
        _insert_run(conn, "ra1", "proj")
        _insert_run(conn, "ra2", "proj")
        _insert_ask_user_event(conn, "ra1", 1, "Confirm deployment?")
        _insert_ask_user_event(conn, "ra2", 1, "Confirm deployment?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertIn("remediation_rung", candidates[0].extra)


# ── Cross-project isolation ───────────────────────────────────────────────────

class TestCrossProjectIsolation(unittest.TestCase):
    """Same question in different projects → separate candidates."""

    def test_separate_candidates_per_project(self):
        conn = _make_db()
        # Project A
        _insert_run(conn, "run-pa1", "project-a")
        _insert_run(conn, "run-pa2", "project-a")
        _insert_ask_user_event(conn, "run-pa1", 1, "Should I push?")
        _insert_ask_user_event(conn, "run-pa2", 1, "Should I push?")
        # Project B
        _insert_run(conn, "run-pb1", "project-b")
        _insert_run(conn, "run-pb2", "project-b")
        _insert_ask_user_event(conn, "run-pb1", 1, "Should I push?")
        _insert_ask_user_event(conn, "run-pb2", 1, "Should I push?")

        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        projects = {c.project for c in candidates}
        self.assertIn("project-a", projects)
        self.assertIn("project-b", projects)
        self.assertEqual(len(candidates), 2)

    def test_signatures_include_correct_project(self):
        conn = _make_db()
        _insert_run(conn, "run-iso1", "iso-project")
        _insert_run(conn, "run-iso2", "iso-project")
        _insert_ask_user_event(conn, "run-iso1", 1, "Unique iso question?")
        _insert_ask_user_event(conn, "run-iso2", 1, "Unique iso question?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertTrue(candidates[0].signature.startswith("iso-project:"))


# ── Bash question detection ───────────────────────────────────────────────────

class TestBashQuestionDetection(unittest.TestCase):
    """Bash tool calls with description ending in '?' are treated as questions."""

    def test_fires_on_recurring_bash_question_description(self):
        conn = _make_db()
        _insert_run(conn, "run-bq1", "myproject")
        _insert_run(conn, "run-bq2", "myproject")
        _insert_bash_question_event(conn, "run-bq1", 1, "Is the build passing?")
        _insert_bash_question_event(conn, "run-bq2", 1, "Is the build passing?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)

    def test_no_fire_single_bash_question(self):
        conn = _make_db()
        _insert_run(conn, "run-bqs", "myproject")
        _insert_bash_question_event(conn, "run-bqs", 1, "One-off question?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Normalisation grouping ────────────────────────────────────────────────────

class TestNormalisationGrouping(unittest.TestCase):
    """Different phrasings that normalise to the same class → single candidate."""

    def test_different_paths_same_class(self):
        """'Allow Read /path/A' and 'Allow Read /path/B' both → same class."""
        conn = _make_db()
        _insert_run(conn, "run-ng1", "myproject")
        _insert_run(conn, "run-ng2", "myproject")
        _insert_permission_event(conn, "run-ng1", 1, "Allow Read /home/eric/proj/foo.rs")
        _insert_permission_event(conn, "run-ng2", 1, "Allow Read /tmp/bar.rs")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        # Both map to the same class (path replaced with <path>)
        self.assertEqual(len(candidates), 1)

    def test_different_literal_questions_no_grouping(self):
        """Genuinely different questions stay separate."""
        conn = _make_db()
        _insert_run(conn, "run-dq1", "myproject")
        _insert_run(conn, "run-dq2", "myproject")
        _insert_run(conn, "run-dq3", "myproject")
        _insert_run(conn, "run-dq4", "myproject")
        _insert_ask_user_event(conn, "run-dq1", 1, "Should I push to main?")
        _insert_ask_user_event(conn, "run-dq2", 1, "Should I push to main?")
        _insert_ask_user_event(conn, "run-dq3", 1, "Should I merge branches?")
        _insert_ask_user_event(conn, "run-dq4", 1, "Should I merge branches?")
        det = RepeatedQuestionDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()
