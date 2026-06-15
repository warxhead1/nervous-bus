"""transcript_snapshot.py — durable mirror of Claude Code session JSONL transcripts.

Claude Code writes per-session append-only ``*.jsonl`` transcripts under
``~/.claude/projects/<munged-cwd>/<sessionid>.jsonl``.  Dirs whose munged name
contains ``worktrees`` are short-lived git-worktree dirs that Claude Code deletes
whole when the worktree is reaped.  This module mirrors those files into a
durable archive so they survive reaping.

Per-file algorithm (append-only with rotation recovery):

* New src file                  -> full copy; manifest records ``{inode, size}``.
* Same inode, src grew          -> append ``src[old_size:]`` to dst; bump size.
* Same inode, same size         -> no-op.
* Different inode OR src shrank -> overwrite dst from src (rotation/truncation).

Destination is *never* shrunk or pruned; a src file disappearing is a *normal*
event (often a successful reap), not an error and not a deletion in dst.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Tuple

DEFAULT_SRC: str = os.path.expanduser("~/.claude/projects")
DEFAULT_DST: str = os.path.expanduser("~/.cache/nervous-bus/reflex/transcripts")

MANIFEST_NAME = ".manifest.json"


# ── manifest I/O ──────────────────────────────────────────────────────────────

def _manifest_path(dst_root: str) -> str:
    return os.path.join(dst_root, MANIFEST_NAME)


def _load_manifest(dst_root: str) -> Dict[str, Dict[str, int]]:
    """Read the manifest. Missing or corrupt -> empty dict (cold start)."""
    try:
        with open(_manifest_path(dst_root), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # Truth is on disk in src; rebuild as we go rather than crash the run.
        return {}


def _write_manifest_atomic(dst_root: str, manifest: Dict[str, Dict[str, int]]) -> None:
    """Write manifest atomically: temp file in dst_root + os.replace."""
    os.makedirs(dst_root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", dir=dst_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _manifest_path(dst_root))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── directory walk ────────────────────────────────────────────────────────────

def _list_src_dirs(src_root: str) -> Tuple[List[str], List[str]]:
    """Return ``(worktree_dirs, normal_dirs)`` under src_root, each sorted.

    A dir is a "worktree dir" iff its name contains ``worktrees`` (the munged
    name preserves the literal substring).  Worktree-first because those dirs
    are the ones that vanish.
    """
    try:
        entries = os.listdir(src_root)
    except OSError:
        return ([], [])
    worktrees, normals = [], []
    for name in entries:
        full = os.path.join(src_root, name)
        try:
            if not os.path.isdir(full):
                continue
        except OSError:
            continue
        (worktrees if "worktrees" in name else normals).append(full)
    worktrees.sort()
    normals.sort()
    return (worktrees, normals)


def _iter_jsonl(src_dir: str) -> List[str]:
    """Sorted list of ``*.jsonl`` paths directly under src_dir."""
    try:
        names = os.listdir(src_dir)
    except OSError:
        return []
    return sorted(os.path.join(src_dir, n) for n in names if n.endswith(".jsonl"))


# ── file-level mirror ─────────────────────────────────────────────────────────

def _stat_inode_size(path: str) -> Tuple[int, int]:
    return (os.stat(path).st_ino, os.stat(path).st_size)


def _copy_full(src_path: str, dst_path: str) -> int:
    """Overwrite dst with src. Returns bytes written. Creates dst dir."""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(src_path, "rb") as s, open(dst_path, "wb") as d:
        shutil.copyfileobj(s, d)
    return os.path.getsize(dst_path)


def _append_tail(src_path: str, dst_path: str, offset: int) -> int:
    """Append src[offset:] to dst. Returns number of bytes appended."""
    with open(src_path, "rb") as s:
        s.seek(offset)
        chunk = s.read()
    if not chunk:
        return 0
    with open(dst_path, "ab") as d:
        d.write(chunk)
    return len(chunk)


def _process_file(src_path: str, dst_path: str, entry: Dict[str, int]) -> Tuple[str, int]:
    """Mirror one src file into dst.

    Returns ``(action, bytes)``; action is one of
    ``"new" | "appended" | "skipped" | "recopied"``.  Raises on fatal I/O.
    """
    src_inode, src_size = _stat_inode_size(src_path)
    prev_inode = entry.get("inode")
    prev_size = entry.get("size", 0)

    if prev_inode is None:
        return ("new", _copy_full(src_path, dst_path))
    if src_inode != prev_inode or src_size < prev_size:
        return ("recopied", _copy_full(src_path, dst_path))
    if src_size == prev_size:
        return ("skipped", 0)

    # Same inode, grew forward — append-only path. A zero-byte read despite a
    # larger size is treated as skipped so we don't loop on the same offset.
    n = _append_tail(src_path, dst_path, prev_size)
    return ("appended", n) if n else ("skipped", 0)


# ── public entry point ────────────────────────────────────────────────────────

def sync_once(src_root: str = DEFAULT_SRC, dst_root: str = DEFAULT_DST) -> Dict[str, Any]:
    """Incrementally mirror every ``*.jsonl`` under ``src_root`` into ``dst_root``.

    See module docstring for the full algorithm.  A single unreadable or
    vanished file appends a string to ``stats["errors"]`` and the run
    continues.  The destination is never shrunk or pruned.
    """
    stats: Dict[str, Any] = {
        "files_seen": 0, "files_new": 0, "files_appended": 0,
        "files_recopied": 0, "bytes_copied": 0, "errors": [],
    }

    try:
        os.makedirs(dst_root, exist_ok=True)
    except OSError as e:
        stats["errors"].append(f"mkdir dst: {e}")
        return stats

    manifest = _load_manifest(dst_root)
    worktree_dirs, normal_dirs = _list_src_dirs(src_root)
    # process_order records per-file attempt order; tests assert worktree-first.
    process_order: List[str] = []
    updated: Dict[str, Dict[str, int]] = {}

    for src_dir in (*worktree_dirs, *normal_dirs):
        for src_path in _iter_jsonl(src_dir):
            stats["files_seen"] += 1
            rel = os.path.relpath(src_path, src_root)
            process_order.append(rel)
            dst_path = os.path.join(dst_root, rel)
            entry = manifest.get(rel, {})
            try:
                action, nbytes = _process_file(src_path, dst_path, entry)
            except (OSError, IOError) as e:
                # File vanished mid-pass, perm denied, etc. Log and move on;
                # don't update manifest so the next pass retries from baseline.
                stats["errors"].append(f"{rel}: {getattr(e, 'strerror', None) or e}")
                continue
            except Exception as e:  # last-resort; never abort the whole run
                stats["errors"].append(f"{rel}: {type(e).__name__}: {e}")
                continue

            counter = {"new": "files_new", "appended": "files_appended",
                       "recopied": "files_recopied"}.get(action)
            if counter:
                stats[counter] += 1
            stats["bytes_copied"] += nbytes

            if action == "skipped":
                # Preserve prior entry; we did not touch the file.
                updated[rel] = {"inode": entry["inode"], "size": entry["size"]}
            else:
                inode, size = _stat_inode_size(src_path)
                updated[rel] = {"inode": inode, "size": size}

    # Always persist — even an empty pass leaves an on-disk record of truth.
    try:
        merged = dict(manifest)
        merged.update(updated)
        _write_manifest_atomic(dst_root, merged)
    except OSError as e:
        stats["errors"].append(f"manifest write: {e}")

    stats["process_order"] = process_order
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────

def print_stats(dst_root: str = DEFAULT_DST) -> Dict[str, Any]:
    """Print and return a manifest summary: file count + total bytes."""
    manifest = _load_manifest(dst_root)
    out = {
        "dst_root": dst_root,
        "files": len(manifest),
        "bytes": sum(int(v.get("size", 0)) for v in manifest.values()),
    }
    print(json.dumps(out, sort_keys=True))
    return out


def _watch(src_root: str, dst_root: str, seconds: float) -> None:
    """Poll loop: sync_once every ``seconds``, flushing stdout each cycle."""
    if seconds <= 0:
        seconds = 1.0
    while True:
        try:
            stats = sync_once(src_root, dst_root)
        except Exception as e:
            # sync_once already catches per-file errors; reaching this branch
            # means a top-level fault. Log and keep looping for self-healing.
            stats = {"files_seen": 0, "files_new": 0, "files_appended": 0,
                     "files_recopied": 0, "bytes_copied": 0,
                     "errors": [f"top: {type(e).__name__}: {e}"]}
        print(json.dumps(stats, sort_keys=True), flush=True)
        time.sleep(seconds)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="transcript_snapshot.py",
        description="Durable incremental mirror of Claude Code session JSONL transcripts.",
    )
    ap.add_argument("--src", default=DEFAULT_SRC, help="source root")
    ap.add_argument("--dst", default=DEFAULT_DST, help="destination root")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="single sync pass and exit")
    mode.add_argument("--stats", action="store_true", help="manifest summary and exit")
    mode.add_argument("--watch", type=float, metavar="SECONDS", help="poll loop, sync every SECONDS")
    args = ap.parse_args(argv)

    try:
        if args.once:
            print(json.dumps(sync_once(args.src, args.dst), sort_keys=True))
            return 0
        if args.stats:
            print_stats(args.dst)
            return 0
        _watch(args.src, args.dst, args.watch)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # Print to stderr + non-zero exit so systemd/cron can detect breakage.
        print(f"fatal: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
