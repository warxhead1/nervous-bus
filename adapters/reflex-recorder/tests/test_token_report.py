"""token_report: ranked waste candidates; digest never carries raw commands/paths."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402
import token_report as tr  # noqa: E402


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class TestReport(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / "tokens.db"
        c = tl.connect(self.db)
        c.execute("INSERT INTO sessions (key, source, project, is_subagent, started) VALUES"
                  " ('k1','claude','proj',1,?)", (_ts(60),))
        reqs = [  # seq, minutes_ago, cache_read, cache_create, cache_5m
            (1, 60, 0, 50_000, 50_000),
            (2, 59, 150_000, 1_000, 1_000),
            (3, 40, 0, 151_000, 151_000),  # 19-min idle gap: 5m TTL expired, full rewrite
        ]
        for seq, ago, cr, cc, c5 in reqs:
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"k1#{seq}", "k1", seq, _ts(ago), "m", 1, cr, cc, 0, c5, 10, 0, 0))
        for i, ago in enumerate((58, 57)):
            c.execute("INSERT INTO tool_calls (id, session_key, ts, tool, cmd_class, cmd, cmd_hash,"
                      " result_chars) VALUES (?,?,?,?,?,?,?,?)",
                      (f"t{i}", "k1", _ts(ago), "Bash", "test",
                       "API_KEY=sekrit cargo test --manifest-path /home/eric/private/x", "h1", 400))
        c.commit()
        c.close()

    def tearDown(self):
        self.td.cleanup()

    def test_candidates_and_sanitization(self):
        rep = tr.build(sqlite3.connect(self.db), days=7)
        cands = {c["id"]: c for c in rep["candidates"]}
        self.assertEqual(set(cands), {"long_context", "cache_rewrites", "big_tool_results",
                                      "startup_context", "redundant_reruns", "uncached_input"})
        gaps = cands["cache_rewrites"]["detail"]["by_idle_gap"]
        self.assertEqual([(g["kind"], g["idle_gap"], g["ttl5m_tokens"]) for g in gaps],
                         [("subagent", "b 5-60m", 151_000)])
        wb = cands["cache_rewrites"]["detail"]["all_cache_writes"]
        self.assertEqual([(w["kind"], w["ttl5m_tokens"]) for w in wb], [("subagent", 202_000)])
        self.assertGreater(cands["long_context"]["iet"], 0)
        top = cands["redundant_reruns"]["detail"]["top"]
        self.assertEqual(top[0]["command"], "cargo test")
        text = tr.to_markdown(rep) + json.dumps(rep)
        for leak in ("sekrit", "/home/eric/private", "API_KEY"):
            self.assertNotIn(leak, text)

    def test_prior_proposals_effect(self):
        rep = tr.build(sqlite3.connect(self.db), days=7)
        props = Path(self.td.name) / "proposals.jsonl"
        props.write_text(json.dumps({"filed": "2026-10-01", "candidate_id": "cache_rewrites",
                                     "title": "t", "candidate_iet": 999}) + "\n")
        tr.attach_prior_proposals(rep, props)
        pp = rep["prior_proposals"][0]
        self.assertEqual(pp["iet_then"], 999)
        self.assertGreater(pp["iet_now"], 0)
        self.assertIn("Prior proposals", tr.to_markdown(rep))


if __name__ == "__main__":
    unittest.main()
