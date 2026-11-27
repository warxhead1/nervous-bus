"""Ledger health checks and the planted-fault self-test."""
import os
import shutil
import tempfile
import time

from . import build, schema
from .fold import fold
from .store import Ledger, bulk_dir

HEARTBEAT_MAX_S = 900


def check(ledger, bulk=None, now=None):
    """Return {"findings": [...], "stale_heartbeat": bool, "heartbeat": str}."""
    records, problems = ledger.read()
    findings = [dict(check="parse", **p) for p in problems]
    seen = set()
    for r in records:
        errs = schema.validate_record(r)
        if errs:
            findings.append({"check": "schema", "id": r.get("id"), "problem": "; ".join(errs)})
        if r.get("id") in seen:
            findings.append({"check": "duplicate_id", "id": r.get("id")})
        seen.add(r.get("id"))
    valid = [r for r in records if not schema.validate_record(r)]
    state = fold(valid)
    for e in state.values():
        if e["kind"] != "decision":
            continue
        q = e["body"].get("q_id")
        if not q or q not in state or state[q]["kind"] != "question":
            findings.append({"check": "orphan_decision", "id": e["id"], "q_id": q})
        sup = e["body"].get("supersedes")
        if sup and sup not in state:
            findings.append({"check": "orphan_decision", "id": e["id"], "supersedes": sup})
    try:
        drift = build.diff(ledger.root, build.expected_views(valid, state))
    except ValueError as ex:
        drift = []
        findings.append({"check": "views", "problem": str(ex)})
    findings += [dict(check="view_drift", **d) for d in drift]

    hb, stale = "absent", False
    game = next((r.get("game") for r in records if r.get("game")), None)
    if bulk is None and game:
        bulk = bulk_dir(game)
    if bulk:
        p = os.path.join(bulk, "steward.heartbeat")
        if os.path.isfile(p):
            age = (now or time.time()) - os.stat(p).st_mtime
            limit = float(os.environ.get("CRUCIBLE_HEARTBEAT_MAX_S", HEARTBEAT_MAX_S))
            hb = "stale" if age > limit else "fresh"
            stale = age > limit
    return {"findings": findings, "stale_heartbeat": stale, "heartbeat": hb}


def self_test():
    tmp = tempfile.mkdtemp(prefix="crucible-selftest-")
    try:
        led = Ledger(tmp, writer="selftest")
        q = "q-01JD0000000000000000000099"
        d = "d-01JD0000000000000000000098"
        base = {"game": "selftest", "by": "crucible", "via": "self-test"}
        led.append(dict(base, type="question.answered", body={"q_id": q, "kind": "pick", "picks": {"p1": "a"}}))
        led.append(dict(base, type="decision.settled", body={
            "d_id": d, "q_id": q, "chosen": {"p1": "a"}, "authority": "owner", "door": "two-way"}))
        records, _ = led.read()
        views = build.expected_views(records, fold(records))
        build.write(tmp, views, build.diff(tmp, views))
        bulk = os.path.join(tmp, "bulk")
        clean = check(led, bulk)["findings"]

        led.append(dict(base, type="decision.settled", body={
            "d_id": "d-01JD0000000000000000000097", "q_id": "q-01JD0000000000000000000096",
            "chosen": {"p1": "a"}, "authority": "lead", "door": "two-way"}))
        view = os.path.join(tmp, build.DECISIONS_DIR, d + ".md")
        with open(view, "a", encoding="utf-8") as f:
            f.write("drifted by hand\n")
        kinds = {f["check"] for f in check(led, bulk)["findings"]}
        res = {
            "clean_baseline": not clean,
            "detected_orphan_decision": "orphan_decision" in kinds,
            "detected_view_drift": "view_drift" in kinds,
        }
        res["ok"] = all(res.values())
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
