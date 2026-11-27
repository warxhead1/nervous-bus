"""Rubric, tree and knobs. Everything the gate decides comes from these files."""
import os
import tomllib

BASE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_cache = {}

# Shipped defaults for design/knobs.toml. A key in the game's file wins.
KNOB_DEFAULTS = {
    "sitting": {"budget": 7},
    "gate": {"min_probes": 20},
    "hearth": {"timeout_s": 30},
    "deferred": {"wake_days": 7},
    "steward": {"poll_s": 30},
}


def _toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def triage():
    if "triage" not in _cache:
        _cache["triage"] = _toml(os.path.join(BASE, "rubric", "triage.v1.toml"))
    return _cache["triage"]


def cards():
    if "cards" not in _cache:
        _cache["cards"] = _toml(os.path.join(BASE, "rubric", "cards.v1.toml"))
    return _cache["cards"]


def tree(path=None):
    return _toml(path or os.path.join(BASE, "trees", "game.v1.toml"))


def clause_ids():
    return {c["id"] for c in triage().get("clause", [])}


def knobs(root):
    out = {k: dict(v) for k, v in KNOB_DEFAULTS.items()}
    p = os.path.join(root, "design", "knobs.toml")
    if os.path.isfile(p):
        for sect, vals in _toml(p).items():
            if isinstance(vals, dict):
                out.setdefault(sect, {}).update(vals)
    return out
