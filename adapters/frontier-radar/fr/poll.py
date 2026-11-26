"""poll: conditional GET per source, event-level dedup into the store, redirect and freshness alarms."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import parsers
from .fetch import Fetcher, Response, Throttle
from .keys import canonical_url
from .parsers import Item
from .scoring import refresh_scores
from .sources import Source
from .store import Store, from_iso, iso

DEFAULT_MAX_AGE_DAYS = 30
FAIL_ALARM_AFTER = 3
PAGE_LINES_KEPT = 5000
ADDED_LINES_IN_SUMMARY = 20
BULK_LASTMOD_PER_DAY = 10


@dataclass
class PollResult:
    source_id: str
    outcome: str                 # ok | not-modified | skipped | error | baseline
    new_events: list = field(default_factory=list)
    sightings: int = 0
    detail: str = ""


def _page_item(src: Source, store: Store, resp: Response, now: datetime) -> tuple[list[tuple[Item, str]], str]:
    lines = parsers.page_lines(resp.body)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    st = store.source_state(src.id)
    prior_hash = st.get("page_hash") or ""
    prior = set(json.loads(st.get("page_lines") or "[]"))
    store.save_source_state(src.id, page_hash=digest, page_lines=json.dumps(lines[:PAGE_LINES_KEPT]))
    if not prior_hash:
        return [], "baseline"
    if digest == prior_hash:
        return [], "unchanged"
    added = [ln for ln in lines if ln not in prior]
    text = " | ".join(added)
    if not added or not src.passes_filters(text):
        return [], "changed-without-news"
    title = f"{src.name} changed: {added[0][:160]}"
    item = Item(url=src.resolved_url(now), title=title, summary=" | ".join(added[:ADDED_LINES_IN_SUMMARY])[:parsers.SUMMARY_MAX],
                published_at=now)
    return [(item, f"page:{canonical_url(src.url)}@{digest[:12]}")], "changed"


def _sitemap_items(src: Source, store: Store, resp: Response, now: datetime) -> tuple[list[tuple[Item, str]], str]:
    entries = parsers.parse_sitemap(resp.body)
    if src.path_regex:
        entries = [(u, m) for u, m in entries if re.search(src.path_regex, urlsplit(u).path)]
    known = store.sitemap_rows(src.id)
    first_run = not known
    cutoff = now - timedelta(days=src.lookback_days)
    out, rows = [], {}
    for url, lastmod in entries:
        lm = iso(lastmod) or ""
        rows[url] = lm
        if known.get(url) == lm:
            continue
        if first_run and (lastmod is None or lastmod < cutoff):
            continue
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        out.append((Item(url=url, title=slug or url, published_at=lastmod), ""))
    store.save_sitemap(src.id, rows)
    per_day = Counter(it.published_at.date() for it, _ in out if it.published_at)
    bulk = {d for d, n in per_day.items() if n > BULK_LASTMOD_PER_DAY}
    kept = [(it, k) for it, k in out if not (it.published_at and it.published_at.date() in bulk)]
    kept.sort(key=lambda p: p[0].published_at or now, reverse=True)
    note = f"bulk lastmod ignored: {len(out) - len(kept)} urls on {len(bulk)} day(s)" if bulk else "ok"
    return kept[:src.max_items], note


def _gate(src: Source, items: list[Item], now: datetime, default_max_age: int) -> list[Item]:
    max_age = timedelta(days=src.max_age_days or default_max_age)
    kept = []
    for it in sorted(items, key=lambda i: i.published_at or now, reverse=True):
        if it.published_at and now - it.published_at > max_age:
            continue
        if not src.passes_filters(f"{it.title} {it.summary}"):
            continue
        if src.min_engagement and it.engagement and \
                max(it.engagement.get(k, 0) for k in ("points", "upvotes", "stars", "likes")) < src.min_engagement:
            continue
        kept.append(it)
    return kept[:src.max_items]


def poll_source(src: Source, store: Store, fetcher: Fetcher, throttle: Throttle, now: datetime,
                force: bool = False, default_max_age: int = DEFAULT_MAX_AGE_DAYS) -> PollResult:
    st = store.source_state(src.id)
    if not force and st.get("last_poll_at"):
        if now - from_iso(st["last_poll_at"]) < timedelta(minutes=src.min_interval_minutes):
            return PollResult(src.id, "skipped", detail="rate limit: polled within min_interval_minutes")
    url = src.resolved_url(now)
    validators = st.get("url") == url
    throttle.wait(url, src.host_delay_s)
    resp = fetcher.get(url, st.get("etag", "") if validators else "", st.get("last_modified", "") if validators else "")
    first_poll = st.get("first_poll_at") or iso(now)
    base = dict(url=url, last_poll_at=iso(now), first_poll_at=first_poll)

    if resp.redirects:
        hops = " -> ".join(f"{c} {b}" for c, a, b in resp.redirects)
        store.raise_alarm(src.id, "redirect", f"{url} moved: {hops}", now)
    else:
        store.clear_alarm(src.id, "redirect", now)

    if resp.status == 0 or resp.status >= 400:
        fails = int(st.get("consecutive_fail") or 0) + 1
        store.save_source_state(src.id, **base, consecutive_fail=fails, last_status=resp.error or str(resp.status))
        if fails >= FAIL_ALARM_AFTER:
            store.raise_alarm(src.id, "error", f"{fails} consecutive failures: {resp.error}", now)
        return PollResult(src.id, "error", detail=resp.error)
    store.clear_alarm(src.id, "error", now)

    if resp.not_modified:
        store.save_source_state(src.id, **base, consecutive_fail=0, last_status="304")
        return PollResult(src.id, "not-modified")

    store.save_source_state(src.id, **base, consecutive_fail=0, last_status=str(resp.status),
                            etag=resp.etag, last_modified=resp.last_modified, final_url=resp.final_url)
    try:
        keyed: list[tuple[Item, str]]
        newest = None
        if src.kind == "page-diff":
            keyed, outcome = _page_item(src, store, resp, now)
            newest = now if keyed else None
        elif src.kind == "sitemap-lastmod":
            keyed, outcome = _sitemap_items(src, store, resp, now)
            dates = [it.published_at for it, _ in keyed if it.published_at]
            newest = max(dates) if dates else None
        else:
            raw = parsers.parse_by_kind(src.kind, resp.body, src)
            dates = [i.published_at for i in raw if i.published_at]
            newest = max(dates) if dates else None
            keyed = [(i, "") for i in _gate(src, raw, now, default_max_age)]
            outcome = "ok"
    except (ValueError, KeyError, TypeError) as e:
        store.save_source_state(src.id, last_status=f"parse error: {e}")
        store.raise_alarm(src.id, "error", f"parse error: {e}", now)
        return PollResult(src.id, "error", detail=f"parse error: {e}")

    res = PollResult(src.id, "baseline" if outcome == "baseline" else "ok",
                     detail="" if outcome in ("ok", "baseline", "changed") else outcome)
    for item, key in keyed:
        ekey, is_new = store.record_sighting(src, item, now, key or None)
        res.sightings += 1
        if is_new:
            res.new_events.append(ekey)
    upd = {}
    prev_newest = from_iso(st.get("newest_published_at"))
    if newest and (prev_newest is None or newest > prev_newest):
        upd["newest_published_at"] = iso(newest)
    if res.new_events:
        upd["last_new_item_at"] = iso(now)
    if upd:
        store.save_source_state(src.id, **upd)
    return res


def check_slo(src: Source, store: Store, now: datetime) -> str | None:
    """Alarm when the source has been silent longer than its freshness SLO."""
    st = store.source_state(src.id)
    if not st.get("last_poll_at"):
        return None
    ref = from_iso(st.get("newest_published_at")) or from_iso(st.get("last_new_item_at")) or from_iso(st["first_poll_at"])
    silent = (now - ref).total_seconds() / 86400
    if silent > src.freshness_slo_days:
        msg = f"silent {silent:.1f} days, SLO {src.freshness_slo_days:g}"
        store.raise_alarm(src.id, "stale", msg, now)
        return msg
    store.clear_alarm(src.id, "stale", now)
    return None


def poll_all(sources: list[Source], store: Store, fetcher: Fetcher, throttle: Throttle, now: datetime,
             only: set | None = None, force: bool = False, default_max_age: int = DEFAULT_MAX_AGE_DAYS) -> list[PollResult]:
    results = []
    for src in sources:
        if not src.enabled or (only and src.id not in only):
            continue
        results.append(poll_source(src, store, fetcher, throttle, now, force, default_max_age))
        check_slo(src, store, now)
    refresh_scores(store)
    return results
