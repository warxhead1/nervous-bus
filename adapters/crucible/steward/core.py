"""One steward cycle: hearth rows in, ledger records out. No model and no message bus.

The poll of `hearth-ask list` is the load-bearing path. Writes are idempotent: a row's
state moves a question out of `asked` once, and a comment is keyed by its time and text."""
import datetime
import hashlib
import json
import os
import subprocess
import time

from crucible_grill import bridge, cfg, ops, state, words
from crucible_ledger.store import LedgerError, bulk_dir

AGENT_ACTORS = {"agent", "crucible", "claude", "steward"}


def sig_status(hid, status, at):
    return f"{hid}:{status}:{at or ''}"


def sig_comment(hid, c):
    h = hashlib.sha256((c.get("text") or "").encode()).hexdigest()[:10]
    return f"{hid}:c:{c.get('at', '')}:{h}"


def _picks(row, q):
    if row.get("answers"):
        return dict(row["answers"])
    if row.get("option_id"):
        parts = (q["asked"] or {}).get("parts", [])
        return {parts[0]["id"] if parts else "p1": row["option_id"]}
    return None


def handle_row(ctx, q, row, timeout, seen):
    """Write the records one row implies. Returns the list of record types written."""
    hid, wrote = q["hearth_id"], []
    for c in row.get("comments") or []:
        sig = sig_comment(hid, c)
        if c.get("by") in AGENT_ACTORS or sig in seen:
            continue
        seen.add(sig)
        ctx.add("question.commented", {"q_id": q["id"], "text": c.get("text", ""), "hearth_id": hid, "row_sig": sig},
                by="owner", via=c.get("via") or "hearth")
        wrote.append("question.commented")
    q = ctx.Q[q["id"]]
    status = row.get("status")
    if q["state"] != "asked" or status == "pending":
        return wrote
    at = row.get("responded_at")
    sig = sig_status(hid, status, at)
    extra = {"hearth_id": hid, "responded_at": at, "row_sig": sig}
    extra = {k: v for k, v in extra.items() if v}
    if status == "answered":
        picks, text = _picks(row, q), (row.get("text") or "").strip() or None
        via = row.get("via") or "hearth"
        hit = words.classify(text) if text and not picks else None
        if hit and hit[0]["id"] in ops.SETTLE_WORDS:
            ops.apply_word(ctx, q["id"], hit[0], hit[1], via, extra)
            return wrote + [ctx.records[-1]["type"]]
        if hit and hit[0]["id"] == "word.yes":
            picks = ops.rec_choice({"card": {"parts": q["asked"]["parts"]}})
        ops.record_answer(ctx, q["id"], picks, text, via=via, hearth_id=hid, responded_at=at, extra={"row_sig": sig})
        wrote.append("question.answered")
    elif status == "defaulted":
        picks = _picks(row, q)
        ctx.add("question.defaulted", dict(q_id=q["id"], picks=picks or {}, **extra), by="steward", via="hearth")
        for t in ops.targets(ctx, q):
            if t["card"]:
                ops.write_decision(ctx, t, ops.rec_choice(t), "lead", defaulted=True)
        wrote.append("question.defaulted")
    elif status == "withdrawn":
        ctx.add("question.withdrawn", dict(q_id=q["id"], reason=row.get("reason") or "withdrawn in hearth",
                                           redraft=False, **extra), by="steward", via="hearth")
        wrote.append("question.withdrawn")
    return wrote


def land(root, n):
    """One commit and one push of the ledger per cycle."""
    def git(*a):
        return subprocess.run(["git", "-C", root, "-c", "core.hooksPath=/dev/null", "-c", "user.name=crucible-steward",
                               "-c", "user.email=crucible-steward@localhost", *a], capture_output=True, text=True)
    if git("rev-parse", "--is-inside-work-tree").returncode:
        return {"git": "not a repository"}
    git("add", "--", os.path.join("design", "ledger"))
    if git("diff", "--cached", "--quiet", "--", os.path.join("design", "ledger")).returncode == 0:
        return {"committed": False}
    r = git("commit", "-q", "-m", f"crucible: steward sync, {n} records", "--", os.path.join("design", "ledger"))
    if r.returncode:
        return {"committed": False, "error": r.stderr.strip()[:200]}
    if not git("remote").stdout.strip():
        return {"committed": True, "pushed": False, "push": "no remote"}
    p = git("push", "-q", "origin", "HEAD")
    return {"committed": True, "pushed": p.returncode == 0, "push_error": p.stderr.strip()[:200] if p.returncode else ""}


def cycle(root):
    """One poll. Raises LedgerError(code 5) when hearth is unreachable, before touching the heartbeat."""
    started = time.time()
    ctx = ops.Ctx(root)
    timeout = ctx.knobs["hearth"]["timeout_s"]
    rows = bridge.run(["list", "--project", ctx.game], timeout=timeout).get("decisions", [])
    by_id = {r["decision_id"]: r for r in rows}
    seen = {b.get("row_sig") for r in ctx.records for b in [r.get("body", {})] if b.get("row_sig")}
    wrote = []
    for qid in sorted(ctx.Q):
        q = ctx.Q[qid]
        row = by_id.get(q["hearth_id"] or "")
        if not row:
            continue
        if q["state"] == "asked" and row.get("status") != "pending":
            row = bridge.run(["get", q["hearth_id"]], timeout=timeout)
        wrote += handle_row(ctx, q, row, timeout, seen)
    bulk = bulk_dir(ctx.game)
    os.makedirs(bulk, exist_ok=True)
    front = state.frontier(cfg.tree(), ctx.Q)
    with open(os.path.join(bulk, "frontier.json"), "w") as f:
        json.dump({"at": ops.now_iso(), "nodes": front}, f)
    landed = land(ctx.root, len(wrote)) if wrote else {"committed": False}
    hb = os.path.join(bulk, "steward.heartbeat")
    with open(hb, "w") as f:
        f.write(ops.now_iso() + "\n")
    res = {"ok": True, "game": ctx.game, "written": len(wrote), "types": wrote, "frontier": len(front),
           "land": landed, "at": ops.now_iso(), "elapsed_s": round(time.time() - started, 3)}
    with open(os.path.join(bulk, "steward.status.json"), "w") as f:
        json.dump(res, f)
    return res


def status(root):
    ctx = ops.Ctx(root)
    bulk = bulk_dir(ctx.game)
    hb = os.path.join(bulk, "steward.heartbeat")
    limit = float(os.environ.get("CRUCIBLE_HEARTBEAT_MAX_S", 900))
    if not os.path.isfile(hb):
        return {"heartbeat": "absent", "ok": False}
    age = time.time() - os.stat(hb).st_mtime
    res = {"heartbeat": "stale" if age > limit else "fresh", "age_s": round(age), "ok": age <= limit}
    try:
        with open(os.path.join(bulk, "steward.status.json")) as f:
            res["last"] = json.load(f)
    except (OSError, ValueError):
        pass
    return res
