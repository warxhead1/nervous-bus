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
            lines.append(f"| `{ch}`{flag} | {desc_cell} |")
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
