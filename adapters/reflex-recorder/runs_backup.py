#!/usr/bin/env python3
"""Daily online backup of runs.db, keeping the newest N copies.

runs.db lost its history once (2026-11-23, bead nervous-bus-z9ry) with no copy newer
than nine days. sqlite's backup API copies a consistent snapshot while the recorder writes.
"""
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup(src: Path, dest_dir: Path, keep: int, now: datetime) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"runs-{now:%Y%m%d}.db"
    tmp = out.with_suffix(".db.tmp")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    tmp.replace(out)
    for old in sorted(dest_dir.glob("runs-*.db"))[:-keep]:
        old.unlink()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path.home() / ".cache/nervous-bus/reflex/runs.db")
    ap.add_argument("--dest", type=Path, default=Path.home() / "data2/backups/reflex")
    ap.add_argument("--keep", type=int, default=3)
    a = ap.parse_args()
    print(backup(a.db, a.dest, a.keep, datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
