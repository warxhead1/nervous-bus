"""tests/test_remediation_effect.py — rule change points x detector hit rates."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from query import query_remediation_effect, rule_change_points  # noqa: E402
from store import SQLiteStore  # noqa: E402


def _git(tree, *args, date=None):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True, env=env)


class TestRemediationEffect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.tree = root / "rules"
        self.tree.mkdir()
        _git(self.tree, "init", "-q", "-b", "main")
        (self.tree / "rules.toml").write_text(
            '[rule.idle]\nfiles = ["idle.md"]\nrung = "inform"\nthemes = ["idle_dispatch"]\n'
            '[rule.leak]\nfiles = ["wt.md"]\nrung = "automate"\ndetectors = ["worktree_leak"]\n'
        )
        (self.tree / "idle.md").write_text("v1")
        (self.tree / "wt.md").write_text("v1")
        _git(self.tree, "add", "-A")
        _git(self.tree, "commit", "-qm", "baseline", date="2026-08-01T00:00:00Z")
        (self.tree / "idle.md").write_text("v2")
        _git(self.tree, "commit", "-qam", "tighten idle rule", date="2026-08-15T00:00:00Z")

        self.db = root / "runs.db"
        SQLiteStore(self.db)._conn.close()
        c = sqlite3.connect(self.db)
        # 10 runs before the change (4 with an idle correction), 10 after (1).
        for i in range(20):
            day = 5 + i if i < 10 else 15 + (i - 10)
            ts = f"2026-08-{day:02d}T12:00:00Z"
            c.execute("INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind, started, ended,"
                      " recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                      (f"r{i}", f"r{i}", "session", "p", "host_claude_code", ts, ts, ts))
            if (i < 4) or (i == 12):
                c.execute("INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
                          (f"r{i}", "user_correction", "idle_dispatch", "p", ts))
        c.commit()
        c.close()
        self.conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_change_points_skip_baseline(self):
        pts = rule_change_points(self.tree)
        self.assertEqual([(p["rule"], p["at"]) for p in pts], [("idle", "2026-08-15T00:00:00Z")])

    def test_before_after_rates(self):
        (row,) = query_remediation_effect(self.conn, tree=self.tree, window_days=14)
        self.assertEqual(row["target"], "theme:idle_dispatch")
        self.assertEqual((row["before_runs"], row["before_per_100"]), (10, 40.0))
        self.assertEqual((row["after_runs"], row["after_per_100"]), (10, 10.0))
        self.assertEqual(row["delta"], -30.0)
        self.assertTrue(row["window_complete"])

    def test_missing_rules_file_yields_nothing(self):
        (self.tree / "rules.toml").unlink()
        self.assertEqual(query_remediation_effect(self.conn, tree=self.tree), [])


if __name__ == "__main__":
    unittest.main()
