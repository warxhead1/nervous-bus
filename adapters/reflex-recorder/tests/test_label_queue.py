"""tests/test_label_queue.py — the incremental label queue (#70), reverify
resume (#82) and retroactive run_key rollup (#60).

Runs are seeded through the real SQLiteStore schema so the label-attempt
columns come from store.py's migration, and compute_label / reverify_run are
stubbed where the test is about WHICH runs get looked at, not what a lookup
returns.
"""
from __future__ import annotations

import io
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import label  # noqa: E402
from store import SQLiteStore  # noqa: E402


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _payload(run_id: str, *, age_days: float = 1.0, **kw) -> dict:
    started = NOW - timedelta(days=age_days)
    base = {
        "run_id": run_id,
        "run_key": kw.pop("run_key", f"key-{run_id}"),
        "run_key_kind": "session",
        "project": kw.pop("project", "proj"),
        "agent_kind": kw.pop("agent_kind", "host_claude_code"),
        "started": _ts(started),
        "ended": _ts(started + timedelta(minutes=5)),
        "event_count": 10,
    }
    base.update(kw)
    return base


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "runs.db"
        self.store = SQLiteStore(self.db)

    def seed(self, run_id: str, **kw):
        self.store.save_run(_payload(run_id, **kw))

    def row(self, run_id: str) -> dict:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
        finally:
            conn.close()

    def sql(self, query: str, params=()):
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def exec(self, query: str, params=()):
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            conn.execute(query, params)
        finally:
            conn.close()


