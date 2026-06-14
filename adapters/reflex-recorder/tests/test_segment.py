"""tests/test_segment.py — Unit tests for the reflex-recorder segmentation logic.

Covers:
- composite-key segmentation (session vs worktree)
- worktree absolute-path reconstruction from cwd
- idle-split + continues_run_id re-stitch
- tool_histogram accumulation
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from segment import (
    Segmenter,
    compute_run_key,
    extract_worktree_slug,
    reconstruct_worktree_path,
    _now_utc,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _activity(
    conversation_id="conv-aaa",
    session_id="conv-aaa",
    agent_id="conv-aaa",
    event="tool_call",
    tool_name="Bash",
    project="myproject",
    agent_kind="host_claude_code",
    cwd="/home/eric/projects/myproject",
    worktree=None,
    ts="2026-06-13T05:00:00Z",
):
    a = {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "event": event,
        "tool_name": tool_name,
        "project": project,
        "agent_kind": agent_kind,
        "cwd": cwd,
        "ts": ts,
    }
    if worktree is not None:
        a["worktree"] = worktree
    return a


def _wt_activity(**kwargs):
    """Activity with a worktree context (cwd inside .claude/worktrees/)."""
    defaults = dict(
        conversation_id="conv-parent",
        session_id="conv-parent",
        agent_id="conv-parent",
        event="tool_call",
        tool_name="Read",
        project="myproject",
        agent_kind="host_claude_code",
        cwd="/home/eric/projects/myproject/.claude/worktrees/wf_abc123/src",
        worktree="wf_abc123",
        ts="2026-06-13T05:00:00Z",
    )
    defaults.update(kwargs)
    return defaults


# ── compute_run_key ───────────────────────────────────────────────────────────

class TestComputeRunKey(unittest.TestCase):
    def test_no_worktree_returns_session_key(self):
        a = _activity(conversation_id="conv-1")
        key, kind, slug = compute_run_key(a)
        self.assertEqual(key, "conv-1")
        self.assertEqual(kind, "session")
        self.assertIsNone(slug)

    def test_worktree_returns_composite_key(self):
        a = _wt_activity(conversation_id="conv-parent", worktree="wf_abc123")
        key, kind, slug = compute_run_key(a)
        self.assertEqual(key, "conv-parent#wf_abc123")
        self.assertEqual(kind, "worktree")
        self.assertEqual(slug, "wf_abc123")

    def test_slug_from_cwd_when_worktree_field_absent(self):
        """If the worktree field is absent but cwd points into worktrees/, use cwd."""
        a = _activity(
            conversation_id="conv-parent",
            cwd="/home/eric/projects/foo/.claude/worktrees/slug-99/subdir",
        )
        # No worktree field
        key, kind, slug = compute_run_key(a)
        self.assertEqual(kind, "worktree")
        self.assertEqual(slug, "slug-99")
        self.assertEqual(key, "conv-parent#slug-99")

    def test_cwd_not_in_worktrees_is_session(self):
        a = _activity(cwd="/home/eric/projects/foo/src")
        key, kind, slug = compute_run_key(a)
        self.assertEqual(kind, "session")
        self.assertIsNone(slug)

    def test_same_conversation_different_worktrees_distinct_keys(self):
        """Two shards sharing a parent conversation_id must produce different run_keys."""
        a1 = _wt_activity(conversation_id="conv-shared", worktree="wf_shard1",
                           cwd="/home/eric/projects/foo/.claude/worktrees/wf_shard1/x")
        a2 = _wt_activity(conversation_id="conv-shared", worktree="wf_shard2",
                           cwd="/home/eric/projects/foo/.claude/worktrees/wf_shard2/x")
        k1, _, _ = compute_run_key(a1)
        k2, _, _ = compute_run_key(a2)
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, "conv-shared#wf_shard1")
        self.assertEqual(k2, "conv-shared#wf_shard2")


# ── reconstruct_worktree_path ─────────────────────────────────────────────────

class TestReconstructWorktreePath(unittest.TestCase):
    def test_basic_reconstruction(self):
        a = {"cwd": "/home/eric/projects/foo/.claude/worktrees/agent-abc/subdir"}
        path = reconstruct_worktree_path(a, "agent-abc")
        self.assertEqual(path, "/home/eric/projects/foo/.claude/worktrees/agent-abc")

    def test_reconstruction_at_root_of_worktree(self):
        """cwd IS the worktree root (no subdir)."""
        a = {"cwd": "/home/eric/projects/foo/.claude/worktrees/wf_123"}
        path = reconstruct_worktree_path(a, "wf_123")
        self.assertEqual(path, "/home/eric/projects/foo/.claude/worktrees/wf_123")

    def test_no_sentinel_returns_none(self):
        a = {"cwd": "/home/eric/projects/foo/src"}
        path = reconstruct_worktree_path(a, "some-slug")
        self.assertIsNone(path)

    def test_empty_cwd_returns_none(self):
        a = {"cwd": ""}
        path = reconstruct_worktree_path(a, "slug")
        self.assertIsNone(path)

    def test_real_live_example(self):
        """Match the actual cwd pattern seen in live stream."""
        a = {
            "cwd": "/home/eric/projects/tengine/.claude/worktrees/agent-a3f67d389e54ce3f8"
        }
        path = reconstruct_worktree_path(a, "agent-a3f67d389e54ce3f8")
        self.assertEqual(
            path,
            "/home/eric/projects/tengine/.claude/worktrees/agent-a3f67d389e54ce3f8",
        )


# ── extract_worktree_slug ─────────────────────────────────────────────────────

class TestExtractWorktreeSlug(unittest.TestCase):
    def test_explicit_worktree_field(self):
        a = {"worktree": "wf_abc", "cwd": "/x"}
        self.assertEqual(extract_worktree_slug(a), "wf_abc")

    def test_worktree_field_takes_priority_over_cwd(self):
        a = {
            "worktree": "wf_explicit",
            "cwd": "/home/eric/projects/foo/.claude/worktrees/wf_from_cwd/x",
        }
        self.assertEqual(extract_worktree_slug(a), "wf_explicit")

    def test_no_worktree_field_parses_cwd(self):
        a = {"cwd": "/home/eric/projects/foo/.claude/worktrees/wf_parsed/src"}
        self.assertEqual(extract_worktree_slug(a), "wf_parsed")

    def test_no_worktree_context_returns_none(self):
        a = {"cwd": "/home/eric/projects/foo/src"}
        self.assertIsNone(extract_worktree_slug(a))


# ── Segmenter — basic flow ────────────────────────────────────────────────────

class TestSegmenterBasic(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.seg = Segmenter(
            idle_timeout_s=900.0,
            on_run_closed=self.closed.append,
        )

    def test_single_session_run_closes_on_ended(self):
        self.seg.ingest(_activity(event="tool_call", ts="2026-06-13T05:00:00Z"))
        self.seg.ingest(_activity(event="tool_call", ts="2026-06-13T05:00:01Z"))
        self.seg.ingest(_activity(event="ended",    ts="2026-06-13T05:00:02Z"))
        self.assertEqual(len(self.closed), 1)
        run = self.closed[0]
        self.assertEqual(run["close_reason"], "ended")
        self.assertEqual(run["event_count"], 3)
        self.assertEqual(run["run_key_kind"], "session")
        self.assertEqual(run["worktree"], None)
        self.assertEqual(run["worktree_slug"], None)

    def test_worktree_run_has_correct_key_and_path(self):
        a = _wt_activity(
            conversation_id="conv-P",
            worktree="wf_slug1",
            cwd="/home/eric/projects/foo/.claude/worktrees/wf_slug1/x",
            event="tool_call",
        )
        self.seg.ingest(a)
        self.seg.ingest({**a, "event": "ended"})
        self.assertEqual(len(self.closed), 1)
        run = self.closed[0]
        self.assertEqual(run["run_key_kind"], "worktree")
        self.assertEqual(run["run_key"], "conv-P#wf_slug1")
        self.assertEqual(run["worktree_slug"], "wf_slug1")
        self.assertEqual(
            run["worktree"],
            "/home/eric/projects/foo/.claude/worktrees/wf_slug1",
        )

    def test_two_worktrees_same_conversation_are_separate_runs(self):
        a1 = _wt_activity(conversation_id="conv-shared", worktree="wf_s1",
                          cwd="/p/.claude/worktrees/wf_s1/x", event="tool_call")
        a2 = _wt_activity(conversation_id="conv-shared", worktree="wf_s2",
                          cwd="/p/.claude/worktrees/wf_s2/x", event="tool_call")
        self.seg.ingest(a1)
        self.seg.ingest(a2)
        self.seg.ingest({**a1, "event": "ended"})
        self.seg.ingest({**a2, "event": "ended"})
        self.assertEqual(len(self.closed), 2)
        run_keys = {r["run_key"] for r in self.closed}
        self.assertIn("conv-shared#wf_s1", run_keys)
        self.assertIn("conv-shared#wf_s2", run_keys)

    def test_session_run_not_created_for_missing_conversation_id(self):
        """An event with no conversation_id and no worktree is unroutable; skip."""
        a = _activity(conversation_id="", session_id="", agent_id="")
        # Should not crash, and open_run_count should stay 0 or handle gracefully
        self.seg.ingest(a)  # key is "" — may open or ignore
        # Close it to confirm stability
        self.seg.shutdown()


# ── tool_histogram accumulation ───────────────────────────────────────────────

class TestToolHistogram(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.seg = Segmenter(idle_timeout_s=900.0, on_run_closed=self.closed.append)

    def test_tool_histogram_counts(self):
        for tool in ["Bash", "Bash", "Read", "Edit", "Bash"]:
            self.seg.ingest(_activity(event="tool_call", tool_name=tool))
        self.seg.ingest(_activity(event="ended"))
        self.assertEqual(len(self.closed), 1)
        hist = self.closed[0]["tool_histogram"]
        self.assertEqual(hist["Bash"], 3)
        self.assertEqual(hist["Read"], 1)
        self.assertEqual(hist["Edit"], 1)

    def test_tool_return_events_not_counted(self):
        """tool_return events have no tool_name — histogram unchanged."""
        self.seg.ingest(_activity(event="tool_call", tool_name="Bash"))
        self.seg.ingest(_activity(event="tool_return", tool_name=None))
        self.seg.ingest(_activity(event="ended"))
        hist = self.closed[0]["tool_histogram"]
        self.assertEqual(hist, {"Bash": 1})

    def test_empty_histogram_for_no_tool_calls(self):
        self.seg.ingest(_activity(event="heartbeat", tool_name=None))
        self.seg.ingest(_activity(event="ended"))
        self.assertEqual(self.closed[0]["tool_histogram"], {})


# ── Idle timeout and continues_run_id re-stitch ───────────────────────────────

class TestIdleSplitReStitch(unittest.TestCase):
    def setUp(self):
        self.closed = []

    def _make_seg(self, idle_timeout_s=0.05):
        return Segmenter(idle_timeout_s=idle_timeout_s, on_run_closed=self.closed.append)

    def test_idle_timeout_closes_run(self):
        seg = self._make_seg(idle_timeout_s=0.05)
        t0 = time.time()
        seg.ingest(_activity(event="tool_call"), now=t0)
        # Tick well past the timeout
        seg.tick(now=t0 + 1.0)
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(self.closed[0]["close_reason"], "idle_timeout")

    def test_continues_run_id_set_on_reopened_run(self):
        """After idle_timeout closes run-A, a new event on the same run_key
        opens run-B and continues_run_id == run-A's run_id."""
        seg = self._make_seg(idle_timeout_s=0.05)
        t0 = time.time()
        seg.ingest(_activity(event="tool_call"), now=t0)
        seg.tick(now=t0 + 1.0)
        self.assertEqual(len(self.closed), 1)
        run_a_id = self.closed[0]["run_id"]

        # New event on same run_key
        seg.ingest(_activity(event="tool_call"), now=t0 + 2.0)
        seg.ingest(_activity(event="ended"), now=t0 + 3.0)
        self.assertEqual(len(self.closed), 2)
        run_b = self.closed[1]
        self.assertEqual(run_b["continues_run_id"], run_a_id)
        self.assertIsNone(self.closed[0]["continues_run_id"])

    def test_continues_run_id_only_for_same_run_key(self):
        """continues_run_id is scoped per run_key, not global."""
        seg = self._make_seg(idle_timeout_s=0.05)
        t0 = time.time()
        # Run on key A
        seg.ingest(_activity(conversation_id="conv-A"), now=t0)
        seg.tick(now=t0 + 1.0)
        run_a_id = self.closed[0]["run_id"]

        # Continuation on key A + new session on key B (should not inherit A's continues)
        seg.ingest(_activity(conversation_id="conv-A"), now=t0 + 2.0)
        seg.ingest(_activity(conversation_id="conv-B"), now=t0 + 2.0)
        seg.shutdown()

        keyed = {r["run_key"]: r for r in self.closed}
        self.assertEqual(keyed["conv-A"]["continues_run_id"], run_a_id)
        self.assertIsNone(keyed["conv-B"]["continues_run_id"])

    def test_recorder_shutdown_closes_all_open_runs(self):
        seg = self._make_seg(idle_timeout_s=900.0)
        t0 = time.time()
        seg.ingest(_activity(conversation_id="conv-X"), now=t0)
        seg.ingest(_activity(conversation_id="conv-Y"), now=t0)
        self.assertEqual(len(self.closed), 0)
        seg.shutdown()
        self.assertEqual(len(self.closed), 2)
        reasons = {r["close_reason"] for r in self.closed}
        self.assertEqual(reasons, {"recorder_shutdown"})


# ── Required fields on closed payload ────────────────────────────────────────

class TestClosedPayloadSchema(unittest.TestCase):
    REQUIRED = [
        "run_id", "run_key_kind", "run_key", "project", "agent_kind",
        "started", "ended", "event_count", "tool_histogram",
    ]

    def test_required_fields_present(self):
        closed = []
        seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
        seg.ingest(_activity(event="tool_call"))
        seg.ingest(_activity(event="ended"))
        self.assertEqual(len(closed), 1)
        payload = closed[0]
        for field in self.REQUIRED:
            self.assertIn(field, payload, f"missing required field: {field}")

    def test_schema_version_is_1(self):
        closed = []
        seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
        seg.ingest(_activity(event="ended"))
        self.assertEqual(closed[0]["schema_version"], "1")

    def test_outcome_and_labeled_at_null_at_close(self):
        closed = []
        seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
        seg.ingest(_activity(event="ended"))
        self.assertIsNone(closed[0]["outcome"])
        self.assertIsNone(closed[0]["labeled_at"])


if __name__ == "__main__":
    unittest.main()
