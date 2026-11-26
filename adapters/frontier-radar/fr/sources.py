"""Source registry loader (sources.toml)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DOMAINS

KINDS = ("rss", "atom", "json-api", "github-releases", "github-search", "hn-algolia", "hf-daily-papers",
         "hf-models", "bsky-search", "sitemap-lastmod", "page-diff")
ROLES = ("primary", "mirror")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "sources.toml"


@dataclass
class Source:
    id: str
    name: str
    url: str
    kind: str
    domains: list
    signal: int
    freshness_slo_days: float
    role: str = "primary"
    notes: str = ""
    enabled: bool = True
    min_interval_minutes: float = 180
    host_delay_s: float = 1.0
    include_regex: str = ""
    exclude_regex: str = ""
    min_engagement: int = 0
    path_regex: str = ""
    lookback_days: int = 14
    link_only: bool = False
    max_items: int = 50
    max_age_days: int = 0          # 0 = use the poll default
    # json-api field mapping
    items_path: str = ""
    title_field: str = "title"
    url_field: str = "url"
    date_field: str = ""
    extra: dict = field(default_factory=dict)

    def resolved_url(self, now: datetime) -> str:
        d = (now - timedelta(days=7)).astimezone(timezone.utc).strftime("%Y-%m-%d")
        return self.url.replace("{date_minus_7d}", d)

    def passes_filters(self, text: str) -> bool:
        if self.include_regex and not re.search(self.include_regex, text):
            return False
        if self.exclude_regex and re.search(self.exclude_regex, text):
            return False
        return True


def load_sources(path: Path | str = DEFAULT_PATH) -> list[Source]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    known = {f for f in Source.__dataclass_fields__ if f != "extra"}
    out, seen = [], set()
    for row in data.get("source", []):
        for req in ("id", "url", "kind", "domains", "signal", "freshness_slo_days"):
            if req not in row:
                raise ValueError(f"source {row.get('id', '?')}: missing {req}")
        if row["id"] in seen:
            raise ValueError(f"duplicate source id {row['id']}")
        seen.add(row["id"])
        if row["kind"] not in KINDS:
            raise ValueError(f"source {row['id']}: unknown kind {row['kind']}")
        if row.get("role", "primary") not in ROLES:
            raise ValueError(f"source {row['id']}: unknown role")
        bad = [d for d in row["domains"] if d not in DOMAINS]
        if bad:
            raise ValueError(f"source {row['id']}: unknown domains {bad}")
        if not 1 <= int(row["signal"]) <= 5:
            raise ValueError(f"source {row['id']}: signal must be 1-5")
        kw = {k: v for k, v in row.items() if k in known}
        kw.setdefault("name", row["id"])
        kw["extra"] = {k: v for k, v in row.items() if k not in known}
        out.append(Source(**kw))
    return out
