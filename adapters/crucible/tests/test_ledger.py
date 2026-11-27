import glob
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from multiprocessing import Process

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

schema = importlib.import_module("crucible_ledger.schema")
Ledger = importlib.import_module("crucible_ledger.store").Ledger
ulid = importlib.import_module("crucible_ledger.ulid").ulid

CRUCIBLE = os.path.join(BASE, "crucible")
Q = "q-01JD0000000000000000000099"
D = "d-01JD0000000000000000000098"


def run(*args, env=None):
    e = dict(os.environ, **(env or {}))
    p = subprocess.run([sys.executable, CRUCIBLE, *args], capture_output=True, text=True, env=e)
    try:
        body = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        body = None
    return p.returncode, body


def _writer(root, n, tag):
    led = Ledger(root, writer="shared")
    for i in range(n):
        led.append({"game": "g", "by": tag, "via": "test", "type": "node.created",
                    "body": {"key": f"{tag}.{i}", "pad": "x" * 3000}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crucible-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"CRUCIBLE_BULK": os.path.join(self.tmp, "bulk")}
        self.root = os.path.join(self.tmp, "design-wt")
        os.makedirs(self.root)

    def seed(self):
        led = Ledger(self.root)
        base = {"game": "g", "by": "owner", "via": "test"}
        led.append(dict(base, type="question.answered", body={"q_id": Q, "kind": "pick", "picks": {"p1": "a"}}))
        led.append(dict(base, type="decision.settled", body={
            "d_id": D, "q_id": Q, "chosen": {"p1": "a"}, "authority": "owner", "door": "two-way",
            "touches": ["economy.rate"]}))
        return led

    def cl(self, *args):
        return run("ledger", *args, "--root", self.root, env=self.env)


class Contract(unittest.TestCase):
    def test_goldens_validate(self):
        files = glob.glob(os.path.join(BASE, "contract", "golden", "*.json"))
        self.assertGreaterEqual(len(files), 3)
        for f in files:
            with open(f) as fh:
                self.assertEqual(schema.validate_record(json.load(fh)), [], f)
            code, body = run("ledger", "validate", f)
            self.assertEqual(code, 0, f)

    def test_out_of_enum_type_exits_1(self):
        f = os.path.join(BASE, "contract", "invalid", "out-of-enum-type.json")
        code, body = run("ledger", "validate", f)
        self.assertEqual(code, 1)
        self.assertFalse(body["valid"])

    def test_all_invalids_fail_validation(self):
        files = sorted(glob.glob(os.path.join(BASE, "contract", "invalid", "*.json")))
        self.assertGreaterEqual(len(files), 11)
        for f in files:
            with open(f, encoding="utf-8") as fh:
                self.assertTrue(schema.validate_record(json.load(fh)), f)
            code, body = run("ledger", "validate", f)
            self.assertEqual(code, 1, f)
            self.assertFalse(body["valid"], f)

    def test_every_written_or_read_record_type_has_body_schema(self):
        import re
        kinds = "node|question|decision|assumption|label|investigation|experiment|finding|bet|audit|claim|token|sitting|lane|probe"
        pat = re.compile(rf"[\"'](({kinds})\.[a-z][a-z_]*)[\"']")
        found = {"label.kept"}  # read via verb dispatch in crucible_label/registry.py
        for dirpath, dirnames, filenames in os.walk(BASE):
            dirnames[:] = [d for d in dirnames if d not in ("tests", "contract", "__pycache__")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                    for m in pat.finditer(fh.read()):
                        found.add(m.group(1))
        missing = sorted(t for t in found if schema.body_schema(t) is None)
        self.assertEqual(missing, [], f"missing contract/bodies schemas for: {missing}")

    def test_ulid_sorted_unique(self):
        ids = [ulid() for _ in range(2000)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 2000)
        self.assertTrue(all(len(i) == 26 for i in ids))


class Concurrency(Base):
    def test_two_writers_no_loss_no_interleave(self):
        n = 150
        ps = [Process(target=_writer, args=(self.root, n, t)) for t in ("a", "b")]
        for p in ps:
            p.start()
        for p in ps:
            p.join()
            self.assertEqual(p.exitcode, 0)
        files = glob.glob(os.path.join(self.root, "design", "ledger", "*.jsonl"))
        lines = []
        for f in files:
            with open(f, "rb") as fh:
                lines += fh.read().split(b"\n")[:-1]
        self.assertEqual(len(lines), 2 * n)
        recs = [json.loads(line) for line in lines]
        self.assertEqual(len({r["id"] for r in recs}), 2 * n)
        self.assertEqual({r["by"] for r in recs}, {"a", "b"})


class Verbs(Base):
    def test_get_why_build_check(self):
        self.seed()
        code, body = self.cl("get", D)
        self.assertEqual(code, 0)
        self.assertEqual(body["entity"]["state"], "settled")
        code, body = self.cl("get", "--kind", "decision")
        self.assertEqual(body["count"], 1)
        code, body = self.cl("why", D)
        self.assertEqual(code, 0)
        self.assertIn(Q, body["chain"])
        self.assertEqual(self.cl("build", "--check")[0], 1)  # views missing
        self.assertEqual(self.cl("build")[0], 0)
        self.assertEqual(self.cl("build", "--check")[0], 0)
        view = os.path.join(self.root, "design", "decisions", D + ".md")
        with open(view, "a") as f:
            f.write("tamper\n")
        code, body = self.cl("build", "--check")
        self.assertEqual(code, 1)
        self.assertEqual(body["drift"][0]["kind"], "changed")
        self.assertEqual(self.cl("build")[0], 0)
        self.assertEqual(self.cl("build", "--check")[0], 0)

    def test_extra_view_is_drift(self):
        self.seed()
        self.cl("build")
        stray = os.path.join(self.root, "design", "decisions", "d-STRAY.md")
        with open(stray, "w") as f:
            f.write("x")
        self.assertEqual(self.cl("build", "--check")[0], 1)

    def test_invalid_append_refused(self):
        f = os.path.join(self.tmp, "bad.json")
        with open(f, "w") as fh:
            json.dump({"type": "bogus.x", "game": "g", "by": "a", "via": "b", "body": {}}, fh)
        self.assertEqual(self.cl("append", f)[0], 1)

    def test_init_adopts_row(self):
        row = os.path.join(self.tmp, "row.json")
        with open(row, "w") as fh:
            json.dump({"decision_id": "hearth-42", "answer": "Ember", "by": "owner"}, fh)
        code, body = self.cl("init", "--game", "g", "--adopt", row)
        self.assertEqual(code, 0)
        code, body = self.cl("get", body["first_record"])
        self.assertEqual(body["record"]["body"]["text"], "Ember")
        self.assertEqual(self.cl("init", "--game", "g")[0], 1)

    def test_doctor_findings_and_heartbeat(self):
        self.seed()
        self.cl("build")
        code, body = self.cl("doctor")
        self.assertEqual(code, 0, body)
        hb = os.path.join(self.tmp, "bulk", "g")
        os.makedirs(hb)
        p = os.path.join(hb, "steward.heartbeat")
        open(p, "w").close()
        os.utime(p, (1, 1))
        self.assertEqual(self.cl("doctor")[0], 5)

    def test_conflicts(self):
        led = self.seed()
        led.append({"game": "g", "by": "owner", "via": "test", "type": "decision.settled", "body": {
            "d_id": "d-01JD0000000000000000000097", "q_id": Q, "chosen": {}, "authority": "owner",
            "door": "one-way", "constrains": ["economy.rate"]}})
        code, body = self.cl("conflicts")
        self.assertEqual(code, 1)
        self.assertEqual(body["conflicts"][0]["key"], "economy.rate")

    def test_index_rebuild_matches(self):
        self.seed()
        code, body = self.cl("reindex")
        self.assertEqual((body["records"], body["entities"]), (2, 2))


class SelfTest(unittest.TestCase):
    def test_self_test(self):
        code, body = run("ledger", "doctor", "--self-test")
        self.assertEqual(code, 0, body)
        self.assertTrue(body["detected_orphan_decision"] and body["detected_view_drift"])


class Dispatcher(unittest.TestCase):
    def test_dummy_group_reachable(self):
        dummy = os.path.join(BASE, "crucible-zzz")
        with open(dummy, "w") as f:
            f.write("#!/bin/sh\necho '{\"zzz\":true}'\n")
        os.chmod(dummy, 0o755)
        self.addCleanup(os.unlink, dummy)
        code, body = run("zzz")
        self.assertEqual((code, body), (0, {"zzz": True}))
        code, body = run("nope")
        self.assertEqual(code, 2)
        self.assertIn("ledger", run()[1]["groups"])


if __name__ == "__main__":
    unittest.main()
