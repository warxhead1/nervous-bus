#!/usr/bin/env python3
"""check_schema_coverage.py — fail if any emitted channel lacks a schema.

Reads the JSON output of `scan_emitted_channels.py` (from stdin or a file)
and compares against `schemas/*.json`.

For each unique channel it checks (in order):
  1. `<channel>.json` exists in schemas/
  2. `<channel>.v<N>.json` exists in schemas/ for some N
  3. channel is on the allowlist (tools/schema_coverage_allowlist.txt)

If a channel hits a schema marked deprecated (channel listed in
`tools/schema_coverage_deprecated.txt`) it is treated as MISSING — the
channel shouldn't be emitted at all.

Exit 1 + table on missing coverage. Exit 0 + summary count otherwise.

Usage:
    scan_emitted_channels.py ... | check_schema_coverage.py \
        --schemas schemas/ \
        --allowlist tools/schema_coverage_allowlist.txt \
        --deprecated tools/schema_coverage_deprecated.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_allowlist(path: Path) -> set[str]:
    if not path or not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def record_schema_version(schema_path: Path) -> str:
    """Compute hash and record version in evidence_graph.db. Returns version_id."""
    try:
        from autobench.evidence_graph import EvidenceGraphDB
    except ImportError:
        return ""
    content = schema_path.read_text()
    schema_hash = hashlib.sha256(content.encode()).hexdigest()
    channel_type = schema_path.stem
    version_id = channel_type.split(".")[-1]
    try:
        db = EvidenceGraphDB()
        db.record_schema_version(channel_type, version_id, content)
    except Exception:
        pass
    return version_id


def scan_schema_versions(
    schema_dir: Path = Path("schemas"),
) -> dict[str, str]:
    """Scan schemas/ dir, record each version. Returns channel -> version_id map."""
    results = {}
    if not schema_dir.exists():
        return results
    for schema_path in sorted(schema_dir.glob("*.json")):
        version_id = record_schema_version(schema_path)
        if version_id:
            results[schema_path.stem] = version_id
    return results


def schema_index(schemas_dir: Path) -> dict[str, list[str]]:
    """Map channel-base-name → list of schema filenames covering it.

    A channel `foo.bar.baz` matches:
        foo.bar.baz.json
        foo.bar.baz.v1.json
        foo.bar.baz.v2.json
        ...

    We also store both the un-versioned and versioned spellings as keys so
    callers can do straight dict lookup against either form.
    """
    out: dict[str, list[str]] = defaultdict(list)
    if not schemas_dir.exists():
        return out
    for p in sorted(schemas_dir.glob("*.json")):
        name = p.name
        if name.startswith("_"):
            continue
        stem = name[: -len(".json")]
        # Strip a trailing .v<N> if present so foo.bar.v1 → foo.bar.
        parts = stem.rsplit(".", 1)
        if len(parts) == 2 and parts[1].startswith("v") and parts[1][1:].isdigit():
            base = parts[0]
            out[base].append(name)
            out[stem].append(name)  # also key the fully-qualified .v1 form
        else:
            out[stem].append(name)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to JSON output of scan_emitted_channels.py. Default: stdin.",
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
        default=Path("tools/schema_coverage_allowlist.txt"),
        help="One channel per line, # comments. Listed channels are exempt.",
    )
    ap.add_argument(
        "--deprecated",
        type=Path,
        default=Path("tools/schema_coverage_deprecated.txt"),
        help="Channels whose schema is marked deprecated → treat as missing.",
    )
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="Print the report but always exit 0 (useful for first-run rollout).",
    )
    ap.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help=(
            "Append a Markdown summary block of any missing channels to this "
            "file (e.g. $GITHUB_STEP_SUMMARY) so misses are loud and "
            "PR-visible even while --report-only keeps the exit code 0."
        ),
    )
    args = ap.parse_args(argv)

    if args.input:
        records = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        records = json.load(sys.stdin)

    allowlist = load_allowlist(args.allowlist)
    deprecated = load_allowlist(args.deprecated)
    schemas = schema_index(args.schemas)

    # Record schema versions in evidence_graph.db
    scan_schema_versions(args.schemas)

    # Aggregate emit sites per channel.
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_channel[r["channel"]].append(r)

    missing: list[tuple[str, list[dict]]] = []
    covered: list[str] = []
    allowlisted: list[str] = []

    for ch in sorted(by_channel):
        if ch in allowlist:
            allowlisted.append(ch)
            continue
        if ch in deprecated:
            missing.append((ch, by_channel[ch]))
            continue
        if ch in schemas:
            covered.append(ch)
            continue
        # Also accept the case where the channel itself is fully-qualified
        # (`foo.bar.v1`) — schemas dict already keys that form.
        missing.append((ch, by_channel[ch]))

    total = len(by_channel)
    print(f"=== Schema coverage report ===")
    print(f"  total unique channels emitted: {total}")
    print(f"  covered by schema:             {len(covered)}")
    print(f"  allowlisted:                   {len(allowlisted)}")
    print(f"  MISSING:                       {len(missing)}")
    print()

    if missing:
        print(f"--- Missing schemas ({len(missing)} channel(s)) ---")
        # Column-aligned table: channel | producer | file:line
        for ch, sites in missing:
            print(f"  channel: {ch}")
            for s in sites[:5]:
                print(f"    - {s['producer']}: {s['file']}:{s['line']}")
            if len(sites) > 5:
                print(f"    - ... and {len(sites) - 5} more emit sites")
        print()
        print("Resolution:")
        print("  1. Add schemas/<channel>.v1.json describing the payload, OR")
        print("  2. Add the channel to tools/schema_coverage_allowlist.txt with a")
        print("     comment explaining why it never crosses the bus.")

        # LOUD, PR-visible summary block. The report itself stays --report-only
        # (exit 0), but we make sure a reviewer cannot miss the gap: a GitHub
        # workflow annotation (::warning::) AND a Markdown block appended to
        # $GITHUB_STEP_SUMMARY (when --summary-file points there).
        _write_summary(args.summary_file, missing, total, len(covered), len(allowlisted))
        _emit_annotation(missing)

        if args.report_only:
            print()
            print("(--report-only) returning 0 despite missing coverage.")
            return 0
        return 1

    print("All emitted channels have schemas. ✔")
    _write_summary(args.summary_file, missing, total, len(covered), len(allowlisted))
    return 0


def _emit_annotation(missing: list[tuple[str, list[dict]]]) -> None:
    """Emit a GitHub Actions ::warning:: annotation so the miss shows up on the
    PR Files/Checks UI even when the step exits 0 (report-only)."""
    names = ", ".join(ch for ch, _ in missing[:20])
    extra = "" if len(missing) <= 20 else f" (+{len(missing) - 20} more)"
    msg = (
        f"schema-coverage: {len(missing)} emitted channel(s) have NO schema: "
        f"{names}{extra}. Add schemas/<channel>.v1.json or allowlist them."
    )
    # `::warning::` is rendered as a yellow annotation on the run/PR; a no-op
    # plain print outside CI.
    print(f"::warning title=Schema coverage gap::{msg}")


def _write_summary(
    summary_file: Path | None,
    missing: list[tuple[str, list[dict]]],
    total: int,
    covered: int,
    allowlisted: int,
) -> None:
    """Append a Markdown summary to summary_file (typically $GITHUB_STEP_SUMMARY).

    No-op when no summary file is configured. The block is intentionally loud
    so any schema miss is impossible to overlook in the PR/run summary."""
    if not summary_file:
        return
    lines: list[str] = []
    lines.append("## Schema coverage")
    lines.append("")
    lines.append(f"- total unique channels emitted: **{total}**")
    lines.append(f"- covered by schema: **{covered}**")
    lines.append(f"- allowlisted: **{allowlisted}**")
    lines.append(f"- **MISSING: {len(missing)}**")
    lines.append("")
    if missing:
        lines.append(
            f"> [!WARNING]\n> {len(missing)} emitted channel(s) have **no schema** "
            "(report-only — not gating this PR yet)."
        )
        lines.append("")
        lines.append("| channel | first emit site |")
        lines.append("| --- | --- |")
        for ch, sites in missing:
            site = sites[0] if sites else {}
            loc = f"{site.get('producer', '?')}: {site.get('file', '?')}:{site.get('line', '?')}"
            more = f" (+{len(sites) - 1} more)" if len(sites) > 1 else ""
            lines.append(f"| `{ch}` | {loc}{more} |")
        lines.append("")
        lines.append(
            "Resolution: add `schemas/<channel>.v1.json` or allowlist the "
            "channel in `tools/schema_coverage_allowlist.txt`."
        )
    else:
        lines.append("All emitted channels have schemas. :white_check_mark:")
    lines.append("")
    try:
        with summary_file.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        sys.stderr.write(f"[schema-coverage] could not write summary: {e}\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
