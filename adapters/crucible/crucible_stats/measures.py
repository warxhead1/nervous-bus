"""The terms of design 3.10 and the owner-effort rows of section 10, computed from ledger records.

Record types read:
  question.raised|triaged|routed|asked|answered  body.q_id; routed/triaged carry body.route
  decision.settled {d_id, authority}, decision.flipped {d_id}
  lane.rework {d_id, lane_hours}
  sitting.closed {sit_id, pushes}
Any body with a model_requests count feeds --requests."""
import datetime
import statistics

ROUTES = ("lookup", "lab", "research", "taken")
RUBBER_RATE, RUBBER_SECONDS, RUBBER_MIN_N = 0.95, 5.0, 5


def _t(ts):
    return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)


def _median(xs):
    return round(statistics.median(xs), 3) if xs else None


def _week(ts):
    y, w, _ = _t(ts).isocalendar()
    return f"{y}-W{w:02d}"


def _share(n, d):
    return round(n / d, 4) if d else None


def compute(records, since=None, rubber=(RUBBER_RATE, RUBBER_SECONDS, RUBBER_MIN_N), requests=False):
    """Counts only records with ts >= since; lookups (when a take was settled) read the whole ledger."""
    win = [r for r in records if not since or r["ts"][:10] >= since]
    first_asked, klass, route = {}, {}, {}
    settled_at, raised = {}, set()
    for r in records:
        b, t = r["body"], r["type"]
        q = b.get("q_id")
        if t == "question.asked":
            first_asked.setdefault(q, r)
            klass[q] = b.get("class", klass.get(q))
        elif t in ("question.triaged", "question.routed") and b.get("route"):
            route[q] = b["route"]
        elif t == "decision.settled":
            settled_at.setdefault(b.get("d_id"), r["ts"])
    answers = {}
    for r in win:
        b, t = r["body"], r["type"]
        q = b.get("q_id")
        if t == "question.raised":
            raised.add(q)
        elif t == "question.answered":
            answers.setdefault(q, r)
    cards = {r["body"]["q_id"] for r in win if r["type"] == "question.asked"}
    raised |= cards
    deflected = sorted(raised - cards)
    by_route = {k: 0 for k in ROUTES}
    by_route["unrouted"] = 0
    for q in deflected:
        by_route[route.get(q) if route.get(q) in ROUTES else "unrouted"] += 1

    seconds = []
    for q in cards:
        a = answers.get(q)
        if not a:
            continue
        if isinstance(a["body"].get("latency_ms"), (int, float)):
            seconds.append(a["body"]["latency_ms"] / 1000)
        elif q in first_asked:
            seconds.append((_t(a["ts"]) - _t(first_asked[q]["ts"])).total_seconds())
    spc = _median(seconds)

    flips = [r for r in win if r["type"] == "decision.flipped"]
    strip = [(_t(r["ts"]) - _t(settled_at[r["body"]["d_id"]])).total_seconds()
             for r in flips if r["body"].get("d_id") in settled_at]
    flipped_ids = {r["body"].get("d_id") for r in flips}
    rework = sum(r["body"].get("lane_hours", 0) for r in win
                 if r["type"] == "lane.rework" and r["body"].get("d_id") in flipped_ids)

    probes, graded = {}, {}
    for q, a in answers.items():
        b = a["body"]
        cls = b.get("class") or klass.get(q) or "unknown"
        if b.get("probe"):
            p = probes.setdefault(cls, [0, 0])
            p[0] += 1
            p[1] += b.get("was_recommended") is False
        if "was_recommended" in b:
            g = graded.setdefault(cls, {"n": 0, "accepted": 0, "seconds": []})
            g["n"] += 1
            g["accepted"] += bool(b["was_recommended"])
            if isinstance(b.get("latency_ms"), (int, float)):
                g["seconds"].append(b["latency_ms"] / 1000)
    rate, max_sec, min_n = rubber
    stamp = {}
    for cls, g in sorted(graded.items()):
        acc, med = _share(g["accepted"], g["n"]), _median(g["seconds"])
        stamp[cls] = {"n": g["n"], "acceptance": acc, "median_seconds": med,
                      "flag": g["n"] >= min_n and acc >= rate and med is not None and med <= max_sec}

    sittings = [r["body"] for r in win if r["type"] == "sitting.closed"]
    pushes = sum(s.get("pushes", 0) for s in sittings)
    settled = {r["body"]["d_id"] for r in win if r["type"] == "decision.settled"}
    weeks = {}
    for q, a in answers.items():
        sec = a["body"].get("latency_ms", 0) / 1000
        weeks[_week(a["ts"])] = weeks.get(_week(a["ts"]), 0) + sec
    n_ans = len(answers)
    typed = [a["body"].get("typed_chars", 0) for a in answers.values()]
    taps = [a["body"].get("taps", len(a["body"].get("picks") or {})) for a in answers.values()]
    free = sum(1 for a in answers.values() if a["body"].get("kind") == "text" or a["body"].get("typed_chars", 0) > 0)

    out = {
        "raised": len(raised), "cards": len(cards), "deflected": len(deflected), "deflected_by_route": by_route,
        "seconds_per_card": spc,
        "gross_saved_seconds": round(len(deflected) * spc, 3) if spc is not None else None,
        "cost_of_wrong_takes": {"flips": len(flips), "strip_seconds": round(sum(strip), 3), "rework_lane_hours": rework},
        "false_auto_rate": {c: {"probes": p[0], "overridden": p[1], "rate": _share(p[1], p[0])} for c, p in sorted(probes.items())},
        "rubber_stamp": stamp,
        "owner_effort": {
            "minutes_per_week": {w: round(s / 60, 3) for w, s in sorted(weeks.items())},
            "taps_per_answer": round(sum(taps) / n_ans, 3) if n_ans else None,
            "typed_chars_per_answer": round(sum(typed) / n_ans, 3) if n_ans else None,
            "pushes_per_sitting": round(pushes / len(sittings), 3) if sittings else None,
            "sittings": len(sittings), "settled_decisions": len(settled),
            "cards_per_settled_decision": round(len(cards) / len(settled), 3) if settled else None,
            "free_text_share": _share(free, n_ans), "answers": n_ans,
        },
    }
    if requests:
        total = sum(r["body"].get("model_requests", 0) for r in win if isinstance(r["body"].get("model_requests"), int))
        out["requests"] = {"total": total, "per_card": round(total / len(cards), 3) if cards else None}
    return out


