"""Shared test doubles. No test touches the network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fr.fetch import Response, Throttle
from fr.sources import Source

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def src(id="s", kind="rss", url="https://example.test/feed", role="primary", domains=("harness",), signal=4,
        slo=14, **kw) -> Source:
    return Source(id=id, name=kw.pop("name", id), url=url, kind=kind, domains=list(domains), signal=signal,
                  freshness_slo_days=slo, role=role, **kw)


class FakeFetcher:
    """Serves canned responses per URL and records the validators it was sent."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[tuple[str, str, str]] = []

    def get(self, url, etag="", last_modified=""):
        self.calls.append((url, etag, last_modified))
        page = self.pages[url]
        if callable(page):
            page = page(etag, last_modified)
        if isinstance(page, Response):
            return page
        return Response(200, page, etag='"v1"', last_modified="Wed, 25 Nov 2026 00:00:00 GMT", final_url=url)


def no_wait_throttle() -> Throttle:
    return Throttle(sleep=lambda s: None, clock=lambda: 0.0)


def json_bytes(obj) -> bytes:
    return json.dumps(obj).encode()
