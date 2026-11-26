#!/usr/bin/env python3
"""steering-hits tailer: publish each line of the steering hit log once.

Steering plugins append one JSON record per finding to a local file (see
schemas/agent.steering.hit.v1.json) and never call the bus. This adapter reads
that file and runs `nervous publish agent.steering.hit.v1 '<record>'` for every new
line, so the schema validation and the file-plus-Redis delivery of the shell SDK
apply unchanged.

Delivery is at-least-once with a checkpoint written after every published line:
a crash between publish and checkpoint re-sends at most one record. The
checkpoint holds the inode and byte offset of the file being read. When the
writer rotates the file to `<name>.1`, the tailer finishes the rotated file from
its saved offset and then starts the new file at 0.

    tailer.py [--hits PATH] [--state PATH] [--nervous BIN]

Exit 0 when caught up, 1 when a publish failed (the checkpoint stays put, the
next run retries). A malformed line is skipped and counted, never retried.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CHANNEL = "agent.steering.hit.v1"
REPO_NERVOUS = Path(__file__).resolve().parents[2] / "sdk" / "shell" / "nervous"
PUBLISH_TIMEOUT_S = 30


def default_hits_path() -> Path:
    env = os.environ.get("AGENT_STEERING_HITS")
    if env:
        return Path(env)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "agent-steering" / "hits.jsonl"


def default_state_path() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "nervous-bus" / "steering-hits" / "checkpoint.json"


def find_nervous(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("NERVOUS_BIN")
    if env:
        return env
    if REPO_NERVOUS.is_file():
        return str(REPO_NERVOUS)
    return shutil.which("nervous") or "nervous"


def load_checkpoint(path: Path) -> dict | None:
    try:
        cp = json.loads(path.read_text())
        return {"inode": int(cp["inode"]), "offset": int(cp["offset"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_checkpoint(path: Path, inode: int, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"inode": inode, "offset": offset}))
    os.replace(tmp, path)


def publish(nervous: str, line: str) -> bool:
    env = dict(os.environ, NERVOUS_SOURCE="/steering-hits")
    try:
        r = subprocess.run(
            [nervous, "publish", CHANNEL, line],
            capture_output=True, text=True, timeout=PUBLISH_TIMEOUT_S, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"steering-hits: publish failed: {exc}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"steering-hits: publish exit {r.returncode}: {r.stderr.strip()[:200]}",
              file=sys.stderr)
        return False
    return True


def drain(path: Path, offset: int, inode: int, state: Path, nervous: str, stats: dict) -> bool:
    """Publish complete lines of `path` from `offset`. False when a publish failed."""
    try:
        f = open(path, "rb")
    except OSError:
        return True
    with f:
        if os.fstat(f.fileno()).st_size < offset:
            offset = 0  # truncated in place
        f.seek(offset)
        while True:
            raw = f.readline()
            if not raw.endswith(b"\n"):
                return True  # EOF or a half-written last line: leave for next run
            offset += len(raw)
            text = raw.decode("utf-8", "replace").strip()
            if text:
                try:
                    ok = isinstance(json.loads(text), dict)
                except ValueError:
                    ok = False
                if not ok:
                    stats["skipped"] += 1
                elif publish(nervous, text):
                    stats["published"] += 1
                else:
                    return False
            save_checkpoint(state, inode, offset)


def run(hits: Path, state: Path, nervous: str) -> int:
    stats = {"published": 0, "skipped": 0}
    rotated = hits.with_name(hits.name + ".1")
    cp = load_checkpoint(state)
    try:
        cur_inode = hits.stat().st_ino
    except OSError:
        cur_inode = None

    if cp is None:
        # First run: read everything that exists, oldest file first.
        if rotated.exists():
            if not drain(rotated, 0, rotated.stat().st_ino, state, nervous, stats):
                return 1
        start_inode, start_off = cur_inode, 0
    elif cur_inode is not None and cp["inode"] == cur_inode:
        start_inode, start_off = cur_inode, cp["offset"]
    else:
        # The file we were reading is gone or replaced. Finish it if it is now `.1`.
        if rotated.exists() and rotated.stat().st_ino == cp["inode"]:
            if not drain(rotated, cp["offset"], cp["inode"], state, nervous, stats):
                return 1
        start_inode, start_off = cur_inode, 0

    if start_inode is not None:
        if not drain(hits, start_off, start_inode, state, nervous, stats):
            print(json.dumps(stats))
            return 1
    print(json.dumps(stats))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hits", type=Path, default=default_hits_path())
    ap.add_argument("--state", type=Path, default=default_state_path())
    ap.add_argument("--nervous", help="path to the nervous CLI (default: repo sdk/shell/nervous)")
    args = ap.parse_args(argv)
    return run(args.hits, args.state, find_nervous(args.nervous))


if __name__ == "__main__":
    sys.exit(main())