def report_md(stats, since=None):
    oe = stats["owner_effort"]
    rows = [
        ("raised", stats["raised"]), ("cards", stats["cards"]), ("deflected", stats["deflected"]),
        ("seconds per card", stats["seconds_per_card"]), ("gross saved (s)", stats["gross_saved_seconds"]),
        ("flips", stats["cost_of_wrong_takes"]["flips"]),
        ("rework lane-hours", stats["cost_of_wrong_takes"]["rework_lane_hours"]),
        ("pushes per sitting", oe["pushes_per_sitting"]), ("free-text share", oe["free_text_share"]),
        ("cards per settled decision", oe["cards_per_settled_decision"]),
    ]
    lines = ["# Crucible report", "", f"Since: {since or 'start'}", "", "| Measure | Value |", "|---|---|"]
    lines += [f"| {k} | {'-' if v is None else v} |" for k, v in rows]
    lines += ["", "Deflected by route: " + ", ".join(f"{k} {v}" for k, v in stats["deflected_by_route"].items()), ""]
    lines += ["False-auto rate by class: " + (", ".join(f"{c} {v['rate']}" for c, v in stats["false_auto_rate"].items()) or "none"), ""]
    flagged = [c for c, v in stats["rubber_stamp"].items() if v["flag"]]
    lines += ["Rubber-stamp classes: " + (", ".join(flagged) or "none"), ""]
    return "\n".join(lines)
