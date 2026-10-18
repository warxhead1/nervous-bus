"""guidance_watch: snapshot, hash, diff, and index page tracking without the network."""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import guidance_watch as gw

TOML = '''
[[source]]
id = "doc"
url = "https://example.test/doc.md"

[[source]]
id = "idx"
kind = "index"
url = "https://example.test/llms.txt"
'''


def _run(tmp_path: Path, pages: dict) -> dict:
    cfg = tmp_path / "s.toml"
    cfg.write_text(TOML)

    def fetcher(url):
        v = pages[url]
        if isinstance(v, Exception):
            raise v
        return v
    return {r["id"]: r for r in gw.run(cfg, tmp_path / "g", fetcher=fetcher)}


def test_first_fetch_is_new_not_a_change(tmp_path):
    r = _run(tmp_path, {"https://example.test/doc.md": "a\n", "https://example.test/llms.txt": "- [A](https://x/a.md)\n"})
    assert {v["status"] for v in r.values()} == {"new"}
    assert not (tmp_path / "g" / "changes.jsonl").exists()


def test_trailing_whitespace_is_not_a_change(tmp_path):
    pages = {"https://example.test/doc.md": "a\nb\n", "https://example.test/llms.txt": "x\n"}
    _run(tmp_path, pages)
    pages["https://example.test/doc.md"] = "a   \nb\n\n"
    assert _run(tmp_path, pages)["doc"]["status"] == "unchanged"


def test_change_writes_diff_and_index_pages(tmp_path):
    pages = {"https://example.test/doc.md": "keep\nold rule\n",
             "https://example.test/llms.txt": "- [A](https://x/a.md)\n"}
    _run(tmp_path, pages)
    pages["https://example.test/doc.md"] = "keep\nnew rule\n"
    pages["https://example.test/llms.txt"] = "- [A](https://x/a.md)\n- [B](https://x/b.md)\n"
    r = _run(tmp_path, pages)
    assert r["doc"]["status"] == "changed"
    assert (r["doc"]["added_lines"], r["doc"]["removed_lines"]) == (1, 1)
    assert "+new rule" in Path(r["doc"]["diff"]).read_text()
    assert r["idx"]["pages_added"] == ["https://x/b.md"] and r["idx"]["pages_removed"] == []
    lines = (tmp_path / "g" / "changes.jsonl").read_text().splitlines()
    assert [json.loads(l)["id"] for l in lines] == ["doc", "idx"]


def test_fetch_error_keeps_snapshot(tmp_path):
    pages = {"https://example.test/doc.md": "body\n", "https://example.test/llms.txt": "x\n"}
    _run(tmp_path, pages)
    pages["https://example.test/doc.md"] = urllib.error.URLError("down")
    r = _run(tmp_path, pages)
    assert r["doc"]["status"] == "error"
    assert (tmp_path / "g" / "sources" / "doc.md").read_text() == "body\n"
    state = json.loads((tmp_path / "g" / "state.json").read_text())
    assert state["doc"]["status"] == "error" and state["doc"]["sha256"]


def test_shipped_watchlist_parses():
    cfg = gw.load_config()
    ids = [s["id"] for s in cfg["source"]]
    assert len(ids) == len(set(ids))
    assert "cc-best-practices" in ids
    assert {t["source"] for t in cfg["threshold"]} <= set(ids)
