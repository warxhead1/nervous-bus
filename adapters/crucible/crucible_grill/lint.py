"""The card shape lint (design 3.3). Limits come from rubric/cards.v1.toml."""
import json
import re

from . import cfg

HOLE = re.compile(r"\{\{l:(lbl-[A-Za-z0-9]+)\}\}")
DOORS = {"one-way", "two-way"}
WEIGHTS = {"outcome-critical", "divergent", "benign"}
PART_BAD = re.compile(r"[,:*=]")


def resolve(text, labels):
    """Fill label holes from {label id: text}. Unknown holes stay as written."""
    return HOLE.sub(lambda m: labels.get(m.group(1), m.group(0)), text or "")


def resolve_card(card, labels):
    out = json.loads(json.dumps(card))
    out["text"] = resolve(out.get("text", ""), labels)
    for p in out.get("parts", []):
        for k in ("text", "say"):
            if k in p:
                p[k] = resolve(p[k], labels)
        for o in p.get("options", []):
            o["label"] = resolve(o.get("label", ""), labels)
            if "changes" in o:
                o["changes"] = resolve(o["changes"], labels)
    return out


def _digits(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            _digits(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _digits(v, acc)
    else:
        acc.update(re.findall(r"\d+(?:\.\d+)?", str(obj)))
    return acc


def card_texts(card):
    yield "question", card.get("text", "")
    yield "context", card.get("context", "")
    for p in card.get("parts", []):
        yield f"part {p.get('id')}", p.get("text", "")
        yield f"part {p.get('id')} say", p.get("say", "")
        for o in p.get("options", []):
            yield f"label {o.get('id')}", o.get("label", "")
            yield f"changes {o.get('id')}", o.get("changes", "")


def lint(card, strict_holes=False):
    """Return a list of error strings. Empty means the card may post."""
    lim = cfg.cards()["limits"]
    errs = []
    text = card.get("text", "")
    if not text.strip():
        errs.append("question is empty")
    if len(text) > lim["question_chars"]:
        errs.append(f"question is {len(text)} characters, at most {lim['question_chars']}")
    if card.get("door") not in DOORS:
        errs.append("door is missing or not one-way/two-way")
    if card.get("weight") not in WEIGHTS:
        errs.append("weight is missing or not outcome-critical/divergent/benign")
    money = card.get("money") or card.get("class") == "money"
    if card.get("default_after") and (card.get("door") == "one-way" or money):
        errs.append("a one-way or money card never carries a default-after time")
    parts = card.get("parts") or []
    if not parts:
        errs.append("card has no parts")
    if len(parts) > lim["batch_parts_max"]:
        errs.append(f"{len(parts)} parts, at most {lim['batch_parts_max']}")
    pids = [p.get("id") for p in parts]
    if len(set(pids)) != len(pids) or not all(pids):
        errs.append("part ids are missing or repeat")
    multi = len(parts) > 1 or card.get("mode") == "batch"
    for p in parts:
        w = f"part {p.get('id')}"
        if len(p.get("text", "")) > lim["question_chars"]:
            errs.append(f"{w}: text over {lim['question_chars']} characters")
        if len(p.get("media", [])) > lim["media_per_part"]:
            errs.append(f"{w}: more than {lim['media_per_part']} media")
        opts = p.get("options") or []
        if len(opts) < lim["options_min"]:
            errs.append(f"{w}: fewer than {lim['options_min']} options")
        if len(opts) > lim["options_posted_max"]:
            errs.append(f"{w}: {len(opts)} options, at most {lim['options_posted_max']}")
        if multi and not p.get("plain"):
            errs.append(f"{w}: its options need a description, so post it as a single decision, not in a batch")
        if multi and PART_BAD.search(p.get("text", "")):
            errs.append(f"{w}: batch part text cannot hold , : * =")
        seen = set()
        for o in opts:
            lbl = o.get("label", "")
            bare = HOLE.sub("x", lbl)
            ow = f"{w} option {o.get('id')}"
            if not lbl.strip():
                errs.append(f"{ow}: label is empty")
            if len(bare.split()) > lim["label_words"]:
                errs.append(f"{ow}: label has more than {lim['label_words']} words")
            n = len(bare) if HOLE.search(lbl) and not strict_holes else len(lbl)
            if n > lim["label_chars"]:
                errs.append(f"{ow}: label is {n} characters, at most {lim['label_chars']}")
            if ":" in bare or (multi and PART_BAD.search(bare)):
                errs.append(f"{ow}: label holds a character the phone surface cannot carry")
            if lbl.strip().lower() in seen:
                errs.append(f"{ow}: duplicate label in {w}")
            seen.add(lbl.strip().lower())
            ch = o.get("changes", "")
            if len(ch) > lim["changes_chars"]:
                errs.append(f"{ow}: changes line is {len(ch)} characters, at most {lim['changes_chars']}")
            if not ch.strip() and not p.get("plain"):
                errs.append(f"{ow}: no changes line")
        recs = [i for i, o in enumerate(opts) if o.get("rec")]
        if len(recs) != 1:
            errs.append(f"{w}: needs exactly one recommended option")
        elif recs[0] != 0:
            errs.append(f"{w}: the recommended option must be posted first")
    if strict_holes:
        for where, t in card_texts(card):
            if HOLE.search(t):
                errs.append(f"{where}: unresolved label hole")
    allowed = _digits(card.get("evidence", {}), set())
    for where, t in card_texts(card):
        for n in re.findall(r"\d+(?:\.\d+)?", HOLE.sub("", t)):
            if n not in allowed:
                errs.append(f"{where}: number {n} is not in the evidence")
    return errs
