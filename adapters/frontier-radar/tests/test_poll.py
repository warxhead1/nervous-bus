import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from fr import USER_AGENT
from fr.fetch import Fetcher, Response, Throttle
from fr.poll import check_slo, poll_all, poll_source
from fr.store import Store
from helpers import NOW, FakeFetcher, fixture, json_bytes, no_wait_throttle, src

RELEASES = "https://github.com/anthropics/claude-code/releases.atom"
HN = "https://hn.algolia.com/api/v1/search_by_date?q=1"
BSKY = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=1"
HFP = "https://huggingface.co/api/daily_papers"
TAG = "https://github.com/anthropics/claude-code/releases/tag/v2.1.332"


def releases_src():
    return src("claude-code-releases", "github-releases", RELEASES, "primary", signal=5, slo=14)


def hn_src():
    return src("hn", "hn-algolia", HN, "mirror", signal=5, slo=7)


def test_release_tags_become_events_and_hn_sighting_is_a_mirror_not_a_new_event():
    store = Store(":memory:")
    hn = json.loads(fixture("hn_claude_code.json"))
    hn["hits"][0]["url"] = TAG
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom"), HN: json_bytes(hn)})
    r1 = poll_source(releases_src(), store, f, no_wait_throttle(), NOW)
    assert len(r1.new_events) == 4
    r2 = poll_source(hn_src(), store, f, no_wait_throttle(), NOW)
    assert "gh:anthropics/claude-code@v2.1.332" not in r2.new_events
    key = "gh:anthropics/claude-code@v2.1.332"
    roles = {s["source_id"]: s["role"] for s in store.sightings(key)}
    assert roles == {"claude-code-releases": "primary", "hn": "mirror"}
    ev = store.event(key)
    assert ev["primary_source"] == "claude-code-releases"
    assert len(store.events("event_key=?", (key,))) == 1


def test_mirror_seen_first_is_upgraded_when_the_primary_arrives():
    store = Store(":memory:")
    hn = json.loads(fixture("hn_claude_code.json"))
    hn["hits"][0]["url"] = TAG
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom"), HN: json_bytes(hn)})
    poll_source(hn_src(), store, f, no_wait_throttle(), NOW)
    key = "gh:anthropics/claude-code@v2.1.332"
    assert store.event(key)["primary_role"] == "mirror"
    poll_source(releases_src(), store, f, no_wait_throttle(), NOW)
    ev = store.event(key)
    assert (ev["primary_source"], ev["primary_role"]) == ("claude-code-releases", "primary")
    assert ev["title"] == "v2.1.332"


def test_paper_dedups_across_hf_daily_and_hn_by_arxiv_id():
    store = Store(":memory:")
    papers = json.loads(fixture("hf_daily_papers.json"))
    pid = papers[0]["paper"]["id"]
    hn = {"hits": [{"objectID": "9", "title": "A paper", "url": f"https://arxiv.org/pdf/{pid}v1",
                    "points": 80, "num_comments": 5, "created_at": "2026-11-26T10:00:00Z"}]}
    f = FakeFetcher({HFP: json_bytes(papers), HN: json_bytes(hn)})
    hf = src("hf-daily", "hf-daily-papers", HFP, "mirror", ("research",), 5)
    poll_source(hf, store, f, no_wait_throttle(), NOW)
    poll_source(hn_src(), store, f, no_wait_throttle(), NOW)
    assert len(store.events("event_key=?", (f"arxiv:{pid}",))) == 1
    assert len(store.sightings(f"arxiv:{pid}")) == 2
    assert len(store.events()) == 3


def test_bot_sighting_is_tagged_bot_mirror_of_the_primary():
    store = Store(":memory:")
    bsky = json.loads(fixture("bsky_search.json"))
    p = bsky["posts"][0]
    p["author"]["handle"] = "claudecodechanges.bsky.social"
    p["embed"] = {"external": {"uri": TAG}}
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom"), BSKY: json_bytes(bsky)})
    poll_source(releases_src(), store, f, no_wait_throttle(), NOW)
    poll_source(src("bsky", "bsky-search", BSKY, "mirror", signal=2), store, f, no_wait_throttle(), NOW)
    roles = {s["source_id"]: s["role"] for s in store.sightings("gh:anthropics/claude-code@v2.1.332")}
    assert roles["bsky"] == "bot-mirror"


def test_filters_drop_prerelease_tags_and_old_items():
    store = Store(":memory:")
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom")})
    s = releases_src()
    s.exclude_regex = "v2.1.33[12]"
    r = poll_source(s, store, f, no_wait_throttle(), NOW)
    assert sorted(r.new_events) == ["gh:anthropics/claude-code@v2.1.329", "gh:anthropics/claude-code@v2.1.330"]
    store2 = Store(":memory:")
    r = poll_source(releases_src(), store2, f, no_wait_throttle(), NOW + timedelta(days=40))
    assert r.new_events == []


