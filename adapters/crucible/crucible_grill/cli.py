"""crucible grill: ask | tree | draft | cards | sit | post | answer | take | flip | due.

Every verb prints one JSON object. Exit: 0 done, 1 negative result (lint, refusal), 2 usage,
3 waiting on the owner, 5 environment (hearth unreachable, no ledger)."""
import argparse
import datetime
import hashlib
import json
import sys

from crucible_ledger.fold import fold
from crucible_ledger.store import LedgerError, find_root
from crucible_ledger.ulid import ulid

from . import bridge, cfg, gate, lint, ops, state, words

CARD_ENDS = ("card_batch", "card_alone")


def out(obj, code=0):
    print(json.dumps(obj, sort_keys=True, ensure_ascii=False))
    return code


def fail(msg, code=1, **kw):
    raise LedgerError(json.dumps(dict(error=msg, **kw), sort_keys=True), code)


def _json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ctx_for(a):
    return ops.Ctx(find_root(a.root))


def resolved(ctx, card):
    return lint.resolve_card(card, ctx.labels)


# ---------------------------------------------------------------- tree

def tree_errors(t):
    errs, by = [], {}
    ok_class = {"fact", "measurable", "lead", "owner", "money"}
    for n in t.get("node", []):
        if n["id"] in by:
            errs.append(f"duplicate node {n['id']}")
        by[n["id"]] = n
    for n in by.values():
        if n.get("class") not in ok_class:
            errs.append(f"{n['id']}: bad class")
        if n.get("door") not in lint.DOORS:
            errs.append(f"{n['id']}: bad door")
        if n.get("weight") not in lint.WEIGHTS:
            errs.append(f"{n['id']}: bad weight")
        if len(n.get("ask", "")) > cfg.cards()["limits"]["question_chars"]:
            errs.append(f"{n['id']}: ask is too long")
        for k in ("prereqs", "dynamic", "aesthetic", "pillar"):
            for p in ([n[k]] if isinstance(n.get(k), str) else n.get(k, [])):
                if p not in by:
                    errs.append(f"{n['id']}: {k} names missing node {p}")
        if n["id"].startswith("mechanic.") and not all(n.get(k) for k in ("dynamic", "aesthetic", "pillar")):
            errs.append(f"{n['id']}: mechanic without dynamic, aesthetic and pillar")
    seen, stack = set(), set()

    def walk(i):
        if i in stack:
            errs.append(f"cycle through {i}")
            return
        if i in seen or i not in by:
            return
        seen.add(i)
        stack.add(i)
        for p in by[i].get("prereqs", []):
            walk(p)
        stack.discard(i)
    for i in by:
        walk(i)
    return errs


def cmd_tree(a):
    t = cfg.tree(a.file)
    errs = tree_errors(t)
    if a.check:
        return out({"ok": not errs, "nodes": len(t["node"]), "errors": errs}, 1 if errs else 0)
    try:
        fr = state.frontier(t, ctx_for(a).Q)
    except LedgerError:
        fr = []
    return out({"nodes": len(t["node"]), "errors": errs, "frontier": fr})


# ---------------------------------------------------------------- raise and route

