"""
CLI tool: scan all event sources, report on invalidation state.

Usage:
    python tools/invalidation_scanner.py --report
    python tools/invalidation_scanner.py --check "ahe:session123:problem456:3"
    python tools/invalidation_scanner.py --report --store-path /tmp/my_store.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _resolve_store_path(path_arg: Optional[str]) -> Path:
    """Resolve the store path, falling back to the default."""
    if path_arg:
        return Path(path_arg)
    return Path.home() / ".cache" / "nervous-bus" / "invalidation_store.jsonl"


def report_all(store_path: Path) -> None:
    """Print all known invalidated scopes, sorted by scope key."""
    if not store_path.exists():
        print(f"(store not found at {store_path})")
        return

    entries: list[dict[str, object]] = []
    with open(store_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue

    if not entries:
        print("(no invalidation entries recorded)")
        return

    # Deduplicate to latest entry per scope_key
    latest: dict[str, dict[str, object]] = {}
    for entry in entries:
        sk = str(entry.get("scope_key", ""))
        if sk:
            latest[sk] = entry  # later lines overwrite earlier

    print(f"{'Scope Key':<55} {'Last Invalidated':<30} {'Reason':<20} {'Count'}")
    print("-" * 120)
    for scope_key in sorted(latest.keys()):
        entry = latest[scope_key]
        invalidated_at = str(entry.get("invalidated_at", ""))
        reason = str(entry.get("reason", ""))
        count = int(entry.get("count", 0))
        print(f"{scope_key:<55} {invalidated_at:<30} {reason:<20} {count}")


def check_scope(store_path: Path, scope_key: str) -> None:
    """Check and print the invalidation status of a specific scope key."""
    if not store_path.exists():
        print(f"Scope: {scope_key}")
        print("  Invalidated: false (store does not exist)")
        print("  Last invalidation: n/a")
        return

    invalidated_at: Optional[str] = None
    with open(store_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if str(entry.get("scope_key", "")) == scope_key:
                    invalidated_at = str(entry.get("invalidated_at", ""))
            except Exception:
                continue

    is_invalidated = invalidated_at is not None
    print(f"Scope: {scope_key}")
    print(f"  Invalidated: {is_invalidated}")
    print(f"  Last invalidation: {invalidated_at or 'n/a'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan and report on nervous-bus scope key invalidation state.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a table of all known invalidated scopes.",
    )
    parser.add_argument(
        "--check",
        type=str,
        metavar="SCOPE_KEY",
        help="Check the invalidation status of a specific scope key.",
    )
    parser.add_argument(
        "--store-path",
        type=str,
        metavar="PATH",
        help="Path to the invalidation store (default: ~/.cache/nervous-bus/invalidation_store.jsonl).",
    )
    args = parser.parse_args(argv)

    if not args.report and not args.check:
        parser.print_help()
        return 1

    store_path = _resolve_store_path(args.store_path)

    if args.report:
        report_all(store_path)

    if args.check:
        check_scope(store_path, args.check)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())