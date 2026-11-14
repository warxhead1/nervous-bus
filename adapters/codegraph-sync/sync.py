#!/usr/bin/env python3
"""codegraph-sync: keep opted-in codegraph indexes fresh, debounced.

A systemd user timer runs this every 5 minutes. For each repo listed under
`codegraph.repos` in kb's config (~/.config/kb/config.yaml) that has a
`.codegraph/` index, it fingerprints the checkout (HEAD, porcelain status,
mtimes of dirty paths). A changed fingerprint is synced once it has held
still for one tick, or once it has been pending for MAX_PENDING_S during
continuous edits. Each sync publishes codegraph.index.synced.v1.

    python3 sync.py            # one pass
    python3 sync.py --dry-run  # decide and report, never sync or publish
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

CONFIG = Path(os.environ.get("CODEGRAPH_SYNC_CONFIG", str(Path.home() / ".config" / "kb" / "config.yaml")))
CACHE_DIR = Path(os.environ.get("CODEGRAPH_SYNC_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "codegraph-sync")))
STATE_FILE = CACHE_DIR / "state.json"
CODEGRAPH = os.environ.get("CODEGRAPH_BIN", "codegraph")
NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN", str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"))
CHANNEL = "codegraph.index.synced.v1"
MAX_PENDING_S = 30 * 60
SYNC_TIMEOUT_S = 600

GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def load_repos(config: Path = CONFIG) -> list[dict]:
    """[{name, path}] from kb config's `codegraph.repos`; [] if absent."""
    try:
        import yaml
        data = yaml.safe_load(config.read_text()) or {}
    except Exception as e:  # missing file, bad YAML, no PyYAML
        sys.stderr.write(f"[codegraph-sync] cannot read {config}: {e}\n")
        return []
    repos = ((data.get("codegraph") or {}).get("repos")) or []
    return [r for r in repos if isinstance(r, dict) and r.get("name") and r.get("path")]


def git(repo: Path, *args: str) -> Optional[bytes]:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           env=GIT_ENV, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def fingerprint(repo: Path, git_fn: Callable[..., Optional[bytes]] = git) -> Optional[dict]:
    """HEAD plus a digest of the dirty tree. Porcelain alone misses a second
    edit to an already-modified file, so dirty paths' mtimes go in too."""
    head = git_fn(repo, "rev-parse", "HEAD")
    status = git_fn(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if head is None or status is None:
        return None
    h = hashlib.sha256(status)
    for entry in status.split(b"\0"):
        if len(entry) > 3:
            try:
                h.update(str((repo / entry[3:].decode(errors="replace")).stat().st_mtime_ns).encode())
            except OSError:
                h.update(b"-")
    return {"head": head.decode().strip(), "tree": h.hexdigest()[:16]}


def decide(st: dict, fp: dict, now: float) -> Optional[str]:
    """Update one repo's state in place; return the sync trigger or None."""
    if fp == st.get("synced"):
        st.pop("pending", None)
        st.pop("pending_since", None)
        return None
    if st.get("pending") == fp:
        return "quiescent"
    since = st.get("pending_since", now)
    st["pending"], st["pending_since"] = fp, since
    if now - since >= MAX_PENDING_S:
        return "max_pending"
    return None


def run_sync(repo: Path) -> tuple[bool, int]:
    start = time.monotonic()
    try:
        ok = subprocess.run([CODEGRAPH, "sync", "-q", str(repo)], capture_output=True,
                            timeout=SYNC_TIMEOUT_S, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return ok, int((time.monotonic() - start) * 1000)


def publish(payload: dict) -> None:
    try:
        subprocess.run([NERVOUS_BIN, "publish", CHANNEL, json.dumps(payload)],
                       check=True, capture_output=True, text=True, timeout=10)
    except Exception as e:
        sys.stderr.write(f"[codegraph-sync] publish failed: {e}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def run_pass(*, dry_run: bool, now: Optional[float] = None,
             repos: Optional[list[dict]] = None,
             fingerprint_fn=fingerprint, sync_fn=run_sync, publish_fn=publish,
             state: Optional[dict] = None) -> tuple[dict, list[dict]]:
    now = time.time() if now is None else now
    state = load_state() if state is None else state
    events = []
    for spec in load_repos() if repos is None else repos:
        name, repo = spec["name"], Path(spec["path"])
        if not (repo / ".codegraph" / "codegraph.db").is_file():
            continue  # opted in but not indexed: nothing to keep fresh
        fp = fingerprint_fn(repo)
        if fp is None:
            continue
        st = state.setdefault(name, {})
        trigger = decide(st, fp, now)
        if trigger is None:
            continue
        if dry_run:
            print(f"[codegraph-sync] (dry-run) would sync {name} ({trigger})")
            continue
        ok, ms = sync_fn(repo)
        pending_s = int(now - st.get("pending_since", now))
        if ok:
            st["synced"] = fp
            st.pop("pending", None)
            st.pop("pending_since", None)
        st["last"] = {"ok": ok, "duration_ms": ms, "at": int(now)}
        event = {"repo": name, "path": str(repo), "head": fp["head"], "ok": ok,
                 "duration_ms": ms, "trigger": trigger, "pending_s": pending_s}
        publish_fn(event)
        events.append(event)
        print(f"[codegraph-sync] {name}: sync ok={ok} {ms}ms trigger={trigger}")
    if not dry_run:
        save_state(state)
    return state, events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_pass(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