def raise_question(ctx, text, node=None, cls=None, clause=None, door=None, weight=None, guard=None,
                   card=None, source="agent", session=None):
    t = cfg.tree()
    by = {n["id"]: n for n in t["node"]}
    if node and node not in by:
        fail("unknown tree node", 2, node=node)
    n = by.get(node)
    if n:
        cls, door, weight = n["class"], n["door"], n["weight"]
        text = text or n["ask"]
        source = "tree"
    else:
        if cls not in {"fact", "measurable", "lead", "owner", "money"}:
            fail("an off-tree question needs --class", 2)
        if clause not in cfg.clause_ids():
            fail("a class needs --clause naming a rubric clause id", 2, clause=clause)
        door, weight = door or "one-way", weight or "outcome-critical"  # door.doubt and weight.doubt
    g = gate.guard_of(text, guard, n)
    radius = state.radius_of(t, node) if n else 0
    fitted = False
    kind = (node or "").split(".")[0]
    bar = ctx.knobs["gate"].get("take_bar")
    if bar is not None and kind:
        probes, over = state.override_stats(ctx.Q, kind)
        fitted = probes >= ctx.knobs["gate"]["min_probes"] and (over / probes) * radius < bar
    cls2, gid, end, default_ok = gate.end_for(cls, door, weight, g, fitted)
    qid = "q-" + ulid()
    body = {"q_id": qid, "ask": text, "source": source, "class": cls2, "orig_class": cls, "door": door,
            "weight": weight, "radius": radius, "gate": gid, "end": end, "default_ok": default_ok}
    for k, v in (("node", node), ("guard", g), ("clause", clause), ("session", session)):
        if v:
            body[k] = v
    if card:
        body["card"] = card
    ctx.add("question.raised", body)
    return qid


def settled_before(ctx, node, text):
    for q in ctx.Q.values():
        r = q["raised"]
        if r and q["state"] in state.SETTLED and ((node and r.get("node") == node) or
                                                  (not node and r.get("ask", "").lower() == text.lower())):
            return q
    return None


def card_check(card):
    errs = lint.lint(card)
    if errs:
        fail("card lint failed", 1, errors=errs)


def route(ctx, qid, post=False):
    q = ctx.q(qid)
    end = q["raised"]["end"]
    if end in ("lookup", "lab"):
        ctx.add("question.routed", {"q_id": qid, "route": end})
        return {"state": "routed", "route": end}
    if not q["card"]:
        return {"state": "routed", "route": "draft", "q": qid}
    card_check(q["card"])
    if end in gate.TAKE_ENDS:
        d = take(ctx, q, why=q["card"].get("why", "taken on the recommendation"))
        return {"state": "settled", "decision": d, "taken": True}
    res = {"state": "asked", "posted": False, "queued": True, "end": end}
    if post:
        res.update(post_question(ctx, qid))
    return res


def take(ctx, q, option=None, why=""):
    r = q["raised"] or {}
    if r.get("guard"):
        fail("a guard-list question is never taken", 1, guard=r["guard"], q=q["id"])
    if r.get("end") not in gate.TAKE_ENDS:
        fail("the gate does not let an agent take this question", 1, end=r.get("end"), q=q["id"])
    card = q["card"]
    if not card:
        fail("nothing to take: the question has no draft", 1, q=q["id"])
    chosen = ops.rec_choice(q)
    if option:
        chosen = {next(iter(chosen)): option}
    pips = card.get("pips") or 0
    what = lint.resolve(card["text"], ctx.labels)
    plain = all(p.get("plain") for p in card["parts"]) and len(card["parts"]) == 1
    d = ops.write_decision(ctx, q, chosen, "lead", taken=True, what=what, why=why or "taken on the recommendation",
                           doubt=max(0, 3 - pips), probe_ok=plain and r.get("end") == "owner_take")
    ctx.add("question.taken", {"q_id": q["id"], "d_id": d, "chosen": chosen})
    return d


def cmd_ask(a):
    ctx = ctx_for(a)
    node = {n["id"]: n for n in cfg.tree()["node"]}.get(a.node)
    if a.node and not node:
        fail("unknown tree node", 2, node=a.node)
    text = a.text or (node["ask"] if node else "")
    prior = settled_before(ctx, a.node, text) if (a.node or text) else None
    if prior:
        ref = prior["decisions"][-1] if prior["decisions"] else prior["id"]
        return out({"state": "settled", "ref": ref, "already": True, "q": prior["id"]})
    card = _json(a.draft) if a.draft else None
    if card:
        card.setdefault("text", text)
        text = text or card["text"]
        card.setdefault("door", node["door"] if node else a.door or "one-way")
        card.setdefault("weight", node["weight"] if node else a.weight or "outcome-critical")
        card_check(card)
    if not (a.node or text):
        fail("give --text or --node", 2)
    qid = raise_question(ctx, text, a.node, a.cls, a.clause, a.door, a.weight, a.guard, card,
                         session=a.session)
    return out(dict(route(ctx, qid, a.post), q=qid))


