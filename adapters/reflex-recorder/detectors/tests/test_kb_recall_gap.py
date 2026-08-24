"""tests/test_kb_recall_gap.py — Unit tests for KbRecallGapDetector.

Covers:
  - has_kb_recall_gap() pure-function unit tests (no DB).
  - Positive: a run with a repeated_question detector_hits row and zero
    kb-recall run_events rows fires kb_recall_gap.
  - Negative: a run with a repeated_question hit but a kb.guidance.provided.v1
    / kb.session.context.v1 / kb.entry.created.v1 or v2 event present does
    NOT fire.
  - Negative: a run with kb-recall activity but no repeated_question hit is
    never considered (kb_recall_gap only scores runs already flagged by
    repeated_question).
  - kb.knowledge.gap.v1 and kb.session.indexed.v1 do NOT count as recall
    activity (deliberately excluded — see module docstring).
  - Cross-project isolation: hits in different projects produce separate
    candidates.
  - Signature stability: no run_id, no timestamp in the signature.
  - failure_taxonomy integration: kb_recall_gap is in _CONTEXT_DETECTORS.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.kb_recall_gap import (
    KbRecallGapDetector,
    KB_RECALL_CHANNELS,
    has_kb_recall_gap,
)
from detectors.failure_taxonomy import _CONTEXT_DETECTORS


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
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


def _insert_run(conn, run_id: str, project: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, ended, close_reason) VALUES (?,?,?,?)",
        (run_id, project, _now_utc(), "idle_timeout"),
    )


def _insert_repeated_question_hit(conn, run_id: str, project: str, qclass: str = "should i push") -> None:
    """Simulate RepeatedQuestionDetector having already run this pass."""
    signature = f"{project}:repeated_question:{qclass}"
    conn.execute(
        "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
        (run_id, "repeated_question", signature, project, _now_utc()),
    )


def _insert_kb_event(conn, run_id: str, seq: int, event_type: str) -> None:
    payload = {
        "specversion": "1.0",
        "id": "kb-id-" + run_id + "-" + str(seq),
        "source": "/kb",
        "type": event_type,
        "time": _now_utc(),
        "datacontenttype": "application/json",
        "data": {"project": "myproject"},
    }
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, _now_utc(), event_type, json.dumps(payload)),
    )


# ── Pure-function unit tests ──────────────────────────────────────────────────

class TestHasKbRecallGap(unittest.TestCase):
    def test_zero_events_is_a_gap(self):
        self.assertTrue(has_kb_recall_gap(0))

    def test_one_event_is_not_a_gap(self):
        self.assertFalse(has_kb_recall_gap(1))

    def test_negative_count_treated_as_gap(self):
        # Defensive: a malformed count (should never happen) still reads as a gap.
        self.assertTrue(has_kb_recall_gap(-1))


# ── Channel set sanity ────────────────────────────────────────────────────────

class TestKbRecallChannels(unittest.TestCase):
    def test_expected_channels_included(self):
        self.assertEqual(
            KB_RECALL_CHANNELS,
            frozenset({
                "kb.guidance.provided.v1",
                "kb.session.context.v1",
                "kb.entry.created.v1",
                "kb.entry.created.v2",
            }),
        )

    def test_gap_and_indexed_channels_excluded(self):
        self.assertNotIn("kb.knowledge.gap.v1", KB_RECALL_CHANNELS)
        self.assertNotIn("kb.session.indexed.v1", KB_RECALL_CHANNELS)


# ── Detector end-to-end tests ─────────────────────────────────────────────────

class TestKbRecallGapDetector(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()

    def test_fires_when_repeated_question_hit_and_no_kb_activity(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)

        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.project, "myproject")
        self.assertEqual(c.detector, "kb_recall_gap")
        self.assertIn("run-1", c.run_ids)
        self.assertEqual(c.extra["bucket"], "context_failure")
        self.assertEqual(c.extra["reason"], "kb_recall_missing")
        self.assertEqual(c.extra["remediation_rung"], "inform")

    def test_does_not_fire_when_guidance_provided_present(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.guidance.provided.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(candidates, [])

    def test_does_not_fire_when_session_context_present(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.session.context.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(candidates, [])

    def test_does_not_fire_when_entry_created_v1_only_present(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.entry.created.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(candidates, [])

    def test_does_not_fire_when_entry_created_v2_only_present(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.entry.created.v2")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(candidates, [])

    def test_fires_when_only_unrelated_event_present(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "custom.unrelated.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(len(candidates), 1)

    def test_knowledge_gap_event_does_not_count_as_recall(self):
        """kb.knowledge.gap.v1 is itself evidence recall FAILED — must not suppress the detector."""
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.knowledge.gap.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(len(candidates), 1)

    def test_session_indexed_event_does_not_count_as_recall(self):
        """kb.session.indexed.v1 is a batch/backfill signal, not live recall."""
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")
        _insert_kb_event(self.conn, "run-1", 1, "kb.session.indexed.v1")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(len(candidates), 1)

    def test_no_fire_without_repeated_question_hit(self):
        """A run with zero kb activity but NO repeated_question hit is never scored."""
        _insert_run(self.conn, "run-1", "myproject")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        self.assertEqual(candidates, [])

    def test_cross_project_isolation(self):
        _insert_run(self.conn, "run-1", "project-a")
        _insert_repeated_question_hit(self.conn, "run-1", "project-a")
        _insert_run(self.conn, "run-2", "project-b")
        _insert_repeated_question_hit(self.conn, "run-2", "project-b")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)

        projects = {c.project for c in candidates}
        self.assertEqual(projects, {"project-a", "project-b"})
        for c in candidates:
            self.assertEqual(len(c.run_ids), 1)

    def test_signature_has_no_run_id_or_timestamp(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.detect(self.conn)
        sig = candidates[0].signature

        self.assertNotIn("run-1", sig)
        self.assertTrue(sig.startswith("myproject:"))
        self.assertIn("kb_recall_gap", sig)

    def test_run_via_base_records_hit_and_issue(self):
        _insert_run(self.conn, "run-1", "myproject")
        _insert_repeated_question_hit(self.conn, "run-1", "myproject")

        detector = KbRecallGapDetector(self.conn)
        candidates = detector.run(self.conn)
        self.assertEqual(len(candidates), 1)

        issue = detector.get_issue(candidates[0].signature)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)


# ── failure_taxonomy integration ──────────────────────────────────────────────

class TestFailureTaxonomyIntegration(unittest.TestCase):
    def test_kb_recall_gap_is_a_context_detector(self):
        self.assertIn("kb_recall_gap", _CONTEXT_DETECTORS)


if __name__ == "__main__":
    unittest.main()
