"""crucible label verbs. Every verb prints one JSON object on stdout.
Exit: 0 pass or done, 1 negative result (lint hit, owner lock, not found), 2 usage, 5 environment."""
import argparse
import json
import os
import re
import sys

from crucible_ledger import build
from crucible_ledger.fold import fold
from crucible_ledger.store import Ledger, LedgerError, find_root
from crucible_ledger.ulid import ulid

from . import registry

MAX_FILE = 1_000_000
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__"}


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LedgerError(message, 2)


def _parser():
    common = _Parser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS)
    common.add_argument("--writer", default=argparse.SUPPRESS)
    common.add_argument("--by", default=argparse.SUPPRESS)
    common.add_argument("--via", default=argparse.SUPPRESS)
    p = _Parser(prog="crucible label", description=__doc__)
    p.add_argument("--root", help="design worktree root (default: $CRUCIBLE_DESIGN_ROOT or nearest design/ledger)")
    p.add_argument("--writer", default="crucible", help="ledger file writer name")
    p.add_argument("--by", default=None, help="who acts: owner or an agent name. Only owner edits owner-locked labels")
    p.add_argument("--via", default="cli", help="surface the edit came through")
    sub = p.add_subparsers(dest="verb", required=True, parser_class=_Parser)

    s = sub.add_parser("propose", parents=[common], help="add a label; lint rejects a name the owner replaced twice")
    s.add_argument("--kind", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--key", help="immutable key (default: kind prefix plus four characters)")
    s.add_argument("--say")
    s.add_argument("--def", dest="definition", default="")
    s.add_argument("--game", help="game slug when the ledger has no records yet")
    s.add_argument("--lock", action="store_true", help="lock against agent edits (owner labels lock by default)")

    s = sub.add_parser("get", parents=[common], help="one label, or a filtered list")
    s.add_argument("id", nargs="?")
    s.add_argument("--kind")
    s.add_argument("--state")

    s = sub.add_parser("edit", parents=[common], help="rename, retag or split a label")
    s.add_argument("id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--rename", "--text", dest="rename", metavar="TEXT",
                   help="new text. With --text and no --by, the edit is the owner's (the Kiln Rename pill)")
    g.add_argument("--retag", metavar="KIND")
    g.add_argument("--split", nargs="+", metavar="TEXT", help="new labels that take over part of this one's meaning")
    s.add_argument("--surface")
    s.add_argument("--q", dest="q_id")

    s = sub.add_parser("merge", parents=[common], help="fold a label into another; holes follow the merge")
    s.add_argument("id")
    s.add_argument("--into", required=True)
    s.add_argument("--surface")
    s.add_argument("--q", dest="q_id")

    s = sub.add_parser("retire", parents=[common], help="retire a label; history still resolves")
    s.add_argument("id")

    s = sub.add_parser("shown", parents=[common], help="record that a label was shown to the owner")
    s.add_argument("id")

    s = sub.add_parser("uses", parents=[common], help="every site that uses a label")
    s.add_argument("id")
    s.add_argument("--scan", action="append", default=[], help="extra directory to scan (repeatable)")

    s = sub.add_parser("resolve", parents=[common], help="replace {{l:<id>}} holes with current text; - reads stdin")
    s.add_argument("text")

    s = sub.add_parser("lint", parents=[common], help="exit 1 when a name uses a word the owner replaced twice, "
                       "or with --outbound when a question contains a registry term")
    s.add_argument("--text")
    s.add_argument("--file", help="read the text from a file; - for stdin")
    s.add_argument("--outbound", action="store_true", help="vocabulary check before a question leaves for an outside service")
    s.add_argument("--times", type=int, default=2, help="replacements that make a word banned")

    s = sub.add_parser("brief", parents=[common], help="naming rules and recent edits for a prompt")
    s.add_argument("--kind", required=True)
    return p


def _ledger(a):
    return Ledger(find_root(a.root), a.writer)


def _game(records, explicit=None):
    g = explicit or next((r["game"] for r in records if r.get("game")), None)
    if not g:
        raise LedgerError("ledger has no records; pass --game", 2)
    return g


def _label(labels, lid):
    lab = labels.get(lid)
    if lab is None:
        raise LedgerError(f"no label {lid}", 1)
    return lab


def _guard_lock(lab, by):
    if lab["locked"] and by != registry.OWNER:
        raise LedgerError(f"label {lab['label_id']} is owner-locked; only the owner edits it", 1)


def _guard_lint(text, records, by):
    if by == registry.OWNER:
        return
    hits = registry.lint_name(text, records)
    if hits:
        raise LedgerError("name uses a word the owner replaced twice: " + ", ".join(h["word"] for h in hits), 1)


def _new_key(kind, labels):
    taken = {l["key"] for l in labels.values()}
    prefix = re.sub(r"[^a-z0-9]", "", kind.lower())[:3] or "lbl"
    while True:
        k = f"{prefix}.{ulid()[-4:].lower()}"
        if k not in taken:
            return k


def _propose(led, a, records, labels, kind, text, game, key=None, say=None, definition="", lock=False):
    key = key or _new_key(kind, labels)
    if any(l["key"] == key for l in labels.values()):
        raise LedgerError(f"key {key} already exists", 1)
    body = {"label_id": "lbl-" + ulid(), "key": key, "kind": kind, "text": text, "say": say or text.lower(),
            "def": definition, "origin": a.by}
    if lock:
        body["locked"] = True
    return led.append({"game": game, "by": a.by, "via": a.via, "type": "label.proposed", "body": body})["body"]


def _edit_record(led, a, game, lid, edit, frm, to, **extra):
    body = {"label_id": lid, "edit": edit, "from": frm, "to": to}
    body.update({k: v for k, v in extra.items() if v is not None})
    return led.append({"game": game, "by": a.by, "via": a.via, "type": "label.edited", "body": body})


def cmd_propose(a):
    led = _ledger(a)
    records, _ = led.read()
    labels = registry.fold_labels(records)
    game = _game(records, a.game)
    _guard_lint(a.text, records, a.by)
    body = _propose(led, a, records, labels, a.kind, a.text, game, a.key, a.say, a.definition, a.lock)
    return out({"ok": True, "label_id": body["label_id"], "key": body["key"], "text": body["text"]})


def cmd_get(a):
    records, _ = _ledger(a).read()
    labels = registry.fold_labels(records)
    if a.id:
        return out({"label": _label(labels, a.id)})
    items = [l for l in sorted(labels.values(), key=lambda l: l["key"])
             if (not a.kind or l["kind"] == a.kind) and (not a.state or l["state"] == a.state)]
    return out({"count": len(items), "labels": items})


def cmd_edit(a):
    led = _ledger(a)
    records, _ = led.read()
    labels = registry.fold_labels(records)
    lab = _label(labels, a.id)
    _guard_lock(lab, a.by)
    game = _game(records)
    if a.rename is not None:
        if a.rename == lab["text"]:
            raise LedgerError("new text equals the current text", 1)
        _guard_lint(a.rename, records, a.by)
        rec = _edit_record(led, a, game, a.id, "rename", lab["text"], a.rename, surface=a.surface, q_id=a.q_id)
        return out({"ok": True, "edit": "rename", "record": rec["id"], "key": lab["key"], "from": lab["text"], "to": a.rename})
    if a.retag is not None:
        rec = _edit_record(led, a, game, a.id, "retag", lab["kind"], a.retag, surface=a.surface, q_id=a.q_id)
        return out({"ok": True, "edit": "retag", "record": rec["id"], "from": lab["kind"], "to": a.retag})
    for t in a.split:
        _guard_lint(t, records, a.by)
    made = [_propose(led, a, records, labels, lab["kind"], t, game) for t in a.split]
    rec = _edit_record(led, a, game, a.id, "split", lab["text"], list(a.split), into=[m["label_id"] for m in made],
                       surface=a.surface, q_id=a.q_id)
    return out({"ok": True, "edit": "split", "record": rec["id"], "into": [{"label_id": m["label_id"], "key": m["key"], "text": m["text"]} for m in made]})


def cmd_merge(a):
    led = _ledger(a)
    records, _ = led.read()
    labels = registry.fold_labels(records)
    lab, dest = _label(labels, a.id), _label(labels, a.into)
    _guard_lock(lab, a.by)
    if registry.target(labels, a.into)["label_id"] == a.id:
        raise LedgerError("merge would make a cycle", 1)
    rec = _edit_record(led, a, _game(records), a.id, "merge", lab["text"], dest["text"], into=a.into,
                       surface=a.surface, q_id=a.q_id)
    return out({"ok": True, "edit": "merge", "record": rec["id"], "into": a.into})


def cmd_retire(a):
    led = _ledger(a)
    records, _ = led.read()
    lab = _label(registry.fold_labels(records), a.id)
    _guard_lock(lab, a.by)
    rec = _edit_record(led, a, _game(records), a.id, "retire", lab["text"], None)
    return out({"ok": True, "edit": "retire", "record": rec["id"]})


def cmd_shown(a):
    led = _ledger(a)
    records, _ = led.read()
    _label(registry.fold_labels(records), a.id)
    rec = led.append({"game": _game(records), "by": a.by, "via": a.via, "type": "label.shown", "body": {"label_id": a.id}})
    return out({"ok": True, "record": rec["id"]})


def _read_text(path):
    try:
        data = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    except OSError as e:
        raise LedgerError(str(e), 2)
    return data


def cmd_resolve(a):
    records, _ = _ledger(a).read()
    text, missing = registry.resolve(_read_text("-") if a.text == "-" else a.text, registry.fold_labels(records))
    return out({"text": text, "unresolved": missing}, 1 if missing else 0)


def cmd_lint(a):
    led = _ledger(a)
    records, _ = led.read()
    if a.text is not None:
        text = a.text
    elif a.file:
        text = _read_text(a.file)
    else:
        raise LedgerError("pass --text or --file", 2)
    if a.outbound:
        hits = registry.outbound_hits(text, registry.fold_labels(records))
        return out({"ok": not hits, "outbound": True, "terms": hits}, 1 if hits else 0)
    hits = registry.lint_name(text, records, a.times)
    return out({"ok": not hits, "hits": hits}, 1 if hits else 0)


def cmd_brief(a):
    records, _ = _ledger(a).read()
    labels = registry.fold_labels(records)
    rules = [{"label_id": l["label_id"], "text": l["text"]} for l in registry.live(labels) if l["kind"] == "rule"][:10]
    return out({"kind": a.kind, "rules": rules, "edits": registry.edit_pairs(records, labels, a.kind, 12)})


def _walk(dirs, skip):
    for d in dirs:
        for base, names, files in os.walk(d):
            names[:] = [n for n in names if n not in SKIP_DIRS and os.path.join(base, n) not in skip]
            for f in sorted(files):
                p = os.path.join(base, f)
                try:
                    if os.path.getsize(p) > MAX_FILE:
                        continue
                    with open(p, encoding="utf-8") as fh:
                        yield p, fh.read()
                except (OSError, UnicodeDecodeError):
                    continue


def _token(s):
    return re.compile(r"(?<![\w.-])" + re.escape(s) + r"(?!\w|\.\w)")


def _lines(text, rx):
    return [i for i, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def cmd_uses(a):
    led = _ledger(a)
    records, _ = led.read()
    labels = registry.fold_labels(records)
    lab = _label(labels, a.id)
    hole, key = "{{l:" + a.id + "}}", _token(lab["key"])
    sites = []
    for r in records:
        if r["type"].startswith("label."):
            continue
        blob = json.dumps(r["body"], ensure_ascii=False)
        if hole in blob or a.id in blob or key.search(blob):
            sites.append({"kind": "record", "record": r["id"], "type": r["type"]})
    views = build.expected_views(records, fold(records))
    for rel, text in sorted(views.items()):
        if a.id in text or hole in text or key.search(text):
            sites.append({"kind": "view", "path": rel})
    root = led.root
    skip = {os.path.join(root, "design", "ledger")} | {os.path.join(root, p) for p in views}
    dirs = [root] + [os.path.abspath(d) for d in a.scan]
    seen, stale = set(), []
    former = [(w, _token(w)) for w in lab["was"] if w != lab["text"]]
    for path, text in _walk(dirs, skip):
        if path in seen or path in skip or os.path.relpath(path, root) in views:
            continue
        seen.add(path)
        hits = sorted(set(_lines(text, key)) | set(_lines(text, re.compile(re.escape(hole)))))
        for n in hits:
            sites.append({"kind": "file", "path": path, "line": n})
        for w, rx in former:
            for n in _lines(text, rx):
                stale.append({"path": path, "line": n, "name": w})
    return out({"label_id": a.id, "key": lab["key"], "text": lab["text"], "count": len(sites), "sites": sites,
                "stale_projections": len(stale), "stale": stale})


def _glue_values(argv):
    """`--text --Sprint now` gives a value that starts with dashes; glue it so argparse reads it as the value."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--text", "--rename") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main(argv):
    try:
        a = _parser().parse_args(_glue_values(argv))
        if a.by is None:
            owner_text = a.verb == "edit" and any(x == "--text" or x.startswith("--text=") for x in argv)
            a.by = registry.OWNER if owner_text else "agent"
        return globals()["cmd_" + a.verb](a)
    except LedgerError as e:
        return out({"error": str(e)}, e.code)
    except SystemExit as e:  # --help
        return e.code or 0