def cmd_draft(a):
    ctx = ctx_for(a)
    q = ctx.q(a.q)
    if q["state"] != "raised":
        fail("only a raised question takes a draft", 1, state=q["state"])
    card = _json(a.file)
    card.setdefault("text", q["raised"]["ask"])
    card.setdefault("door", q["raised"]["door"])
    card.setdefault("weight", q["raised"]["weight"])
    card_check(card)
    ctx.add("question.drafted", {"q_id": a.q, "card": card})
    return out(dict(route(ctx, a.q, a.post), q=a.q))


# ---------------------------------------------------------------- posting

def part_view(p):
    return {"id": p["id"], "text": p.get("text", ""), "options": [
        {k: v for k, v in o.items() if k in ("id", "label", "rec", "changes", "evidence")} for o in p["options"]]}


def post_card(ctx, qid, card, extra=None, repost_of=None):
    """Post one card to hearth and write question.asked. `card` is unresolved."""
    rc = resolved(ctx, card)
    errs = lint.lint(rc, strict_holes=True)
    if errs:
        fail("card lint failed", 1, errors=errs)
    n = (repost_of["post_n"] + 1) if repost_of else 1
    hid = qid if n == 1 else f"{qid}.{n}"
    if repost_of:
        bridge.run(["withdraw", repost_of["hearth_id"], "--reason", "renamed"], timeout=ctx.knobs["hearth"]["timeout_s"])
    default_after = ctx.knobs["gate"].get("default_after") or rc.get("default_after")
    res = bridge.post(ctx.game, hid, rc, default_after, ctx.knobs["hearth"]["timeout_s"])
    multi = len(card["parts"]) > 1 or card.get("mode") == "batch"
    body = {"q_id": qid, "mode": "batch" if multi else "single", "class": "owner" if card.get("class") != "lead" else "lead",
            "door": card["door"], "weight": card["weight"], "pips": card.get("pips", 0),
            "parts": [dict(part_view(p), **{k: p[k] for k in ("src", "probe", "plain") if k in p}) for p in card["parts"]],
            "hearth_id": hid, "post_n": n, "card_hash": hashlib.sha256(ops.dumps(rc).encode()).hexdigest()[:16],
            "viewer_url": res["viewer_url"], "card_mode": card.get("mode", "fork"), "money": bool(card.get("money"))}
    body.update(extra or {})
    if "radius" in card:
        body["radius"] = card["radius"]
    ctx.add("question.asked", body, via="hearth")
    return {"posted": True, "hearth_id": hid, "mode": body["mode"]}


def pushes_outstanding(ctx):
    return [q["id"] for q in ctx.Q.values() if q["state"] == "asked" and not q["src_of"]]


def post_question(ctx, qid):
    q = ctx.q(qid)
    card = q["card"]
    if not card:
        fail("the question has no draft", 1, q=qid)
    if q["state"] == "asked":
        rc = resolved(ctx, card)
        h = hashlib.sha256(ops.dumps(rc).encode()).hexdigest()[:16]
        if q["card_hash"] == h:
            return {"posted": False, "unchanged": True, "hearth_id": q["hearth_id"]}
        errs = lint.lint(rc, strict_holes=True)
        if errs:
            fail("card lint failed", 1, errors=errs)
        return post_card(ctx, qid, card, repost_of=q)
    if q["state"] != "raised":
        fail("question is not waiting to be posted", 1, state=q["state"])
    other = [x for x in pushes_outstanding(ctx) if x != qid]
    if other:
        raise LedgerError(json.dumps({"posted": False, "reason": "one push is outstanding", "waiting_on": other}), 3)
    return post_card(ctx, qid, card)


