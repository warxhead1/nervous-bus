"""tests/test_query.py — Unit tests for query.py.

Uses an in-memory fixture SQLite DB — never touches the real runs.db.

Covers:
- project filtering
- time-window filtering (--since / --days)
- outcome filtering
- null-vs-clean gating (the invariant: labeled_at IS NULL → unlabeled, NOT clean)
- prevalence math (reusing base.py helper logic; verified against manual calc)
- empty-DB graceful path
- JSON output mode (via argparse / main())
- stats outcome breakdown correctness
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# Add parent directory so we can import query.py and detectors/base.py
_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))

from query import (
    query_runs,
    query_thrash,
    query_prevalence,
    query_issues,
    query_stats,
    query_sql,
    schema_catalog,
    _validate_select,
    open_db_ro,
    _cutoff,
    main,
    THRASH_OUTCOMES,
    SCHEMA_CATALOG,
)

# ── Fixture helpers ───────────────────────────────────────────────────────────

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    run_key             TEXT NOT NULL,
    run_key_kind        TEXT NOT NULL,
    host_conversation_id TEXT,
    project             TEXT NOT NULL,
    agent_kind          TEXT NOT NULL DEFAULT 'host_claude_code',
    session_id          TEXT,
    agent_id            TEXT,
    started             TEXT NOT NULL,
    ended               TEXT NOT NULL,
    close_reason        TEXT,
    continues_run_id    TEXT,
    event_count         INTEGER NOT NULL DEFAULT 0,
    tool_histogram      TEXT NOT NULL DEFAULT '{}',
    worktree            TEXT,
    worktree_slug       TEXT,
    git_branch          TEXT,
    bead_id             TEXT,
    outcome             TEXT,
    labeled_at          TEXT,
    label_version       INTEGER,
    label_history       TEXT NOT NULL DEFAULT '[]',
    features            TEXT NOT NULL DEFAULT '{}',
    schema_version      TEXT NOT NULL DEFAULT '1',
    recorded_at         TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z'
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detector_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    detector    TEXT NOT NULL,
    signature   TEXT NOT NULL,
    project     TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dh_project_det ON detector_hits(project, detector, ts);

CREATE TABLE IF NOT EXISTS issues (
    signature                  TEXT PRIMARY KEY,
    project                    TEXT NOT NULL,
    detector                   TEXT NOT NULL,
    first_seen                 TEXT NOT NULL,
    last_seen                  TEXT NOT NULL,
    recurrence_count           INTEGER NOT NULL DEFAULT 1,
    recurrence_count_at_apply  INTEGER,
    evidence_json              TEXT NOT NULL DEFAULT '[]'
);
"""


def _make_db() -> sqlite3.Connection:
    """Create a fresh writable in-memory DB with the full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_RUNS_DDL)
    return conn


def _ts(days_ago: int = 0, hours_ago: int = 0) -> str:
    """Return an RFC3339 UTC timestamp for (days_ago * 24 + hours_ago) hours back."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    project: str,
    outcome: str | None = None,
    labeled_at: str | None = None,
    started: str | None = None,
    ended: str | None = None,
    event_count: int = 5,
    tool_histogram: dict | None = None,
    worktree_slug: str | None = None,
    git_branch: str | None = None,
    bead_id: str | None = None,
    close_reason: str = "idle_timeout",
    features: dict | None = None,
) -> None:
    now = _ts()
    conn.execute(
        """
        INSERT INTO runs (
            run_id, run_key, run_key_kind, project, agent_kind,
            started, ended, close_reason, event_count, tool_histogram,
            worktree_slug, git_branch, bead_id,
            outcome, labeled_at, label_history, features, recorded_at
        ) VALUES (
            ?, ?, 'session', ?, 'host_claude_code',
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, '[]', ?, ?
        )
        """,
        (
            run_id, run_id, project,
            started or now, ended or now,
            close_reason, event_count,
            json.dumps(tool_histogram or {}),
            worktree_slug, git_branch, bead_id,
            outcome, labeled_at,
            json.dumps(features or {}),
            now,
        ),
    )


def _insert_hit(
    conn: sqlite3.Connection,
    run_id: str,
    detector: str,
    signature: str,
    project: str,
    ts: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
        (run_id, detector, signature, project, ts or _ts()),
    )


