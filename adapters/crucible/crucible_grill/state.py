"""Question state, derived from the ledger records in id order.

States: raised, asked, answered, taken, routed, delegated, deferred, investigating,
withdrawn, defaulted. A comment never changes state."""
import datetime

from crucible_ledger.fold import fold

SETTLED = {"answered", "taken", "delegated", "defaulted"}
OPEN = {"raised"}


def ms_of(ts):
    return int(datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
               .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _new(qid):
    return {"id": qid, "state": "unknown", "raised": None, "card": None, "asked": None, "hearth_id": None,
            "posted_ts": None, "post_n": 0, "card_hash": "", "answered": None, "comments": [], "decisions": [],
            "src_of": None, "ts": None}


def derive(records):
    Q = {}

    def q_(qid):
        return Q.setdefault(qid, _new(qid))

    def cascade(q, state, requeue=False):
        for p in (q["asked"] or {}).get("parts", []):
            s = p.get("src")
            if s and s in Q and not p.get("probe"):
                Q[s]["state"] = "raised" if requeue else state
                if requeue:
                    Q[s]["src_of"] = None

    for r in records:
        kind, _, verb = r["type"].partition(".")
        b = r.get("body", {})
        qid = b.get("q_id")
        if kind == "decision" and verb == "settled" and qid in Q:
            Q[qid]["decisions"].append(b["d_id"])
            continue
        if kind == "investigation" and qid:
            q_(qid)["state"] = "investigating"
            continue
        if kind != "question" or not qid:
            continue
        q = q_(qid)
        if verb == "raised":
            q.update(raised=b, state="raised", card=b.get("card"), ts=r["ts"])
        elif verb == "drafted":
            q["card"] = b["card"]
        elif verb == "asked":
            q["card"] = q["card"] or b.get("card")
            q.update(asked=b, state="asked", hearth_id=b.get("hearth_id", qid), card_hash=b.get("card_hash", ""),
                     post_n=b.get("post_n", 1))
            q["posted_ts"] = q["posted_ts"] or r["ts"]
            for p in b.get("parts", []):
                if p.get("src") in Q and not p.get("probe"):
                    Q[p["src"]].update(state="asked", src_of=[qid, p["id"]])
        elif verb == "commented":
            q["comments"].append({"text": b.get("text", ""), "ts": r["ts"], "by": r["by"]})
        elif verb in ("answered", "taken", "delegated", "defaulted", "routed", "deferred"):
            q["state"] = verb
            if verb == "answered":
                q["answered"] = b
            cascade(q, verb)
        elif verb == "withdrawn":
            q["state"] = "withdrawn"
            cascade(q, "withdrawn", requeue=True)
    return Q


def label_map(records):
    """{label id: current text} from label.* records."""
    out = {}
    for e in fold(records).values():
        if e["kind"] == "label" and e["body"].get("text"):
            out[e["id"]] = e["body"]["text"]
    return out


def node_settled(Q, node):
    return any(q["raised"] and q["raised"].get("node") == node and q["state"] in SETTLED for q in Q.values())


def node_live(Q, node):
    """A node that has a question that is not withdrawn."""
    return any(q["raised"] and q["raised"].get("node") == node and q["state"] != "withdrawn" for q in Q.values())


def frontier(tree, Q):
    return [n["id"] for n in tree["node"]
            if not node_live(Q, n["id"]) and all(node_settled(Q, p) for p in n.get("prereqs", []))]


def radius_of(tree, node_id):
    """Count of nodes that need this node settled first, directly or through others."""
    by = {n["id"]: n for n in tree["node"]}
    down, grew = set(), True
    while grew:
        grew = False
        for n in tree["node"]:
            if n["id"] not in down and any(p == node_id or p in down for p in n.get("prereqs", [])):
                down.add(n["id"])
                grew = True
    return len(down & by.keys())


def override_stats(Q, kind):
    """(probes, overrides) over answered batches whose probe part came from a node of this kind."""
    n = over = 0
    for q in Q.values():
        a = q["answered"]
        if not a or not a.get("probe_parts"):
            continue
        for pid in a["probe_parts"]:
            part = next((p for p in (q["asked"] or {}).get("parts", []) if p["id"] == pid), None)
            src = Q.get(part.get("src")) if part else None
            node = ((src or {}).get("raised") or {}).get("node") or ""
            if node.split(".")[0] == kind:
                n += 1
                over += pid in a.get("probe_overrides", [])
    return n, over
