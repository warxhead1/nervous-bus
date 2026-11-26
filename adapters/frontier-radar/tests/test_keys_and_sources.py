import json
import re
from pathlib import Path

import pytest

from fr import DOMAINS
from fr.keys import canonical_url, event_key
from fr.sources import KINDS, load_sources

REPO = Path(__file__).resolve().parents[3]


def test_canonical_url_sorts_query_and_drops_utm():
    assert canonical_url("https://www.example.com/p/?utm_medium=a&z=1&a=2#x") == "https://example.com/p?a=2&z=1"


@pytest.mark.parametrize("url,key", [
    ("https://arxiv.org/abs/2611.34363v2", "arxiv:2611.34363"),
    ("https://arxiv.org/pdf/2611.34363", "arxiv:2611.34363"),
    ("https://huggingface.co/papers/2611.34363", "arxiv:2611.34363"),
    ("https://github.com/Anthropics/Claude-Code/releases/tag/v2.1.332", "gh:anthropics/claude-code@v2.1.332"),
    ("https://github.com/anthropics/claude-code/", "gh:anthropics/claude-code"),
    ("https://huggingface.co/microsoft/TRELLIS.2-4B", "hf:microsoft/TRELLIS.2-4B"),
    ("https://huggingface.co/blog/some-post", "https://huggingface.co/blog/some-post"),
    ("https://example.com/post/?utm_source=hn", "https://example.com/post"),
])
def test_event_key(url, key):
    assert event_key(url) == key


def test_registry_is_valid_and_sized():
    sources = load_sources()
    enabled = [s for s in sources if s.enabled]
    assert 25 <= len(enabled) <= 40
    assert len({s.id for s in sources}) == len(sources)
    assert all(s.kind in KINDS for s in sources)
    assert {d for s in sources for d in s.domains} <= set(DOMAINS)


def test_registry_excludes_reddit_and_paid_keys():
    for s in load_sources():
        assert "reddit" not in s.url.lower()
        assert not re.search(r"(api[_-]?key|apikey|token)=", s.url, re.I)
    text = (Path(__file__).resolve().parent.parent / "sources.toml").read_text()
    assert "x-api-key" not in text.lower()


def test_registry_covers_required_kinds():
    kinds = {s.kind for s in load_sources()}
    assert {"github-releases", "sitemap-lastmod", "page-diff", "hf-daily-papers", "hn-algolia", "bsky-search",
            "github-search", "rss", "atom"} <= kinds


def test_registry_rejects_bad_rows(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[[source]]\nid="a"\nurl="https://x.test"\nkind="nope"\ndomains=["harness"]\nsignal=3\nfreshness_slo_days=1\n')
    with pytest.raises(ValueError, match="unknown kind"):
        load_sources(p)


def test_payload_schemas_exist_and_parse():
    for n in ("research.radar.item.v1.json", "research.radar.digest.v1.json"):
        assert json.loads((REPO / "schemas" / n).read_text())["type"] == "object"
