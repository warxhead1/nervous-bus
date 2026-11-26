"""Deterministic score plus engagement rescoring (T+24h, T+72h). First-sight score is never final."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .store import Store, from_iso

SNAPSHOT_AGES = (("t24", timedelta(hours=24)), ("t72", timedelta(hours=72)))
POINT_KEYS = ("points", "upvotes", "stars", "likes")


def latest_engagement(store: Store, key: str) -> dict:
    """Newest snapshot wins; fall back to the sightings' last-seen values."""
    snaps = store.snapshots(key)
    merged: dict = {}
    for s in sightings_engagement(store, key):
        for k, v in s.items():
            merged[k] = max(merged.get(k, 0), v)
    for s in snaps:
        if s["label"] != "first":
            for k, v in s["engagement"].items():
                merged[k] = max(merged.get(k, 0), v)
    return merged


def sightings_engagement(store: Store, key: str) -> list[dict]:
    return [s["engagement"] for s in store.sightings(key)]


def stage_of(store: Store, key: str) -> str:
    labels = {s["label"] for s in store.snapshots(key)}
    return "final" if "t72" in labels else "t24" if "t24" in labels else "first"


def score_event(store: Store, key: str) -> tuple[float, str]:
    ev = store.event(key)
    sights = store.sightings(key)
    eng = latest_engagement(store, key)
    pts = max((eng.get(k, 0) for k in POINT_KEYS), default=0)
    independent = {s["source_id"] for s in sights if s["role"] != "bot-mirror"}
    score = ev["signal"] * 10.0
    if any(s["role"] == "primary" for s in sights):
        score += 6
    score += 3 * min(3, max(0, len(independent) - 1))
    score += min(15.0, 5 * math.log10(1 + pts))
    score += min(5.0, 2 * math.log10(1 + eng.get("comments", 0)))
    return round(score, 2), stage_of(store, key)


def refresh_scores(store: Store) -> int:
    n = 0
    for ev in store.events():
        score, stage = score_event(store, ev["event_key"])
        if (score, stage) != (ev["score"], ev["score_stage"]):
            store.update_event(ev["event_key"], score=score, score_stage=stage)
            n += 1
    return n


def _gh_repo(key: str) -> str:
    return key[3:].split("@")[0] if key.startswith("gh:") else ""


def fetch_engagement(store: Store, key: str, fetch_json) -> dict:
    """Ask each engagement-capable sighting for its current numbers. fetch_json(url) -> obj or None."""
    out: dict = {}

    def merge(d):
        for k, v in d.items():
            out[k] = max(out.get(k, 0), v)

    for s in store.sightings(key):
        if s["kind"] == "hn-algolia" and s["ext_id"]:
            d = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{s['ext_id']}.json")
            if d:
                merge({"points": d.get("score") or 0, "comments": d.get("descendants") or 0})
        elif s["kind"] == "hf-daily-papers" and s["ext_id"]:
            d = fetch_json(f"https://huggingface.co/api/papers/{s['ext_id']}")
            if d:
                merge({"upvotes": d.get("upvotes") or 0})
    repo = _gh_repo(key)
    if repo:
        d = fetch_json(f"https://api.github.com/repos/{repo}")
        if d:
            merge({"stars": d.get("stargazers_count") or 0})
    return out


def rescore(store: Store, now: datetime, fetch_json) -> dict:
    """Take the T+24h / T+72h snapshot for events that reached that age, then refresh scores."""
    taken = 0
    for ev in store.events("status != 'dropped'"):
        age = now - from_iso(ev["first_seen_at"])
        have = {s["label"] for s in store.snapshots(ev["event_key"])}
        due = [label for label, a in SNAPSHOT_AGES if age >= a and label not in have]
        if not due:
            continue
        label = due[-1]
        eng = fetch_engagement(store, ev["event_key"], fetch_json)
        if eng:
            store.add_snapshot(ev["event_key"], label, now, eng)
            taken += 1
        elif label == "t72" or age >= SNAPSHOT_AGES[1][1]:
            store.add_snapshot(ev["event_key"], label, now, {})
    changed = refresh_scores(store)
    return {"snapshots": taken, "rescored": changed}
