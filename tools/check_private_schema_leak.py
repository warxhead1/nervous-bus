#!/usr/bin/env python3
"""Fail if any private-prefixed schema is tracked in this PUBLIC repo.

Private event contracts (e.g. ``tachyonos.*`` trading schemas) belong in the
``$NERVOUS_HOME/schemas/`` overlay (``~/.config/nervous-bus/schemas/``), never
here. ``.gitignore`` blocks accidental *new* adds, but it cannot stop a merge
of a branch that still carries them — this guard catches that case too.

Prefixes are configured in ``tools/private_schema_prefixes.txt`` (one per
line). Exit 0 when clean, 1 when a private schema is tracked. See
``schemas/README.md`` for the policy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFIX_FILE = REPO / "tools" / "private_schema_prefixes.txt"


def load_prefixes() -> list[str]:
    if not PREFIX_FILE.exists():
        return []
    out: list[str] = []
    for line in PREFIX_FILE.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def tracked_schema_files() -> list[str]:
    res = subprocess.run(
        ["git", "ls-files", "schemas/"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    return [ln for ln in res.stdout.splitlines() if ln.endswith(".json")]


def main() -> int:
    prefixes = load_prefixes()
    if not prefixes:
        print("no private prefixes configured; nothing to check")
        return 0

    offenders = [
        path for path in tracked_schema_files()
        if any(path.rsplit("/", 1)[-1].startswith(p) for p in prefixes)
    ]

    if offenders:
        print("ERROR: private schemas are tracked in this PUBLIC repo:", file=sys.stderr)
        for o in offenders:
            print(f"  {o}", file=sys.stderr)
        print(
            "\nThese belong in $NERVOUS_HOME/schemas/ (~/.config/nervous-bus/schemas/), "
            "not this repo.\nMove each out:  git rm <file>  &&  nervous schema install <file>\n"
            f"Private prefixes ({PREFIX_FILE.name}): {', '.join(prefixes)}\n"
            "See schemas/README.md.",
            file=sys.stderr,
        )
        return 1

    print(f"OK — no private-prefixed schemas tracked ({len(prefixes)} prefix(es) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
