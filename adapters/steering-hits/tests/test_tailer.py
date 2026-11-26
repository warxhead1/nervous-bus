"""Tailer tests with a fake `nervous` that records each publish."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tailer

SAMPLE = json.loads((Path(__file__).parent / "sample_hit.json").read_text())


def rec(n: int) -> str:
    return json.dumps(dict(SAMPLE, findings=n))


@pytest.fixture
def env(tmp_path):
    sent = tmp_path / "sent.log"
    fake = tmp_path / "nervous"
    fake.write_text(
        "#!/bin/sh\n"
        f'[ -f "{tmp_path}/fail" ] && exit 7\n'
        f'[ "$1" = publish ] && [ "$2" = agent.steering.hit.v1 ] && printf "%s\\n" "$3" >> "{sent}"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return {
        "hits": tmp_path / "hits.jsonl",
        "state": tmp_path / "state" / "cp.json",
        "nervous": str(fake),
        "sent": sent,
        "fail": tmp_path / "fail",
    }


def go(env) -> int:
    return tailer.run(env["hits"], env["state"], env["nervous"])


def sent_findings(env) -> list[int]:
    if not env["sent"].exists():
        return []
    return [json.loads(line)["findings"] for line in env["sent"].read_text().splitlines()]


def append(env, *lines: str) -> None:
    with open(env["hits"], "a") as f:
        for line in lines:
            f.write(line + "\n")


def test_publishes_each_line_once_and_resumes(env):
    append(env, rec(1), rec(2))
    assert go(env) == 0
    assert go(env) == 0
    assert sent_findings(env) == [1, 2]
    append(env, rec(3))
    assert go(env) == 0
    assert sent_findings(env) == [1, 2, 3]


def test_partial_last_line_waits(env):
    append(env, rec(1))
    with open(env["hits"], "a") as f:
        f.write(rec(2)[:20])
    go(env)
    assert sent_findings(env) == [1]
    with open(env["hits"], "a") as f:
        f.write(rec(2)[20:] + "\n")
    go(env)
    assert sent_findings(env) == [1, 2]


def test_malformed_line_skipped_not_retried(env):
    append(env, "not json", rec(1))
    assert go(env) == 0
    assert go(env) == 0
    assert sent_findings(env) == [1]


def test_failure_keeps_checkpoint_and_retries(env):
    append(env, rec(1), rec(2))
    env["fail"].write_text("")
    assert go(env) == 1
    assert sent_findings(env) == []
    env["fail"].unlink()
    assert go(env) == 0
    assert sent_findings(env) == [1, 2]


def test_rotation_finishes_old_file_then_reads_new(env):
    append(env, rec(1), rec(2))
    go(env)
    append(env, rec(3))  # unread tail of the file about to rotate
    os.rename(env["hits"], str(env["hits"]) + ".1")
    append(env, rec(4))
    assert go(env) == 0
    assert sent_findings(env) == [1, 2, 3, 4]
    assert go(env) == 0
    assert sent_findings(env) == [1, 2, 3, 4]


def test_rotation_replacing_older_dot_one(env):
    append(env, rec(1))
    go(env)
    os.rename(env["hits"], str(env["hits"]) + ".1")
    append(env, rec(2))
    go(env)
    os.replace(env["hits"], str(env["hits"]) + ".1")  # second rotation clobbers .1
    append(env, rec(3))
    go(env)
    assert sent_findings(env) == [1, 2, 3]


def test_first_run_reads_rotated_file_too(env):
    append(env, rec(1))
    os.rename(env["hits"], str(env["hits"]) + ".1")
    append(env, rec(2))
    go(env)
    assert sent_findings(env) == [1, 2]


def test_missing_file_is_a_clean_noop(env):
    assert go(env) == 0
    assert sent_findings(env) == []


def test_truncated_in_place_restarts(env):
    append(env, rec(1), rec(2))
    go(env)
    env["hits"].write_text(rec(9) + "\n")
    go(env)
    assert sent_findings(env) == [1, 2, 9]
