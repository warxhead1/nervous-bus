"""digest: weekly markdown under an attention budget, optional kb ingest into the inbox tier only."""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import DOMAINS
from .store import Store, iso

DEFAULT_MAX = 7
FRESH_HOLD = timedelta(hours=24)
KB_PROJECT = "nervous-bus"
HALF_LIFE_DAYS = 90


class KbRefused(RuntimeError):
    pass


def kb_command() -> list[str]:
    return shlex.split(os.environ.get("FRONTIER_RADAR_KB", "kb"))


def kb_preflight(run=subprocess.run) -> list[str]:
    """Refuse unless `kb ingest --help` lists --tier. Without it an ingest would land in the promoted tier."""
    cmd = kb_command()
    try:
        p = run([*cmd, "ingest", "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KbRefused(f"refusing --kb: cannot run `{' '.join(cmd)} ingest --help` ({e})") from e
    if "--tier" not in (p.stdout + p.stderr):
        raise KbRefused("refusing --kb: this kb has no `ingest --tier`, so the digest would enter the promoted tier. "
                        "Upgrade kb, or run the digest without --kb and ingest the file by hand.")
    return cmd


def select(store: Store, now: datetime, days: int, max_items: int, include_fresh: bool = False) -> list[dict]:
    since = iso(now - timedelta(days=days + 1))
    rows = store.events("lane='digest' AND digested_at IS NULL AND first_seen_at >= ?", (since,))
    if not include_fresh:
        rows = [e for e in rows if now - datetime.fromisoformat(e["first_seen_at"].replace("Z", "+00:00")) >= FRESH_HOLD]
    return rows[:max_items]


def render(items: list[dict], now: datetime, days: int, review_count: int) -> str:
    fm = ["---", f"title: Frontier radar digest {now:%Y-%m-%d}", "source_type: external-research", "status: inbox",
          "needs_human_review: true", f"half_life_days: {HALF_LIFE_DAYS}", f"project: {KB_PROJECT}",
          f"created: {now:%Y-%m-%d}", "---", ""]
    body = [f"# Frontier radar digest {now:%Y-%m-%d}", "", f"{len(items)} items from the last {days} days.", ""]
    by_domain: dict[str, list[dict]] = {}
    for e in items:
        by_domain.setdefault(e["triage"]["domain"], []).append(e)
    for d in DOMAINS:
        if d not in by_domain:
            continue
        body += [f"## {d}", ""]
        for e in by_domain[d]:
            t = e["triage"]
            body.append(f"- {t['claim']} {t['why_it_matters']} [{e['primary_source']}]({e['url']}) - **{t['action']}**")
        body.append("")
    if review_count:
        body += [f"{review_count} low-confidence items wait in the review lane (`frontier-radar review`).", ""]
    return "\n".join(fm + body)


def ingest(path: Path, kb_cmd: list[str], run=subprocess.run) -> None:
    p = run([*kb_cmd, "ingest", "--project", KB_PROJECT, "--tier", "inbox", str(path)],
            capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"kb ingest failed: {(p.stderr or p.stdout).strip()[:300]}")


def build_digest(store: Store, now: datetime, days: int = 7, max_items: int = DEFAULT_MAX, out_dir: Path | None = None,
                 kb: bool = False, dry_run: bool = False, include_fresh: bool = False, run=subprocess.run) -> dict:
    kb_cmd = kb_preflight(run) if kb else None
    items = select(store, now, days, max_items, include_fresh)
    review_count = len(store.events("lane='review'"))
    text = render(items, now, days, review_count)
    result = {"text": text, "items": items, "path": None, "review_count": review_count, "kb_ingested": False}
    if dry_run:
        return result
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"digest-{now:%Y-%m-%d}.md"
        path.write_text(text, encoding="utf-8")
        result["path"] = path
        if kb_cmd:
            ingest(path, kb_cmd, run)
            result["kb_ingested"] = True
    for e in items:
        store.update_event(e["event_key"], digested_at=iso(now))
    return result
