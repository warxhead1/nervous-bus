#!/usr/bin/env python3
"""guidance_watch.py — keep a local, diffable copy of external agentic-coding guidance.

Fetches every source in guidance_sources.toml, stores the text under
GUIDANCE_DIR/sources/<id>.md, and records a sha256 per source in state.json.
When a source's hash moves, it writes GUIDANCE_DIR/diffs/<YYYY-MM-DD>/<id>.diff
and appends one line to changes.jsonl. For `kind = "index"` sources (llms.txt)
the change record also lists the pages added and removed, which is how a new
feature page surfaces before anyone has audited against it.

A fetch failure keeps the previous snapshot and marks the source `error`; it
never deletes or blanks a snapshot. The first fetch of a source is recorded as
`new`, not as a change, so a fresh install does not report 25 "changes".

steering_audit.py reads the snapshots (thresholds, failure-pattern crosswalk)
and changes.jsonl (what moved since the last audit). Pure stdlib.

    guidance_watch.py [--sources F] [--dir D] [--only ID ...] [--json]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES_TOML = HERE / "guidance_sources.toml"
GUIDANCE_DIR = Path.home() / ".cache" / "nervous-bus" / "reflex" / "guidance"
FETCH_TIMEOUT = 30
USER_AGENT = "nervous-bus-guidance-watch/1 (+https://github.com/warxhead1/nervous-bus)"
_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


def load_config(path: Path = SOURCES_TOML) -> dict:
    cfg = tomllib.loads(path.read_text())
    for s in cfg.get("source", []):
        s.setdefault("kind", "doc")
    return cfg


def normalize(text: str) -> str:
    """Strip trailing whitespace per line so cosmetic churn is not a change."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip() + "\n"


def fetch(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def index_links(text: str) -> set[str]:
    return set(_LINK.findall(text))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state(d: Path) -> dict:
    p = d / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def snapshot_path(d: Path, source_id: str) -> Path:
    return d / "sources" / f"{source_id}.md"


def update_source(src: dict, d: Path, state: dict, fetcher=fetch) -> dict:
    """Fetch one source, update its snapshot/state, and return a result record."""
    sid, now = src["id"], _now()
    prev = state.get(sid, {})
    rec = {"id": sid, "url": src["url"], "kind": src["kind"]}
    try:
        text = normalize(fetcher(src["url"]))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        state[sid] = {**prev, "status": "error", "error": str(e)[:200], "checked_at": now}
        return {**rec, "status": "error", "error": str(e)[:200]}
    sha = hashlib.sha256(text.encode()).hexdigest()
    snap = snapshot_path(d, sid)
    old_text = snap.read_text() if snap.exists() else None
    base = {"sha256": sha, "bytes": len(text.encode()), "checked_at": now,
            "fetched_at": now, "url": src["url"], "kind": src["kind"], "error": None}
    if old_text is None:
        status = "new"
        base["changed_at"] = now
    elif prev.get("sha256") == sha and old_text == text:
        state[sid] = {**prev, **base, "status": "unchanged",
                      "changed_at": prev.get("changed_at", now)}
        return {**rec, "status": "unchanged"}
    else:
        status = "changed"
        base["changed_at"] = now
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(text)
    state[sid] = {**base, "status": status}
    rec["status"] = status
    if status == "changed":
        diff = list(difflib.unified_diff(old_text.splitlines(), text.splitlines(),
                                         f"{sid}@{prev.get('sha256', '')[:12]}",
                                         f"{sid}@{sha[:12]}", lineterm=""))
        ddir = d / "diffs" / now[:10]
        ddir.mkdir(parents=True, exist_ok=True)
        dpath = ddir / f"{sid}.diff"
        dpath.write_text("\n".join(diff) + "\n")
        rec.update(old_sha256=prev.get("sha256"), sha256=sha, diff=str(dpath),
                   added_lines=sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
                   removed_lines=sum(1 for l in diff if l.startswith("-") and not l.startswith("---")))
        if src["kind"] == "index":
            old_l, new_l = index_links(old_text), index_links(text)
            rec["pages_added"] = sorted(new_l - old_l)
            rec["pages_removed"] = sorted(old_l - new_l)
        with open(d / "changes.jsonl", "a") as f:
            f.write(json.dumps({"ts": now, **rec}) + "\n")
    return rec


def run(sources_toml: Path = SOURCES_TOML, d: Path = GUIDANCE_DIR,
        only: list[str] | None = None, fetcher=fetch) -> list[dict]:
    cfg = load_config(sources_toml)
    d.mkdir(parents=True, exist_ok=True)
    state = _load_state(d)
    results = [update_source(s, d, state, fetcher)
               for s in cfg.get("source", []) if not only or s["id"] in only]
    (d / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sources", type=Path, default=SOURCES_TOML)
    ap.add_argument("--dir", type=Path, default=GUIDANCE_DIR)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    results = run(a.sources, a.dir, a.only)
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            extra = f" +{r['added_lines']}/-{r['removed_lines']}" if r["status"] == "changed" else ""
            if r.get("pages_added"):
                extra += f" pages+{len(r['pages_added'])}"
            print(f"{r['status']:9} {r['id']}{extra}{' ' + r['error'] if r.get('error') else ''}")
    # A fetch error is reported, not fatal: one dead source must not fail the batch.
    return 0


if __name__ == "__main__":
    sys.exit(main())
