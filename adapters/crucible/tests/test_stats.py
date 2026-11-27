import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from crucible_ledger.store import Ledger

CRUCIBLE = os.path.join(BASE, "crucible")

FAKE_HEARTH = """#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
print(open(os.environ["FAKE_HEARTH_ROWS"]).read())
"""

FAKE_REFLEX = """#!/usr/bin/env python3
import json, os, sqlite3, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\\n")
assert args[0] == "sql" and args[-1].lstrip().upper().startswith("SELECT"), args
con = sqlite3.connect(os.environ["FAKE_REFLEX_DB"])
con.row_factory = sqlite3.Row
print(json.dumps([dict(r) for r in con.execute(args[-1])]))
"""


def run(args, env):
    p = subprocess.run([sys.executable, CRUCIBLE, *args], capture_output=True, text=True, env=dict(os.environ, **env))
    assert p.stdout.count("\n") == 1 or not p.stdout.strip(), "stdout must be one JSON document: " + p.stdout
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crucible-stats-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "CRUCIBLE_BULK": os.path.join(self.tmp, "bulk")}


class LedgerStats(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "design")
        os.makedirs(self.root)
        self.led = Ledger(self.root)
        for i in range(1, 8):
            self.rec("question.raised", {
                "q_id": self.q(i), "ask": f"Question {i}?", "class": "lead" if i == 3 else "owner",
                "door": "two-way", "weight": "divergent", "end": "card_batch",
            }, ts="2026-11-24T08:00:0%dZ" % i)
        for i in (1, 2, 3, 7):
            self.rec("question.asked", {"q_id": self.q(i), "mode": "batch", "class": "lead" if i == 3 else "owner",
                                        "door": "two-way", "parts": [{"id": "p1", "text": "t", "options": [{"id": "a", "label": "A"}]}]},
                     ts="2026-11-24T09:00:00Z")
        self.rec("question.triaged", {"q_id": self.q(4), "route": "lookup"})
        self.rec("question.triaged", {"q_id": self.q(5), "route": "lab"})
        self.rec("question.routed", {"q_id": self.q(6), "route": "taken"})
        ans = [(1, 10000, True, False, 0, "pick"), (2, 20000, False, True, 0, "pick"),
               (3, 30000, False, True, 12, "text"), (7, 40000, True, True, 0, "pick")]
        for i, ms, rec, probe, typed, kind in ans:
            body = {"q_id": self.q(i), "kind": kind, "latency_ms": ms, "was_recommended": rec, "probe": probe, "typed_chars": typed}
            if kind == "pick":
                body["picks"] = {"p1": "a"}
            self.rec("question.answered", body, ts="2026-11-24T09:10:00Z")
        self.rec("decision.settled", {"d_id": "d-1", "q_id": self.q(1), "chosen": {"p1": "a"}, "authority": "lead", "door": "two-way"},
                 ts="2026-11-24T10:00:00Z")
        self.rec("decision.settled", {"d_id": "d-2", "q_id": self.q(2), "chosen": {"p1": "a"}, "authority": "owner", "door": "two-way"},
                 ts="2026-11-24T10:00:00Z")
        self.rec("decision.flipped", {"d_id": "d-1", "via": "kiln-tap"}, ts="2026-11-24T10:05:00Z")
        self.rec("lane.rework", {"l_id": "l-1", "d_id": "d-1", "lane_hours": 1.5})
        self.rec("lane.rework", {"l_id": "l-2", "d_id": "d-2", "lane_hours": 9})
        self.rec("sitting.closed", {"sit_id": "sit-1", "pushes": 3}, ts="2026-11-24T11:00:00Z")
        self.rec("sitting.closed", {"sit_id": "sit-2", "pushes": 1}, ts="2026-11-25T11:00:00Z")
        self.rec("investigation.finished", {"inv_id": "inv-1", "model_requests": 8})
        self.rec("audit.finished", {"aud_id": "aud-1", "model_requests": 4})

    def q(self, i):
        return "q-01JD00000000000000000000%02d" % i

    def rec(self, typ, body, ts="2026-11-24T12:00:00Z"):
        self.led.append({"game": "g", "by": "crucible", "via": "test", "type": typ, "ts": ts, "body": body})

    def stats(self, *args):
        return run(["stats", "--root", self.root, *args], self.env)

    def test_terms_equal_the_hand_computed_values(self):
        rc, s = self.stats("--requests")
        self.assertEqual(rc, 0, s)
        self.assertEqual((s["raised"], s["cards"], s["deflected"]), (7, 4, 3))
        self.assertEqual(s["deflected_by_route"], {"lookup": 1, "lab": 1, "research": 0, "taken": 1, "unrouted": 0})
        self.assertEqual(s["seconds_per_card"], 25.0)
        self.assertEqual(s["gross_saved_seconds"], 75.0)
        self.assertEqual(s["cost_of_wrong_takes"], {"flips": 1, "strip_seconds": 300.0, "rework_lane_hours": 1.5})
        self.assertEqual(s["false_auto_rate"], {"lead": {"probes": 1, "overridden": 1, "rate": 1.0},
                                                "owner": {"probes": 2, "overridden": 1, "rate": 0.5}})
        oe = s["owner_effort"]
        self.assertEqual(oe["pushes_per_sitting"], 2.0)
        self.assertEqual((oe["sittings"], oe["settled_decisions"], oe["cards_per_settled_decision"]), (2, 2, 2.0))
        self.assertEqual((oe["free_text_share"], oe["answers"]), (0.25, 4))
        self.assertEqual((oe["typed_chars_per_answer"], oe["taps_per_answer"]), (3.0, 0.75))
        self.assertEqual(oe["minutes_per_week"], {"2026-W48": 1.667})
        self.assertEqual(s["requests"], {"total": 12, "per_card": 3.0})

    def test_rubber_stamp_flags_a_fast_accepted_class_only(self):
        flags = lambda *a: {c: v["flag"] for c, v in self.stats("--rubber-min-n", "2", "--rubber-rate", "0.6", *a)[1]["rubber_stamp"].items()}
        self.assertEqual(flags("--rubber-seconds", "15"), {"lead": False, "owner": False})
        self.assertEqual(flags("--rubber-seconds", "25"), {"lead": False, "owner": True})

    def test_since_drops_older_records_and_keeps_strip_lookups(self):
        rc, s = self.stats("--since", "2026-11-25")
        self.assertEqual((s["raised"], s["cards"], s["owner_effort"]["sittings"]), (0, 0, 1))
        self.assertEqual(s["cost_of_wrong_takes"]["flips"], 0)
        self.assertIsNone(s["seconds_per_card"])
        self.assertIsNone(s["gross_saved_seconds"])

    def test_report_is_written_under_the_cache_dir_of_a_temp_home(self):
        rc, s = self.stats()
        path = os.path.join(self.home, ".cache", "nervous-bus", "crucible", "report.md")
        self.assertEqual(s["report"], path)
        text = open(path).read()
        self.assertIn("| cards | 4 |", text)
        self.assertIn("| gross saved (s) | 75.0 |", text)

    def test_bad_usage_is_one_json_error(self):
        rc, s = self.stats("--since", "yesterday")
        self.assertEqual(rc, 2)
        self.assertIn("error", s)
        rc, s = run(["stats", "--root", os.path.join(self.tmp, "nowhere", "x"), "--nope"], self.env)
        self.assertEqual(rc, 2)


