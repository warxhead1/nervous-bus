"""Parsers: one function per source kind, bytes in, Items out. No network, no state."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BOT_HANDLE_RE = re.compile(
    r"(brid\.gy$|xmirror\.bot$|mirr|(^|[.\-_])bots?([.\-_]|$)|"
    r"^(claudeupdates|claudecodechanges|whatsnew|hn100|hnws|hackernewsbot|hn-frontpage-bot)\b)", re.I)
SUMMARY_MAX = 600


@dataclass
class Item:
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    ext_id: str = ""            # source-native id (HN objectID)
    discussion_url: str = ""    # where the sighting lives when it differs from the artifact
    engagement: dict = field(default_factory=dict)  # points, comments, stars, upvotes
    bot: bool = False


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_text(markup: str, limit: int = SUMMARY_MAX) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el, *names) -> str:
    for c in el:
        if _local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_feed(body: bytes) -> list[Item]:
    """RSS 2.0 and Atom, including GitHub releases and commits atoms."""
    root = ET.fromstring(body)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = ""
        for c in el:
            if _local(c.tag) == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if not href and (c.text or "").strip():
                    link = c.text.strip()
                    break
        if not link:
            guid = _child_text(el, "guid", "id")
            link = guid if guid.startswith("http") else ""
        title = to_text(_child_text(el, "title"), 300)
        if not link or not title:
            continue
        body_html = _child_text(el, "description", "summary", "content", "encoded")
        out.append(Item(url=link, title=title, summary=to_text(body_html),
                        published_at=parse_dt(_child_text(el, "pubDate", "published", "updated", "date"))))
    return out


def parse_hn(body: bytes) -> list[Item]:
    out = []
    for h in json.loads(body).get("hits", []):
        oid = str(h.get("objectID", ""))
        title = h.get("title") or h.get("story_title") or ""
        if not oid or not title:
            continue
        disc = f"https://news.ycombinator.com/item?id={oid}"
        out.append(Item(url=h.get("url") or disc, title=title, summary=to_text(h.get("story_text") or ""),
                        published_at=parse_dt(h.get("created_at")), ext_id=oid,
                        discussion_url=disc if h.get("url") else "",
                        engagement={"points": h.get("points") or 0, "comments": h.get("num_comments") or 0}))
    return out


def parse_hf_daily_papers(body: bytes) -> list[Item]:
    out = []
    for row in json.loads(body):
        p = row.get("paper") or {}
        pid = p.get("id")
        if not pid or not p.get("title"):
            continue
        eng = {"upvotes": p.get("upvotes") or 0}
        if p.get("githubStars") is not None:
            eng["stars"] = p["githubStars"]
        out.append(Item(url=f"https://arxiv.org/abs/{pid}", title=p["title"], summary=to_text(p.get("ai_summary") or p.get("summary") or ""),
                        published_at=parse_dt(row.get("publishedAt") or p.get("publishedAt")), ext_id=pid,
                        discussion_url=f"https://huggingface.co/papers/{pid}", engagement=eng))
    return out


def parse_hf_models(body: bytes) -> list[Item]:
    out = []
    for m in json.loads(body):
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        tag = m.get("pipeline_tag") or "model"
        out.append(Item(url=f"https://huggingface.co/{mid}", title=f"{mid} ({tag})",
                        published_at=parse_dt(m.get("createdAt")), ext_id=mid,
                        engagement={"likes": m.get("likes") or 0, "downloads": m.get("downloads") or 0}))
    return out


def parse_github_search(body: bytes) -> list[Item]:
    out = []
    for r in json.loads(body).get("items", []):
        if not r.get("html_url"):
            continue
        out.append(Item(url=r["html_url"], title=f"{r.get('full_name')}: {r.get('description') or ''}"[:300],
                        summary=to_text(r.get("description") or ""), published_at=parse_dt(r.get("created_at")),
                        ext_id=r.get("full_name", ""), engagement={"stars": r.get("stargazers_count") or 0}))
    return out


def parse_bsky(body: bytes, extra_bot_re: str = "", link_only: bool = False) -> list[Item]:
    extra = re.compile(extra_bot_re, re.I) if extra_bot_re else None
    out = []
    for p in json.loads(body).get("posts", []):
        handle = (p.get("author") or {}).get("handle", "")
        rec = p.get("record") or {}
        link = ((p.get("embed") or {}).get("external") or {}).get("uri", "")
        if not link:
            for f in rec.get("facets") or []:
                for ft in f.get("features") or []:
                    if ft.get("uri"):
                        link = ft["uri"]
        rkey = (p.get("uri") or "").rsplit("/", 1)[-1]
        post_url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        if not link and link_only:
            continue
        text = to_text(rec.get("text") or "", 300)
        out.append(Item(url=link or post_url, title=text or post_url, summary="",
                        published_at=parse_dt(rec.get("createdAt")), ext_id=p.get("uri", ""),
                        discussion_url=post_url if link else "",
                        engagement={"likes": p.get("likeCount") or 0},
                        bot=bool(BOT_HANDLE_RE.search(handle) or (extra and extra.search(handle)))))
    return out


def parse_json_api(body: bytes, items_path: str, title_field: str, url_field: str, date_field: str) -> list[Item]:
    data = json.loads(body)
    for key in filter(None, items_path.split(".")):
        data = data[key]
    out = []
    for row in data:
        if row.get(url_field) and row.get(title_field):
            out.append(Item(url=row[url_field], title=str(row[title_field])[:300],
                            published_at=parse_dt(row.get(date_field)) if date_field else None))
    return out


def parse_sitemap(body: bytes) -> list[tuple[str, datetime | None]]:
    root = ET.fromstring(body)
    out = []
    for u in root.iter():
        if _local(u.tag) == "url":
            loc = _child_text(u, "loc")
            if loc:
                out.append((loc, parse_dt(_child_text(u, "lastmod"))))
    return out


def page_lines(body: bytes) -> list[str]:
    """Visible text of a changelog page as unique non-empty lines, in order."""
    raw = body.decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    seen, out = set(), []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def parse_by_kind(kind: str, body: bytes, src) -> list[Item]:
    if kind in ("rss", "atom", "github-releases"):
        return parse_feed(body)
    if kind == "hn-algolia":
        return parse_hn(body)
    if kind == "hf-daily-papers":
        return parse_hf_daily_papers(body)
    if kind == "hf-models":
        return parse_hf_models(body)
    if kind == "github-search":
        return parse_github_search(body)
    if kind == "bsky-search":
        return parse_bsky(body, src.extra.get("bot_handle_regex", ""), src.link_only)
    if kind == "json-api":
        return parse_json_api(body, src.items_path, src.title_field, src.url_field, src.date_field)
    raise ValueError(f"no item parser for kind {kind}")
