#!/usr/bin/env python3
"""check_subscription_coverage.py — subscribe-side schema/emit drift gate.

The subscribe-side analogue of `check_schema_coverage.py`. Reads the JSON
output of `scan_subscribed_channels.py` (stdin or `--input`), the EMITTED
channels (JSON output of `scan_emitted_channels.py` via `--emitted`), and the
`schemas/` directory, then reports three classes of drift:

  1. subscribed-but-no-schema  (BLOCK)  — a subscription matches ZERO schema
     names. There is no contract for what this handler consumes. Exit 1
     (unless `--report-only`, or the channel is on the baseline/allowlist).

  2. subscribed-but-never-emitted  (WARN, "dead handler") — a subscription
     matches ≥1 schema but ZERO emitted channels. A handler waiting on an
     event nobody fires. WARN by default; exit 1 only with `--strict`.

  3. schema-exists-but-never-used  (WARN, "orphan") — a schema base-name that
     is matched by NO emitted channel AND NO subscription. WARN only.

Matcher semantics:
  - exact `x.y.z` matches a name N iff
        N == x.y.z
        OR N startswith `x.y.z.`     (subscribing to a base w/ versioned schemas)
        OR strip_v(x.y.z) == N       (subscribing to `foo.bar.v1`, schema `foo.bar`)
  - prefix `x.y` matches N iff
        N == x.y
        OR N startswith `x.y.`

Allowlist / baseline:
  - `--allowlist`  : channels here are exempt from ALL classes (same loader /
    format as the emit checker).
  - `--baseline`   : channels here are downgraded from BLOCK → WARN, so known
    drift can be ratcheted rather than blocking CI. Pre-seed with current drift.

Usage:
    scan_subscribed_channels.py ... > subs.json
    scan_emitted_channels.py    ... > emits.json
    check_subscription_coverage.py \
        --input subs.json \
        --emitted emits.json \
        --schemas schemas/ \
        --allowlist tools/subscription_coverage_allowlist.txt \
        --baseline  tools/subscription_drift_baseline.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_allowlist(path: Path) -> set[str]:
    """One channel per line, `#` comments, blank lines ignored."""
    if not path or not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def schema_index(schemas_dir: Path) -> dict[str, list[str]]:
    """Map channel-base-name → list of schema filenames covering it.

    A channel `foo.bar.baz` matches:
        foo.bar.baz.json
        foo.bar.baz.v1.json
        ...

    We store both the un-versioned and versioned spellings as keys so callers
    can do straight lookup against either form. (Verbatim from the emit checker.)
    """
    out: dict[str, list[str]] = defaultdict(list)
    if not schemas_dir.exists():
        return out
    for p in sorted(schemas_dir.glob("*.json")):
        name = p.name
        if name.startswith("_"):
            continue
        stem = name[: -len(".json")]
        parts = stem.rsplit(".", 1)
        if len(parts) == 2 and parts[1].startswith("v") and parts[1][1:].isdigit():
            base = parts[0]
            out[base].append(name)
            out[stem].append(name)
        else:
            out[stem].append(name)
    return out


def _strip_v(channel: str) -> str:
    """Strip a trailing `.vN` segment (foo.bar.v1 → foo.bar)."""
    parts = channel.rsplit(".", 1)
    if len(parts) == 2 and parts[1].startswith("v") and parts[1][1:].isdigit():
        return parts[0]
    return channel


def exact_matches(channel: str, names: set[str]) -> set[str]:
    """Names matched by an exact subscription `channel`."""
    base = _strip_v(channel)
    hit: set[str] = set()
    for n in names:
        if n == channel or n == base or n.startswith(channel + ".") or n.startswith(base + "."):
            hit.add(n)
    return hit


def prefix_matches(prefix: str, names: set[str]) -> set[str]:
    """Names matched by a prefix subscription `prefix`."""
    hit: set[str] = set()
    for n in names:
        if n == prefix or n.startswith(prefix + "."):
            hit.add(n)
    return hit


def matcher(channel: str, match_type: str, names: set[str]) -> set[str]:
    if match_type == "prefix":
        return prefix_matches(channel, names)
    return exact_matches(channel, names)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=None,
        help="JSON output of scan_subscribed_channels.py. Default: stdin.",
    )
    ap.add_argument(
        "--emitted",
        type=Path,
        default=None,
        help="JSON output of scan_emitted_channels.py (emitted channels).",
    )
    ap.add_argument(
        "--schemas",
        type=Path,
        default=Path("schemas"),
        help="Path to schemas/ directory.",
    )
    ap.add_argument(
        "--allowlist",
        type=Path,
        default=Path("tools/subscription_coverage_allowlist.txt"),
        help="Channels exempt from ALL drift classes.",
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        default=Path("tools/subscription_drift_baseline.txt"),
        help="Channels downgraded from BLOCK to WARN (known-drift ratchet).",
    )
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="Print the report but always exit 0.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Make dead-handler (subscribed-but-never-emitted) also exit 1.",
    )
    args = ap.parse_args(argv)

    if args.input:
        sub_records = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        sub_records = json.load(sys.stdin)

    emitted_records = []
    if args.emitted and args.emitted.exists():
        emitted_records = json.loads(args.emitted.read_text(encoding="utf-8"))

    allowlist = load_allowlist(args.allowlist)
    baseline = load_allowlist(args.baseline)
    schemas = schema_index(args.schemas)

    schema_names: set[str] = set(schemas.keys())
    emitted_names: set[str] = {r["channel"] for r in emitted_records}
    universe: set[str] = schema_names | emitted_names

    # Aggregate subscription sites per (channel, match_type).
    by_sub: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in sub_records:
        by_sub[(r["channel"], r["match_type"])].append(r)

    # Drift class buckets.
    no_schema: list[tuple[str, str, list[dict]]] = []     # class 1 (BLOCK)
    dead_handler: list[tuple[str, str, list[dict]]] = []  # class 2 (WARN)
    healthy: list[tuple[str, str]] = []
    allowlisted: list[tuple[str, str]] = []

    # Track which universe names are "used" by some subscription (for orphans).
    matched_by_sub: set[str] = set()

    for (channel, mtype) in sorted(by_sub):
        sites = by_sub[(channel, mtype)]
        if channel in allowlist:
            allowlisted.append((channel, mtype))
            # Allowlisted subs still count as "using" what they match.
            matched_by_sub |= matcher(channel, mtype, universe)
            continue

        schema_hits = matcher(channel, mtype, schema_names)
        emit_hits = matcher(channel, mtype, emitted_names)
        matched_by_sub |= matcher(channel, mtype, universe)

        if not schema_hits:
            no_schema.append((channel, mtype, sites))
        elif not emit_hits:
            dead_handler.append((channel, mtype, sites))
        else:
            healthy.append((channel, mtype))

    # Class 3: orphan schemas — base-name matched by NO emit and NO subscription.
    # schema_index stores BOTH the versioned (`foo.bar.v1`) and the stripped
    # base (`foo.bar`) spelling as keys; collapse to the base so a versioned
    # schema isn't double-listed.
    orphans: list[str] = []
    for name in sorted(schema_names):
        if name in allowlist:
            continue
        # If this is a versioned spelling whose base is also a key, skip it —
        # the base spelling carries the single canonical orphan entry.
        base = _strip_v(name)
        if base != name and base in schema_names:
            continue
        emitted_here = any(
            n == name or n.startswith(name + ".") or _strip_v(n) == name for n in emitted_names
        )
        if emitted_here:
            continue
        if name in matched_by_sub:
            continue
        orphans.append(name)

    # Split no_schema into BLOCK vs baselined-WARN.
    blocking = [t for t in no_schema if t[0] not in baseline]
    baselined = [t for t in no_schema if t[0] in baseline]

    # ── Report ───────────────────────────────────────────────────────────────
    print("=== Subscription coverage report ===")
    print(f"  unique subscriptions:          {len(by_sub)}")
    print(f"  schema names:                  {len(schema_names)}")
    print(f"  emitted channels:              {len(emitted_names)}")
    print(f"  healthy (schema + emitted):    {len(healthy)}")
    print(f"  allowlisted:                   {len(allowlisted)}")
    print(f"  subscribed-but-no-schema:      {len(no_schema)}  "
          f"(BLOCK={len(blocking)}, baselined-WARN={len(baselined)})")
    print(f"  subscribed-but-never-emitted:  {len(dead_handler)}  (dead handlers)")
    print(f"  schema-exists-but-never-used:  {len(orphans)}  (orphans)")
    print()

    def _print_sites(channel: str, mtype: str, sites: list[dict]) -> None:
        print(f"  [{mtype}] {channel}")
        for s in sites[:5]:
            print(f"    - {s['consumer']}: {s['file']}:{s['line']}")
        if len(sites) > 5:
            print(f"    - ... and {len(sites) - 5} more subscribe sites")

    if blocking:
        print(f"--- BLOCK: subscribed-but-no-schema ({len(blocking)}) ---")
        for channel, mtype, sites in blocking:
            _print_sites(channel, mtype, sites)
        print()

    if baselined:
        print(f"--- WARN (baselined): subscribed-but-no-schema ({len(baselined)}) ---")
        for channel, mtype, sites in baselined:
            _print_sites(channel, mtype, sites)
        print()

    if dead_handler:
        print(f"--- WARN: subscribed-but-never-emitted / dead handler ({len(dead_handler)}) ---")
        for channel, mtype, sites in dead_handler:
            _print_sites(channel, mtype, sites)
        print()

    if orphans:
        print(f"--- WARN: schema-exists-but-never-used / orphan ({len(orphans)}) ---")
        for name in orphans:
            print(f"  {name}")
        print()

    # ── Exit code ──────────────────────────────────────────────────────────────
    fail = False
    if blocking:
        fail = True
    if args.strict and dead_handler:
        fail = True

    if not (blocking or dead_handler or orphans):
        print("All subscriptions have schemas and live emitters. ✔")

    if args.report_only:
        if fail:
            print("(--report-only) returning 0 despite drift.")
        return 0
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
