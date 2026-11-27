"""crucible steward: hearth rows in, ledger records out, with no model and no Redis.

    python3 -m unittest discover -s adapters/crucible/steward/tests -p 'test_steward*'
"""
import configparser
import datetime
import json
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CRUCIBLE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(CRUCIBLE, "tests"))

from test_grill_env import LEDGER, Env, card


class StewardEnv(Env):
    def asked(self, text="Which name fits?", **kw):
        q = self.ask(text, card(text, **kw))[1]["q"]
        self.assertEqual(self.grill("post", q)[0], 0)
        return q

    def sync(self, **kw):
        return self.steward("sync", "--once", **kw)

    def heartbeat(self):
        return os.path.join(self.bulk, "demo", "steward.heartbeat")


class TestSync(StewardEnv):
    def test_answered_row_yields_exactly_one_record_and_a_second_run_none(self):
        q = self.asked()
        self.fake("answer", q, "--option", "b", "--via", "phone")
        code, r = self.sync()
        self.assertEqual((code, r["written"]), (0, 1))
        recs = self.records("question.answered", q)
        self.assertEqual(len(recs), 1)
        b = recs[0]["body"]
        self.assertEqual((b["kind"], b["picks"], b["was_recommended"], b["hearth_id"]), ("pick", {"p1": "b"}, False, q))
        self.assertEqual(recs[0]["via"], "phone")
        code, r = self.sync()
        self.assertEqual((code, r["written"]), (0, 0))
        self.assertEqual(len(self.records("question.answered", q)), 1)

    def test_batch_answers_text_and_recommendation_flag(self):
        for t, lb in (("Pick a mark", ("Ember", "Ash")), ("Pick a tone", ("Moss", "Fern"))):
            self.ask(t, card(t, plain=True, labels=lb))
        bq = self.grill("sit", "--no-open")[1]["pushed"]["q"]
        self.fake("answer", bq, "--pick", "p1=a", "--pick", "p2=a")
        self.sync()
        self.assertTrue(self.records("question.answered", bq)[0]["body"]["was_recommended"])
        q = self.asked("Free words?")
        self.fake("answer", q, "--text", "neither, make it sharper")
        self.sync()
        b = self.records("question.answered", q)[0]["body"]
        self.assertEqual((b["kind"], b["text"], b["typed_chars"]), ("text", "neither, make it sharper", 24))

    def test_defaulted_and_withdrawn_rows_map_to_outcomes(self):
        d = self.asked("Soft pick")
        row_path = os.path.join(self.hearth, "row-" + d + ".json")
        with open(row_path) as f:
            row = json.load(f)
        row.update(status="defaulted", option_id="a", responded_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        with open(row_path, "w") as f:
            json.dump(row, f)
        self.sync()
        self.assertEqual(len(self.records("question.defaulted", d)), 1)
        self.assertEqual(self.records("decision.settled", d)[0]["body"]["authority"], "lead")
        w = self.asked("Dropped pick")
        self.fake("withdraw", w, "--reason", "no longer needed")
        self.sync()
        rec = self.records("question.withdrawn", w)[0]["body"]
        self.assertEqual(rec["reason"], "no longer needed")
        self.assertEqual(self.records("question.answered"), [])
        n = len(self.records())
        self.sync()
        self.assertEqual(len(self.records()), n)

    def test_each_new_comment_is_one_record(self):
        q = self.asked()
        self.fake("comment", q, "--text", "after I play tonight's build")
        self.sync()
        self.sync()
        cs = self.records("question.commented", q)
        self.assertEqual([c["body"]["text"] for c in cs], ["after I play tonight's build"])
        self.fake("comment", q, "--text", "and keep it short")
        self.sync()
        self.assertEqual(len(self.records("question.commented", q)), 2)
        self.assertEqual(self.records("question.answered", q), [])
        self.assertEqual(self.grill("cards", "--json")[1]["cards"][0]["q"], q)

    def test_reserved_words_typed_on_the_phone_settle_without_a_model(self):
        q = self.asked()
        self.fake("answer", q, "--text", "you decide")
        self.sync()
        self.assertEqual(len(self.records("question.delegated", q)), 1)
        self.assertEqual(self.records("question.answered", q), [])
        q2 = self.asked("Second one")
        self.fake("answer", q2, "--text", "later")
        self.sync()
        self.assertEqual(len(self.records("question.deferred", q2)), 1)

    def test_old_row_withdrawn_by_a_rename_does_not_withdraw_the_question(self):
        self.append("label.created", {"lbl_id": "lbl-AAA", "text": "Ember"})
        q = self.ask("Which name fits?", card(labels=("{{l:lbl-AAA}}", "Ash")))[1]["q"]
        self.grill("post", q)
        self.append("label.edited", {"lbl_id": "lbl-AAA", "text": "Cinder"})
        self.grill("post", q)
        self.sync()
        self.assertEqual(self.records("question.withdrawn", q), [])
        self.fake("answer", q + ".2", "--option", "a")
        self.sync()
        self.assertEqual(self.records("question.answered", q)[0]["body"]["hearth_id"], q + ".2")

    def test_answer_through_kiln_is_not_written_twice(self):
        q = self.asked()
        self.grill("answer", q, "--text", "Ash")
        self.sync()
        self.assertEqual(len(self.records("question.answered", q)), 1)

    def test_answer_lands_within_the_poll_interval_plus_five_seconds(self):
        q = self.asked()
        self.fake("answer", q, "--option", "a")
        started = time.time()
        code, _ = self.sync()
        took = time.time() - started
        self.assertEqual(code, 0)
        row = self.fake("get", q)[1]
        answered = datetime.datetime.fromisoformat(row["responded_at"])
        wrote = datetime.datetime.strptime(self.records("question.answered", q)[0]["ts"], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=datetime.timezone.utc)
        poll = 30
        with open(os.path.join(CRUCIBLE, "steward", "crucible-steward.timer")) as f:
            self.assertIn(f"OnUnitActiveSec={poll}s", f.read())
        self.assertLess((wrote - answered).total_seconds(), poll + 5)
        self.assertLess(took, 5)
        self.assertEqual(self.records("question.answered", q)[0]["body"]["latency_ms"] >= 0, True)


class TestHeartbeat(StewardEnv):
    def test_unreachable_hearth_exits_5_and_leaves_the_heartbeat_stale(self):
        q = self.asked()
        self.assertEqual(self.sync()[0], 0)
        old = time.time() - 7200
        os.utime(self.heartbeat(), (old, old))
        self.fake("answer", q, "--option", "a")
        code, r = self.sync(env={"FAKE_HEARTH_DOWN": "1"})
        self.assertEqual(code, 5)
        self.assertFalse(r["ok"])
        self.assertAlmostEqual(os.stat(self.heartbeat()).st_mtime, old, delta=1)
        self.assertEqual(self.records("question.answered", q), [])
        code, r = self.steward("status")
        self.assertEqual((code, r["heartbeat"]), (5, "stale"))
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.steward("status")[0], 0)

    def test_stale_heartbeat_makes_ledger_doctor_exit_5(self):
        self.asked()
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.run_cli(LEDGER, "build", "--root", self.root)[0], 0)
        self.assertEqual(self.run_cli(LEDGER, "doctor", "--root", self.root)[0], 0)
        old = time.time() - 7200
        os.utime(self.heartbeat(), (old, old))
        code, r = self.run_cli(LEDGER, "doctor", "--root", self.root)
        self.assertEqual((code, r["stale_heartbeat"]), (5, True))

    def test_no_heartbeat_yet_is_stale_for_status(self):
        self.assertEqual(self.steward("status")[0], 5)


