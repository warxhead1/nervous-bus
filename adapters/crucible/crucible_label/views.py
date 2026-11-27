"""Generated label views for `crucible ledger build`: render(records, state) -> {path: text}.
Nothing is produced until the first label record exists."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from crucible_label import registry

RULE_KIND = "rule"
MAX_RULES = 10


def _j(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def labels_json(labels):
    rows = [{k: l[k] for k in ("label_id", "key", "kind", "text", "say", "def", "was", "locked", "state", "merged_into")}
            for l in sorted(labels.values(), key=lambda l: l["key"])]
    return _j({"labels": rows})


def glossary(labels):
    out = ["# Glossary", "", "Generated from the label registry. Do not edit by hand.", ""]
    for l in sorted(labels.values(), key=lambda l: (l["kind"], l["key"])):
        if l["kind"] == RULE_KIND:
            continue
        line = f"- **{l['text']}** (`{l['key']}`, {l['kind'] or 'unkinded'})"
        if l["def"]:
            line += f": {l['def']}"
        if l["was"]:
            line += " Formerly: " + ", ".join(l["was"]) + "."
        if l["state"] in ("retired", "merged"):
            line += " " + ("Retired." if l["state"] == "retired" else f"Merged into {l['merged_into']}.")
        out.append(line)
    return "\n".join(out) + "\n"


def lexicon(labels, records):
    out = ["# Lexicon", "", "Generated from the label registry. Do not edit by hand.", "", "## Edits", ""]
    for p in registry.edit_pairs(records, labels, ""):
        out.append(f"- {p['from']} -> {p['to']} ({p['label_id']})")
    out += ["", "## Naming rules", ""]
    rules = [l for l in registry.live(labels) if l["kind"] == RULE_KIND][:MAX_RULES]
    out += [f"- {l['text']} ({l['label_id']})" for l in rules]
    return "\n".join(out) + "\n"


def strings(labels):
    return _j({l["key"]: l["text"] for l in registry.live(labels) if l["kind"] != RULE_KIND})


def render(records, state):
    if not any(r["type"].startswith("label.") for r in records):
        return {}
    labels = registry.fold_labels(records)
    return {
        os.path.join("design", "labels.json"): labels_json(labels),
        os.path.join("design", "GLOSSARY.md"): glossary(labels),
        os.path.join("design", "lexicon.md"): lexicon(labels, records),
        os.path.join("assets", "strings", "en.json"): strings(labels),
    }