def cmd_post(a):
    ctx = ctx_for(a)
    if a.card:
        card = _json(a.card)
        card_check(card)
        errs = lint.lint(resolved(ctx, card), strict_holes=True)
        if errs:
            fail("card lint failed", 1, errors=errs)
        cls = "money" if card.get("money") else card.get("class", "owner")
        qid = raise_question(ctx, card["text"], card.get("node"), cls, "class.doubt" if cls == "owner" else "class.money",
                             card["door"], card["weight"], card.get("guard"), card)
        ctx.add("question.drafted", {"q_id": qid, "card": card})
    else:
        qid = a.q
        if qid not in ctx.Q:
            fail("unknown question", 1, q=qid)
    return out(dict(post_question(ctx, qid), q=qid))


# ---------------------------------------------------------------- sitting

def cmd_sit(a):
    ctx = ctx_for(a)
    waiting = pushes_outstanding(ctx)
    for qid in waiting:  # a rename reposts the card in place
        if ctx.Q[qid]["card"]:
            post_question(ctx, qid)
    if waiting:
        return out({"posted": False, "reason": "one push is outstanding", "waiting_on": waiting}, 3)
    budget = a.budget or ctx.knobs["sitting"]["budget"]
    opened = []
    if not a.no_open:
        for node in state.frontier(cfg.tree(), ctx.Q):
            qid = raise_question(ctx, None, node)
            route(ctx, qid)
            opened.append(qid)
    cands = [q for q in ctx.Q.values() if q["state"] == "raised" and q["card"]
             and (q["raised"] or {}).get("end") in CARD_ENDS]
    undrafted = [q["id"] for q in ctx.Q.values() if q["state"] == "raised" and not q["card"]
                 and (q["raised"] or {}).get("end") in CARD_ENDS]
    cands.sort(key=lambda q: (gate.WEIGHT_RANK[q["raised"]["weight"]], -q["raised"].get("radius", 0), q["id"]))
    cands = cands[:budget]
    sit_id = "sit-" + ulid()
    ctx.add("sitting.opened", {"sit_id": sit_id, "budget": budget, "qs": [q["id"] for q in cands]})
    res = {"sit": sit_id, "opened": opened, "undrafted": undrafted, "queued": [q["id"] for q in cands], "pushed": None}
    if not cands:
        return out(res)

    def batchable(q):
        c = q["card"]
        return q["raised"]["end"] == "card_batch" and len(c["parts"]) == 1 and c["parts"][0].get("plain") \
            and c.get("mode") != "batch"
    top = cands[0]
    if not batchable(top):
        res["pushed"] = dict(post_card(ctx, top["id"], top["card"], {"sitting": sit_id}), q=top["id"])
        return out(res)
    group = [q for q in cands if batchable(q)]
    probe = _probe_candidate(ctx) if len(group) < cfg.cards()["limits"]["batch_parts_max"] else None
    if len(group) == 1 and not probe:
        res["pushed"] = dict(post_card(ctx, top["id"], top["card"], {"sitting": sit_id}), q=top["id"])
        return out(res)
    parts, evidence = [], {}
    for i, q in enumerate(group, 1):
        p = json.loads(json.dumps(q["card"]["parts"][0]))
        p.update(id=f"p{i}", src=q["id"], text=p.get("text") or q["card"]["text"])
        parts.append(p)
        evidence.update(q["card"].get("evidence", {}))
    if probe:
        p = json.loads(json.dumps(probe["card"]["parts"][0]))
        p.update(id=f"p{len(parts) + 1}", src=probe["id"], probe=True, text=p.get("text") or probe["card"]["text"])
        parts.append(p)
        evidence.update(probe["card"].get("evidence", {}))
    heaviest = min((q["raised"]["weight"] for q in group), key=lambda w: gate.WEIGHT_RANK[w])
    card = {"text": "Quick picks", "door": "two-way", "weight": heaviest, "mode": "batch", "parts": parts,
            "evidence": evidence}
    bq = "q-" + ulid()
    res["pushed"] = dict(post_card(ctx, bq, card, {"sitting": sit_id, "card": card}), q=bq,
                         parts=len(parts), probe=bool(probe))
    if probe:
        ctx.add("probe.asked", {"probe_id": "pr-" + ulid(), "q_id": probe["id"], "batch": bq, "sit_id": sit_id})
    return out(res)


