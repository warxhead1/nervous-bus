"""HTTP: conditional GET, redirect recording, per-host throttle. Stdlib only."""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from . import USER_AGENT

MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 4


@dataclass
class Response:
    status: int
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""
    final_url: str = ""
    redirects: list = field(default_factory=list)  # [(status, from_url, to_url)]
    error: str = ""

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    """Follows redirects by hand so each hop is recorded for the redirect alarm."""

    def __init__(self, timeout: float = 30, user_agent: str = USER_AGENT):
        self.timeout = timeout
        self.user_agent = user_agent
        self.opener = urllib.request.build_opener(_NoRedirect)

    def get(self, url: str, etag: str = "", last_modified: str = "") -> Response:
        redirects: list = []
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            headers = {"User-Agent": self.user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            req = urllib.request.Request(cur, headers=headers)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    return Response(r.status, r.read(MAX_BYTES), r.headers.get("ETag", ""),
                                    r.headers.get("Last-Modified", ""), cur, redirects)
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return Response(304, b"", e.headers.get("ETag", etag),
                                    e.headers.get("Last-Modified", last_modified), cur, redirects)
                loc = e.headers.get("Location")
                if e.code in (301, 302, 303, 307, 308) and loc:
                    nxt = urljoin(cur, loc)
                    redirects.append((e.code, cur, nxt))
                    cur = nxt
                    continue
                return Response(e.code, b"", final_url=cur, redirects=redirects, error=f"HTTP {e.code}")
            except (OSError, ValueError, http.client.HTTPException) as e:
                return Response(0, b"", final_url=cur, redirects=redirects, error=f"{type(e).__name__}: {e}")
        return Response(0, b"", final_url=cur, redirects=redirects, error="too many redirects")


class Throttle:
    """Minimum gap between requests to one host."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.last: dict[str, float] = {}

    def wait(self, url: str, delay_s: float) -> None:
        host = urlsplit(url).netloc
        prev = self.last.get(host)
        if prev is not None:
            gap = delay_s - (self.clock() - prev)
            if gap > 0:
                self.sleep(gap)
        self.last[host] = self.clock()
