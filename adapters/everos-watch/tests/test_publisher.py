import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import everos_watch as ew  # noqa: E402


def test_nervous_publisher_argv_flags_before_channel(tmp_path, monkeypatch):
    fake = tmp_path / "nervous"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("OUT", str(tmp_path / "argv"))
    ok = ew.nervous_publisher(str(fake))(
        {"channel": "memory.node.created.v1", "subject": "a/b", "data": {"x": 1}})
    assert ok
    assert (tmp_path / "argv").read_text().split("\n")[:4] == [
        "publish", "--subject", "a/b", "memory.node.created.v1"]
