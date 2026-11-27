"""crucible ledger verbs. Every verb prints one JSON object on stdout.
Exit: 0 pass, 1 negative result, 2 usage, 5 stale heartbeat or environment."""
import argparse
import json
import os
import subprocess
import sys

from . import build, doctor, index, schema
from .fold import conflicts as fold_conflicts, fold
from .store import Ledger, LedgerError, bulk_dir, find_root
from .ulid import ulid


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
    p = _Parser(prog="crucible ledger", description=__doc__)
    p.add_argument("--root", help="design worktree root (default: $CRUCIBLE_DESIGN_ROOT or nearest design/ledger)")
    p.add_argument("--writer", default="crucible", help="ledger file writer name")
    sub = p.add_subparsers(dest="verb", required=True, parser_class=_Parser)

    s = sub.add_parser("init", parents=[common], help="create the ledger; adopt a hearth codename row as record one")
    s.add_argument("--game", required=True)
    s.add_argument("--adopt", help="JSON file of the hearth row (decision_id, answer, by)")
    s.add_argument("--repo", help="git repo to create the design branch worktree from")

    s = sub.add_parser("get", parents=[common], help="state of one entity or record, or a filtered list")
    s.add_argument("id", nargs="?")
    s.add_argument("--kind")
    s.add_argument("--state")

    s = sub.add_parser("why", parents=[common], help="the records that explain an id")
    s.add_argument("id")

    s = sub.add_parser("build", parents=[common], help="generate views; --check fails on drift")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true")
    g.add_argument("--land", action="store_true", help="write views and commit them on the design branch")

    s = sub.add_parser("doctor", parents=[common], help="ledger health; --self-test plants faults and expects detection")
    s.add_argument("--self-test", action="store_true")

    sub.add_parser("conflicts", parents=[common], help="live decisions whose touches hit another's constrains")
    sub.add_parser("reindex", parents=[common], help="rebuild the SQLite index from the JSONL")

    s = sub.add_parser("validate", parents=[common], help="validate one record file against the contract")
    s.add_argument("file", help="path or - for stdin")
    s = sub.add_parser("append", parents=[common], help="validate and append one record")
    s.add_argument("file", help="path or - for stdin")
    return p


def _read_json(path):
    data = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    return json.loads(data)


def _ledger(a):
    return Ledger(find_root(a.root), a.writer)


def _game(records):
    return next((r["game"] for r in records if r.get("game")), "unknown")


def _db(ledger, records):
    return os.path.join(bulk_dir(_game(records)), "index", "ledger.sqlite")


def cmd_init(a):
    root = os.path.abspath(a.root or os.environ.get("CRUCIBLE_DESIGN_ROOT") or os.getcwd())
    if a.repo and not os.path.exists(root):
        br = subprocess.run(["git", "-C", a.repo, "rev-parse", "--verify", "-q", "design"],
                            capture_output=True).returncode == 0
        cmd = ["git", "-C", a.repo, "worktree", "add"] + ([root, "design"] if br else ["-b", "design", root])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            return out({"error": "worktree add failed", "detail": r.stderr.strip()}, 5)
    led = Ledger(root, a.writer)
    if led.files():
        return out({"error": "ledger already initialised", "root": root}, 1)
    base = {"game": a.game, "by": "crucible", "via": "init"}
    if a.adopt:
        row = _read_json(a.adopt)
        hid = str(row.get("decision_id", ""))
        qid = hid if hid.startswith("q-") else "q-" + ulid()
        rec = led.append(dict(base, type="question.answered", by=row.get("by", "owner"), via="hearth",
                              body={"q_id": qid, "kind": "text", "text": row.get("answer", ""),
                                    "hearth_decision_id": hid, "typed_chars": len(str(row.get("answer", ""))),
                                    "probe": False}))
    else:
        rec = led.append(dict(base, type="node.created", body={"key": "root"}))
    return out({"ok": True, "root": root, "ledger": led.dir, "first_record": rec["id"]})


