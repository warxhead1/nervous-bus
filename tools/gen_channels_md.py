#!/usr/bin/env python3
"""Generate schemas/CHANNELS.md — a clustered index of every channel schema.

Source of truth: schemas/*.json filenames (each is a channel contract named
`<channel>.json`, where `<channel>` should be `<project>.<subsystem>.<event>.v<n>`).

Clusters (precedence-ordered — each channel lands in exactly ONE):

    Session Lifecycle  agent.*, *.session.*, bus.agent.*, deer-flow.thread.*
    Autobench          autobench.* ONLY (the --cluster autobench contract)
    Hearth             hearth*, loom.*, bus.hearth.*, bus.bead*, bus.beads.*
    Tengine            tengine.*
    Cross-cutting      everything else (bus.notify, bus.dead_letter, kb.*,
                       kernel.*, funsearch.*, codeforces_problem.*, sys.*,
                       pulse.*, _per-project.*, ...)

The classification rules here are mirrored by `nervous schemas --cluster X`
in sdk/shell/nervous so the CLI and this doc never drift. Re-run after adding
or renaming schemas:  python3 tools/gen_channels_md.py

Naming convention codified: `<project>.<subsystem>.<event>.v<n>` (lowercase,
dot-separated, trailing major version). Violations are listed in their own
section so they can be fixed or consciously grandfathered.

Status annotations: a schema may carry a free-text top-level `"status"` key
(e.g. `"unconsumed"`, `"orphaned-consumer-mismatch"`, `"retired"`) to flag
that it's known write-only telemetry, a producer/consumer field mismatch, or
similar. When present, it's surfaced inline in the channel's Description
cell (🔇 marker) so this doc doesn't silently imply a live listener where
none is evidenced. Channels whose schema file was removed entirely (producer
gone, nothing left to regenerate a row from) are listed by hand in the
"Retired channels (no schema file)" section — see RETIRED_NO_SCHEMA below.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO / "schemas"
OUT = SCHEMA_DIR / "CHANNELS.md"

# Cluster order matters: first match wins. Keep in sync with cmd_schemas()
# in sdk/shell/nervous (the `--cluster` filter applies the same rules).
CLUSTERS: list[tuple[str, str]] = [
    ("Session Lifecycle", "agent session lifecycle, heartbeats, thread/run start-stop"),
    ("Autobench", "autobench.* evolution loop (case/judge/improver/budget/...)"),
    ("Hearth", "hearth-loom PR pipeline, bead lifecycle, loom executions"),
    ("Tengine", "tengine shadergen + silo session telemetry"),
    ("Cross-cutting", "bus internals, kb, GPU kernels, funsearch, system/pulse, per-project broadcast"),
]

# Filename convention: <project>.<subsystem>.<event>.v<n>
#   project   = [a-z0-9-]+         (e.g. autobench, deer-flow, hearth-loom)
#   then >= 1 dotted segment(s)    (subsystem[.event...], [a-z0-9_-]+)
#   ends with .v<n>
NAME_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9_-]+)+\.v[0-9]+$")


def classify(channel: str) -> str:
    """Return the single cluster a channel belongs to (first rule wins)."""
    c = channel
    # ── Session Lifecycle ──────────────────────────────────────────────────
    if c.startswith("agent."):
        return "Session Lifecycle"
    if c.startswith("bus.agent."):
        return "Session Lifecycle"
    if c.startswith("deer-flow.thread.") or c.startswith("deer-flow.session."):
        return "Session Lifecycle"
    if ".session." in c and not c.startswith("bus.hearth.") and not c.startswith("tengine."):
        return "Session Lifecycle"
    # ── Autobench ──────────────────────────────────────────────────────────
    # Strictly the autobench.* family. `nervous schemas --cluster autobench`
    # must return ONLY autobench.* channels — GPU-kernel / funsearch / problem
    # corpora are adjacent infra and live in Cross-cutting, not here.
    if c.startswith("autobench."):
        return "Autobench"
    # ── Hearth ─────────────────────────────────────────────────────────────
    if c.startswith("hearth") or c.startswith("loom."):
        return "Hearth"
    if c.startswith("bus.hearth.") or c.startswith("bus.bead") or c.startswith("bus.beads."):
        return "Hearth"
    # ── Tengine ────────────────────────────────────────────────────────────
    if c.startswith("tengine."):
        return "Tengine"
    # ── Cross-cutting (default) ────────────────────────────────────────────
    return "Cross-cutting"


def channel_of(path: Path) -> str:
    return path.name[: -len(".json")]


def title_of(path: Path) -> str:
    """Pull a one-line description from the schema's `title`/`description`."""
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return ""
    for key in ("title", "description"):
        val = doc.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.strip().split())[:90]
    return ""


def status_of(path: Path) -> str | None:
    """Pull the schema-level `status` annotation (e.g. 'unconsumed',
    'orphaned-consumer-mismatch', 'retired'), if present. This is a free-text
    vendor keyword, not part of the JSON Schema vocabulary — see e.g.
    tengine.frame.metrics.v1.json and the kb.* zombie-event schemas."""
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return None
    val = doc.get("status")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