class Baseline(Base):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        for name, body in (("hearth-ask", FAKE_HEARTH), ("reflex", FAKE_REFLEX)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        rows = []
        for i in range(10):
            rows.append({"decision_id": f"d{i}", "status": "answered", "answered_by": "default" if i == 9 else "owner",
                         "text": ("note %d" % i) if i < 4 else ("  " if i == 4 else None),
                         "created_at": "2026-11-20T10:00:00+00:00", "responded_at": "2026-11-20T10:01:40+00:00"})
        self.hearth = os.path.join(self.tmp, "rows.json")
        with open(self.hearth, "w") as f:
            json.dump({"count": len(rows), "decisions": rows}, f)
        self.db = os.path.join(self.tmp, "runs.sqlite")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE runs (run_id TEXT, project TEXT, session_id TEXT, outcome TEXT, labeled_at TEXT, started TEXT)")
        runs = [("r1", "s1", "landed", "t"), ("r2", "s1", "landed", "t"), ("r3", "s2", "landed", "t"), ("r4", "s3", "landed", "t"),
                ("r5", "s4", None, None), ("r6", "s5", "abandoned", "t"), ("r7", "s6", "landed", None), ("r8", "s7", None, "t")]
        con.executemany("INSERT INTO runs VALUES (?, 'p', ?, ?, ?, '2026-11-23T00:00:00Z')", runs)
        con.commit()
        con.close()
        ev = lambda s, items, t: json.dumps({"type": "agent.attention.v1", "time": t, "data": {"session_id": s, "items": items}})
        self.ev1 = os.path.join(self.tmp, "events.jsonl")
        with open(self.ev1, "w") as f:
            f.write("\n".join([ev("s1", ["a", "b"], "2026-11-21T00:00:00Z"), ev("s2", [], "2026-11-21T00:00:00Z"),
                               ev("s4", ["x"], "2026-11-21T00:00:00Z"), "not json agent.attention.v1",
                               json.dumps({"type": "other", "data": {"session_id": "s3", "items": ["z"]}})]) + "\n")
        self.ev2 = os.path.join(self.tmp, "events.jsonl.1.gz")
        with gzip.open(self.ev2, "wt") as f:
            f.write("\n".join([ev("s1", ["b", "c"], "2026-11-22T00:00:00Z"), ev("s5", ["y"], "2026-11-22T00:00:00Z")]) + "\n")
        self.env.update({"CRUCIBLE_HEARTH_ASK": os.path.join(self.bin, "hearth-ask"), "CRUCIBLE_REFLEX": os.path.join(self.bin, "reflex"),
                         "FAKE_LOG": self.log, "FAKE_HEARTH_ROWS": self.hearth, "FAKE_REFLEX_DB": self.db})

    def baseline(self, *args):
        return run(["stats", "baseline", "--events", self.ev1, "--events", self.ev2, *args], self.env)

    def test_free_text_share_equals_the_fixtures_known_count(self):
        rc, b = self.baseline()
        self.assertEqual(rc, 0, b)
        h = b["hearth"]
        self.assertEqual((h["answered"], h["free_text"], h["free_text_share"], h["defaulted"]), (10, 4, 0.4, 1))
        self.assertEqual(h["median_seconds_to_answer"], 100.0)

    def test_asks_per_landed_run_joins_events_to_labelled_runs(self):
        rc, b = self.baseline()
        landed = b["runs"]["landed"]
        self.assertEqual((landed["runs"], landed["sessions"], landed["asks"]), (4, 3, 3))
        self.assertEqual(b["asks_per_landed_run"], 0.75)
        self.assertEqual(landed["sessions_with_asks"], 1)
        self.assertEqual(b["runs"]["abandoned"]["asks_per_run"], 1.0)
        self.assertEqual(b["attention"], {"sessions": 4, "events": 5})

    def test_runs_whose_outcome_is_null_are_excluded(self):
        rc, b = self.baseline()
        self.assertEqual(sorted(b["runs"]), ["abandoned", "landed"])
        self.assertEqual(sum(v["runs"] for v in b["runs"].values()), 5)
        self.assertEqual(b["excluded_unlabeled_runs"], 3)
        self.assertEqual(b["attention"]["sessions"], 4)

    def test_only_read_verbs_are_called_and_since_filters_events(self):
        rc, b = self.baseline("--project", "p", "--since", "2026-11-22")
        calls = [json.loads(l) for l in open(self.log)]
        self.assertTrue(all(c[0] in ("list", "sql") for c in calls), calls)
        self.assertIn(["list", "--status", "answered", "--project", "p"], calls)
        self.assertEqual(b["attention"], {"sessions": 2, "events": 2})
        self.assertEqual(b["runs"]["landed"]["asks"], 2)

    def test_report_file_lands_under_the_temp_home(self):
        rc, b = self.baseline()
        path = os.path.join(self.home, ".cache", "nervous-bus", "crucible", "baseline.json")
        self.assertEqual(b["report"], path)
        self.assertEqual(json.load(open(path))["asks_per_landed_run"], 0.75)

    def test_missing_tool_and_bad_project_exit_with_one_json_error(self):
        self.env["CRUCIBLE_HEARTH_ASK"] = os.path.join(self.bin, "absent")
        rc, b = self.baseline()
        self.assertEqual(rc, 127)
        self.assertIn("error", b)
        self.env["CRUCIBLE_HEARTH_ASK"] = os.path.join(self.bin, "hearth-ask")
        rc, b = self.baseline("--project", "x'; DROP")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