def _insert_issue(
    conn: sqlite3.Connection,
    signature: str,
    project: str,
    detector: str,
    recurrence_count: int = 1,
    first_seen: str | None = None,
    last_seen: str | None = None,
    recurrence_count_at_apply: int | None = None,
    evidence: list | None = None,
) -> None:
    now = _ts()
    conn.execute(
        """
        INSERT INTO issues (
            signature, project, detector, first_seen, last_seen,
            recurrence_count, recurrence_count_at_apply, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signature, project, detector,
            first_seen or now, last_seen or now,
            recurrence_count, recurrence_count_at_apply,
            json.dumps(evidence or []),
        ),
    )


# ── Tests: query_runs ─────────────────────────────────────────────────────────

class TestQueryRuns(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = _make_db()
        # Three projects, mix of labeled/unlabeled
        # started= is set explicitly so time-window filters work as expected.
        _insert_run(self.conn, "run-001", "alpha", outcome="clean",    labeled_at=_ts(1), started=_ts(1))
        _insert_run(self.conn, "run-002", "alpha", outcome="clean",    labeled_at=_ts(0, 2), started=_ts(0, 2))
        _insert_run(self.conn, "run-003", "alpha", outcome=None,       labeled_at=None, started=_ts(0, 1))
        _insert_run(self.conn, "run-004", "beta",  outcome="abandoned", labeled_at=_ts(2), started=_ts(2))
        _insert_run(self.conn, "run-005", "gamma", outcome=None,        labeled_at=None)

    def tearDown(self) -> None:
        self.conn.close()

    def test_no_filter_returns_all(self) -> None:
        rows = query_runs(self.conn)
        self.assertEqual(len(rows), 5)

    def test_project_filter(self) -> None:
        rows = query_runs(self.conn, project="alpha")
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["project"], "alpha")

    def test_outcome_clean_gates_on_labeled_at(self) -> None:
        # outcome='clean' must only return labeled rows with outcome='clean'
        rows = query_runs(self.conn, outcome="clean")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["outcome"], "clean")
            self.assertTrue(r["labeled"], "outcome='clean' rows must have labeled=1")

    def test_outcome_unlabeled_returns_null_labeled_at(self) -> None:
        rows = query_runs(self.conn, outcome="unlabeled")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertIsNone(r["outcome"], "unlabeled rows must have outcome=NULL")
            self.assertFalse(r["labeled"])

    def test_null_outcome_never_returned_as_clean(self) -> None:
        """Critical: unlabeled rows must NEVER appear in outcome='clean' results."""
        clean_rows = query_runs(self.conn, outcome="clean")
        for r in clean_rows:
            self.assertIsNotNone(
                r["outcome"],
                "A run with labeled_at IS NULL must not appear in outcome='clean' results",
            )
            self.assertTrue(r["labeled"])

    def test_since_filter(self) -> None:
        # run-001 was started 1 day ago; run-002 and run-003 started <1 day ago
        since = _ts(0, 3)  # 3 hours ago — should exclude run-001 (1 day old)
        rows = query_runs(self.conn, project="alpha", since=since)
        run_ids = {r["run_id"] for r in rows}
        self.assertIn("run-002", run_ids)
        self.assertIn("run-003", run_ids)
        self.assertNotIn("run-001", run_ids)

    def test_days_filter(self) -> None:
        # Only runs from last 1 day should return; run-004 is 2 days old
        rows = query_runs(self.conn, days=1)
        run_ids = {r["run_id"] for r in rows}
        self.assertNotIn("run-004", run_ids)

    def test_outcome_abandoned_filter(self) -> None:
        rows = query_runs(self.conn, outcome="abandoned")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run-004")

    def test_empty_db(self) -> None:
        empty = _make_db()
        rows = query_runs(empty)
        self.assertEqual(rows, [])
        empty.close()


# ── Tests: query_thrash ───────────────────────────────────────────────────────

class TestQueryThrash(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = _make_db()
        # started= explicit so time-window tests are deterministic
        _insert_run(self.conn, "t-001", "proj", outcome="thrashed",  labeled_at=_ts(1), started=_ts(1))
        _insert_run(self.conn, "t-002", "proj", outcome="abandoned", labeled_at=_ts(0, 2), started=_ts(0, 2))
        _insert_run(self.conn, "t-003", "proj", outcome="reverted",  labeled_at=_ts(0, 1), started=_ts(0, 1))
        _insert_run(self.conn, "t-004", "proj", outcome="clean",     labeled_at=_ts(0, 1), started=_ts(0, 1))
        _insert_run(self.conn, "t-005", "proj", outcome=None,        labeled_at=None)

    def tearDown(self) -> None:
        self.conn.close()

    def test_only_thrash_outcomes_returned(self) -> None:
        rows = query_thrash(self.conn)
        outcomes = {r["outcome"] for r in rows}
        self.assertEqual(outcomes, THRASH_OUTCOMES)

    def test_clean_not_included(self) -> None:
        rows = query_thrash(self.conn)
        run_ids = {r["run_id"] for r in rows}
        self.assertNotIn("t-004", run_ids)

    def test_unlabeled_not_included(self) -> None:
        """Unlabeled runs must not appear in thrash — not confirmed thrash."""
        rows = query_thrash(self.conn)
        run_ids = {r["run_id"] for r in rows}
        self.assertNotIn("t-005", run_ids)

    def test_project_filter(self) -> None:
        _insert_run(self.conn, "t-006", "other", outcome="abandoned", labeled_at=_ts(0))
        rows = query_thrash(self.conn, project="proj")
        for r in rows:
            self.assertEqual(r["project"], "proj")

    def test_days_filter_excludes_old(self) -> None:
        # t-001 started 1 day ago; days=0 means cutoff=now, so 1-day-old row excluded
        rows = query_thrash(self.conn, days=0)
        run_ids = {r["run_id"] for r in rows}
        self.assertNotIn("t-001", run_ids)

    def test_empty_db(self) -> None:
        rows = query_thrash(_make_db())
        self.assertEqual(rows, [])


# ── Tests: query_prevalence ───────────────────────────────────────────────────

class TestQueryPrevalence(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = _make_db()
        # 4 runs total in "alpha", 2 hit by worktree_leak
        for i in range(4):
            _insert_run(self.conn, f"p-{i}", "alpha", started=_ts(0, i))
        _insert_hit(self.conn, "p-0", "worktree_leak", "sig-1", "alpha")
        _insert_hit(self.conn, "p-1", "worktree_leak", "sig-2", "alpha")

    def tearDown(self) -> None:
        self.conn.close()

    def test_rate_math(self) -> None:
        rows = query_prevalence(self.conn, project="alpha", days=7)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["project"], "alpha")
        self.assertEqual(row["detector"], "worktree_leak")
        self.assertEqual(row["hit_runs"], 2)
        self.assertEqual(row["total_runs"], 4)
        self.assertAlmostEqual(row["rate"], 0.5, places=3)

    def test_no_hits_no_rows(self) -> None:
        empty = _make_db()
        rows = query_prevalence(empty, days=7)
        self.assertEqual(rows, [])
        empty.close()

    def test_project_filter(self) -> None:
        # Add runs + hits for "beta"
        _insert_run(self.conn, "pb-0", "beta")
        _insert_hit(self.conn, "pb-0", "worktree_leak", "sig-b", "beta")
        rows = query_prevalence(self.conn, project="alpha", days=7)
        projects = {r["project"] for r in rows}
        self.assertNotIn("beta", projects)

    def test_all_projects_when_no_filter(self) -> None:
        _insert_run(self.conn, "pb-0", "beta")
        _insert_hit(self.conn, "pb-0", "worktree_leak", "sig-b", "beta")
        rows = query_prevalence(self.conn, days=7)
        projects = {r["project"] for r in rows}
        self.assertIn("alpha", projects)
        self.assertIn("beta", projects)

    def test_window_excludes_old_hits(self) -> None:
        # Add an old hit (15 days ago) — should not count in 7-day window
        old_ts = _ts(15)
        self.conn.execute(
            "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
            ("p-2", "worktree_leak", "sig-old", "alpha", old_ts),
        )
        rows = query_prevalence(self.conn, project="alpha", days=7)
        row = rows[0]
        # hit_runs should still be 2 (not 3)
        self.assertEqual(row["hit_runs"], 2)


# ── Tests: query_issues ───────────────────────────────────────────────────────

class TestQueryIssues(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = _make_db()
        _insert_issue(self.conn, "sig-wt-1", "proj-a", "worktree_leak", recurrence_count=5)
        _insert_issue(self.conn, "sig-wt-2", "proj-a", "worktree_leak", recurrence_count=2)
        _insert_issue(self.conn, "sig-other", "proj-b", "some_detector", recurrence_count=3)

    def tearDown(self) -> None:
        self.conn.close()

    def test_ordered_by_recurrence(self) -> None:
        rows = query_issues(self.conn)
        counts = [r["recurrence_count"] for r in rows]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_project_filter(self) -> None:
        rows = query_issues(self.conn, project="proj-a")
        self.assertTrue(all(r["project"] == "proj-a" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_evidence_parsed(self) -> None:
        _insert_issue(self.conn, "sig-ev", "p", "d", evidence=["foo=bar", "baz=qux"])
        rows = query_issues(self.conn, project="p")
        row = rows[0]
        self.assertIsInstance(row["evidence"], list)
        self.assertIn("foo=bar", row["evidence"])

    def test_since_filter(self) -> None:
        old_ts = _ts(10)
        _insert_issue(
            self.conn, "sig-old", "proj-a", "worktree_leak",
            last_seen=old_ts, first_seen=old_ts,
        )
        rows = query_issues(self.conn, project="proj-a", since=_ts(3))
        sigs = {r["signature"] for r in rows}
        self.assertNotIn("sig-old", sigs)

    def test_empty_db(self) -> None:
        rows = query_issues(_make_db())
        self.assertEqual(rows, [])


# ── Tests: query_stats ────────────────────────────────────────────────────────

class TestQueryStats(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = _make_db()
        # project "proj": 3 clean labeled, 1 abandoned labeled, 2 unlabeled
        _insert_run(self.conn, "s-001", "proj", outcome="clean",     labeled_at=_ts(1), event_count=10,
                    tool_histogram={"Read": 5, "Edit": 2})
        _insert_run(self.conn, "s-002", "proj", outcome="clean",     labeled_at=_ts(0), event_count=20,
                    tool_histogram={"Grep": 4, "Write": 1})
        _insert_run(self.conn, "s-003", "proj", outcome="clean",     labeled_at=_ts(0), event_count=15,
                    tool_histogram={"Glob": 3, "Edit": 3})
        _insert_run(self.conn, "s-004", "proj", outcome="abandoned", labeled_at=_ts(0), event_count=8,
                    tool_histogram={"Bash": 5})
        _insert_run(self.conn, "s-005", "proj", outcome=None,        labeled_at=None,   event_count=3)
        _insert_run(self.conn, "s-006", "proj", outcome=None,        labeled_at=None,   event_count=2)

    def tearDown(self) -> None:
        self.conn.close()

    def test_total_and_unlabeled_counts(self) -> None:
        rows = query_stats(self.conn, project="proj")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["total_runs"], 6)
        self.assertEqual(row["unlabeled_runs"], 2)
        self.assertEqual(row["labeled_runs"], 4)

    def test_outcome_breakdown_separates_unlabeled(self) -> None:
        """NULL outcome must appear as 'unlabeled' bucket, NOT folded into 'clean'."""
        rows = query_stats(self.conn, project="proj")
        breakdown = rows[0]["outcome_breakdown"]
        # clean bucket
        self.assertEqual(breakdown.get("clean", 0), 3)
        # abandoned bucket
        self.assertEqual(breakdown.get("abandoned", 0), 1)
        # unlabeled bucket exists and has count 2
        self.assertEqual(breakdown.get("unlabeled", 0), 2)
        # clean count is exactly 3 — the 2 unlabeled runs did NOT inflate it
        self.assertNotEqual(breakdown.get("clean", 0), 5,
                            "unlabeled runs must not be counted under 'clean'")

    def test_read_to_finding_ratio(self) -> None:
        rows = query_stats(self.conn, project="proj")
        ratio = rows[0]["read_to_finding_ratio"]
        # Runs with writes: s-001 (5 reads, 2 writes → 2.5), s-002 (4 reads, 1 write → 4.0),
        #                    s-003 (3 reads, 3 writes → 1.0)
        # Average = (2.5 + 4.0 + 1.0) / 3 = 2.5
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 2.5, places=1)

    def test_empty_db_returns_empty(self) -> None:
        rows = query_stats(_make_db())
        self.assertEqual(rows, [])

    def test_project_filter(self) -> None:
        _insert_run(self.conn, "other-01", "other-proj", outcome="clean", labeled_at=_ts(0))
        rows = query_stats(self.conn, project="proj")
        self.assertTrue(all(r["project"] == "proj" for r in rows))


# ── Tests: _cutoff ────────────────────────────────────────────────────────────

class TestCutoff(unittest.TestCase):

    def test_since_wins_over_days(self) -> None:
        result = _cutoff("2026-01-01T00:00:00Z", days=7)
        self.assertEqual(result, "2026-01-01T00:00:00Z")

    def test_days_only(self) -> None:
        result = _cutoff(None, days=7)
        self.assertIsNotNone(result)
        # Should be approximately 7 days ago
        expected = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:00Z")
        # Just check the date portion
        self.assertEqual(result[:10], expected[:10])

    def test_neither_returns_none(self) -> None:
        self.assertIsNone(_cutoff(None, None))


# ── Tests: CLI JSON output ────────────────────────────────────────────────────

class TestCLIJsonOutput(unittest.TestCase):
    """Smoke-test the CLI entrypoint with a mock DB path."""

    def _make_tmp_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_RUNS_DDL)
        _insert_run(conn, "cli-001", "myproj", outcome="clean",    labeled_at=_ts(0))
        _insert_run(conn, "cli-002", "myproj", outcome=None,       labeled_at=None)
        _insert_run(conn, "cli-003", "myproj", outcome="abandoned", labeled_at=_ts(0))
        conn.commit()
        conn.close()
        return db_path

    def test_runs_json_output(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "runs", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(len(data), 3)

    def test_stats_json_output(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "stats", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(len(data), 1)
            row = data[0]
            breakdown = row["outcome_breakdown"]
            # Null outcome (cli-002) must appear as 'unlabeled', not 'clean'
            self.assertEqual(breakdown.get("clean", 0), 1)
            self.assertEqual(breakdown.get("unlabeled", 0), 1)
            self.assertEqual(breakdown.get("abandoned", 0), 1)

    def test_thrash_json_output(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "thrash", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            # Only 'abandoned' qualifies from our fixture
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["outcome"], "abandoned")

    def test_missing_db_returns_error(self) -> None:
        buf = StringIO()
        with patch("sys.stderr", buf):
            rc = main(["--db", "/nonexistent/path/runs.db", "runs", "--json"])
        self.assertEqual(rc, 1)


# ── Tests: empty detector tables ─────────────────────────────────────────────

class TestEmptyDetectorTables(unittest.TestCase):
    """Ensure prevalence and issues work gracefully when detector tables are empty."""

    def test_prevalence_empty_returns_empty(self) -> None:
        conn = _make_db()
        rows = query_prevalence(conn, days=7)
        self.assertEqual(rows, [])
        conn.close()

    def test_issues_empty_returns_empty(self) -> None:
        conn = _make_db()
        rows = query_issues(conn)
        self.assertEqual(rows, [])
        conn.close()


# ── Tests: _validate_select + query_sql ──────────────────────────────────────

class TestValidateSelect(unittest.TestCase):
    """Unit tests for the SQL guard layer."""

    def test_valid_select_passes(self) -> None:
        sql = "SELECT run_id, project FROM runs WHERE project = 'alpha'"
        result = _validate_select(sql)
        self.assertEqual(result, sql)

    def test_select_with_leading_whitespace(self) -> None:
        sql = "   SELECT 1   "
        result = _validate_select(sql)
        self.assertEqual(result.strip(), "SELECT 1")

    def test_select_with_line_comment_stripped(self) -> None:
        # Comment should be stripped, leaving a valid SELECT
        sql = "-- this is a comment\nSELECT run_id FROM runs"
        result = _validate_select(sql)
        self.assertIn("SELECT", result)

    def test_semicolon_rejected(self) -> None:
        """Semicolons are multi-statement indicators — must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            _validate_select("SELECT 1; DROP TABLE runs")
        self.assertIn("semicolon", str(ctx.exception).lower())

    def test_trailing_semicolon_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("SELECT run_id FROM runs;")

    def test_insert_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _validate_select("INSERT INTO runs VALUES (1)")
        self.assertIn("insert", str(ctx.exception).lower())

    def test_update_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("UPDATE runs SET outcome='clean' WHERE run_id='x'")

    def test_delete_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("DELETE FROM runs")

    def test_drop_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("DROP TABLE runs")

    def test_pragma_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("PRAGMA journal_mode=WAL")

    def test_attach_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("ATTACH DATABASE '/tmp/x.db' AS x")

    def test_with_cte_rejected(self) -> None:
        """WITH ... SELECT is blocked (conservative guard — no CTE gymnastics)."""
        with self.assertRaises(ValueError):
            _validate_select("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _validate_select("   ")
        self.assertIn("Empty", str(ctx.exception))

    def test_case_insensitive_rejection(self) -> None:
        with self.assertRaises(ValueError):
            _validate_select("insert into runs values (1)")


class TestQuerySql(unittest.TestCase):
    """Integration tests for query_sql against a fixture DB."""

    def setUp(self) -> None:
        self.conn = _make_db()
        _insert_run(self.conn, "qs-001", "alpha", outcome="clean", labeled_at=_ts(0))
        _insert_run(self.conn, "qs-002", "beta",  outcome="clean", labeled_at=_ts(0))

    def tearDown(self) -> None:
        self.conn.close()

    def test_valid_select_returns_rows(self) -> None:
        rows = query_sql(self.conn, "SELECT run_id, project FROM runs ORDER BY run_id")
        self.assertEqual(len(rows), 2)
        self.assertIn("run_id", rows[0])
        self.assertIn("project", rows[0])

    def test_invalid_statement_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            query_sql(self.conn, "DELETE FROM runs")

    def test_semicolon_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            query_sql(self.conn, "SELECT 1; SELECT 2")

    def test_empty_result_returns_empty_list(self) -> None:
        rows = query_sql(self.conn, "SELECT * FROM runs WHERE project = 'nonexistent'")
        self.assertEqual(rows, [])

    def test_rows_are_dicts(self) -> None:
        rows = query_sql(self.conn, "SELECT run_id FROM runs LIMIT 1")
        self.assertIsInstance(rows[0], dict)


# ── Tests: schema_catalog ─────────────────────────────────────────────────────

class TestSchemaCatalog(unittest.TestCase):

    def test_catalog_returns_dict(self) -> None:
        cat = schema_catalog()
        self.assertIsInstance(cat, dict)

    def test_all_four_tables_present(self) -> None:
        cat = schema_catalog()
        for table in ("runs", "run_events", "detector_hits", "issues"):
            self.assertIn(table, cat)

    def test_outcome_column_documents_null_caveat(self) -> None:
        """The outcome column description must mention NULL = unlabeled."""
        desc = SCHEMA_CATALOG["runs"]["outcome"]
        self.assertIn("NULL", desc)
        self.assertIn("labeled_at", desc)

    def test_labeled_at_column_present(self) -> None:
        self.assertIn("labeled_at", SCHEMA_CATALOG["runs"])

    def test_raw_json_describes_data_subobject(self) -> None:
        desc = SCHEMA_CATALOG["run_events"]["raw_json"]
        self.assertIn("tool_name", desc)

    def test_catalog_via_conn(self) -> None:
        conn = _make_db()
        cat = schema_catalog(conn)
        self.assertIn("runs", cat)
        conn.close()


# ── Tests: CLI sql + schema subcommands ───────────────────────────────────────

class TestCLISqlSchema(unittest.TestCase):

    def _make_tmp_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_RUNS_DDL)
        _insert_run(conn, "sql-001", "proj", outcome="clean", labeled_at=_ts(0))
        conn.commit()
        conn.close()
        return db_path

    def test_sql_select_json(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main([
                    "--db", str(db_path),
                    "sql", "SELECT run_id, project FROM runs",
                    "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(len(data), 1)
            self.assertIn("run_id", data[0])

    def test_sql_write_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            err_buf = StringIO()
            with patch("sys.stderr", err_buf):
                try:
                    rc = main([
                        "--db", str(db_path),
                        "sql", "DELETE FROM runs",
                    ])
                    # Should have exited with error
                    self.assertNotEqual(rc, 0)
                except SystemExit as e:
                    self.assertNotEqual(e.code, 0)

    def test_sql_semicolon_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            err_buf = StringIO()
            with patch("sys.stderr", err_buf):
                try:
                    main([
                        "--db", str(db_path),
                        "sql", "SELECT 1; DROP TABLE runs",
                    ])
                    self.fail("Expected SystemExit")
                except SystemExit as e:
                    self.assertNotEqual(e.code, 0)

    def test_schema_json_output(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "schema", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIn("runs", data)
            self.assertIn("outcome", data["runs"])

    def test_schema_text_output(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._make_tmp_db(Path(tmp))
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "schema"])
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("runs", output)
            self.assertIn("outcome", output)
            # Must mention the null caveat
            self.assertIn("NULL", output)


if __name__ == "__main__":
    unittest.main()
