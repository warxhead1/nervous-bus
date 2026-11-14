"""token_audit_publish: the deterministic gate between the mmx report and the queue."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "automation"))
import token_audit_publish as pub  # noqa: E402

ROW = '{"kind":"subagent","idle_gap":"b 5-60m","requests":492}'
DIGEST = {"candidates": [{"id": "cache_rewrites", "iet": 1000, "share_of_total": 0.1}]}


def _report(**over):
    prop = {"candidate_id": "cache_rewrites", "title": "Give subagents the 1h cache TTL",
            "rung": "automate", "mechanism": "setting X in agent defs",
            "expected_iet_saved_per_week": 500, "evidence": [ROW], "verify": "rewrites drop 50%"}
    prop.update(over)
    return "# token-audit\n\nprose\n\n```json\n" + json.dumps({"proposal": prop}) + "\n```\n"


class TestPublish(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        (self.dir / "latest.json").write_text(json.dumps(DIGEST))
        (self.dir / "latest.md").write_text("by_idle_gap:\n    " + ROW + "\n")

    def tearDown(self):
        self.td.cleanup()

    def _run(self, text):
        rp = self.dir / "r.md"
        rp.write_text(text)
        return pub.publish(rp, self.dir, None, dry_run=True)

    def test_valid_proposal_passes(self):
        rec = self._run(_report())
        self.assertEqual(rec["candidate_iet"], 1000)
        self.assertTrue(rec["dry_run"])

    def test_rejections(self):
        cases = {
            "candidate_id": _report(candidate_id="made_up"),
            "exceeds": _report(expected_iet_saved_per_week=5000),
            "verbatim": _report(evidence=['{"kind":"main","requests":9999}']),
            "rung": _report(rung="pray"),
            "json block": "# no block\n",
        }
        for needle, text in cases.items():
            with self.assertRaises(pub.Rejected, msg=needle) as ctx:
                self._run(text)
            self.assertIn(needle, str(ctx.exception))

    def test_candidate_cooldown(self):
        recent = pub.date.today().isoformat()
        (self.dir / "proposals.jsonl").write_text(json.dumps(
            {"title": "something else", "candidate_id": "cache_rewrites", "filed": recent}) + "\n")
        with self.assertRaises(pub.Rejected) as ctx:
            self._run(_report())
        self.assertIn("cooldown", str(ctx.exception))
        (self.dir / "proposals.jsonl").write_text(json.dumps(
            {"title": "something else", "candidate_id": "cache_rewrites", "filed": "2026-01-01"}) + "\n")
        self.assertTrue(self._run(_report())["dry_run"])

    def test_duplicate_title_rejected(self):
        (self.dir / "proposals.jsonl").write_text(
            json.dumps({"title": "Give subagents the 1h cache TTL"}) + "\n")
        with self.assertRaises(pub.Rejected):
            self._run(_report())


class FakeRun:
    def __init__(self, state):
        self.state, self.calls = state, []

    def __call__(self, argv, **kw):
        self.calls.append(argv[:3])
        out = {("gh", "issue", "view"): self.state,
               ("gh", "issue", "create"): "https://github.com/o/r/issues/9",
               ("bd", "create", argv[2]): '{"id": "nb-1"}'}.get(tuple(argv[:3]), "")
        return pub.subprocess.CompletedProcess(argv, 0, out, "")


class TestOpenIssueIsUpdated(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        (self.dir / "latest.json").write_text(json.dumps(DIGEST))
        (self.dir / "latest.md").write_text("by_idle_gap:\n    " + ROW + "\n")
        (self.dir / "proposals.jsonl").write_text(json.dumps(
            {"title": "older wording", "candidate_id": "cache_rewrites", "filed": "2026-01-01",
             "issue": "https://github.com/o/r/issues/4"}) + "\n")
        (self.dir / "r.md").write_text(_report())
        self.real_run = pub.subprocess.run

    def tearDown(self):
        pub.subprocess.run = self.real_run
        self.td.cleanup()

    def _publish(self, state):
        fake = FakeRun(state)
        pub.subprocess.run = fake
        rec = pub.publish(self.dir / "r.md", self.dir, "o/r", dry_run=False)
        ledger = [json.loads(x) for x in (self.dir / "proposals.jsonl").read_text().splitlines()]
        return rec, [c[:3] for c in fake.calls], ledger

    def test_open_issue_gets_a_comment_and_no_new_issue_or_bead(self):
        rec, calls, ledger = self._publish("OPEN")
        self.assertEqual(rec["updated_issue"], "https://github.com/o/r/issues/4")
        self.assertIn(["gh", "issue", "comment"], calls)
        self.assertNotIn(["gh", "issue", "create"], calls)
        self.assertFalse(any(c[0] == "bd" for c in calls))
        self.assertEqual(ledger[-1]["updated_issue"], "https://github.com/o/r/issues/4")

    def test_closed_issue_files_a_new_one(self):
        rec, calls, _ = self._publish("CLOSED")
        self.assertEqual(rec["issue"], "https://github.com/o/r/issues/9")
        self.assertNotIn(["gh", "issue", "comment"], calls)
        self.assertTrue(any(c[0] == "bd" for c in calls))


if __name__ == "__main__":
    unittest.main()
