"""Shared fixture for the grill and steward tests: a throwaway ledger, a fake hearth, one CLI runner."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

GRILL = os.path.join(BASE, "crucible-grill")
LABEL = os.path.join(BASE, "crucible-label")
LEDGER = os.path.join(BASE, "crucible-ledger")
STEWARD = os.path.join(BASE, "crucible-steward")
FAKE = os.path.join(BASE, "testdata", "fake-hearth-ask")


def card(text="Pick a name", door="two-way", weight="divergent", plain=False, labels=("Ember", "Ash"), **kw):
    opts = [{"id": chr(97 + i), "label": lb, "changes": "data only", **({"rec": True} if i == 0 else {})}
            for i, lb in enumerate(labels)]
    c = {"text": text, "door": door, "weight": weight, "mode": "fork",
         "parts": [{"id": "p1", "text": text, "plain": plain, "options": opts}]}
    c.update(kw)
    return c


class Env(unittest.TestCase):
    """setUp builds a ledger and a fake hearth under one temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crucible-grill-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "wt")
        self.hearth = os.path.join(self.tmp, "hearth")
        self.bulk = os.path.join(self.tmp, "bulk")
        self.env = dict(os.environ, CRUCIBLE_DESIGN_ROOT=self.root, CRUCIBLE_BULK=self.bulk,
                        CRUCIBLE_HEARTH_ASK=FAKE, FAKE_HEARTH_DIR=self.hearth)
        self.env.pop("FAKE_HEARTH_DOWN", None)
        code, _ = self.run_cli(LEDGER, "init", "--game", "demo", "--root", self.root)
        self.assertEqual(code, 0)

    def run_cli(self, exe, *args, env=None, stdin=None):
        p = subprocess.run([sys.executable, exe, *args], capture_output=True, text=True, input=stdin,
                           env=dict(self.env, **(env or {})))
        try:
            body = json.loads(p.stdout) if p.stdout.strip() else None
        except ValueError:
            body = {"raw": p.stdout}
        return p.returncode, body

    def grill(self, *args, env=None):
        return self.run_cli(GRILL, *args, env=env)

    def steward(self, *args, env=None):
        return self.run_cli(STEWARD, *args, env=env)

    def write(self, name, obj):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def post_card(self, c, name="card.json"):
        code, body = self.grill("post", "--card", self.write(name, c))
        return code, body

    def fake(self, *args):
        p = subprocess.run([sys.executable, FAKE, *args], capture_output=True, text=True, env=self.env)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)

    def calls(self, verb=None):
        try:
            with open(os.path.join(self.hearth, "argv.log")) as f:
                rows = [json.loads(x) for x in f if x.strip()]
        except OSError:
            rows = []
        return [r for r in rows if verb is None or r[0] == verb]

    def records(self, rtype=None, q=None):
        from crucible_ledger.store import Ledger
        recs, _ = Ledger(self.root).read()
        return [r for r in recs if (rtype is None or r["type"] == rtype) and (q is None or r["body"].get("q_id") == q)]

    def append(self, rtype, body):
        from crucible_ledger.store import Ledger
        return Ledger(self.root).append({"type": rtype, "game": "demo", "by": "owner", "via": "test", "body": body})

    def ask(self, text, card_obj=None, extra=(), cls="owner", door="two-way", weight="divergent"):
        args = ["ask", "--text", text, "--class", cls, "--clause", "class.doubt", "--door", door, "--weight", weight]
        if card_obj is not None:
            args += ["--draft", self.write("draft-%d.json" % len(os.listdir(self.tmp)), card_obj)]
        return self.grill(*args, *extra)
