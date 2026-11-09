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
        seg.ingest(_activity(event="tool_call"))
        seg.ingest(_activity(event="ended"))
        self.assertEqual(closed[0]["schema_version"], "1")

    def test_outcome_and_labeled_at_null_at_close(self):
        closed = []
        seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
        seg.ingest(_activity(event="tool_call"))
        seg.ingest(_activity(event="ended"))
        self.assertIsNone(closed[0]["outcome"])
        self.assertIsNone(closed[0]["labeled_at"])


# ── data2 worktree layout (2026-08-24 move) ──────────────────────────────────


class TestReconstructWorktreePathData2Layout(unittest.TestCase):
    """The current layout is ~/data2/worktrees/<project>/<slug>.

    Before this was fixed, `reconstruct_worktree_path` only knew the retired
    `.claude/worktrees/` sentinel, so every data2-era run stored a NULL
    absolute worktree path — which in turn removed the only join key between
    a recorder run and the `orca.worker.lifecycle.v1` envelopes that carry
    task/dispatch/bead identity.
    """

    def test_data2_layout_reconstructs(self):
        a = {"cwd": "/home/eric/data2/worktrees/tachyonac-engine/green-pastures/scripts"}
        self.assertEqual(
            reconstruct_worktree_path(a, "green-pastures"),
            "/home/eric/data2/worktrees/tachyonac-engine/green-pastures",
        )

    def test_data2_layout_at_worktree_root(self):
        a = {"cwd": "/home/eric/data2/worktrees/nervous-bus/next24-telemetry"}
        self.assertEqual(
            reconstruct_worktree_path(a, "next24-telemetry"),
            "/home/eric/data2/worktrees/nervous-bus/next24-telemetry",
        )

    def test_slug_equal_to_project_segment_is_not_truncated_early(self):
        """A worktree named after its own project must not cut at the project dir."""
        a = {"cwd": "/home/eric/data2/worktrees/nervous-bus/nervous-bus/tools"}
        self.assertEqual(
            reconstruct_worktree_path(a, "nervous-bus"),
            "/home/eric/data2/worktrees/nervous-bus/nervous-bus",
        )

    def test_slug_appearing_outside_worktrees_is_ignored_when_a_worktrees_match_exists(self):
        a = {"cwd": "/home/eric/shared/worktrees/proj/shared/src"}
        self.assertEqual(
            reconstruct_worktree_path(a, "shared"),
            "/home/eric/shared/worktrees/proj/shared",
        )

    def test_slug_not_a_path_segment_returns_none(self):
        a = {"cwd": "/home/eric/data2/worktrees/nervous-bus/next24-telemetry"}
        self.assertIsNone(reconstruct_worktree_path(a, "some-other-slug"))

    def test_empty_slug_returns_none(self):
        a = {"cwd": "/home/eric/data2/worktrees/nervous-bus/next24-telemetry"}
        self.assertIsNone(reconstruct_worktree_path(a, ""))

    def test_substring_of_a_segment_does_not_match(self):
        a = {"cwd": "/home/eric/data2/worktrees/nervous-bus/next24-telemetry-2"}
        self.assertIsNone(reconstruct_worktree_path(a, "next24-telemetry"))


if __name__ == "__main__":
    unittest.main()


def test_arbitrary_matching_directory_is_not_worktree_evidence():
    assert reconstruct_worktree_path({"cwd": "/tmp/shared/source"}, "shared") is None


# ── `ended` closes a run, it never opens one ─────────────────────────────────

PARENT = "parent-session-ulid"


def _sub_call(sub="a1b2c3d4e5f60718", parent=PARENT, ts="2026-11-06T12:00:00Z",
              worktree=None, tool_name="Bash"):
    """A tool_call from a dispatched subagent in its own isolated worktree.

    Shape taken from live host_subagent traffic: every id field holds the
    PARENT session's id, and the worktree is named after the subagent.
    """
    slug = worktree or f"agent-{sub}"
    return {
        "agent_kind": "host_subagent", "event": "tool_call", "tool_name": tool_name,
        "conversation_id": parent, "session_id": parent, "agent_id": parent,
        "project": "myproject", "ts": ts,
        "cwd": f"/home/eric/data2/worktrees/myproject/{slug}",
        "worktree": slug, "subagent_id": sub,
        "assignment_id": "toolu_dispatch1", "parent_tool_use_id": "toolu_dispatch1",
    }


def _sub_end(sub="a1b2c3d4e5f60718", parent=PARENT, ts="2026-11-06T12:05:00Z", **over):
    """The terminal `ended` for that dispatch: no worktree, no cwd."""
    a = {
        "agent_kind": "host_subagent", "event": "ended", "status": "completed",
        "conversation_id": parent, "session_id": parent, "agent_id": parent,
        "project": "myproject", "ts": ts, "subagent_id": sub,
        "assignment_id": "toolu_dispatch1", "parent_tool_use_id": "toolu_dispatch1",
    }
    a.update(over)
    return a


