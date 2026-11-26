"""Tests for SteeringHitsDetector and the steering_trends read side."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

import steering_trends
from detectors.steering_hits import SteeringHitsDetector, aggregate


def hit(ts, check="reply-score", surface="reply", session="s1", findings=0, rules=None,
        avg=None, words=None, lost=None, action="none", cwd="/home/x/projects/alpha",
        plugin="plain-prose"):
    r = {"v": 1, "ts": ts, "plugin": plugin, "check": check, "surface": surface,
         "session_id": session, "cwd": cwd, "path": "", "action": action,
         "findings": findings, "rules": rules or {}}
    if avg is not None:
        r.update(avg_sentence=avg, max_sentence=int(avg * 2), words=words or 100)
    if lost:
        r["lost"] = lost
    return r


class SteeringHitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hits.jsonl"
        self.db = sqlite3.connect(":memory:", isolation_level=None)

    def tearDown(self):
        self.tmp.cleanup()

    def detector(self):
        d = SteeringHitsDetector(self.db)
        d.use_bus = False
        d.hits_path = self.path
        return d

    def write(self, *recs, rotated=()):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in recs) + "garbage\n")
        if rotated:
            Path(str(self.path) + ".1").write_text("".join(json.dumps(r) + "\n" for r in rotated))

    def test_aggregates_per_day_and_session_with_weighted_average(self):
        self.write(
            hit("2026-11-20T10:00:00Z", avg=10.0, findings=2, rules={"ai-tell": 2}, action="report"),
            hit("2026-11-20T11:00:00Z", avg=20.0, findings=1, rules={"ai-tell": 1}, action="block"),
            hit("2026-11-21T10:00:00Z", avg=30.0, session="s2"),
            hit("2026-11-21T10:05:00Z", check="prose-compare", surface="doc", findings=1,
                lost={"numbers": 2, "hedges": 1}, action="report"),
        )
        self.detector().detect(self.db)
        by_day = steering_trends.steering_hit_rows(self.db, by="day")
        d20 = next(r for r in by_day if r["day"] == "2026-11-20")
        self.assertEqual((d20["records"], d20["findings"], d20["blocks"]), (2, 3, 1))
        self.assertEqual(d20["avg_sentence"], 15.0)
        d21 = next(r for r in by_day if r["day"] == "2026-11-21")
        self.assertEqual(d21["lost"], 3)
        self.assertEqual(d21["avg_sentence"], 30.0)
        sessions = steering_trends.steering_hit_rows(self.db, by="session")
        self.assertEqual({(r["day"], r["session_id"]) for r in sessions},
                         {("2026-11-20", "s1"), ("2026-11-21", "s1"), ("2026-11-21", "s2")})
        rules = steering_trends.steering_hit_rows(self.db, by="rule")
        self.assertEqual([(r["rule"], r["findings"]) for r in rules], [("ai-tell", 3)])
        self.assertEqual(steering_trends.steering_hit_rows(self.db, by="project")[0]["project"], "alpha")

    def test_change_day_splits_before_and_after(self):
        self.write(hit("2026-11-18T10:00:00Z", avg=24.0), hit("2026-11-19T10:00:00Z", avg=22.0),
                   hit("2026-11-22T10:00:00Z", avg=14.0), hit("2026-11-23T10:00:00Z", avg=16.0))
        self.detector().detect(self.db)
        rows = steering_trends.steering_hit_rows(self.db, by="check", change="2026-11-20")
        got = {r["period"]: r["avg_sentence"] for r in rows}
        self.assertEqual(got, {"before": 23.0, "after": 15.0})

    def test_rotated_file_is_read_and_rerun_is_idempotent(self):
        self.write(hit("2026-11-20T10:00:00Z", avg=10.0),
                   rotated=[hit("2026-11-19T10:00:00Z", avg=10.0)])
        d = self.detector()
        d.detect(self.db)
        d.detect(self.db)
        n = self.db.execute("SELECT SUM(records) FROM steering_hit_daily").fetchone()[0]
        self.assertEqual(n, 2)

    def test_no_prose_in_tables(self):
        self.write(hit("2026-11-20T10:00:00Z", avg=10.0, rules={"ai-tell": 1}))
        self.detector().detect(self.db)
        cols = [c[1] for t in ("steering_hit_daily", "steering_hit_rules")
                for c in self.db.execute(f"PRAGMA table_info({t})")]
        self.assertNotIn("path", cols)
        self.assertNotIn("text", cols)

    def test_filters_and_since(self):
        self.write(hit("2026-11-20T10:00:00Z", avg=10.0),
                   hit("2026-11-21T10:00:00Z", avg=10.0, plugin="other", cwd="/srv/beta"))
        self.detector().detect(self.db)
        self.assertEqual(len(steering_trends.steering_hit_rows(self.db, plugin="other")), 1)
        self.assertEqual(len(steering_trends.steering_hit_rows(self.db, project="alpha")), 1)
        self.assertEqual(len(steering_trends.steering_hit_rows(self.db, since="2026-11-21")), 1)
        with self.assertRaises(ValueError):
            steering_trends.steering_hit_rows(self.db, since="yesterday")

    def test_bus_down_falls_back_to_file_and_unreachable_is_none(self):
        self.write(hit("2026-11-20T10:00:00Z", avg=10.0))
        d = SteeringHitsDetector(self.db)
        d.redis_url = "redis://127.0.0.1:1"  # refused
        d.hits_path = self.path
        self.assertEqual(len(d.records()), 1)

    def test_aggregate_skips_records_without_ts(self):
        agg = aggregate([{"check": "reply-score"}, hit("2026-11-20T10:00:00Z")])
        self.assertEqual(len(agg["daily"]), 1)

    def test_query_without_tables_reports_hint(self):
        with self.assertRaises(LookupError):
            steering_trends.steering_hit_rows(sqlite3.connect(":memory:"))
        with self.assertRaises(LookupError):
            steering_trends.stretch_open_rows(sqlite3.connect(":memory:"))


class QueryWiringTest(unittest.TestCase):
    def test_cli_and_synthesis_registration(self):
        import query
        import synthesis
        args = query.build_parser().parse_args(["steering-hits", "--by", "session", "--change", "2026-11-20"])
        self.assertEqual((args.command, args.by, args.change), ("steering-hits", "session", "2026-11-20"))
        args = query.build_parser().parse_args(["stretch-opens", "--by", "form"])
        self.assertEqual(args.command, "stretch-opens")
        names = [c.DETECTOR_NAME for c in synthesis.DETECTOR_CLASSES]
        self.assertTrue({"steering_hits", "stretch_opens"} <= set(names))
        self.assertLess(names.index("stretch_opens"), names.index("failure_taxonomy"))

    def test_cli_end_to_end(self):
        import io
        import contextlib
        import query
        with tempfile.TemporaryDirectory() as d:
            dbp = Path(d) / "runs.db"
            conn = sqlite3.connect(dbp, isolation_level=None)
            hits = Path(d) / "h.jsonl"
            hits.write_text(json.dumps(hit("2026-11-20T10:00:00Z", avg=12.0)) + "\n")
            det = SteeringHitsDetector(conn)
            det.use_bus, det.hits_path = False, hits
            det.detect(conn)
            conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = query.main(["--db", str(dbp), "steering-hits", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())[0]["avg_sentence"], 12.0)


if __name__ == "__main__":
    unittest.main()
