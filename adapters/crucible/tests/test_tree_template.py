"""The design tree and the rubric are well formed, and each check fails on a planted defect.

    python3 adapters/crucible/tests/test_tree_template.py

Set CRUCIBLE_DENYLIST to a file with one name per line to scan the text for game titles.
"""
import copy
import itertools
import os
import re
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
TREE = HERE / "trees" / "game.v1.toml"
RUBRIC = HERE / "rubric"
TEXT_FILES = [TREE, *sorted(RUBRIC.glob("*")), REPO / "steering" / "grill" / "skills" / "grill" / "SKILL.md",
              REPO / "steering" / "rulings" / "grill.md"]

CLASSES = {"fact", "measurable", "lead", "owner", "money"}
DOORS = {"one-way", "two-way"}
WEIGHTS = {"outcome-critical", "divergent", "benign"}
NODE_MODES = {"fork", "duel", "feel", "name", "rule"}
KINDS = {"frame", "market", "money", "legal", "aesthetic", "pillar", "dynamic", "mechanic", "look", "sound", "feel",
         "tech", "tuning", "content", "ux", "name"}
TRACES = ("dynamic", "aesthetic", "pillar")
ENDS = {"lookup", "lab", "lead_take", "owner_take", "card_batch", "card_alone"}
ID = re.compile(r"^[a-z]+(\.[a-z_]+)+$")


def load(path):
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def load_rubric():
    return {p.name: load(p) for p in sorted(RUBRIC.glob("*.toml"))}


def rubric_items(rubric):
    """Every table that sits in an array of tables, in every rubric file."""
    for name, doc in rubric.items():
        for key, val in doc.items():
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                for item in val:
                    yield name, key, item


def kind(node_id):
    return node_id.split(".")[0]


def closure(node_id, by_id, seen=None):
    """Every node that must settle before this one."""
    seen = set() if seen is None else seen
    for p in by_id.get(node_id, {}).get("prereqs", []):
        if p not in seen and p in by_id:
            seen.add(p)
            closure(p, by_id, seen)
    return seen


def cycle_nodes(nodes):
    """Ids left after every node with settled prerequisites is removed. Empty means no cycle."""
    left = {n["id"]: set(n.get("prereqs", [])) for n in nodes if "id" in n}
    while True:
        free = [i for i, ps in left.items() if not (ps & set(left))]
        if not free:
            return sorted(left)
        for i in free:
            del left[i]


def tree_problems(tree, rubric):
    out, nodes = [], tree.get("node", [])
    limit = rubric["cards.v1.toml"]["limits"]["question_chars"]
    guards = {c["value"] for _, key, c in rubric_items(rubric) if key == "clause" and c.get("check") == "guard" and "value" in c}
    ids = [n.get("id") for n in nodes]
    by_id = {n["id"]: n for n in nodes if "id" in n}
    for i in {i for i in ids if ids.count(i) > 1}:
        out.append(f"duplicate node id {i}")
    asks = [n.get("ask") for n in nodes]
    for n in nodes:
        i = n.get("id")
        if not isinstance(i, str) or not ID.match(i) or kind(i) not in KINDS:
            out.append(f"bad id {i!r}")
            continue
        if n.get("class") not in CLASSES:
            out.append(f"{i}: class {n.get('class')!r} is not in the enum")
        if n.get("door") not in DOORS:
            out.append(f"{i}: door {n.get('door')!r} is not in the enum")
        if n.get("weight") not in WEIGHTS:
            out.append(f"{i}: weight {n.get('weight')!r} is not in the enum")
        ask = n.get("ask", "")
        if not ask or len(ask) > limit or ask.count("?") != 1 or not ask.endswith("?") or asks.count(ask) > 1:
            out.append(f"{i}: ask must be one unique question of at most {limit} characters")
        if not isinstance(n.get("prereqs"), list):
            out.append(f"{i}: prereqs is missing")
            continue
        for p in n["prereqs"]:
            if p not in by_id or p == i:
                out.append(f"{i}: prerequisite {p} does not resolve")
        takes_card = n.get("class") in ("owner", "money")
        if ("mode" in n) != takes_card or n.get("mode", "fork") not in NODE_MODES:
            out.append(f"{i}: a mode belongs on owner and money nodes only, from {sorted(NODE_MODES)}")
        if "guard" in n and (n["guard"] not in guards or not takes_card):
            out.append(f"{i}: guard {n['guard']!r} needs a rubric guard clause and an owner or money class")
        if kind(i) == "mechanic":
            before = closure(i, by_id)
            for t in TRACES:
                target = n.get(t)
                if target not in by_id or kind(target) != t:
                    out.append(f"{i}: trace field {t} must name a {t} node")
                elif target not in before:
                    out.append(f"{i}: {target} must settle first, so it belongs in the prerequisites")
    if nodes and not any(n.get("prereqs") == [] for n in nodes):
        out.append("no node is open at the start")
    stuck = cycle_nodes(nodes)
    if stuck:
        out.append("cycle among " + ", ".join(stuck))
    return out