#: The runs table exactly as a pre-migration runs.db has it (no label_attempts,
#: last_attempt_at or label_state).
_LEGACY_RUNS_DDL = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL, run_key_kind TEXT NOT NULL,
    host_conversation_id TEXT, project TEXT NOT NULL, agent_kind TEXT NOT NULL,
    session_id TEXT, agent_id TEXT, started TEXT NOT NULL, ended TEXT NOT NULL,
    close_reason TEXT, continues_run_id TEXT, event_count INTEGER NOT NULL DEFAULT 0,
    tool_histogram TEXT NOT NULL DEFAULT '{}', worktree TEXT, worktree_slug TEXT,
    git_branch TEXT, bead_id TEXT, outcome TEXT, labeled_at TEXT, label_version INTEGER,
    label_history TEXT NOT NULL DEFAULT '[]', features TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT NOT NULL DEFAULT '1', recorded_at TEXT NOT NULL
)
"""


class TestStoreMigration(unittest.TestCase):
    def test_existing_db_gains_attempt_columns_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(db), isolation_level=None)
            conn.execute(_LEGACY_RUNS_DDL)
            conn.execute(
                "INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind, started, "
                "ended, recorded_at) VALUES ('old', 'k', 'session', 'p', 'a', 's', 'e', 'r')"
            )
            conn.close()

            SQLiteStore(db)._conn.close()
            SQLiteStore(db)._conn.close()  # second open is a no-op, not a duplicate-column error

            conn = sqlite3.connect(str(db))
            cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            row = conn.execute(
                "SELECT label_attempts, last_attempt_at, label_state FROM runs WHERE run_id='old'"
            ).fetchone()
            conn.close()
        self.assertTrue({"label_attempts", "last_attempt_at", "label_state"} <= cols)
        self.assertEqual(row, (0, None, None))


class TestIncrementalQueue(_DbCase):
    def _run_pass(self, calls: list, result=None, **kw) -> list[dict]:
        def fake(run, events, **_):
            calls.append(run["run_id"])
            return result
        with patch.object(label, "compute_label", side_effect=fake):
            return label.backfill(self.db, unlabeled_only=True, **kw)

    def test_second_pass_skips_runs_not_yet_due(self):
        for i in range(3):
            self.seed(f"r{i}", git_branch=f"feature/{i}")
        first: list = []
        self._run_pass(first)
        second: list = []
        self._run_pass(second)
        self.assertEqual(len(first), 3)
        self.assertEqual(second, [], "runs attempted seconds ago must wait out their backoff")

    def test_backoff_doubles_then_run_is_parked(self):
        self.seed("r0", git_branch="feature/pending")
        waits = []
        for attempt in range(1, label.LABEL_MAX_ATTEMPTS + 1):
            calls: list = []
            self._run_pass(calls)
            self.assertEqual(calls, ["r0"], f"attempt {attempt} should run")
            row = self.row("r0")
            self.assertEqual(row["label_attempts"], attempt)
            if attempt < label.LABEL_MAX_ATTEMPTS:
                self.assertIsNone(row["label_state"])
                # age the attempt by just under / just over its backoff window
                window = label.LABEL_BACKOFF_BASE_DAYS * 2 ** (attempt - 1)
                waits.append(window)
                last = datetime.strptime(row["last_attempt_at"], "%Y-%m-%dT%H:%M:%SZ")
                just_under = _ts(last - timedelta(days=window - 0.1))
                self.exec("UPDATE runs SET last_attempt_at=? WHERE run_id='r0'", (just_under,))
                early: list = []
                self._run_pass(early)
                self.assertEqual(early, [], "still inside the window")
                just_over = _ts(last - timedelta(days=window + 0.1))
                self.exec("UPDATE runs SET last_attempt_at=? WHERE run_id='r0'", (just_over,))
        self.assertEqual(waits, [1, 2, 4][: label.LABEL_MAX_ATTEMPTS - 1])
        row = self.row("r0")
        self.assertEqual(row["label_state"], "unattributable")
        self.assertIsNone(row["outcome"], "parking must not invent an outcome")

        # parked runs leave the queue even when their backoff is long past
        self.exec("UPDATE runs SET last_attempt_at='2020-01-01T00:00:00Z' WHERE run_id='r0'")
        again: list = []
        self._run_pass(again)
        self.assertEqual(again, [])

    def test_ignore_backoff_requeues_parked_runs_and_a_label_clears_the_state(self):
        self.seed("r0", git_branch="feature/x")
        self.exec("UPDATE runs SET label_attempts=4, label_state='unattributable', "
                  "last_attempt_at='2026-01-01T00:00:00Z' WHERE run_id='r0'")
        calls: list = []
        self._run_pass(calls, result=("landed", "pr_merge"), ignore_backoff=True)
        self.assertEqual(calls, ["r0"])
        row = self.row("r0")
        self.assertEqual(row["outcome"], "landed")
        self.assertIsNone(row["label_state"])

    def test_run_no_external_source_can_label_is_parked_on_first_attempt(self):
        self.seed("nohook")                                  # no branch, no bead
        self.seed("hooked", git_branch="feature/pending")
        self._run_pass([])
        self.assertEqual(self.row("nohook")["label_state"], "unattributable")
        self.assertIsNone(self.row("hooked")["label_state"])
        # all three states stay separable in SQL
        pending = self.sql("SELECT run_id FROM runs WHERE outcome IS NULL AND label_state IS NULL")
        parked = self.sql("SELECT run_id FROM runs WHERE label_state='unattributable'")
        self.assertEqual(pending, [("hooked",)])
        self.assertEqual(parked, [("nohook",)])

    def test_queue_is_newest_first_so_old_dead_runs_cannot_starve_new_ones(self):
        for i in range(5):
            self.seed(f"old{i}", age_days=20 + i, git_branch=f"feature/old{i}")
        self.seed("fresh", age_days=0.1, git_branch="feature/fresh")
        calls: list = []
        self._run_pass(calls)
        self.assertEqual(calls[0], "fresh")

    def test_time_budget_stops_cleanly_and_next_pass_resumes_where_it_left_off(self):
        for i in range(6):
            self.seed(f"r{i}", age_days=1 + i, git_branch=f"feature/{i}")
        first: list = []
        with patch.object(label.time, "monotonic", side_effect=itertools.count()):
            # fake clock ticks once per check: t0=0, then 1, 2, 3, 4 ...
            self._run_pass(first, budget_seconds=4)
        self.assertEqual(len(first), 3)
        second: list = []
        self._run_pass(second)
        self.assertEqual(sorted(first + second), sorted(f"r{i}" for i in range(6)))
        self.assertEqual(len(set(first) & set(second)), 0, "no run is looked at twice")

    def test_progress_line_every_n_runs(self):
        for i in range(5):
            self.seed(f"r{i}", git_branch=f"feature/{i}")
        buf = io.StringIO()
        with redirect_stdout(buf), patch.object(label, "compute_label", return_value=None):
            label.backfill(self.db, unlabeled_only=True, progress_every=2)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("[label] ")]
        self.assertEqual([ln.split()[1] for ln in lines], ["2/5", "4/5"])

    def test_dry_run_books_no_attempts(self):
        self.seed("r0", git_branch="feature/x")
        with patch.object(label, "compute_label", return_value=None):
            label.backfill(self.db, unlabeled_only=True, dry_run=True)
        self.assertEqual(self.row("r0")["label_attempts"], 0)

    def test_orca_rollout_scan_is_bounded_to_the_queue_age(self):
        self.seed("r0", age_days=2, agent_kind="codex-cli", session_id="sess-1")
        seen = []

        def fake_index(since_days=None, **_):
            seen.append(since_days)
            return {}
        import orca_outcome
        with patch.object(orca_outcome, "build_worker_done_index", side_effect=fake_index):
            label.backfill(self.db, unlabeled_only=True)
        self.assertTrue(seen, "orca join was never consulted")
        self.assertIsNotNone(seen[0], "an unbounded scan reads every rollout ever written")
        self.assertLess(seen[0], 40)
        self.assertGreaterEqual(seen[0], 2)


class TestPassLookupCost(_DbCase):
    def test_runs_sharing_a_branch_cost_one_gh_call_and_one_git_classification(self):
        from types import SimpleNamespace
        import git_outcome
        for i in range(3):
            self.seed(f"shared{i}", git_branch="feature/shared")
        self.seed("other", git_branch="feature/other")
        pending = SimpleNamespace(outcome=None, source=None)
        with patch.object(label, "_gh_pr_state", return_value=None) as gh, \
                patch.object(label, "_git_has_revert", return_value=False), \
                patch.object(label, "_project_repo_root", return_value="/fake/repo"), \
                patch.object(git_outcome, "live_worktree_branches", return_value=[]), \
                patch.object(git_outcome, "classify_branch_outcome",
                             return_value=pending) as classify:
            label.backfill(self.db, unlabeled_only=True)
        self.assertEqual(gh.call_count, 2, "one gh lookup per distinct branch")
        self.assertEqual(classify.call_count, 2, "one git classification per distinct branch")

    def test_memo_does_not_outlive_the_pass(self):
        self.seed("r0", git_branch="feature/x")
        with patch.object(label, "_gh_pr_state", return_value=None) as gh, \
                patch.object(label, "_git_has_revert", return_value=False), \
                patch.object(label, "_project_repo_root", return_value=None):
            label.backfill(self.db, unlabeled_only=True, ignore_backoff=True)
            label.backfill(self.db, unlabeled_only=True, ignore_backoff=True)
            label.label_from_pr("feature/x", None)
            label.label_from_pr("feature/x", None)
        self.assertEqual(gh.call_count, 4, "a new pass, or a direct call, must look again")

    def test_rollout_index_is_not_built_for_agents_that_leave_no_rollout(self):
        self.seed("claude", agent_kind="host_claude_code", session_id="sess-claude")
        import orca_outcome
        with patch.object(orca_outcome, "build_worker_done_index", return_value={}) as build:
            label.backfill(self.db, unlabeled_only=True)
        build.assert_not_called()


class TestSlowVerdictsGoLast(_DbCase):
    """A few runs whose verdict needs the history-wide pickaxe check must not
    eat the pass before the cheap runs behind them are looked at."""

    def _git_world(self, slow_branches, verify):
        from types import SimpleNamespace
        import git_outcome

        def classify(repo, branch, **_):
            if branch in slow_branches:
                return SimpleNamespace(outcome="abandoned", source="git_discarded_unmerged")
            return SimpleNamespace(outcome=None, source=None)
        return [
            patch.object(label, "_gh_pr_state", return_value=None),
            patch.object(label, "_git_has_revert", return_value=False),
            patch.object(label, "_project_repo_root", return_value="/fake/repo"),
            patch.object(git_outcome, "live_worktree_branches", return_value=[]),
            patch.object(git_outcome, "classify_branch_outcome", side_effect=classify),
            patch.object(git_outcome, "verify_discarded_landed", side_effect=verify),
        ]

    def _run(self, patches, **kw):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return label.backfill(self.db, **{"unlabeled_only": True, **kw})

    def test_cheap_runs_are_labeled_before_the_slow_ones_at_the_head_of_the_queue(self):
        self.seed("slow-new", age_days=0.1, git_branch="feature/slow")
        self.seed("slow-new-2", age_days=0.2, git_branch="feature/slow")
        for i in range(3):
            self.seed(f"cheap{i}", age_days=1 + i, git_branch=f"feature/cheap{i}")
        verify = unittest.mock.Mock(return_value=False)
        results = self._run(self._git_world({"feature/slow"}, verify))
        order = [r["run_id"] for r in results]
        self.assertEqual(sorted(order[:3]), ["cheap0", "cheap1", "cheap2"])
        self.assertEqual(sorted(order[3:]), ["slow-new", "slow-new-2"])
        by_id = {r["run_id"]: r for r in results}
        self.assertEqual(by_id["slow-new"]["outcome"], "abandoned")
        self.assertEqual(by_id["slow-new-2"]["source"], "git_discarded_verified")
        self.assertEqual(verify.call_count, 1, "two runs on one branch pay for one pickaxe check")

    def test_a_pass_killed_inside_the_slow_check_has_already_booked_the_attempt(self):
        self.seed("slow", age_days=0.1, git_branch="feature/slow")
        seen = {}

        def verify(repo, branch):
            seen["attempts"] = self.row("slow")["label_attempts"]
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._run(self._git_world({"feature/slow"}, verify))
        self.assertEqual(seen["attempts"], 1, "booked before the check started")
        conn = sqlite3.connect(str(self.db))
        try:
            queued = label.select_label_queue(conn, since_days=30, unlabeled_only=True)
        finally:
            conn.close()
        self.assertEqual([r["run_id"] for r in queued], [], "backed off, not retried first tomorrow")

    def test_a_labeled_run_is_never_parked_by_a_full_pass_that_dies_in_the_slow_check(self):
        self.seed("slow", age_days=0.1, git_branch="feature/slow")
        self.exec("UPDATE runs SET outcome='landed', labeled_at='2026-01-01T00:00:00Z', "
                  "label_attempts=5 WHERE run_id='slow'")

        def verify(repo, branch):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._run(self._git_world({"feature/slow"}, verify), unlabeled_only=False)
        row = self.row("slow")
        self.assertEqual(row["outcome"], "landed")
        self.assertIsNone(row["label_state"], "a real outcome and a parked state must never coexist")

    def test_slow_run_booked_once_whatever_the_check_decides(self):
        self.seed("slow", age_days=0.1, git_branch="feature/slow")
        self._run(self._git_world({"feature/slow"}, lambda repo, branch: None))
        row = self.row("slow")
        self.assertEqual(row["label_attempts"], 1)
        self.assertIsNone(row["outcome"])
        self.assertIsNone(row["label_state"])

    def test_single_run_lookup_is_never_deferred(self):
        self.seed("slow", age_days=0.1, git_branch="feature/slow")
        verify = unittest.mock.Mock(return_value=True)
        results = self._run(self._git_world({"feature/slow"}, verify), run_id_filter="slow")
        self.assertEqual([(r["outcome"], r["source"]) for r in results],
                         [("landed", "git_pickaxe_landed")])


class TestReverifyResume(_DbCase):
    def _seed_inferred(self, run_id: str, outcome: str, **kw):
        hist = [{"outcome": outcome, "labeled_at": _ts(NOW), "label_version": 1,
                 "source": "behavior_inference"}]
        self.seed(run_id, outcome=outcome, label_version=1, labeled_at=_ts(NOW),
                  label_history=hist, git_branch=f"feature/{run_id}", **kw)

    def _pass(self, verdicts: dict, **kw):
        calls = []

        def fake(run, **_):
            calls.append(run["run_id"])
            return verdicts[run["run_id"]]
        with patch.object(label, "reverify_run", side_effect=fake):
            res = label.reverify_labels(self.db, dry_run=False, **kw)
        return calls, res

    def test_rerun_skips_confirmed_flipped_and_no_signal_runs(self):
        self._seed_inferred("confirmed", "abandoned")
        self._seed_inferred("flipped", "abandoned")
        self._seed_inferred("silent", "clean")
        verdicts = {
            "confirmed": ("abandoned", "git_discarded_verified"),
            "flipped": ("landed", "git_merged_into_main"),
            "silent": None,
        }
        first, res = self._pass(verdicts)
        self.assertEqual(sorted(first), ["confirmed", "flipped", "silent"])
        self.assertEqual({r["run_id"]: r["status"] for r in res},
                         {"confirmed": "confirmed", "flipped": "flip", "silent": "no_explicit_signal"})
        second, res2 = self._pass(verdicts)
        self.assertEqual(second, [], "a rerun must not redo lookups it already did")
        self.assertEqual(res2, [])

    def test_stamp_expires_after_cooldown(self):
        self._seed_inferred("silent", "clean")
        verdicts = {"silent": None}
        self._pass(verdicts)
        old = _ts(NOW - timedelta(days=label.REVERIFY_COOLDOWN_DAYS + 1))
        feats = json.dumps({"reverify": {"at": old, "status": "no_explicit_signal"}})
        self.exec("UPDATE runs SET features=? WHERE run_id='silent'", (feats,))
        again, _ = self._pass(verdicts)
        self.assertEqual(again, ["silent"])

    def test_dry_run_stamps_nothing(self):
        self._seed_inferred("confirmed", "abandoned")
        with patch.object(label, "reverify_run", return_value=("abandoned", "git_discarded_verified")):
            label.reverify_labels(self.db, dry_run=True)
        calls, _ = self._pass({"confirmed": ("abandoned", "git_discarded_verified")})
        self.assertEqual(calls, ["confirmed"])

    def test_false_abandon_audit_still_sees_stamped_runs(self):
        self._seed_inferred("confirmed", "abandoned")
        self._pass({"confirmed": ("abandoned", "git_discarded_verified")})
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            with patch.object(label, "reverify_run", return_value=("abandoned", "x")):
                report = label.false_abandon_rate(conn, n=10)
        finally:
            conn.close()
        self.assertEqual(report["n_population"], 1)

    def test_reverify_progress_line(self):
        for i in range(3):
            self._seed_inferred(f"r{i}", "clean")
        buf = io.StringIO()
        with redirect_stdout(buf), patch.object(label, "reverify_run", return_value=None):
            label.reverify_labels(self.db, dry_run=True, progress_every=2)
        self.assertIn("[reverify] 2/3", buf.getvalue())


class TestRollup(_DbCase):
    def _abandoned(self, run_id: str, run_key: str, source="behavior_inference", **kw):
        hist = [{"outcome": "abandoned", "labeled_at": _ts(NOW), "label_version": 1, "source": source}]
        self.seed(run_id, run_key=run_key, outcome="abandoned", label_version=1,
                  labeled_at=_ts(NOW), label_history=hist, **kw)

    def _fixture(self):
        self._abandoned("seg1", "K1")                                    # sibling landed -> flip
        self.seed("seg1-landed", run_key="K1", outcome="landed",
                  label_history=[{"outcome": "landed", "source": "pr_merge"}])
        self._abandoned("solo", "K2")                                    # no sibling -> kept
        self._abandoned("seg3", "K3")                                    # sibling only inferred-clean
        self.seed("seg3-clean", run_key="K3", outcome="clean",
                  label_history=[{"outcome": "clean", "source": "behavior_inference"}])
        self._abandoned("seg4", "K4")                                    # sibling committed -> flip
        self.seed("seg4-commit", run_key="K4", features={"has_resolving_commit": True})
        self._abandoned("explicit", "K1", source="git_empty_branch")     # explicit label: never touched

    def test_dry_run_reports_flips_and_writes_nothing(self):
        self._fixture()
        before = self.sql("SELECT run_id, outcome, features FROM runs ORDER BY run_id")
        results = label.rollup_labels(self.db, dry_run=True)
        flips = sorted(r["run_id"] for r in results if r["would_flip"])
        self.assertEqual(flips, ["seg1", "seg4"])
        self.assertNotIn("explicit", [r["run_id"] for r in results])
        self.assertEqual(self.sql("SELECT run_id, outcome, features FROM runs ORDER BY run_id"), before)

    def test_apply_refuses_without_an_undo_file(self):
        self._fixture()
        with self.assertRaises(ValueError):
            label.rollup_labels(self.db, dry_run=False)

    def test_apply_clears_parks_and_the_undo_file_restores(self):
        self._fixture()
        undo = Path(self._tmp.name) / "undo.jsonl"
        label.rollup_labels(self.db, dry_run=False, undo_path=undo)
        for rid in ("seg1", "seg4"):
            row = self.row(rid)
            self.assertIsNone(row["outcome"])
            self.assertIsNone(row["labeled_at"])
            self.assertEqual(row["label_state"], "rolled_up")
            self.assertEqual(json.loads(row["features"])["rollup"]["reason"],
                             "landed_sibling_on_run_key")
        for rid in ("solo", "seg3", "explicit"):
            self.assertEqual(self.row(rid)["outcome"], "abandoned", rid)

        saved = [json.loads(ln) for ln in undo.read_text().splitlines()]
        self.assertEqual(sorted(s["run_id"] for s in saved), ["seg1", "seg4"])
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        for s in saved:
            conn.execute(
                "UPDATE runs SET outcome=?, labeled_at=?, label_version=?, label_history=?, "
                "features=?, label_attempts=?, last_attempt_at=?, label_state=? WHERE run_id=?",
                (s["outcome"], s["labeled_at"], s["label_version"], s["label_history"],
                 s["features"], s["label_attempts"], s["last_attempt_at"], s["label_state"],
                 s["run_id"]),
            )
        conn.close()
        self.assertEqual(self.row("seg1")["outcome"], "abandoned")
        self.assertIsNone(self.row("seg1")["label_state"])

    def test_rolled_up_runs_stay_out_of_the_nightly_queue_and_keep_their_state(self):
        self._fixture()
        label.rollup_labels(self.db, dry_run=False,
                            undo_path=Path(self._tmp.name) / "undo.jsonl")
        calls: list = []

        def fake(run, events, **_):
            calls.append(run["run_id"])
            return None
        with patch.object(label, "compute_label", side_effect=fake):
            label.backfill(self.db, unlabeled_only=True)
            self.assertNotIn("seg1", calls)
            label.backfill(self.db, unlabeled_only=True, ignore_backoff=True)
        self.assertIn("seg1", calls)
        self.assertEqual(self.row("seg1")["label_state"], "rolled_up",
                         "a forced re-check must not relabel it as the labeler giving up")

    def test_apply_is_idempotent(self):
        self._fixture()
        undo = Path(self._tmp.name) / "undo.jsonl"
        label.rollup_labels(self.db, dry_run=False, undo_path=undo)
        again = label.rollup_labels(self.db, dry_run=False, undo_path=undo)
        self.assertFalse(any(r["would_flip"] for r in again))

    def test_cli_apply_without_undo_file_exits_nonzero(self):
        self._fixture()
        argv = ["label.py", "--db-path", str(self.db), "--rollup-days", "30", "--apply"]
        err = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(sys, "stderr", err), \
                self.assertRaises(SystemExit) as cm:
            label.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("requires --undo-file", err.getvalue())


if __name__ == "__main__":
    unittest.main()
