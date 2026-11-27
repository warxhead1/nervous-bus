"""The deterministic gate: class, door and weight to an end, read from the rubric."""
from . import cfg

TAKE_ENDS = {"owner_take", "lead_take"}
WEIGHT_RANK = {"outcome-critical": 0, "divergent": 1, "benign": 2}

GUARD_WORDS = {
    "pillar": ("pillar",),
    "spend": ("spend", "budget", "paid service", "subscription", "pricing"),
    "look": ("look baseline", "art canon", "locked token"),
    "public-name": ("public name", "store listing", "game title", "product name"),
    "scope": ("first release", "cut from", "scope"),
    "save-format": ("save format", "save file", "save game"),
}


def end_for(cls, door, weight, guard=None, fitted=False):
    """Return (class, gate id, end, default allowed). A guard raises a class, never lowers it.
    `fitted` lets the owner two-way row end as a take once the fitted bar clears."""
    if guard and cls in ("lead", "owner"):
        cls = "money" if guard == "spend" else "owner"
    for g in cfg.triage().get("gate", []):
        if g["class"] == cls and door in g["doors"] and weight in g["weights"]:
            end, gid = g["end"], g["id"]
            if guard and end in TAKE_ENDS:
                end = "card_alone" if door == "one-way" else "card_batch"
            elif fitted and not guard and gid == "gate.owner.two_way":
                end = "owner_take"
            return cls, gid, end, bool(g.get("default"))
    raise ValueError(f"no gate row for {cls}/{door}/{weight}")


def guard_of(text, explicit=None, node=None):
    """An explicit guard, a node's guard, or a guard word in the text."""
    if explicit:
        return explicit
    if node and node.get("guard"):
        return node["guard"]
    low = text.lower()
    for g, words in GUARD_WORDS.items():
        if any(w in low for w in words):
            return g
    return None
