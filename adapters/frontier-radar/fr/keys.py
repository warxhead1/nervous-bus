"""Event keys: one key per primary artifact, so mirrors collapse onto it."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "ref", "ref_src", "from", "source", "mc_cid", "mc_eid", "igshid", "s"}
HF_RESERVED = {"blog", "papers", "api", "docs", "datasets", "spaces", "collections", "models", "posts",
               "organizations", "learn", "join", "login", "settings", "tasks", "chat"}
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/|^arxiv:)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
GH_TAG_RE = re.compile(r"^/([^/]+)/([^/]+)/releases/tag/(.+)$")
GH_REPO_RE = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?/?$")


def canonical_url(url: str) -> str:
    """Strip tracking, fragments, www, default ports and trailing slashes; sort the query."""
    u = urlsplit(url.strip())
    scheme = "https" if u.scheme in ("http", "https", "") else u.scheme
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if u.port and u.port not in (80, 443):
        host = f"{host}:{u.port}"
    query = sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    path = u.path.rstrip("/") or ""
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def event_key(url: str) -> str:
    """Key on the primary artifact: arXiv id, GitHub owner/repo@tag, HF model id, else canonical URL."""
    m = ARXIV_RE.search(url)
    if m:
        return f"arxiv:{m.group(1)}"
    canon = canonical_url(url)
    u = urlsplit(canon)
    host = u.netloc
    if host == "github.com":
        m = GH_TAG_RE.match(u.path)
        if m:
            return f"gh:{m.group(1).lower()}/{m.group(2).lower()}@{m.group(3)}"
        m = GH_REPO_RE.match(u.path)
        if m:
            return f"gh:{m.group(1).lower()}/{m.group(2).lower()}"
    if host == "huggingface.co":
        parts = [p for p in u.path.split("/") if p]
        if len(parts) == 2 and parts[0] not in HF_RESERVED:
            return f"hf:{parts[0]}/{parts[1]}"
    return canon
