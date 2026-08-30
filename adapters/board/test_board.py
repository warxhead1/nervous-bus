#!/usr/bin/env python3
"""Tests for adapters/board/board.py. Per house convention (see
adapters/ci-watch/test_watch.py), all external I/O (dolt/pymysql, orca sqlite,
redis) is either stubbed with fixture data or exercised against a throwaway
temp sqlite file board.py itself creates -- never a live dolt server, orca
orchestration.db, or real Redis. board.py's live substrate was manually
verified separately (see the dispatch report); these tests cover the pure
logic: bead-id extraction, lane-precedence derivation, scoring, orca sqlite
join, and the board.json contract shape.

Run: python3 test_board.py -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import board


# --------------------------------------------------------------------------
# extract_bead_id -- must mirror orca's nbus-emit.ts extractBeadId() exactly
# --------------------------------------------------------------------------

class TestExtractBeadId(unittest.TestCase):
    def test_tag_form(self):
        self.assertEqual(board.extract_bead_id("do the thing [bead:hearth-qpkti] now"), "hearth-qpkti")

    def test_tag_form_case_insensitive(self):
        self.assertEqual(board.extract_bead_id("[BEAD:Nervous-Bus-05g1]"), "Nervous-Bus-05g1")

    def test_prose_form(self):
        self.assertEqual(
            board.extract_bead_id("Implement Hearth bead hearth-qpkti using the accepted contract"),
            "hearth-qpkti",
        )

    def test_prose_form_requires_hyphenated_id(self):
        # "bead" followed by a bare word with no hyphen is not a valid id
        # under BEAD_PROSE_RE (\bbead\s+([a-z][a-z0-9]*-[a-z0-9-]+)\b) --
        # mirrors orca's regex exactly, so a false match here would mean
        # this file has drifted from nbus-emit.ts.
        self.assertIsNone(board.extract_bead_id("there is no bead here"))

    def test_tag_wins_over_prose_when_both_present(self):
        text = "bead nervous-bus-wrong but actually [bead:nervous-bus-right]"
        self.assertEqual(board.extract_bead_id(text), "nervous-bus-right")

    def test_none_and_empty(self):
        self.assertIsNone(board.extract_bead_id(None))
        self.assertIsNone(board.extract_bead_id(""))


# --------------------------------------------------------------------------
# derive_lane -- precedence: in_flight > in_review > blocked > in_progress > ready
# --------------------------------------------------------------------------

class TestDeriveLane(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_closed_within_7d_is_done(self):
        lane = board.derive_lane(
            status="closed", closed_at_epoch=self.NOW - 3600, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "done_7d")

    def test_closed_older_than_7d_is_excluded(self):
        lane = board.derive_lane(
            status="closed", closed_at_epoch=self.NOW - 8 * 86400, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "_closed_stale")

    def test_closed_missing_closed_at_is_excluded_not_assumed_recent(self):
        lane = board.derive_lane(
            status="closed", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "_closed_stale")

    def test_in_flight_beats_everything_else(self):
        # open PR + open blocker + in_progress status + dispatched orca ->
        # in_flight must win per the stated precedence.
        lane = board.derive_lane(
            status="in_progress", closed_at_epoch=None, has_open_blocker=True,
            orca={"state": "dispatched"}, lifecycle_recent=False,
            pr={"url": "x", "state": "open"}, now=self.NOW,
        )
        self.assertEqual(lane, "in_flight")

    def test_lifecycle_recent_heartbeat_also_means_in_flight_without_orca_row(self):
        lane = board.derive_lane(
            status="open", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=True, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "in_flight")

    def test_in_review_beats_blocked_and_in_progress(self):
        lane = board.derive_lane(
            status="in_progress", closed_at_epoch=None, has_open_blocker=True,
            orca=None, lifecycle_recent=False, pr={"url": "x", "state": "open"}, now=self.NOW,
        )
        self.assertEqual(lane, "in_review")

    def test_merged_pr_does_not_count_as_in_review(self):
        lane = board.derive_lane(
            status="open", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr={"url": "x", "state": "merged"}, now=self.NOW,
        )
        self.assertEqual(lane, "ready")

    def test_blocked_beats_in_progress(self):
        lane = board.derive_lane(
            status="in_progress", closed_at_epoch=None, has_open_blocker=True,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "blocked")

    def test_status_blocked_field_also_triggers_blocked_lane(self):
        lane = board.derive_lane(
            status="blocked", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "blocked")

    def test_in_progress_status_without_higher_precedence_signal(self):
        lane = board.derive_lane(
            status="in_progress", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "in_progress")

    def test_plain_open_is_ready(self):
        lane = board.derive_lane(
            status="open", closed_at_epoch=None, has_open_blocker=False,
            orca=None, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "ready")

    def test_orca_pending_state_is_in_flight(self):
        lane = board.derive_lane(
            status="open", closed_at_epoch=None, has_open_blocker=False,
            orca={"state": "pending"}, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "in_flight")

    def test_orca_completed_state_alone_is_not_in_flight(self):
        lane = board.derive_lane(
            status="open", closed_at_epoch=None, has_open_blocker=False,
            orca={"state": "completed"}, lifecycle_recent=False, pr=None, now=self.NOW,
        )
        self.assertEqual(lane, "ready")


# --------------------------------------------------------------------------
# compute_score
# --------------------------------------------------------------------------

class TestComputeScore(unittest.TestCase):
    def test_higher_priority_scores_higher_at_same_age(self):
        p0 = board.compute_score(0, age_days=10, blocked=False)
        p4 = board.compute_score(4, age_days=10, blocked=False)
        self.assertGreater(p0, p4)

    def test_older_scores_higher_at_same_priority(self):
        young = board.compute_score(2, age_days=1, blocked=False)
        old = board.compute_score(2, age_days=100, blocked=False)
        self.assertGreater(old, young)

    def test_blocked_multiplier_applied(self):
        base = board.compute_score(2, age_days=10, blocked=False)
        blocked = board.compute_score(2, age_days=10, blocked=True)
        self.assertAlmostEqual(blocked, base * 1.5, places=6)

    def test_unknown_priority_falls_back_to_default_weight(self):
        # priority 9 isn't in PRIORITY_WEIGHT -- must not raise, must use
        # DEFAULT_PRIORITY_WEIGHT rather than crash the whole run over one
        # malformed row.
        score = board.compute_score(9, age_days=10, blocked=False)
        self.assertGreater(score, 0.0)


# --------------------------------------------------------------------------
# fetch_orca_state -- against a real temp sqlite file (board.py's own
# read-only-open code path), never a live orchestration.db.
# --------------------------------------------------------------------------

class TestFetchOrcaState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "orchestration.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, run_id TEXT, task_title TEXT, spec TEXT)"
        )
        conn.execute(
            "CREATE TABLE dispatch_contexts (task_id TEXT, status TEXT, "
            "last_heartbeat_at TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('task_1', 'run_a', NULL, "
            "'Implement bead nervous-bus-abc1 now')"
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('task_2', 'run_a', '[bead:hearth-xyz9] fix it', NULL)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('task_3', 'run_a', 'no bead reference here', NULL)"
        )
        # task_1 has two dispatch contexts -- the later one (by created_at) must win.
        conn.execute(
            "INSERT INTO dispatch_contexts VALUES ('task_1', 'failed', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO dispatch_contexts VALUES ('task_1', 'dispatched', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_extracts_from_title_and_spec(self):
        state = board.fetch_orca_state(self.db_path)
        self.assertIn("nervous-bus-abc1", state)
        self.assertIn("hearth-xyz9", state)
        self.assertNotIn("task_3", state)  # no bead ref -> not present under any key

    def test_latest_dispatch_context_wins(self):
        state = board.fetch_orca_state(self.db_path)
        self.assertEqual(state["nervous-bus-abc1"]["state"], "dispatched")
        self.assertEqual(state["nervous-bus-abc1"]["last_heartbeat_at"], "2026-01-02T00:00:00Z")

    def test_missing_db_returns_empty(self):
        self.assertEqual(board.fetch_orca_state(Path("/nonexistent/orchestration.db")), {})


# --------------------------------------------------------------------------
# board.json contract shape
# --------------------------------------------------------------------------

class TestBuildBoardContract(unittest.TestCase):
    def _issue(self, **overrides):
        row = {
            "project": "nervous-bus", "id": "nervous-bus-1", "title": "t",
            "description": "", "status": "open", "priority": 2, "issue_type": "task",
            "assignee": None, "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z", "closed_at": None, "notes": "",
        }
        row.update(overrides)
        return row

    def test_top_level_keys_exact(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        self.assertEqual(set(board_dict.keys()), board.CONTRACT_TOP_LEVEL_KEYS)

    def test_lanes_list_matches_spec(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        self.assertEqual(
            board_dict["lanes"],
            ["ready", "in_progress", "in_flight", "in_review", "blocked", "done_7d"],
        )

    def test_issue_keys_exact(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        self.assertEqual(set(board_dict["issues"][0].keys()), board.CONTRACT_ISSUE_KEYS)

    def test_summary_shape(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        self.assertIn("per_project", board_dict["summary"])
        self.assertIn("per_lane", board_dict["summary"])
        self.assertIn("nervous-bus", board_dict["summary"]["per_project"])

    def test_closed_stale_issue_excluded_from_output(self):
        now = time.time()
        old_closed = self._issue(
            id="nervous-bus-2", status="closed",
            closed_at="2020-01-01T00:00:00Z",
        )
        board_dict = board.build_board([old_closed], {}, {}, [], {}, now=now)
        self.assertEqual(board_dict["issues"], [])

    def test_json_serializable(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        json.dumps(board_dict)  # must not raise

    def test_issues_sorted_by_score_descending(self):
        rows = [
            self._issue(id="a", priority=4, created_at="2026-08-29T00:00:00Z"),
            self._issue(id="b", priority=0, created_at="2026-08-01T00:00:00Z"),
        ]
        board_dict = board.build_board(rows, {}, {}, [], {}, now=time.time())
        ids = [i["id"] for i in board_dict["issues"]]
        self.assertEqual(ids, ["b", "a"])

    def test_blocked_by_populated_from_map(self):
        rows = [self._issue(id="nervous-bus-1")]
        blocked_map = {"nervous-bus-1": ["nervous-bus-9", "nervous-bus-8"]}
        board_dict = board.build_board(rows, blocked_map, {}, [], {}, now=time.time())
        issue = board_dict["issues"][0]
        self.assertEqual(issue["blocked_by"], ["nervous-bus-9", "nervous-bus-8"])
        self.assertEqual(issue["lane"], "blocked")

    def test_orca_and_pr_null_when_absent(self):
        board_dict = board.build_board([self._issue()], {}, {}, [], {}, now=time.time())
        issue = board_dict["issues"][0]
        self.assertIsNone(issue["orca"])
        self.assertIsNone(issue["pr"])


# --------------------------------------------------------------------------
# fetch_pr_events -- "most recent wins" across multiple stream naming variants
# --------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, data):
        self.data = data  # stream -> list of (entry_id, fields) newest-first

    def xrevrange(self, stream, count=None):
        return self.data.get(stream, [])[:count]


class TestFetchPrEvents(unittest.TestCase):
    def _entry(self, envelope):
        return ("1-0", {"_raw": json.dumps(envelope)})

    def test_opened_then_merged_across_streams_resolves_to_merged(self):
        opened = {
            "type": "loom.lifecycle.pr.v1", "time": "2026-08-01T00:00:00Z",
            "data": {"bead_id": "nervous-bus-1", "event": "opened", "pr_url": "https://x/1"},
        }
        merged = {
            "type": "bus.hearth-loom.pr.merged.v1", "time": "2026-08-02T00:00:00Z",
            "data": {"bead_id": "nervous-bus-1", "event": "merged", "pr_url": "https://x/1"},
        }
        client = _FakeRedis({
            "nbus:loom.lifecycle.pr.v1": [self._entry(opened)],
            "nbus:bus.hearth-loom.pr.merged.v1": [self._entry(merged)],
        })
        result = board.fetch_pr_events(
            client,
            streams=["nbus:loom.lifecycle.pr.v1", "nbus:bus.hearth-loom.pr.merged.v1"],
        )
        self.assertEqual(result["nervous-bus-1"]["state"], "merged")

    def test_legacy_stream_with_no_event_field_defaults_to_open(self):
        legacy = {
            "type": "bus.bead.pr_opened", "time": "2026-08-01T00:00:00Z",
            "data": {"bead_id": "nervous-bus-2", "title": "x"},
        }
        client = _FakeRedis({"nbus:bus.bead.pr_opened": [self._entry(legacy)]})
        result = board.fetch_pr_events(client, streams=["nbus:bus.bead.pr_opened"])
        self.assertEqual(result["nervous-bus-2"]["state"], "open")

    def test_missing_stream_is_silently_empty(self):
        client = _FakeRedis({})
        result = board.fetch_pr_events(client, streams=["nbus:does.not.exist"])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