class TestSubagentDispatchEnd(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.seg = Segmenter(idle_timeout_s=900.0, on_run_closed=self.closed.append)

    def test_end_is_keyed_like_the_subagents_own_tool_calls(self):
        self.assertEqual(compute_run_key(_sub_end())[0], compute_run_key(_sub_call())[0])

    def test_end_closes_the_subagents_run_and_mints_no_other(self):
        self.seg.ingest(_sub_call(ts="2026-11-06T12:00:00Z"))
        self.seg.ingest(_sub_call(ts="2026-11-06T12:00:01Z"))
        self.seg.ingest(_sub_end())
        self.assertEqual(len(self.closed), 1)
        run = self.closed[0]
        self.assertEqual(run["close_reason"], "ended")
        self.assertEqual(run["run_key_kind"], "worktree")
        self.assertEqual(run["event_count"], 3)
        self.assertEqual(run["tool_histogram"], {"Bash": 2})
        self.assertEqual(run["ended"], "2026-11-06T12:05:00Z")
        self.assertEqual(self.seg.open_run_count, 0)

    def test_end_does_not_close_the_parent_session_run(self):
        parent_call = _activity(conversation_id=PARENT, session_id=PARENT, agent_id=PARENT,
                                event="tool_call")
        self.seg.ingest(parent_call)
        self.seg.ingest(_sub_call())
        self.seg.ingest(_sub_end())
        self.assertEqual([r["run_key_kind"] for r in self.closed], ["worktree"])
        self.assertEqual(self.seg.open_run_count, 1)
        self.seg.ingest({**parent_call, "ts": "2026-11-06T12:06:00Z"})
        self.seg.shutdown()
        parent_runs = [r for r in self.closed if r["run_key"] == PARENT]
        self.assertEqual(len(parent_runs), 1)
        self.assertEqual(parent_runs[0]["event_count"], 2)
        self.assertIsNone(parent_runs[0]["continues_run_id"])

    def test_end_with_no_open_run_mints_nothing(self):
        self.assertFalse(self.seg.ingest(_sub_end()))
        self.assertEqual(self.closed, [])
        self.assertEqual(self.seg.open_run_count, 0)

    def test_end_after_the_run_idle_closed_mints_nothing(self):
        t0 = time.time()
        self.seg.ingest(_sub_call(), now=t0)
        self.seg.tick(now=t0 + 901.0)
        self.assertEqual([r["close_reason"] for r in self.closed], ["idle_timeout"])
        self.seg.ingest(_sub_end(), now=t0 + 5000.0)
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(self.seg.open_run_count, 0)

    def test_end_without_subagent_id_cannot_touch_the_parent_run(self):
        parent_call = _activity(conversation_id=PARENT, session_id=PARENT, agent_id=PARENT,
                                event="tool_call")
        self.seg.ingest(parent_call)
        end = _sub_end()
        del end["subagent_id"]
        self.assertFalse(self.seg.ingest(end))
        self.assertEqual(self.closed, [])
        self.assertEqual(self.seg.open_run_count, 1)

    def test_subagent_sharing_a_named_worktree_does_not_close_that_run(self):
        # Not isolated: the worktree carries the parent's name, so there is no
        # run named after the subagent for its terminal to close.
        self.seg.ingest(_sub_call(worktree="option-edge"))
        self.assertFalse(self.seg.ingest(_sub_end()))
        self.assertEqual(self.closed, [])
        self.assertEqual(self.seg.open_run_count, 1)

    def test_events_after_the_end_reopen_a_continuation(self):
        self.seg.ingest(_sub_call())
        self.seg.ingest(_sub_end())
        self.seg.ingest(_sub_call(ts="2026-11-06T12:10:00Z"))
        self.seg.shutdown()
        self.assertEqual(len(self.closed), 2)
        self.assertEqual(self.closed[1]["continues_run_id"], self.closed[0]["run_id"])


class TestEndedNeverOpensARun(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.seg = Segmenter(idle_timeout_s=900.0, on_run_closed=self.closed.append)

    def test_lone_ended_is_absorbed_for_every_agent_kind(self):
        for kind in ("codex-cli", "deerflow", "claude-code-minimax", "antigravity-cli",
                     "host_claude_code"):
            with self.subTest(kind=kind):
                self.assertFalse(self.seg.ingest(
                    _activity(conversation_id=f"c-{kind}", event="ended", agent_kind=kind)))
        self.assertEqual(self.closed, [])
        self.assertEqual(self.seg.open_run_count, 0)

    def test_ended_still_closes_an_open_run(self):
        self.assertTrue(self.seg.ingest(_activity(event="tool_call")))
        self.assertTrue(self.seg.ingest(_activity(event="ended")))
        self.assertEqual([r["close_reason"] for r in self.closed], ["ended"])

    def test_second_ended_after_close_is_absorbed(self):
        self.seg.ingest(_activity(event="tool_call"))
        self.seg.ingest(_activity(event="ended"))
        self.assertFalse(self.seg.ingest(_activity(event="ended")))
        self.assertEqual(len(self.closed), 1)

    def test_route_plans_a_batch_the_way_folding_will_play_it(self):
        live = {}
        call, end1, end2 = _sub_call(), _sub_end(), _sub_end()
        key = compute_run_key(call)[0]
        self.assertEqual(self.seg.route(call, live), key)
        self.assertEqual(self.seg.route(end1, live), key)
        self.assertIsNone(self.seg.route(end2, live))
        # planning mutated only the overlay, never the segmenter
        self.assertEqual(self.seg.open_run_count, 0)
        self.assertIsNone(self.seg.route(end1))
