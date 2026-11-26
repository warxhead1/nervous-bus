import sqlite3
from datetime import datetime, timezone

from runs_backup import backup


def test_backup_copies_rows_and_prunes_to_keep(tmp_path):
    src = tmp_path / "runs.db"
    c = sqlite3.connect(src)
    c.execute("create table runs (run_id text primary key)")
    c.executemany("insert into runs values (?)", [("a",), ("b",)])
    c.commit()
    c.close()
    dest = tmp_path / "bk"
    for day in range(1, 5):
        out = backup(src, dest, keep=2, now=datetime(2026, 11, day, tzinfo=timezone.utc))
    assert sorted(p.name for p in dest.glob("runs-*.db")) == ["runs-20261103.db", "runs-20261104.db"]
    assert sqlite3.connect(out).execute("select count(*) from runs").fetchone()[0] == 2
    assert not list(dest.glob("*.tmp"))