def test_conditional_get_sends_validators_and_skips_parse_on_304():
    store = Store(":memory:")
    seen = []

    def page(etag, lm):
        seen.append((etag, lm))
        if etag:
            return Response(304, b"", etag=etag, last_modified=lm, final_url=RELEASES)
        return Response(200, fixture("claude_code_releases.atom"), etag='"abc"',
                        last_modified="Wed, 25 Nov 2026 01:00:00 GMT", final_url=RELEASES)

    f = FakeFetcher({RELEASES: page})
    s = releases_src()
    poll_source(s, store, f, no_wait_throttle(), NOW)
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=4))
    assert seen == [("", ""), ('"abc"', "Wed, 25 Nov 2026 01:00:00 GMT")]
    assert r.outcome == "not-modified" and r.new_events == []
    assert store.source_state(s.id)["last_status"] == "304"


def test_per_source_rate_limit_skips_a_recent_poll():
    store = Store(":memory:")
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom")})
    s = releases_src()
    poll_source(s, store, f, no_wait_throttle(), NOW)
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(minutes=30))
    assert r.outcome == "skipped" and len(f.calls) == 1
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(minutes=30), force=True)
    assert r.outcome != "skipped" and len(f.calls) == 2


def test_throttle_sleeps_between_requests_to_one_host():
    slept, t = [], [0.0]
    th = Throttle(sleep=slept.append, clock=lambda: t[0])
    th.wait("https://arxiv.org/a", 3)
    t[0] = 1.0
    th.wait("https://arxiv.org/b", 3)
    th.wait("https://other.test/c", 3)
    assert slept == [2.0]


def test_redirect_raises_an_alarm_and_clears_when_it_stops():
    store = Store(":memory:")
    moved = Response(200, fixture("claude_code_releases.atom"), final_url="https://new.test/feed",
                     redirects=[(301, RELEASES, "https://new.test/feed")])
    f = FakeFetcher({RELEASES: moved})
    s = releases_src()
    poll_source(s, store, f, no_wait_throttle(), NOW)
    [al] = store.open_alarms()
    assert al["kind"] == "redirect" and "https://new.test/feed" in al["detail"]
    f.pages[RELEASES] = fixture("claude_code_releases.atom")
    poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=5))
    assert store.open_alarms() == []


def test_freshness_slo_alarm_fires_when_a_source_goes_silent_then_clears():
    store = Store(":memory:")
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom")})
    s = releases_src()
    poll_source(s, store, f, no_wait_throttle(), NOW)
    assert check_slo(s, store, NOW + timedelta(days=5)) is None
    msg = check_slo(s, store, NOW + timedelta(days=20))
    assert msg and "SLO 14" in msg
    assert [a["kind"] for a in store.open_alarms()] == ["stale"]
    newer = fixture("claude_code_releases.atom").replace(b"2026-11-25T01:32:14Z", b"2026-12-14T01:32:14Z")
    f.pages[RELEASES] = newer
    poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(days=20), force=True)
    assert check_slo(s, store, NOW + timedelta(days=20)) is None
    assert store.open_alarms() == []


def test_poll_all_raises_stale_alarm_for_a_dead_feed():
    store = Store(":memory:")
    f = FakeFetcher({RELEASES: fixture("claude_code_releases.atom")})
    poll_all([releases_src()], store, f, no_wait_throttle(), NOW + timedelta(days=60))
    assert [a["kind"] for a in store.open_alarms()] == ["stale"]


def test_repeated_failures_raise_an_error_alarm():
    store = Store(":memory:")
    f = FakeFetcher({RELEASES: Response(503, final_url=RELEASES, error="HTTP 503")})
    s = releases_src()
    for i in range(3):
        poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=5 * i))
    assert [a["kind"] for a in store.open_alarms()] == ["error"]


def test_disabled_source_is_not_polled():
    store = Store(":memory:")
    f = FakeFetcher({})
    s = releases_src()
    s.enabled = False
    assert poll_all([s], store, f, no_wait_throttle(), NOW) == []


def test_page_diff_baselines_then_emits_only_added_lines():
    store = Store(":memory:")
    url = "https://aistudio.example.test/changelog"
    s = src("gemini-changelog", "page-diff", url, signal=5)
    f = FakeFetcher({url: fixture("changelog_v1.html")})
    r = poll_source(s, store, f, no_wait_throttle(), NOW)
    assert r.outcome == "baseline" and r.new_events == []
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=6))
    assert r.new_events == []
    f.pages[url] = fixture("changelog_v2.html")
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=12))
    [key] = r.new_events
    ev = store.event(key)
    assert key.startswith("page:https://aistudio.example.test/changelog@")
    assert "deprecated and ignored" in ev["summary"]
    assert "Gemini 4 Neon" not in ev["summary"]
    assert "tracking" not in ev["summary"]


