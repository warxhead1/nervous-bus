"""The hearth bridge: every call goes through the real hearth-ask CLI."""
import json
import os
import shlex
import subprocess

from crucible_ledger.store import LedgerError


def exe():
    return shlex.split(os.environ.get("CRUCIBLE_HEARTH_ASK") or "hearth-ask")


def run(args, stdin=None, timeout=30):
    """Run one hearth-ask verb and return its JSON. Any failure exits 5."""
    try:
        p = subprocess.run(exe() + args, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LedgerError(f"hearth-ask unreachable: {e}", 5)
    if p.returncode:
        raise LedgerError(f"hearth-ask {args[0]} exited {p.returncode}: {p.stderr.strip()[:300]}", 5)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise LedgerError(f"hearth-ask {args[0]} printed no JSON", 5)


def context_text(card):
    lines = [f"Door: {card['door']}. Weight: {card['weight']}."]
    if card.get("pips") is not None:
        lines.append("Evidence: " + "●" * card["pips"] + "○" * max(0, 3 - card["pips"]))
    if card.get("context"):
        lines += ["", card["context"]]
    return "\n".join(lines) + "\n"


def post_args(game, hid, card, default_after=None):
    """Build the post argv. One part and no batch request is a single decision, which
    renders option descriptions. Anything else is a batch of --part."""
    parts = card["parts"]
    a = ["post", "--project", game, "--title", card["text"], "--id", hid, "--context-file", "-"]
    if len(parts) == 1 and card.get("mode") != "batch":
        for o in parts[0]["options"]:
            a += ["--option", f"{o['id']}:{o['label']}:{o.get('changes', '')}"]
        a += ["--recommend", next(o["id"] for o in parts[0]["options"] if o.get("rec"))]
        for m in parts[0].get("media", []):
            a += ["--link", m]
    else:
        for p in parts:
            opts = ",".join(("%s*=%s" if o.get("rec") else "%s=%s") % (o["id"], o["label"]) for o in p["options"])
            a += ["--part", f"{p['id']}:{p['text']}:{opts}"]
            for m in p.get("media", []):
                a += ["--part-media", f"{p['id']}={m}"]
    for link in card.get("links", []):
        a += ["--link", link]
    if default_after and card["door"] != "one-way" and not (card.get("money") or card.get("class") == "money"):
        a += ["--default-after", default_after]
    return a


def post(game, hid, card, default_after=None, timeout=30):
    res = run(post_args(game, hid, card, default_after), stdin=context_text(card), timeout=timeout)
    if res.get("decision_id") != hid:
        raise LedgerError(f"hearth returned decision_id {res.get('decision_id')!r}, wanted {hid!r}", 5)
    try:
        row = run(["get", hid], timeout=timeout)
    except LedgerError:
        row = {}
    return {"hearth_id": hid, "viewer_url": row.get("viewer_url", "")}