def cmd_get(a):
    led = _ledger(a)
    records, _ = led.read()
    con = index.ensure(led, _db(led, records))
    try:
        if a.id and len(a.id) == 26:
            row = con.execute("SELECT json FROM records WHERE id=?", (a.id,)).fetchone()
            return out({"record": json.loads(row[0])}, 0) if row else out({"error": "not found", "id": a.id}, 1)
        if a.id:
            row = con.execute("SELECT json FROM entities WHERE id=?", (a.id,)).fetchone()
            if not row:
                return out({"error": "not found", "id": a.id}, 1)
            ent = json.loads(row[0])
            ent["record_bodies"] = [json.loads(r[0]) for r in con.execute(
                "SELECT json FROM records WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ent["records"])),
                ent["records"])]
            return out({"entity": ent})
        q, args = "SELECT json FROM entities WHERE 1=1", []
        if a.kind:
            q += " AND kind=?"
            args.append(a.kind)
        if a.state:
            q += " AND state=?"
            args.append(a.state)
        items = [json.loads(r[0]) for r in con.execute(q + " ORDER BY id", args)]
        return out({"count": len(items), "entities": items})
    finally:
        con.close()


def cmd_why(a):
    led = _ledger(a)
    records, _ = led.read()
    state = fold(records)
    if a.id not in state:
        return out({"error": "not found", "id": a.id}, 1)
    ids, todo = {a.id}, [a.id]
    while todo:  # follow question origin and the supersedes chain both ways
        cur = todo.pop()
        e = state.get(cur)
        if not e:
            continue
        related = [e["body"].get("q_id"), e["body"].get("supersedes")]
        related += [o["id"] for o in state.values() if o["body"].get("supersedes") == cur]
        for r in related:
            if r and r in state and r not in ids:
                ids.add(r)
                todo.append(r)
    because = []
    for r in records:
        blob = json.dumps(r["body"])
        if any(i in blob for i in ids):
            because.append({"record": r["id"], "type": r["type"], "ts": r["ts"], "by": r["by"], "via": r["via"]})
    return out({"id": a.id, "state": state[a.id]["state"], "chain": sorted(ids), "because": because})


def cmd_build(a):
    led = _ledger(a)
    records, problems = led.read()
    if problems:
        return out({"error": "unparseable ledger lines", "problems": problems}, 5)
    state = fold(records)
    views = build.expected_views(records, state)
    drift = build.diff(led.root, views)
    if a.check:
        return out({"clean": not drift, "drift": drift}, 1 if drift else 0)
    build.write(led.root, views, drift)
    res = {"written": [d["path"] for d in drift], "views": len(views)}
    if a.land:
        res["commit"] = build.commit(led.root, [d["path"] for d in drift])
        res["on_main"] = False
    return out(res)


def cmd_doctor(a):
    if a.self_test:
        res = doctor.self_test()
        return out(res, 0 if res["ok"] else 1)
    led = _ledger(a)
    res = doctor.check(led)
    res["ok"] = not res["findings"] and not res["stale_heartbeat"]
    if res["stale_heartbeat"]:
        return out(res, 5)
    return out(res, 1 if res["findings"] else 0)


def cmd_conflicts(a):
    led = _ledger(a)
    records, _ = led.read()
    found = fold_conflicts(fold(records))
    return out({"conflicts": found}, 1 if found else 0)


def cmd_reindex(a):
    led = _ledger(a)
    records, _ = led.read()
    n, e = index.rebuild(led, _db(led, records), records)
    return out({"records": n, "entities": e})


def cmd_validate(a):
    try:
        rec = _read_json(a.file)
    except (OSError, ValueError) as ex:
        return out({"valid": False, "errors": [str(ex)]}, 2)
    errs = schema.validate_record(rec) if isinstance(rec, dict) else ["not an object"]
    return out({"valid": not errs, "errors": errs}, 1 if errs else 0)


def cmd_append(a):
    rec = _ledger(a).append(_read_json(a.file))
    return out({"ok": True, "id": rec["id"]})


def main(argv):
    try:
        a = _parser().parse_args(argv)
        return globals()["cmd_" + a.verb](a)
    except LedgerError as e:
        return out({"error": str(e)}, e.code)
    except SystemExit as e:  # --help
        return e.code or 0
