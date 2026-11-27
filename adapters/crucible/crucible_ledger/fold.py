"""Fold records to entity state. State is the verb of the latest record for an
entity; decisions named by a later `supersedes` become superseded."""

ID_KEYS = {
    "question": "q_id", "decision": "d_id", "assumption": "a_id", "label": "label_id",
    "investigation": "inv_id", "experiment": "exp_id", "finding": "f_id", "bet": "bet_id",
    "audit": "aud_id", "claim": "claim_id", "token": "token_id", "sitting": "sit_id",
    "lane": "l_id", "probe": "probe_id", "node": "key",
}


def entity_of(rec):
    kind, _, verb = rec["type"].partition(".")
    key = ID_KEYS.get(kind)
    eid = rec.get("body", {}).get(key) if key else None
    return kind, verb, eid


def fold(records):
    ents = {}
    for r in records:
        kind, verb, eid = entity_of(r)
        if not eid:
            continue
        e = ents.setdefault(eid, {"id": eid, "kind": kind, "state": verb, "first_ts": r["ts"],
                                  "records": [], "body": {}})
        e["state"] = verb
        e["last_ts"] = r["ts"]
        e["records"].append(r["id"])
        body = r.get("body", {})
        e["body"].update(body)
        if kind == "label" and verb == "edited":
            edit = body.get("edit")
            if edit == "rename" and "to" in body:
                e["body"]["text"] = body["to"]
            elif edit == "retag" and "to" in body:
                e["body"]["kind"] = body["to"]
            elif edit == "retire":
                e["state"] = "retired"
            elif edit == "merge":
                e["state"] = "merged"
    for e in list(ents.values()):
        sup = e["body"].get("supersedes") if e["kind"] == "decision" else None
        if sup and sup in ents:
            ents[sup]["state"] = "superseded"
    return ents


def conflicts(state):
    """Pairs of live decisions where one touches a key the other constrains."""
    live = [e for e in state.values() if e["kind"] == "decision" and e["state"] != "superseded"]
    out = []
    for a in live:
        for b in live:
            if a is b:
                continue
            for k in a["body"].get("touches", []):
                if k in b["body"].get("constrains", []):
                    out.append({"touches": a["id"], "constrained_by": b["id"], "key": k})
    return out