def test_sitemap_first_run_honors_lookback_then_reports_lastmod_changes():
    store = Store(":memory:")
    url = "https://www.anthropic.example/sitemap.xml"
    s = src("anthropic", "sitemap-lastmod", url, signal=5, path_regex="/(news|engineering)/", lookback_days=14)
    xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
           "<url><loc>https://www.anthropic.example/news/new-post</loc><lastmod>2026-11-20T10:00:00Z</lastmod></url>"
           "<url><loc>https://www.anthropic.example/news/old-post</loc><lastmod>2026-08-01T10:00:00Z</lastmod></url>"
           "<url><loc>https://www.anthropic.example/legal/terms</loc><lastmod>2026-11-25T10:00:00Z</lastmod></url>"
           "</urlset>").encode()
    f = FakeFetcher({url: xml})
    r = poll_source(s, store, f, no_wait_throttle(), NOW)
    assert r.new_events == ["https://anthropic.example/news/new-post"]
    xml2 = xml.replace(b"2026-08-01T10:00:00Z", b"2026-11-26T10:00:00Z")
    f.pages[url] = xml2
    r = poll_source(s, store, f, no_wait_throttle(), NOW + timedelta(hours=6))
    assert r.new_events == ["https://anthropic.example/news/old-post"]


def test_sitemap_bulk_lastmod_bump_is_not_news():
    store = Store(":memory:")
    url = "https://www.anthropic.example/sitemap.xml"
    s = src("anthropic", "sitemap-lastmod", url, signal=5, path_regex="/news/", lookback_days=30, max_items=50)
    bulk = "".join(f"<url><loc>https://www.anthropic.example/news/p{i}</loc><lastmod>2026-11-13T09:00:00Z</lastmod></url>"
                   for i in range(30))
    real = "<url><loc>https://www.anthropic.example/news/real</loc><lastmod>2026-11-25T09:00:00Z</lastmod></url>"
    xml = f"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{bulk}{real}</urlset>".encode()
    r = poll_source(s, store, FakeFetcher({url: xml}), no_wait_throttle(), NOW)
    assert r.new_events == ["https://anthropic.example/news/real"]
    assert "bulk" in r.detail


def test_real_sitemap_fixture_parses_into_news_items():
    store = Store(":memory:")
    url = "https://www.anthropic.com/sitemap.xml"
    s = src("anthropic", "sitemap-lastmod", url, signal=5, path_regex="/(news|engineering|research)/", lookback_days=400)
    r = poll_source(s, store, FakeFetcher({url: fixture("anthropic_sitemap.xml")}), no_wait_throttle(), NOW)
    assert r.new_events and all("/news/" in k or "/engineering/" in k or "/research/" in k for k in r.new_events)


class _Handler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _Handler.hits.append((self.path, dict(self.headers)))
        if self.path == "/old":
            self.send_response(301)
            self.send_header("Location", "/feed")
            self.end_headers()
        elif self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Last-Modified", "Wed, 25 Nov 2026 00:00:00 GMT")
            self.end_headers()
            self.wfile.write(b"<rss/>")

    def log_message(self, *a):
        pass


def test_real_fetcher_over_loopback_conditional_get_redirect_and_user_agent():
    _Handler.hits.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        fx = Fetcher(timeout=5)
        first = fx.get(base + "/feed")
        assert first.status == 200 and first.etag == '"v1"'
        again = fx.get(base + "/feed", etag=first.etag, last_modified=first.last_modified)
        assert again.not_modified
        moved = fx.get(base + "/old")
        assert moved.status == 200 and moved.redirects == [(301, base + "/old", base + "/feed")]
    finally:
        srv.shutdown()
    assert all(h["User-Agent"] == USER_AGENT for _, h in _Handler.hits)
    assert "@" not in USER_AGENT and "eric" not in USER_AGENT.lower()


def test_malformed_feed_is_a_source_error_and_the_poll_continues():
    store = Store(":memory:")
    bad = src("bad-feed", "rss", "https://example.test/bad")
    f = FakeFetcher({"https://example.test/bad": b"<html><body>not a feed", RELEASES: fixture("claude_code_releases.atom")})
    results = poll_all([bad, releases_src()], store, f, no_wait_throttle(), NOW)
    by_id = {r.source_id: r for r in results}
    assert by_id["bad-feed"].outcome == "error"
    assert "parse error" in by_id["bad-feed"].detail
    assert len(by_id["claude-code-releases"].new_events) == 4
