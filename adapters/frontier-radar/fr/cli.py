"""frontier-radar command line."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import USER_AGENT
from .digest import DEFAULT_MAX, KbRefused, build_digest
from .fetch import Fetcher, Throttle
from .poll import DEFAULT_MAX_AGE_DAYS, poll_all
from .scoring import rescore
from .sources import DEFAULT_PATH, load_sources
from .store import Store, default_state_path, iso
from .triage import DEFAULT_MIN_CONFIDENCE, triage

NERVOUS_BIN = os.environ.get("NERVOUS_BIN", str(Path(__file__).resolve().parents[3] / "sdk" / "shell" / "nervous"))
EXIT_KB_REFUSED = 3


def publish(channel: str, payload: dict) -> bool:
    """Publish through the shell SDK only. A failed publish warns and never fails the run."""
    try:
        subprocess.run([NERVOUS_BIN, "publish", channel, json.dumps(payload)], check=True,
                       capture_output=True, text=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"[frontier-radar] publish {channel} failed: {e}\n")
        return False


def item_payload(ev: dict, stage: str) -> dict:
    sights = ev.get("_sightings") or []
    t = ev.get("triage") or {}
    return {
        "event_key": ev["event_key"], "stage": stage, "url": ev["url"][:2048], "title": ev["title"][:300],
        "source_id": ev["primary_source"], "domains": ev["domains"], "first_seen_at": ev["first_seen_at"],
        "published_at": ev["published_at"], "signal": ev["signal"], "mirror_count": len(sights),
        "score": ev["score"], "lane": ev["lane"], "action": t.get("action"), "confidence": t.get("confidence"),
    }


def make_item_publisher(store: Store, enabled: bool):
    def pub(ev: dict, stage: str) -> None:
        if enabled:
            ev = dict(ev)
            ev["_sightings"] = [s for s in store.sightings(ev["event_key"]) if s["role"] != "primary"]
            publish("research.radar.item.v1", item_payload(ev, stage))
    return pub


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read(2_000_000))
    except (OSError, ValueError) as e:
        sys.stderr.write(f"[frontier-radar] engagement fetch failed {url}: {e}\n")
        return None


def cmd_poll(a, store, now) -> int:
    sources = load_sources(a.sources)
    results = poll_all(sources, store, Fetcher(), Throttle(), now, set(a.source or []), a.force, a.max_age_days)
    pub = make_item_publisher(store, not a.no_publish)
    new = 0
    for r in results:
        new += len(r.new_events)
        line = f"{r.source_id}: {r.outcome} new={len(r.new_events)}"
        print(line + (f" ({r.detail})" if r.detail else ""))
        for k in r.new_events:
            pub(store.event(k), "new")
    for al in store.open_alarms():
        sys.stderr.write(f"ALARM {al['kind']} {al['source_id']}: {al['detail']}\n")
    print(f"polled {len(results)} sources, {new} new events, {len(store.open_alarms())} open alarms")
    return 0


def cmd_rescore(a, store, now) -> int:
    print(json.dumps(rescore(store, now, fetch_json)))
    return 0


def cmd_triage(a, store, now) -> int:
    counts = triage(store, now, cmd=a.cmd, limit=a.limit, min_confidence=a.min_confidence,
                    publish=make_item_publisher(store, not a.no_publish))
    print(json.dumps(counts))
    return 1 if counts["errors"] and not (counts["digest"] + counts["review"] + counts["dropped"]) else 0


def cmd_digest(a, store, now) -> int:
    out_dir = Path(a.out_dir) if a.out_dir else store.path.parent / "digests"
    try:
        res = build_digest(store, now, a.days, a.max, out_dir, a.kb, a.dry_run, a.include_fresh)
    except KbRefused as e:
        sys.stderr.write(f"frontier-radar: {e}\n")
        return EXIT_KB_REFUSED
    print(res["text"])
    if not a.dry_run and not a.no_publish:
        publish("research.radar.digest.v1", {
            "generated_at": iso(now), "window_days": a.days, "max_items": a.max, "item_count": len(res["items"]),
            "event_keys": [e["event_key"] for e in res["items"]], "review_count": res["review_count"],
            "path": res["path"].name if res["path"] else None, "kb_ingested": res["kb_ingested"]})
    return 0


def cmd_review(a, store, now) -> int:
    if a.promote or a.drop:
        key, lane = (a.promote, "digest") if a.promote else (a.drop, "dropped")
        if not store.event(key):
            sys.stderr.write(f"no event {key}\n")
            return 1
        store.update_event(key, lane=lane)
        print(f"{key} -> {lane}")
        return 0
    for e in store.events("lane='review' AND digested_at IS NULL"):
        t = e["triage"] or {}
        print(f"{e['event_key']}\t{t.get('confidence')}\t{t.get('action')}\t{e['title'][:90]}\t{e['url']}")
    return 0


def cmd_status(a, store, now) -> int:
    alarms = store.open_alarms()
    lanes = {r[0] or "untriaged": r[1] for r in store.db.execute("SELECT lane, COUNT(*) FROM events GROUP BY lane")}
    if a.json:
        print(json.dumps({"alarms": alarms, "events_by_lane": lanes}))
    else:
        for al in alarms:
            print(f"ALARM {al['kind']:<8} {al['source_id']:<32} {al['detail']}")
        print("events by lane: " + ", ".join(f"{k}={v}" for k, v in sorted(lanes.items())))
    return 0


def cmd_sources(a, store, now) -> int:
    for s in load_sources(a.sources):
        flag = "" if s.enabled else "\tDISABLED"
        print(f"{s.id}\t{s.kind}\t{s.role}\tsignal={s.signal}\tslo={s.freshness_slo_days:g}d\t{','.join(s.domains)}{flag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="frontier-radar", description="Poll primary sources for agentic frontier news.")
    ap.add_argument("--state", help="sqlite path (default $FRONTIER_RADAR_STATE or $XDG_STATE_HOME/frontier-radar/radar.db)")
    ap.add_argument("--sources", default=str(DEFAULT_PATH), help="source registry TOML")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("poll", help="fetch sources, dedup into the store, raise alarms")
    p.add_argument("--source", action="append", help="only this source id (repeatable)")
    p.add_argument("--force", action="store_true", help="ignore per-source min_interval_minutes")
    p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    p.add_argument("--no-publish", action="store_true")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("rescore", help="take T+24h and T+72h engagement snapshots and refresh scores")
    p.set_defaults(fn=cmd_rescore)

    p = sub.add_parser("triage", help="classify new events through a command against profile.md")
    p.add_argument("--cmd", help="classifier command (default $FRONTIER_RADAR_TRIAGE_CMD or claude -p --model haiku)")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    p.add_argument("--no-publish", action="store_true")
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("digest", help="write the weekly markdown digest under the attention budget")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--max", type=int, default=DEFAULT_MAX, help="attention budget (items)")
    p.add_argument("--out-dir")
    p.add_argument("--kb", action="store_true", help="ingest into kb inbox tier (refuses when kb lacks --tier)")
    p.add_argument("--dry-run", action="store_true", help="print only; mark nothing as digested")
    p.add_argument("--include-fresh", action="store_true", help="include events younger than 24h")
    p.add_argument("--no-publish", action="store_true")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("review", help="list low-confidence items, or --promote / --drop one")
    p.add_argument("--promote", metavar="EVENT_KEY")
    p.add_argument("--drop", metavar="EVENT_KEY")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("status", help="open alarms (redirect, stale, error) and event counts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("sources", help="list the registry")
    p.set_defaults(fn=cmd_sources)
    return ap


def main(argv=None, now: datetime | None = None) -> int:
    a = build_parser().parse_args(argv)
    store = Store(default_state_path(a.state))
    try:
        return a.fn(a, store, now or datetime.now(timezone.utc))
    finally:
        store.close()