def _probe_candidate(ctx):
    """The oldest taken owner question that has not been probed and explains itself."""
    probed = {p.get("src") for q in ctx.Q.values() for p in (q["asked"] or {}).get("parts", []) if p.get("probe")}
    st = fold(ctx.records)
    for q in sorted(ctx.Q.values(), key=lambda x: x["id"]):
        r = q["raised"] or {}
        if q["state"] == "taken" and r.get("end") == "owner_take" and q["id"] not in probed and q["card"] \
                and not r.get("guard") and len(q["card"]["parts"]) == 1 and q["card"]["parts"][0].get("plain"):
            live = ops.live_decision(ctx, q["id"])
            if live and st[live]["body"].get("taken"):
                return q
    return None


# ---------------------------------------------------------------- cards feed

def cmd_cards(a):
    ctx = ctx_for(a)
    st = fold(ctx.records)
    cards = []
    for q in sorted(ctx.Q.values(), key=lambda x: x["id"]):
        if q["state"] != "asked" or q["src_of"]:
            continue
        asked = q["asked"]
        rc = resolved(ctx, {"text": "", "parts": asked["parts"]})
        item = {"q": q["id"], "stamp": state.ms_of(q["posted_ts"]), "door": asked["door"], "weight": asked["weight"],
                "mode": asked["mode"], "viewer_url": asked.get("viewer_url", ""),
                "parts": [{"id": p["id"], "text": p["text"], "say": p.get("say") or p["text"],
                           "options": [dict({"id": o["id"], "label": o["label"]}, **({"rec": True} if o.get("rec") else {}))
                                       for o in p["options"]],
                           "links": [{"href": asked["viewer_url"], "label": "brief"}] if asked.get("viewer_url") else []}
                          for p in rc["parts"]]}
        cards.append(item)
    taken = [{"d": e["id"], "what": lint.resolve(e["body"].get("what", ""), ctx.labels), "why": e["body"].get("why", ""),
              "at": e["first_ts"], "doubt": e["body"].get("doubt", 0)}
             for e in st.values() if e["kind"] == "decision" and e["body"].get("taken") and e["state"] != "superseded"]
    names = [{"label": e["id"], "kind": e["body"].get("kind", "label"), "text": e["body"].get("text", "")}
             for e in st.values() if e["kind"] == "label" and e["state"] != "retired"]
    return out({"cards": cards, "taken": taken, "names": names})


# ---------------------------------------------------------------- answers

def _label_to_option(part, line):
    want = line.strip().lower()
    return next((o["id"] for o in part["options"] if o["label"].strip().lower() == want), None)


