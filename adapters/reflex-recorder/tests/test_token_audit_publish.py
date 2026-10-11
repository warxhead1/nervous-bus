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

    def test_duplicate_title_rejected(self):
        (self.dir / "proposals.jsonl").write_text(
            json.dumps({"title": "Give subagents the 1h cache TTL"}) + "\n")
        with self.assertRaises(pub.Rejected):
            self._run(_report())


if __name__ == "__main__":
    unittest.main()
