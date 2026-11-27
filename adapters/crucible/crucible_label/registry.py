"""Fold label.* records into the registry, resolve {{l:<id>}} holes, and the word checks.

Records (body keys): label.proposed {label_id, key, kind, text, say?, def?, locked?, origin?},
label.edited {label_id, edit: rename|merge|split|retire|retag, from, to, ...},
label.shown {label_id}, label.kept {label_id}. `key` never changes; `text` does."""
import re

HOLE = re.compile(r"\{\{l:([^}\s]+)\}\}")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
STOP = frozenset("a an the of and or to in on for with is are".split())
OWNER = "owner"


def fold_labels(records):
    """{label_id: label dict} from the records, in id order. Edits to unknown ids are ignored."""
    labels = {}
    for r in records:
        if not r["type"].startswith("label."):
            continue
        b, verb = r["body"], r["type"].split(".", 1)[1]
        lid = b.get("label_id")
        if verb == "proposed":
            labels[lid] = {
                "label_id": lid, "key": b["key"], "kind": b.get("kind", ""), "text": b["text"],
                "say": b.get("say", b["text"].lower()), "def": b.get("def", ""), "was": [],
                "origin": b.get("origin", r["by"]), "by": r["by"], "at": r["ts"],
                "locked": bool(b.get("locked", r["by"] == OWNER)), "state": "proposed",
                "merged_into": None, "shown_at": None, "edited_at": None, "edits": 0,
            }
            continue
        lab = labels.get(lid)
        if lab is None:
            continue
        if verb == "shown":
            lab["shown_at"] = lab["shown_at"] or r["ts"]
            if lab["state"] == "proposed":
                lab["state"] = "shown"
        elif verb == "kept":
            lab["state"] = "kept"
        elif verb == "edited":
            _apply_edit(lab, r)
    return labels


def _apply_edit(lab, r):
    b, edit = r["body"], r["body"].get("edit")
    lab["edited_at"], lab["edits"] = r["ts"], lab["edits"] + 1
    if edit == "rename":
        if lab["text"] != b["to"]:
            lab["was"].append(lab["text"])
        lab["text"], lab["say"], lab["state"] = b["to"], b.get("say", b["to"].lower()), "edited"
    elif edit == "merge":
        lab["state"], lab["merged_into"] = "merged", b["into"]
    elif edit == "retire":
        lab["state"] = "retired"
    elif edit == "retag":
        lab["kind"], lab["state"] = b["to"], "edited"
    elif edit == "split":
        lab["state"] = "edited"
    if r["by"] == OWNER:
        lab["locked"] = True


def live(labels):
    return [l for l in labels.values() if l["state"] not in ("retired", "merged")]


def target(labels, lid):
    """The label a hole resolves to: follow merges, guard cycles."""
    seen = set()
    while lid in labels and labels[lid]["merged_into"] and lid not in seen:
        seen.add(lid)
        lid = labels[lid]["merged_into"]
    return labels.get(lid)


def resolve(text, labels):
    """Replace each {{l:<id>}} with the current text. Returns (text, unresolved ids)."""
    missing = []

    def sub(m):
        lab = target(labels, m.group(1))
        if lab is None:
            missing.append(m.group(1))
            return m.group(0)
        return lab["text"]

    return HOLE.sub(sub, text), missing


def words(text):
    return [w.lower() for w in WORD.findall(text) if w.lower() not in STOP]


def replaced_words(records):
    """{word: [edit record ids]}: words an owner rename removed, one entry per edit."""
    out = {}
    for r in records:
        b = r["body"]
        if r["type"] != "label.edited" or r["by"] != OWNER or b.get("edit") != "rename":
            continue
        gone = set(words(str(b.get("from", "")))) - set(words(str(b.get("to", ""))))
        for w in gone:
            out.setdefault(w, []).append(r["id"])
    return out


def lint_name(text, records, times=2):
    """Hits: words in a proposed name that the owner has replaced at least `times` times."""
    rep = replaced_words(records)
    return [{"word": w, "replaced": len(rep[w]), "edits": rep[w]} for w in sorted(set(words(text)))
            if len(rep.get(w, [])) >= times]


def vocabulary(labels):
    """Terms an outside service must never see: current and former names of every label."""
    terms = set()
    for l in labels.values():
        for t in [l["text"], l["say"]] + l["was"]:
            if len(t.strip()) >= 3:
                terms.add(t.strip())
    return sorted(terms, key=str.lower)


def outbound_hits(text, labels):
    low = text.lower()
    return [t for t in vocabulary(labels)
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(t.lower()) + r"(?![A-Za-z0-9])", low)]


def edit_pairs(records, labels, kind, limit=12):
    """The last `limit` owner rename pairs for labels of `kind`, oldest first."""
    pairs = []
    for r in records:
        b = r["body"]
        if r["type"] == "label.edited" and r["by"] == OWNER and b.get("edit") == "rename":
            lab = labels.get(b.get("label_id"))
            if lab and (not kind or lab["kind"] == kind):
                pairs.append({"label_id": lab["label_id"], "from": b["from"], "to": b["to"], "at": r["ts"]})
    return pairs[-limit:]