def cmd_answer(a):
    ctx = ctx_for(a)
    q = ctx.q(a.q)
    if q["state"] != "asked" or q["src_of"]:
        fail("question has no open card", 1, state=q["state"], q=a.q)
    hid, timeout = q["hearth_id"], ctx.knobs["hearth"]["timeout_s"]
    asked = q["asked"]
    rc = resolved(ctx, {"text": "", "parts": asked["parts"]})
    parts = rc["parts"]
    single = asked["mode"] == "single"
    text = (a.text or "").strip()
    hit = words.classify(text) if text and "\n" not in text else None
    via = a.via
    if hit:
        w, arg = hit
        wid = w["id"]
        if wid in ("word.send", "word.why"):
            return out({"word": wid, "records": w["records"], "brief": asked.get("viewer_url", "")})
        if wid in ("word.harder", "word.enough"):
            return out(dict(ops.apply_word(ctx, a.q, w, arg, via), word=wid))
        if wid in ops.SETTLE_WORDS:
            if wid == "word.wrong_question":
                bridge.run(["withdraw", hid, "--reason", arg or "wrong question"], timeout=timeout)
                row = {}
            else:
                row = bridge.run(["answer", hid, "--text", text, "--via", "kiln"], timeout=timeout)
            extra = {"hearth_id": hid}
            if row.get("responded_at"):
                extra["responded_at"] = row["responded_at"]
            return out(dict(ops.apply_word(ctx, a.q, w, arg, via, extra), word=wid))
    picks, raw = {}, None
    if hit and hit[0]["id"] == "word.yes":
        picks = ops.rec_choice({"card": {"parts": parts}})
    elif hit and hit[0]["id"] == "word.call_it":
        picks = ops.rec_choice({"card": {"parts": parts}})
        first = next(iter(picks))
        opt = next(o for o in asked["parts"][0]["options"] if o["id"] == picks[first])
        m = lint.HOLE.fullmatch(opt["label"])
        if m:
            ctx.add("label.edited", {"lbl_id": m.group(1), "text": hit[1]}, by="owner", via=via)
        else:
            raw = "call it " + hit[1]
    else:
        lines = [x for x in text.splitlines() if x.strip()]
        if lines and len(lines) == len(parts):
            got = [_label_to_option(p, ln) for p, ln in zip(parts, lines)]
            if all(got):
                picks = {p["id"]: o for p, o in zip(parts, got)}
        if not picks:
            raw = text
    if not picks and not raw:
        fail("answer needs --text", 2)
    if picks:
        args = ["answer", hid, "--via", "kiln"]
        args += ["--option", next(iter(picks.values()))] if single else sum((["--pick", f"{k}={v}"] for k, v in picks.items()), [])
        row = bridge.run(args, timeout=timeout)
    else:
        row = bridge.run(["answer", hid, "--text", raw, "--via", "kiln"], timeout=timeout)
    rec = ops.record_answer(ctx, a.q, picks or None, raw, via=via, hearth_id=hid, responded_at=row.get("responded_at"))
    return out({"recorded": rec["id"], "picks": picks, "raw": raw})


# ---------------------------------------------------------------- take, flip, due

def cmd_take(a):
    ctx = ctx_for(a)
    q = ctx.q(a.q)
    if q["state"] != "raised":
        fail("only a raised question can be taken", 1, state=q["state"])
    d = take(ctx, q, a.option, a.why)
    return out({"state": "settled", "decision": d, "q": a.q})


def cmd_flip(a):
    ctx = ctx_for(a)
    st = fold(ctx.records)
    e = st.get(a.d)
    if not e or e["kind"] != "decision":
        fail("unknown decision", 1, d=a.d)
    if e["state"] == "superseded":
        fail("decision is already superseded", 1, d=a.d)
    qid = e["body"]["q_id"]
    q = ctx.q(qid)
    chosen = next(iter(e["body"]["chosen"].values()))
    opt = a.option
    if not opt:
        others = [o["id"] for p in (q["card"] or {}).get("parts", [])[:1] for o in p["options"] if o["id"] != chosen]
        if len(others) != 1:
            fail("name the option with --option", 2, options=others)
        opt = others[0]
    return out({"flipped": a.d, "decision": ops.flip_decision(ctx, qid, opt, via="grill")})


def _cmp(op, a, b):
    return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b, "==": a == b}.get(op, False)


