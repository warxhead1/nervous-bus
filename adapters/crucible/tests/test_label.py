import hashlib
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

from crucible_ledger.store import Ledger

CRUCIBLE = os.path.join(BASE, "crucible")


def run(*args, env=None, stdin=None):
    e = dict(os.environ, **(env or {}))
    p = subprocess.run([sys.executable, CRUCIBLE, *args], capture_output=True, text=True, env=e, input=stdin)
    assert p.stdout.count("\n") <= 1, "stdout must be one JSON document: " + p.stdout
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crucible-label-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {"CRUCIBLE_BULK": os.path.join(self.tmp, "bulk")}
        self.root = os.path.join(self.tmp, "design-wt")
        os.makedirs(self.root)
        Ledger(self.root).append({"game": "g", "by": "crucible", "via": "test", "type": "node.created", "body": {"key": "root"}})

    def label(self, *args):
        return run("label", *args, "--root", self.root, env=self.env)

    def ledger(self, *args):
        return run("ledger", *args, "--root", self.root, env=self.env)

    def propose(self, kind, text, by="owner", *extra):
        rc, body = self.label("propose", "--kind", kind, "--text", text, "--by", by, *extra)
        self.assertEqual(rc, 0, body)
        return body

    def rename(self, lid, to, by="owner"):
        rc, body = self.label("edit", lid, "--rename", to, "--by", by, "--via", "kiln-voice")
        self.assertEqual(rc, 0, body)
        return body

    def view(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()


class RenameReachesEveryView(Base):
    def test_rename_changes_views_not_key_or_replay_hash(self):
        a = self.propose("resource", "Salvage", "owner", "--key", "res.k7f3", "--def", "What a crew carries out of a wreck")
        replay = os.path.join(self.root, "replay.jsonl")
        with open(replay, "w") as f:
            f.write(json.dumps({"tick": 1, "gain": {"res.k7f3": 3}}) + "\n")
        self.assertEqual(self.ledger("build")[0], 0)
        for rel in ("design/labels.json", "design/GLOSSARY.md", "design/lexicon.md", "assets/strings/en.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.root, rel)), rel)
        self.assertIn("Salvage", self.view("assets/strings/en.json"))
        before = hashlib.sha256(open(replay, "rb").read()).hexdigest()

        self.rename(a["label_id"], "Scrap")
        self.assertEqual(self.ledger("build")[0], 0)
        for rel in ("design/labels.json", "design/GLOSSARY.md", "design/lexicon.md", "assets/strings/en.json"):
            text = self.view(rel)
            self.assertIn("Scrap", text, rel)
        strings = json.loads(self.view("assets/strings/en.json"))
        self.assertEqual(strings, {"res.k7f3": "Scrap"})
        glossary = self.view("design/GLOSSARY.md")
        self.assertIn("Formerly: Salvage", glossary)
        self.assertIn("Salvage -> Scrap", self.view("design/lexicon.md"))
        lab = json.loads(self.view("design/labels.json"))["labels"][0]
        self.assertEqual((lab["key"], lab["text"], lab["was"]), ("res.k7f3", "Scrap", ["Salvage"]))
        self.assertEqual(hashlib.sha256(open(replay, "rb").read()).hexdigest(), before)
        self.assertEqual(self.ledger("build", "--check")[0], 0)

    def test_hand_edited_view_fails_ledger_build_check(self):
        self.propose("resource", "Scrap")
        self.assertEqual(self.ledger("build")[0], 0)
        path = os.path.join(self.root, "design", "labels.json")
        with open(path, "a") as f:
            f.write(" ")
        rc, body = self.ledger("build", "--check")
        self.assertEqual(rc, 1)
        self.assertIn("design/labels.json", [d["path"] for d in body["drift"]])

    def test_no_label_records_produce_no_views(self):
        rc, body = self.ledger("build")
        self.assertEqual((rc, body["written"]), (0, []))


class Uses(Base):
    def test_uses_lists_every_fixture_site(self):
        a = self.propose("resource", "Salvage", "owner", "--key", "res.k7f3")
        lid = a["label_id"]
        Ledger(self.root).append({"game": "g", "by": "crucible", "via": "test", "type": "question.asked", "body": {
            "q_id": "q-01JD0000000000000000000001", "mode": "batch", "class": "owner", "door": "two-way",
            "parts": [{"id": "p1", "text": "Keep {{l:%s}}?" % lid, "options": [{"id": "a", "label": "yes"}]}]}})
        game = os.path.join(self.tmp, "game")
        os.makedirs(os.path.join(game, "assets"))
        with open(os.path.join(game, "rules.toml"), "w") as f:
            f.write("[economy]\nrate = 4\nunit = \"res.k7f3\"\n")
        with open(os.path.join(game, "assets", "crate.json"), "w") as f:
            f.write(json.dumps({"label": "res.k7f3"}))
        with open(os.path.join(game, "brief.md"), "w") as f:
            f.write("Collect {{l:%s}} first.\nUnrelated line.\nSalvage is the old word.\n" % lid)
        with open(os.path.join(game, "other.txt"), "w") as f:
            f.write("res.k7f3x is a different key\n")
        self.assertEqual(self.ledger("build")[0], 0)
        self.rename(lid, "Scrap")
        self.assertEqual(self.ledger("build")[0], 0)

        rc, body = self.label("uses", lid, "--scan", game)
        self.assertEqual(rc, 0)
        sites = body["sites"]
        paths = {os.path.relpath(s["path"], game) for s in sites if s["kind"] == "file"}
        self.assertEqual(paths, {"rules.toml", "assets/crate.json", "brief.md"})
        self.assertIn(("rules.toml", 3), {(os.path.relpath(s["path"], game), s["line"]) for s in sites if s["kind"] == "file"})
        self.assertEqual([s["type"] for s in sites if s["kind"] == "record"], ["question.asked"])
        self.assertEqual({s["path"] for s in sites if s["kind"] == "view"},
                         {"design/labels.json", "design/GLOSSARY.md", "design/lexicon.md", "assets/strings/en.json"})
        self.assertEqual(body["count"], len(sites))
        self.assertEqual(body["stale_projections"], 1)
        self.assertEqual(body["stale"][0]["name"], "Salvage")
        self.assertTrue(body["stale"][0]["path"].endswith("brief.md"))


class Lint(Base):
    def seed_two_replacements(self):
        a = self.propose("resource", "Salvage")
        b = self.propose("place", "Salvage Yard")
        self.rename(a["label_id"], "Scrap")
        self.rename(b["label_id"], "Scrap Yard")

    def test_lint_exits_1_for_a_word_replaced_twice_and_0_otherwise(self):
        self.seed_two_replacements()
        rc, body = self.label("lint", "--text", "Salvage Depot")
        self.assertEqual(rc, 1)
        self.assertEqual([h["word"] for h in body["hits"]], ["salvage"])
        self.assertEqual(body["hits"][0]["replaced"], 2)
        self.assertEqual(self.label("lint", "--text", "Scrap Depot")[0], 0)

    def test_a_word_replaced_once_is_allowed(self):
        a = self.propose("resource", "Salvage")
        self.rename(a["label_id"], "Scrap")
        self.assertEqual(self.label("lint", "--text", "Salvage Depot")[0], 0)

    def test_agent_proposal_with_a_banned_word_is_rejected_and_owner_is_not(self):
        self.seed_two_replacements()
        rc, body = self.label("propose", "--kind", "place", "--text", "Salvage Depot", "--by", "agent")
        self.assertEqual(rc, 1, body)
        rc, body = self.label("propose", "--kind", "place", "--text", "Salvage Depot", "--by", "owner")
        self.assertEqual(rc, 0, body)

    def test_outbound_lint_exits_1_on_a_registry_term(self):
        a = self.propose("resource", "Salvage")
        self.rename(a["label_id"], "Scrap")
        for q in ("How do games balance Scrap income against upkeep?", "Is salvage a good loop for roguelikes?"):
            rc, body = self.label("lint", "--outbound", "--text", q)
            self.assertEqual(rc, 1, q)
            self.assertTrue(body["terms"])
        rc, body = self.label("lint", "--outbound", "--text", "How do roguelikes balance income against upkeep?")
        self.assertEqual((rc, body["terms"]), (0, []))
        self.assertEqual(self.label("lint", "--outbound", "--text", "Scraps of paper")[0], 0)
        p = subprocess.run([sys.executable, CRUCIBLE, "label", "lint", "--outbound", "--file", "-", "--root", self.root],
                           input="the Scrap economy", capture_output=True, text=True, env=dict(os.environ, **self.env))
        self.assertEqual(p.returncode, 1)

    def test_outbound_lint_stdin_exits_1_on_hit_and_0_on_clean(self):
        a = self.propose("resource", "Salvage")
        self.rename(a["label_id"], "Scrap")
        rc, body = run("label", "lint", "--outbound", "--stdin", "--root", self.root, env=self.env,
                       stdin="How do roguelikes balance Scrap income?")
        self.assertEqual((rc, body["ok"], body["outbound"]), (1, False, True))
        self.assertIn("Scrap", body["terms"])
        rc, body = run("label", "lint", "--outbound", "--stdin", "--root", self.root, env=self.env,
                       stdin="Is salvage a common drop mechanic?")
        self.assertEqual(rc, 1)
        self.assertIn("Salvage", body["terms"])
        rc, body = run("label", "lint", "--outbound", "--stdin", "--root", self.root, env=self.env,
                       stdin="How do roguelikes balance income against upkeep?")
        self.assertEqual((rc, body["ok"], body["terms"]), (0, True, []))


class OwnerLock(Base):
    def test_agent_edit_of_an_owner_locked_label_exits_1(self):
        a = self.propose("resource", "Scrap", "owner")
        lid = a["label_id"]
        rc, body = self.label("edit", lid, "--rename", "Junk", "--by", "agent")
        self.assertEqual(rc, 1, body)
        self.assertIn("owner-locked", body["error"])
        self.assertEqual(self.label("edit", lid, "--retag", "mechanic", "--by", "agent")[0], 1)
        self.assertEqual(self.label("retire", lid, "--by", "agent")[0], 1)
        b = self.propose("resource", "Ore", "agent")
        self.assertEqual(self.label("merge", b["label_id"], "--into", lid, "--by", "agent")[0], 0)
        self.assertEqual(self.label("merge", lid, "--into", b["label_id"], "--by", "agent")[0], 1)
        rc, body = self.label("get", lid)
        self.assertEqual(body["label"]["text"], "Scrap")

    def test_an_owner_edit_locks_an_agent_label(self):
        b = self.propose("resource", "Ore", "agent")
        lid = b["label_id"]
        self.assertFalse(self.label("get", lid)[1]["label"]["locked"])
        self.assertEqual(self.label("edit", lid, "--rename", "Ingot", "--by", "agent")[0], 0)
        self.rename(lid, "Bar", "owner")
        self.assertTrue(self.label("get", lid)[1]["label"]["locked"])
        self.assertEqual(self.label("edit", lid, "--rename", "Rod", "--by", "agent")[0], 1)


class Brief(Base):
    def test_brief_returns_at_most_twelve_edit_pairs_of_the_kind(self):
        for i in range(15):
            a = self.propose("mechanic", f"Gather {i}")
            self.rename(a["label_id"], f"Haul {i}")
        other = self.propose("resource", "Ore")
        self.rename(other["label_id"], "Ingot")
        self.propose("rule", "Prefer one-syllable names", "owner")
        rc, body = self.label("brief", "--kind", "mechanic")
        self.assertEqual(rc, 0)
        self.assertEqual(len(body["edits"]), 12)
        self.assertEqual([e["to"] for e in body["edits"]][-1], "Haul 14")
        self.assertEqual(body["edits"][0]["to"], "Haul 3")
        self.assertNotIn("Ingot", json.dumps(body["edits"]))
        self.assertEqual([r["text"] for r in body["rules"]], ["Prefer one-syllable names"])

    def test_brief_caps_rules_at_ten(self):
        for i in range(13):
            self.propose("rule", f"Rule {i}")
        self.assertEqual(len(self.label("brief", "--kind", "mechanic")[1]["rules"]), 10)


class Resolution(Base):
    def test_holes_follow_rename_merge_and_retire(self):
        a = self.propose("resource", "Salvage")
        b = self.propose("resource", "Ore")
        hole = "Collect {{l:%s}} and {{l:%s}} and {{l:lbl-missing}}" % (a["label_id"], b["label_id"])
        rc, body = self.label("resolve", hole)
        self.assertEqual((rc, body["unresolved"]), (1, ["lbl-missing"]))
        self.assertEqual(body["text"], "Collect Salvage and Ore and {{l:lbl-missing}}")
        self.rename(a["label_id"], "Scrap")
        self.assertEqual(self.label("merge", b["label_id"], "--into", a["label_id"], "--by", "owner")[0], 0)
        rc, body = self.label("resolve", "{{l:%s}} {{l:%s}}" % (a["label_id"], b["label_id"]))
        self.assertEqual((rc, body["text"]), (0, "Scrap Scrap"))
        self.assertEqual(self.label("retire", a["label_id"], "--by", "owner")[0], 0)
        self.assertEqual(self.label("resolve", "{{l:%s}}" % a["label_id"])[1]["text"], "Scrap")
        self.ledger("build")
        self.assertEqual(json.loads(self.view("assets/strings/en.json")), {})

    def test_split_creates_new_labels_and_keeps_the_original(self):
        a = self.propose("resource", "Metal", "owner", "--key", "res.metl")
        rc, body = self.label("edit", a["label_id"], "--split", "Iron", "Copper", "--by", "owner")
        self.assertEqual(rc, 0, body)
        self.assertEqual(len(body["into"]), 2)
        rc, listing = self.label("get", "--kind", "resource")
        self.assertEqual(listing["count"], 3)
        self.assertEqual(self.label("get", a["label_id"])[1]["label"]["key"], "res.metl")

    def test_duplicate_key_and_unknown_label_are_refused(self):
        self.propose("resource", "Scrap", "owner", "--key", "res.k7f3")
        rc, _ = self.label("propose", "--kind", "resource", "--text", "Other", "--key", "res.k7f3")
        self.assertEqual(rc, 1)
        self.assertEqual(self.label("edit", "lbl-nope", "--rename", "X")[0], 1)
        self.assertEqual(self.label("edit", "lbl-nope")[0], 2)


class KilnRename(Base):
    def test_kiln_argv_renames_as_owner_and_records_via(self):
        a = self.propose("resource", "Salvage", "owner")
        lid = a["label_id"]
        rc, body = self.label("edit", lid, "--text", "--Sprint now", "--via", "kiln")
        self.assertEqual(rc, 0, body)
        self.assertEqual(self.label("get", lid)[1]["label"]["text"], "--Sprint now")
        recs, _ = Ledger(self.root).read()
        edit = [r for r in recs if r["type"] == "label.edited"][-1]
        self.assertEqual((edit["via"], edit["by"], edit["body"]["edit"]), ("kiln", "owner", "rename"))

    def test_text_without_by_is_owner_but_an_explicit_agent_stays_locked_out(self):
        a = self.propose("resource", "Salvage", "owner")
        rc, _ = self.label("edit", a["label_id"], "--text", "Scrap", "--by", "agent")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