class TestCycleCommit(StewardEnv):
    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd or self.root,
                              capture_output=True, text=True)

    def test_one_commit_and_one_push_per_cycle(self):
        remote = os.path.join(self.tmp, "remote.git")
        self.git("init", "-q", "--bare", remote, cwd=self.tmp)
        self.git("init", "-q", "-b", "design", self.root, cwd=self.tmp)
        self.git("remote", "add", "origin", remote)
        a, b = self.asked("First pick"), None
        self.fake("answer", a, "--option", "a")
        # a second question is asked before the cycle runs: two records, one commit
        self.fake("comment", a, "--text", "note")
        code, r = self.sync()
        self.assertEqual((code, r["written"]), (0, 2))
        self.assertEqual((r["land"]["committed"], r["land"]["pushed"]), (True, True))
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.git("rev-parse", "design", cwd=remote).stdout)
        files = self.git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertTrue(all(f.startswith("design/ledger/") for f in files))
        code, r = self.sync()
        self.assertEqual(r["land"], {"committed": False})
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")


class TestUnits(unittest.TestCase):
    def test_unit_files_are_committed_and_portable(self):
        d = os.path.join(CRUCIBLE, "steward")
        svc, tim = configparser.ConfigParser(strict=False), configparser.ConfigParser(strict=False)
        svc.read(os.path.join(d, "crucible-steward.service"))
        tim.read(os.path.join(d, "crucible-steward.timer"))
        self.assertIn("steward sync --once", svc["Service"]["ExecStart"])
        self.assertEqual(svc["Service"]["Type"], "oneshot")
        self.assertEqual(tim["Timer"]["OnUnitActiveSec"], "30s")
        for n in ("crucible-steward.service", "crucible-steward.timer"):
            with open(os.path.join(d, n)) as f:
                self.assertNotIn("/home/", f.read())

    def test_steward_imports_no_model_runner_and_no_redis(self):
        d = os.path.join(CRUCIBLE, "steward")
        for n in ("core.py", "cli.py"):
            with open(os.path.join(d, n)) as f:
                src = f.read().lower()
            for banned in ("redis", "claude -p", "agy", "anthropic"):
                self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