def cmd_due(a):
    ctx = ctx_for(a)
    now = a.now or ops.now_iso()
    metrics = dict(m.split("=", 1) for m in a.metric or [])
    due = []
    for q in ctx.Q.values():
        if q["state"] == "deferred":
            d = next((r for r in reversed(ctx.records) if r["type"] == "question.deferred" and r["body"]["q_id"] == q["id"]), None)
            wake = d["body"].get("wake", {}) if d else {}
            if (wake.get("date") and wake["date"] <= now) or (wake.get("event") and wake["event"] in (a.event or [])):
                due.append({"kind": "deferred", "q": q["id"]})
    st = fold(ctx.records)
    for e in st.values():
        if e["kind"] != "decision" or e["state"] == "superseded":
            continue
        for rw in e["body"].get("review_when", []):
            m = rw.get("metric")
            if m in metrics:
                try:
                    hit = _cmp(rw.get("op", "=="), float(metrics[m]), float(rw["value"]))
                except (ValueError, KeyError):
                    hit = metrics[m] == str(rw.get("value"))
                if hit:
                    due.append({"kind": "review", "d": e["id"], "metric": m})
            if rw.get("event") and rw["event"] in (a.event or []):
                due.append({"kind": "review", "d": e["id"], "event": rw["event"]})
    return out({"due": due, "at": now})


# ---------------------------------------------------------------- parser

def parser():
    p = argparse.ArgumentParser(prog="crucible grill", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)

    def add(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--root", help="design worktree root")
        return s
    s = add("ask", "raise a question: returns settled, routed or asked")
    s.add_argument("--text")
    s.add_argument("--node", help="tree node id; the node sets class, door and weight")
    s.add_argument("--class", dest="cls", help="off-tree class: fact|measurable|lead|owner|money")
    s.add_argument("--clause", help="rubric clause id the class cites")
    s.add_argument("--door")
    s.add_argument("--weight")
    s.add_argument("--guard", help="pillar|spend|look|public-name|scope|save-format")
    s.add_argument("--draft", help="card JSON file")
    s.add_argument("--session")
    s.add_argument("--post", action="store_true", help="post the card now")
    s = add("tree", "show the tree frontier or validate the tree")
    s.add_argument("--check", action="store_true")
    s.add_argument("--file")
    s = add("draft", "attach a card draft to a raised question")
    s.add_argument("q")
    s.add_argument("--file", required=True)
    s.add_argument("--post", action="store_true")
    s = add("cards", "the feed for the Kiln plugin")
    s.add_argument("--json", action="store_true")
    s = add("sit", "open a sitting and push one card or batch")
    s.add_argument("--budget", type=int)
    s.add_argument("--no-open", action="store_true", help="do not raise frontier nodes")
    s = add("post", "lint and post a card to hearth")
    s.add_argument("q", nargs="?")
    s.add_argument("--card", help="card JSON file; raises the question and posts it")
    s = add("answer", "record an answer typed or tapped in Kiln")
    s.add_argument("q")
    s.add_argument("--text", help="option labels one per part, or words")
    s.add_argument("--via", default="kiln-tap")
    s = add("take", "an agent takes a question the gate allows")
    s.add_argument("q")
    s.add_argument("--option")
    s.add_argument("--why", default="")
    s = add("flip", "reverse a taken decision")
    s.add_argument("d")
    s.add_argument("--option")
    s = add("due", "deferred questions and decisions whose review fired")
    s.add_argument("--now")
    s.add_argument("--event", action="append")
    s.add_argument("--metric", action="append", metavar="NAME=VALUE")
    return p


def main(argv):
    try:
        a = parser().parse_args(argv)
        if a.verb == "post" and not (a.q or a.card):
            return out({"error": "give a question id or --card"}, 2)
        return globals()["cmd_" + a.verb](a)
    except LedgerError as e:
        try:
            return out(json.loads(str(e)), e.code)
        except ValueError:
            return out({"error": str(e)}, e.code)
    except SystemExit as e:
        return e.code or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