def rubric_problems(rubric):
    out, ids, phrases = [], [], []
    for name, key, item in rubric_items(rubric):
        i = item.get("id")
        if not isinstance(i, str) or not ID.match(i):
            out.append(f"{name}: a {key} has a bad id {i!r}")
        ids.append(i)
        if key in ("clause", "rule", "gate", "word") and not item.get("text", "").strip():
            out.append(f"{i}: text is empty")
        phrases += item.get("phrases", [])
    for i in {i for i in ids if ids.count(i) > 1}:
        out.append(f"duplicate rubric id {i}")
    for p in {p for p in phrases if phrases.count(p) > 1}:
        out.append(f"reserved phrase {p!r} appears twice")
    triage = rubric["triage.v1.toml"]
    classed = {c.get("value") for c in triage["clause"] if c.get("check") == "class"}
    for c in CLASSES - classed:
        out.append(f"no class clause for {c}")
    for combo in itertools.product(sorted(CLASSES), sorted(DOORS), sorted(WEIGHTS)):
        rows = [g for g in triage["gate"] if g.get("class") == combo[0] and combo[1] in g.get("doors", []) and combo[2] in g.get("weights", [])]
        if len(rows) != 1:
            out.append(f"{combo} matches {len(rows)} gate rows, not one")
            continue
        g = rows[0]
        if g.get("end") not in ENDS:
            out.append(f"{g.get('id')}: end {g.get('end')!r} is not in the enum")
        if combo[0] in ("fact", "measurable") and str(g.get("end")).startswith("card"):
            out.append(f"{g.get('id')}: a {combo[0]} question is never a card")
        if (combo[0] == "money" or combo[1] == "one-way") and combo[0] in ("owner", "money") and (g.get("end") != "card_alone" or g.get("default") is not False):
            out.append(f"{g.get('id')}: a one-way or money question is a card, alone, with no default")
    return out


class Template(unittest.TestCase):
    def setUp(self):
        self.tree, self.rubric = load(TREE), load_rubric()

    def test_the_tree_is_well_formed(self):
        self.assertEqual(tree_problems(self.tree, self.rubric), [])
        self.assertGreater(len(self.tree["node"]), 0)

    def test_the_rubric_is_well_formed(self):
        self.assertEqual(rubric_problems(self.rubric), [])
        self.assertGreater(sum(1 for _ in rubric_items(self.rubric)), 0)

    def test_the_tree_has_mechanic_nodes_to_trace(self):
        self.assertTrue([n for n in self.tree["node"] if kind(n["id"]) == "mechanic"])

    def test_no_game_title_from_the_denylist(self):
        path = os.environ.get("CRUCIBLE_DENYLIST")
        if not path:
            self.skipTest("CRUCIBLE_DENYLIST is not set")
        names = [l.strip().lower() for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(names)
        for f in TEXT_FILES:
            text = f.read_text(encoding="utf-8").lower()
            for name in names:
                self.assertIsNone(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text), f"{name!r} appears in {f.name}")


class PlantedDefects(unittest.TestCase):
    """Each check must go red on the defect it exists for."""

    def setUp(self):
        self.tree, self.rubric = load(TREE), load_rubric()

    def node(self, tree, node_id):
        return next(n for n in tree["node"] if n["id"] == node_id)

    def planted(self, edit):
        tree = copy.deepcopy(self.tree)
        edit(tree)
        return " | ".join(tree_problems(tree, self.rubric))

    def test_a_class_outside_the_enum(self):
        self.assertIn("class 'taste'", self.planted(lambda t: self.node(t, "frame.audience").update({"class": "taste"})))

    def test_a_missing_door(self):
        self.assertIn("door None", self.planted(lambda t: self.node(t, "frame.audience").pop("door")))

    def test_a_missing_id(self):
        self.assertIn("bad id None", self.planted(lambda t: self.node(t, "ux.hud").pop("id")))

    def test_a_prerequisite_that_does_not_resolve(self):
        self.assertIn("prerequisite frame.nowhere does not resolve",
                      self.planted(lambda t: self.node(t, "frame.session")["prereqs"].append("frame.nowhere")))

    def test_a_cycle(self):
        self.assertIn("cycle among", self.planted(lambda t: self.node(t, "frame.audience")["prereqs"].append("dynamic.loop")))

    def test_a_mechanic_without_each_trace_field(self):
        for t in TRACES:
            self.assertIn(f"trace field {t}", self.planted(lambda tree: self.node(tree, "mechanic.verbs").pop(t)))

    def test_a_trace_to_the_wrong_kind(self):
        self.assertIn("trace field pillar", self.planted(lambda t: self.node(t, "mechanic.verbs").update({"pillar": "dynamic.loop"})))

    def test_a_trace_that_settles_too_late(self):
        self.assertIn("must settle first", self.planted(lambda t: self.node(t, "mechanic.verbs").update({"dynamic": "dynamic.social"})))

    def test_a_guarded_node_an_agent_could_take(self):
        self.assertIn("guard 'scope'", self.planted(lambda t: self.node(t, "frame.scope").update({"class": "lead"})))

    def test_a_duplicate_rubric_id(self):
        rubric = copy.deepcopy(self.rubric)
        rubric["cards.v1.toml"]["rule"][0]["id"] = rubric["triage.v1.toml"]["clause"][0]["id"]
        self.assertIn("duplicate rubric id", " | ".join(rubric_problems(rubric)))

    def test_a_gate_that_takes_a_one_way_question(self):
        rubric = copy.deepcopy(self.rubric)
        next(g for g in rubric["triage.v1.toml"]["gate"] if g["id"] == "gate.owner.one_way")["end"] = "owner_take"
        self.assertIn("alone, with no default", " | ".join(rubric_problems(rubric)))

    def test_a_gate_with_a_gap(self):
        rubric = copy.deepcopy(self.rubric)
        rubric["triage.v1.toml"]["gate"] = [g for g in rubric["triage.v1.toml"]["gate"] if g["id"] != "gate.owner.benign"]
        self.assertIn("matches 0 gate rows", " | ".join(rubric_problems(rubric)))


if __name__ == "__main__":
    unittest.main()