# Channels formally retired whose producer AND schema file were both removed
# from this repo (not just deprecated-in-place). Listed by hand because the
# generator only has *.json files to walk — there's nothing on disk to
# classify() for these. Keep in sync with any future retirement: add the
# channel + a short reason, do not fabricate a schema file just to get a row.
RETIRED_NO_SCHEMA: list[tuple[str, str]] = [
    (
        "hearth.device.state.v1",
        "Formally retired (2026-07 zombie-event audit). Producer was "
        "adapters/hearth-bridge (home IoT bridge); both the adapter and its "
        "schema file were deleted from this repo in commit 8dbb391 "
        "(oss-prep private-schema migration) and never restored publicly. "
        "No consumer evidenced anywhere. Not resurrecting speculative "
        "cross-project infra without an evidenced product need.",
    ),
    (
        "hearth.presence.v1",
        "Formally retired (2026-07 zombie-event audit). Same producer "
        "(adapters/hearth-bridge) and same removal commit (8dbb391) as "
        "hearth.device.state.v1. No consumer evidenced anywhere. "
        "hearth.health.snapshot.v1 was removed in the same commit and never "
        "had an evidenced producer even before removal (no publish call "
        "site found) — worth knowing if this channel is ever revisited, "
        "though it isn't itself being re-registered here.",
    ),
]


def main() -> int:
    schemas = sorted(SCHEMA_DIR.glob("*.json"))
    if not schemas:
        print(f"no schemas in {SCHEMA_DIR}", file=sys.stderr)
        return 2

    buckets: dict[str, list[Path]] = {name: [] for name, _ in CLUSTERS}
    violations: list[str] = []
    for p in schemas:
        ch = channel_of(p)
        buckets[classify(ch)].append(p)
        if not NAME_RE.match(ch):
            violations.append(ch)

    lines: list[str] = []
    lines.append("# Channel taxonomy")
    lines.append("")
    lines.append(
        "Generated index of every channel schema in `schemas/*.json`, clustered "
        "by domain. **Do not hand-edit** — regenerate with "
        "`python3 tools/gen_channels_md.py` after adding or renaming a schema."
    )
    lines.append("")
    lines.append(
        "Discover from the CLI: `nervous schemas --cluster <name>` filters to one "
        "cluster, `nervous schemas --search <keyword>` does a substring match."
    )
    lines.append("")
    lines.append(f"**{len(schemas)} channels** across {len(CLUSTERS)} clusters.")
    lines.append("")

    # Summary table
    lines.append("| Cluster | Channels | Scope |")
    lines.append("| --- | --: | --- |")
    for name, desc in CLUSTERS:
        lines.append(f"| [{name}](#{slug(name)}) | {len(buckets[name])} | {desc} |")
    lines.append("")

    for name, desc in CLUSTERS:
        items = sorted(buckets[name], key=lambda p: p.name)
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"_{desc}_")
        lines.append("")
        if not items:
            lines.append("_(none)_")
            lines.append("")
            continue
        lines.append("| Channel | Description |")
        lines.append("| --- | --- |")
        for p in items:
            ch = channel_of(p)
            flag = " ⚠️" if ch in violations else ""
            desc_cell = title_of(p).replace("|", "\\|") or "—"
            status = status_of(p)
            if status:
                desc_cell = f"🔇 **{status}** — {desc_cell}"
            lines.append(f"| `{ch}`{flag} | {desc_cell} |")
        lines.append("")

    # Retired channels with no schema file on disk. These can't come from the
    # SCHEMA_DIR.glob() walk above (there's no *.json to walk), so they're
    # listed explicitly. See RETIRED_NO_SCHEMA docstring for why.
    lines.append("## Retired channels (no schema file)")
    lines.append("")
    lines.append(
        "Producer and schema file both removed from this repo — nothing to "
        "regenerate a row from, so these are listed by hand. Present here so "
        "contributors don't mistake silent absence for 'never existed' or "
        "'still planned'."
    )
    lines.append("")
    for ch, note in RETIRED_NO_SCHEMA:
        lines.append(f"- `{ch}` 🔇 **retired** — {note}")
    lines.append("")

    # Naming-convention violations
    lines.append("## Naming-convention violations")
    lines.append("")
    lines.append(
        "Convention: `<project>.<subsystem>.<event>.v<n>` (lowercase, "
        "dot-separated, trailing major version). The following filenames do not "
        "match and are flagged with ⚠️ above:"
    )
    lines.append("")
    if violations:
        for ch in sorted(violations):
            lines.append(f"- `{ch}` — {_violation_reason(ch)}")
    else:
        lines.append("_None — all schemas conform._")
    lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} — {len(schemas)} channels, {len(violations)} naming violation(s)")
    return 0


def _violation_reason(ch: str) -> str:
    if ch.startswith("_"):
        return "leading underscore (template/placeholder, not a real `<project>` segment)"
    if not re.search(r"\.v[0-9]+$", ch):
        return "missing trailing `.v<n>` version segment"
    # Strip the trailing .v<n> and count remaining dotted segments. Need at least
    # project + subsystem + event (3) before the version.
    body = re.sub(r"\.v[0-9]+$", "", ch)
    if body.count(".") < 2:
        return "too few segments — needs `<project>.<subsystem>.<event>` before `.v<n>`"
    if "_" in body.split(".")[0]:
        return "underscore in the `<project>` segment (use a single dotted project token)"
    return "does not match `<project>.<subsystem>.<event>.v<n>`"


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


if __name__ == "__main__":
    raise SystemExit(main())
