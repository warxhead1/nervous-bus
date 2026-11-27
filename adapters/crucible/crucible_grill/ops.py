"""Ledger writes shared by the grill verbs and the steward. Each helper appends validated records."""
import datetime
import json

from crucible_ledger.fold import fold
from crucible_ledger.store import Ledger, LedgerError
from crucible_ledger.ulid import ulid

from . import cfg, state, words


class Ctx:
    def __init__(self, root, writer="crucible"):
        self.root = root
        self.led = Ledger(root, writer)
        self.reload()

    def reload(self):
        self.records, self.problems = self.led.read()
        self.Q = state.derive(self.records)
        self.labels = state.label_map(self.records)
        game = next((r.get("game") for r in self.records if r.get("game")), None)
        if not game:
            raise LedgerError("ledger has no records; run `crucible ledger init` first", 5)
        self.game = game
        self.knobs = cfg.knobs(self.root)

    def add(self, rtype, body, by="crucible", via="grill"):
        rec = self.led.append({"type": rtype, "game": self.game, "by": by, "via": via, "body": body})
        self.records.append(rec)
        self.Q = state.derive(self.records)
        return rec

    def q(self, qid):
        if qid not in self.Q:
            raise LedgerError(f"unknown question {qid}", 1)
        return self.Q[qid]


def rec_choice(q):
    card = q["card"] or {}
    return {p["id"]: next((o["id"] for o in p["options"] if o.get("rec")), p["options"][0]["id"])
            for p in card.get("parts", [])}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_decision(ctx, q, chosen, authority, **extra):
    r = q["raised"] or {}
    d_id = "d-" + ulid()
    body = {"d_id": d_id, "q_id": q["id"], "chosen": chosen, "authority": authority,
            "door": r.get("door", "two-way"), "touches": r.get("touches", []), "constrains": [], "supersedes": None}
    body.update(extra)
    ctx.add("decision.settled", body, by="crucible" if authority == "lead" else "owner")
    return d_id


def live_decision(ctx, qid):
    st = fold(ctx.records)
    for d in reversed(ctx.q(qid)["decisions"]):
        if st[d]["state"] != "superseded":
            return d
    return None


def flip_decision(ctx, qid, option, via="grill"):
    """Supersede a taken decision with the owner's pick."""
    q = ctx.q(qid)
    old = live_decision(ctx, qid)
    if not old:
        raise LedgerError(f"{qid} has no live decision to flip", 1)
    pid = next(iter(rec_choice(q)), "p1")
    d_new = write_decision(ctx, q, {pid: option}, "owner", supersedes=old, flipped=True)
    ctx.add("question.flipped", {"q_id": qid, "d_id": old, "to": d_new, "option": option}, by="owner", via=via)
    return d_new


def answer_body(ctx, qid, picks=None, text=None, hearth_id=None, responded_at=None, created_at=None):
    """Body of question.answered, with the recommendation check and probe split."""
    q = ctx.q(qid)
    parts = (q["asked"] or {}).get("parts", [])
    recs = {p["id"]: next((o["id"] for o in p["options"] if o.get("rec")), None) for p in parts}
    probe_parts = [p["id"] for p in parts if p.get("probe")]
    body = {"q_id": qid, "kind": "pick" if picks else "text", "typed_chars": len(text or ""),
            "probe": bool(probe_parts)}
    if picks:
        body["picks"] = picks
        body["was_recommended"] = all(v == recs.get(k) for k, v in picks.items() if k not in probe_parts)
        if probe_parts:
            body["probe_parts"] = probe_parts
            body["probe_overrides"] = [k for k in probe_parts if picks.get(k) != recs.get(k)]
    if text:
        body["text"] = text
    start = q["posted_ts"]
    if start and responded_at:
        body["latency_ms"] = max(0, _ms(responded_at) - state.ms_of(start))
    if hearth_id:
        body["hearth_id"] = hearth_id
    if responded_at:
        body["responded_at"] = responded_at
    return body


def _ms(iso):
    s = iso.replace("Z", "+00:00")
    return int(datetime.datetime.fromisoformat(s).timestamp() * 1000)


def record_answer(ctx, qid, picks=None, text=None, via="hearth", by="owner", hearth_id=None,
                  responded_at=None, kind=None, extra=None):
    body = answer_body(ctx, qid, picks, text, hearth_id, responded_at)
    body.update(extra or {})
    if kind:
        body["kind"] = kind
    rec = ctx.add("question.answered", body, by=by, via=via)
    q = ctx.q(qid)
    for pid in body.get("probe_overrides", []):
        part = next(p for p in (q["asked"] or {})["parts"] if p["id"] == pid)
        flip_decision(ctx, part["src"], picks[pid], via=via)
    return rec


def targets(ctx, q):
    """The raised questions an answer settles: the parts' sources of a batch, else the question."""
    srcs = [p["src"] for p in (q["asked"] or {}).get("parts", []) if p.get("src") and not p.get("probe")]
    return [ctx.Q[s] for s in srcs if s in ctx.Q] or [q]


def apply_word(ctx, qid, word, arg="", via="grill", extra=None):
    """Write the record a reserved word maps to. No model runs. Returns a summary dict."""
    q, wid, extra = ctx.q(qid), word["id"], extra or {}
    if wid == "word.you_decide":
        ds = [write_decision(ctx, t, rec_choice(t), "lead", delegated=True) for t in targets(ctx, q)]
        ctx.add("question.delegated", {"q_id": qid, "authority": "lead", "decisions": ds, **extra}, by="owner", via=via)
        return {"records": "delegate", "decisions": ds}
    if wid == "word.later":
        days = ctx.knobs["deferred"]["wake_days"]
        wake = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ctx.add("question.deferred", {"q_id": qid, "wake": {"date": wake}, "note": arg, **extra}, by="owner", via=via)
        return {"records": "deferred", "wake": wake}
    if wid == "word.test_it":
        inv = "inv-" + ulid()
        ctx.add("investigation.started", {"inv_id": inv, "q_id": qid, "origin": "owner", **extra}, by="owner", via=via)
        return {"records": "investigation", "inv_id": inv}
    if wid == "word.wrong_question":
        ctx.add("question.withdrawn", {"q_id": qid, "reason": "wrong question", "note": arg, "redraft": True, **extra},
                by="owner", via=via)
        return {"records": "withdrawn"}
    if wid == "word.harder":
        node = (q["raised"] or {}).get("node") or qid
        ctx.add("node.pressure", {"key": node, "dial": "up"}, by="owner", via=via)
        return {"records": "pressure", "node": node}
    if wid == "word.enough":
        opened = [r for r in ctx.records if r["type"] == "sitting.opened"]
        sit = opened[-1]["body"]["sit_id"] if opened else "sit-none"
        ctx.add("sitting.closed", {"sit_id": sit, "reason": "enough"}, by="owner", via=via)
        return {"records": "sitting closed", "sit_id": sit}
    return {"records": "nothing"}


SETTLE_WORDS = {"word.you_decide", "word.later", "word.test_it", "word.wrong_question"}


def dumps(o):
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False)
