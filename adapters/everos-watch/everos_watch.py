#!/usr/bin/env python3
"""everos-watch: publish EverOS memory-tree changes to the nervous-bus.

Polls the EverOS markdown memory root, diffs per-file entry sets against the
last observed state, and publishes memory.node.{created,updated,deleted}.v1
through `sdk/shell/nervous publish`. Payloads carry ids and metadata ONLY;
memory content never leaves this process (content is hashed in local state
purely to detect in-place updates).

stdlib only. Polling (not inotify) on purpose: no third-party dependency, the
tree is small, and a stat-signature + debounce makes atomic rename-writes
(`.tmp/` staging then os.replace) collapse into one event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

CHANNEL = "memory.node.{ev}.v1"
IGNORED_TOP = {".index", ".tmp"}
DAILY = {  # directory name -> (kind, user/agent)
    "episodes": "episode",
    ".atomic_facts": "atomic_fact",
    ".foresights": "foresight",
    ".cases": "agent_case",
}
ENTRY_RE = re.compile(r"<!-- entry:(\S+) -->(.*?)<!-- /entry:\1 -->", re.S)
SESSION_RE = re.compile(r"^\*\*session_id\*\*:\s*(.+?)\s*$", re.M)
REDACTED_SEG = "_redacted_"


@dataclass(frozen=True)
class FileInfo:
    app_id: str
    project_id: str
    owner_kind: str
    owner_id: str
    kind: str
    single: Optional[str]  # entry id for single-node files (profile, skill)


def classify(parts: tuple[str, ...]) -> Optional[FileInfo]:
    """Map a root-relative path to its node kind, or None when not a memory file."""
    if len(parts) < 5 or parts[0] in IGNORED_TOP or not parts[-1].endswith(".md"):
        return None
    app = "default" if parts[0] == "default_app" else parts[0]
    proj = "default" if parts[1] == "default_project" else parts[1]
    scope, owner, rest = parts[2], parts[3], parts[4:]
    if scope == "users":
        if rest == ("user.md",):
            return FileInfo(app, proj, "user", owner, "profile", "user")
        if len(rest) == 2 and DAILY.get(rest[0]) and rest[0] != ".cases":
            return FileInfo(app, proj, "user", owner, DAILY[rest[0]], None)
    elif scope == "agents":
        if len(rest) == 2 and rest[0] == ".cases":
            return FileInfo(app, proj, "agent", owner, "agent_case", None)
        if len(rest) == 3 and rest[0] == "skills" and rest[2] == "SKILL.md":
            return FileInfo(app, proj, "agent", owner, "agent_skill", rest[1])
    return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def parse_nodes(info: FileInfo, text: str) -> dict[str, tuple[str, str]]:
    """entry_id -> (content digest, session_id or ''). Content stays local."""
    if info.single is not None:
        m = SESSION_RE.search(text)
        return {info.single: (_digest(text), m.group(1) if m else "")}
    out = {}
    for m in ENTRY_RE.finditer(text):
        s = SESSION_RE.search(m.group(2))
        out[m.group(1)] = (_digest(m.group(2)), s.group(1) if s else "")
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Watcher:
    def __init__(self, root: Path, private_apps=("studio",), debounce: float = 1.0,
                 state_path: Optional[Path] = None):
        self.root = Path(root)
        self.private_apps = set(private_apps)
        self.debounce = debounce
        self.state_path = state_path
        # rel path -> {"sig": [mtime_ns, size], "nodes": {id: [digest, session]}}
        self.state: dict[str, dict] = {}
        self.primed = False
        if state_path and state_path.exists():
            try:
                self.state = json.loads(state_path.read_text())
                self.primed = True
            except (OSError, ValueError):
                self.state = {}

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def _walk(self):
        """Yield (rel_parts, path) for candidate files; prunes .index/.tmp/knowledge."""
        for dirpath, dirs, files in os.walk(self.root):
            rel = Path(dirpath).relative_to(self.root).parts
            if not rel:
                dirs[:] = [d for d in dirs if d not in IGNORED_TOP]
            elif len(rel) == 2:
                dirs[:] = [d for d in dirs if d in ("users", "agents")]
            for fn in files:
                if fn.endswith(".md"):
                    yield rel + (fn,), Path(dirpath) / fn

    def _event(self, ev: str, info: FileInfo, eid: str, session: str, parts, now_iso: str) -> dict:
        redacted = info.app_id in self.private_apps
        md = list(parts)
        d = {
            "app_id": info.app_id, "project_id": info.project_id,
            "owner_kind": info.owner_kind, "kind": info.kind,
            "entry_id": eid, "observed_at": now_iso,
        }
        if redacted:
            md[3] = REDACTED_SEG
            d["redacted"] = True
        else:
            d["owner_id"] = info.owner_id
            if session:
                d["session_id"] = session
        d["md_path"] = "/".join(md)
        return {"channel": CHANNEL.format(ev=ev), "subject": f"{info.app_id}/{info.project_id}", "data": d}

    def poll(self, publish: Callable[[dict], bool], emit_existing: bool = False,
             now: Optional[float] = None) -> int:
        """One scan. Returns events published. A failed publish leaves that file's
        state untouched so the next poll retries (at-least-once)."""
        now = time.time() if now is None else now
        now_iso = _now_iso()
        baseline = not self.primed and not emit_existing
        seen, sent, dirty = set(), 0, False
        for parts, path in self._walk():
            info = classify(parts)
            if info is None:
                continue
            key = "/".join(parts)
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = [st.st_mtime_ns, st.st_size]
            old = self.state.get(key)
            if old and old["sig"] == sig:
                continue
            if now - st.st_mtime < self.debounce:
                continue  # still settling; keep old state, retry next poll
            try:
                nodes = parse_nodes(info, path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            prev = old["nodes"] if old else {}
            events = []
            if not baseline:
                for eid, (dg, sess) in nodes.items():
                    if eid not in prev:
                        events.append(self._event("created", info, eid, sess, parts, now_iso))
                    elif prev[eid][0] != dg:
                        events.append(self._event("updated", info, eid, sess, parts, now_iso))
                for eid, (_, sess) in prev.items():
                    if eid not in nodes:
                        events.append(self._event("deleted", info, eid, sess, parts, now_iso))
            if all(publish(e) for e in events):
                sent += len(events)
                self.state[key] = {"sig": sig, "nodes": {k: list(v) for k, v in nodes.items()}}
                dirty = True
        for key in [k for k in self.state if k not in seen]:
            parts = tuple(key.split("/"))
            info = classify(parts)
            events = [] if baseline or info is None else [
                self._event("deleted", info, eid, sess, parts, now_iso)
                for eid, (_, sess) in self.state[key]["nodes"].items()]
            if all(publish(e) for e in events):
                sent += len(events)
                del self.state[key]
                dirty = True
        self.primed = True
        if dirty:
            self.save()
        return sent


def nervous_publisher(nervous_bin: str) -> Callable[[dict], bool]:
    env = dict(os.environ, NERVOUS_SOURCE="/everos/watch")

    def publish(ev: dict) -> bool:
        r = subprocess.run(
            [nervous_bin, "publish", "--subject", ev["subject"], ev["channel"],
             json.dumps(ev["data"], separators=(",", ":"))],
            env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"everos-watch: publish failed rc={r.returncode}: {r.stderr.strip()[:300]}",
                  file=sys.stderr, flush=True)
        return r.returncode == 0

    return publish


def print_publisher(ev: dict) -> bool:
    print(json.dumps({"channel": ev["channel"], "subject": ev["subject"], "data": ev["data"]}), flush=True)
    return True


def default_nervous() -> str:
    here = Path(__file__).resolve().parents[2] / "sdk" / "shell" / "nervous"
    return os.environ.get("NERVOUS_BIN") or (str(here) if here.exists() else "nervous")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.environ.get("EVEROS_ROOT", "/home/eric/data2/everos/root"))
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--debounce", type=float, default=1.0)
    ap.add_argument("--private-app", action="append", default=None,
                    help="app_id whose owner/session ids are withheld (default: studio)")
    ap.add_argument("--state", default=os.path.expanduser("~/.local/state/everos-watch/state.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print events as JSON lines instead of publishing; never writes state")
    ap.add_argument("--emit-existing", action="store_true",
                    help="treat the first scan as all-created instead of a silent baseline")
    ap.add_argument("--once", action="store_true", help="single scan then exit")
    a = ap.parse_args(argv)
    w = Watcher(Path(a.root), a.private_app or ["studio"], a.debounce,
                None if a.dry_run else Path(a.state))
    pub = print_publisher if a.dry_run else nervous_publisher(default_nervous())
    first = True
    while True:
        w.poll(pub, emit_existing=a.emit_existing and first)
        first = False
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
