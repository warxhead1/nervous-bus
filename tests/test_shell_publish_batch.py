"""`nervous publish-batch`: JSONL on stdin, one process, one log append, one Redis connection."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import site
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[1]
NERVOUS = REPO / "sdk" / "shell" / "nervous"
CHANNEL = "agent.tool.span.v1"


def span(i: int, **over) -> dict:
    rec = {"v": 1, "span_id": f"{i:016x}", "session": "s1", "project": "alpha", "tool": "Bash",
           "class": "build", "start": "2026-11-27T00:00:00.000Z", "status": "ok", "duration_ms": 5}
    rec.update(over)
    return rec


@pytest.fixture
def box(tmp_path):
    class Box:
        pass
    b = Box()
    b.tmp, b.log = tmp_path, tmp_path / "debug.jsonl"
    b.env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONUSERBASE": site.getuserbase(),
             "NERVOUS_HOME": str(tmp_path / "home"),
             "NERVOUS_NO_REDIS": "1", "NERVOUS_NO_ZELLIJ": "1", "NERVOUS_DEBUG_LOG": str(b.log)}
    b.run = lambda lines, *args, channel=CHANNEL, env=None: subprocess.run(
        [str(NERVOUS), "publish-batch", *args, channel], input="".join(json.dumps(x) + "\n" if not isinstance(x, str) else x
                                                                       for x in lines),
        text=True, capture_output=True, cwd=tmp_path, env={**b.env, **(env or {})})
    b.events = lambda: [json.loads(x) for x in b.log.read_text().splitlines()] if b.log.exists() else []
    return b


def test_every_line_becomes_one_envelope_in_one_call(box):
    r = box.run([span(i) for i in range(50)])
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout)
    assert summary["published"] == 50 and summary["rejected"] == [] and summary["validation"] == "ok"
    events = box.events()
    assert len(events) == 50 and {e["type"] for e in events} == {CHANNEL}
    assert len({e["id"] for e in events}) == 50
    assert events[3]["data"] == span(3)
    assert all(e["specversion"] == "1.0" and e["source"] and e["time"].endswith("Z") for e in events)


def test_source_and_traceparent_come_from_the_environment(box):
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    box.run([span(1)], env={"NERVOUS_SOURCE": "/spans", "NERVOUS_TRACEPARENT": tp})
    e = box.events()[0]
    assert e["source"] == "/spans" and e["traceparent"] == tp
    box.log.unlink()
    r = box.run([span(1)], env={"NERVOUS_TRACEPARENT": "nope"})
    assert "traceparent" not in box.events()[0] and "NERVOUS_TRACEPARENT" in r.stderr


def test_a_schema_violation_is_rejected_dead_lettered_and_the_rest_published(box):
    r = box.run([span(1), span(2, status="exploded"), span(3)])
    assert r.returncode == 2
    summary = json.loads(r.stdout)
    assert summary["published"] == 2 and summary["rejected"] == [{"line": 2, "reason": "schema_violation"}]
    types = [e["type"] for e in box.events()]
    assert types.count(CHANNEL) == 2 and types.count("bus.dead_letter") == 1
    dl = next(e for e in box.events() if e["type"] == "bus.dead_letter")["data"]
    assert dl["failure_reason"] == "schema_violation" and dl["original_type"] == CHANNEL
    assert "exploded" in dl["schema_violation_detail"] or dl["schema_violation_detail"]


def test_bad_json_is_rejected_by_line_number(box):
    r = box.run([span(1), "{not json\n", span(2)])
    assert r.returncode == 2
    assert json.loads(r.stdout)["rejected"] == [{"line": 2, "reason": "invalid_json"}]
    assert [e["type"] for e in box.events()].count(CHANNEL) == 2


def test_an_oversize_envelope_is_refused_and_never_written(box):
    r = box.run([span(1), span(2, label="x" * 400)], "--max-bytes", "600")
    assert r.returncode == 2
    assert json.loads(r.stdout)["rejected"] == [{"line": 2, "reason": "payload_too_large"}]
    logged = box.log.read_text()
    assert "x" * 400 not in logged
    assert [e["type"] for e in box.events()].count(CHANNEL) == 1


def test_empty_input_publishes_nothing(box):
    r = box.run([])
    assert r.returncode == 0 and json.loads(r.stdout)["published"] == 0
    assert not box.log.exists()


def test_the_allowlist_applies_once_for_the_whole_batch(box):
    (box.tmp / "nervous.toml").write_text('channels = ["something.else.*"]\n')
    r = box.run([span(1)])
    assert r.returncode == 2 and "not in allowlist" in r.stderr
    assert not box.log.exists()


def test_unknown_channel_warns_and_still_publishes(box):
    r = box.run([{"x": 1}], channel="made.up.channel.v1")
    assert r.returncode == 0 and "no schema" in r.stderr
    assert box.events()[0]["data"] == {"x": 1}


def test_strict_validation_aborts_when_jsonschema_is_missing(box):
    blocker = box.tmp / "blocker"
    blocker.mkdir()
    (blocker / "jsonschema.py").write_text("raise ImportError('blocked for the test')\n")
    env = {"PYTHONPATH": str(blocker), "NERVOUS_STRICT_VALIDATION": "1"}
    r = box.run([span(1)], env=env)
    assert r.returncode == 4 and not box.log.exists()
    lax = box.run([span(1)], env={"PYTHONPATH": str(blocker)})
    assert lax.returncode == 0 and json.loads(lax.stdout)["validation"] == "skipped"


def test_usage_errors_exit_2(box):
    r = subprocess.run([str(NERVOUS), "publish-batch"], input="", text=True, capture_output=True, env=box.env)
    assert r.returncode == 2 and "usage" in r.stderr


# ------------------------------------------------------------ Redis delivery

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def redis_server(tmp_path):
    if importlib.util.find_spec("redis") is None:
        pytest.skip("SKIPPED LOUDLY: python redis module missing, the xadd_python path is NOT tested here")
    if not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    port = free_port()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                             "--dir", str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis
    client = redis.Redis(port=port)
    for _ in range(50):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.1)
    yield client, f"redis://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


def test_events_reach_both_streams_over_one_connection(box, redis_server):
    client, url = redis_server
    r = box.run([span(i) for i in range(20)], env={"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": url})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["delivery"] == "redis"
    assert client.xlen(f"nbus:{CHANNEL}") == 20 and client.xlen("nbus:all") == 20
    row = client.xrange(f"nbus:{CHANNEL}")[0][1]
    assert json.loads(row[b"_raw"])["data"] == span(0) and row[b"type"].decode() == CHANNEL


def test_maxlen_caps_the_typed_stream(box, redis_server):
    client, url = redis_server
    box.run([span(i) for i in range(300)], "--maxlen", "50", env={"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": url})
    assert client.xlen(f"nbus:{CHANNEL}") < 300


def test_dead_letters_are_delivered_too(box, redis_server):
    client, url = redis_server
    box.run([span(1), span(2, status="exploded")], env={"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": url})
    assert client.xlen("nbus:bus.dead_letter") == 1 and client.xlen(f"nbus:{CHANNEL}") == 1


def test_python_redis_client_is_the_path_used_when_the_module_exists(box, redis_server):
    client, url = redis_server
    fake = box.tmp / "fakebin"
    fake.mkdir()
    cli = fake / "redis-cli"
    cli.write_text(f"#!/bin/sh\ntouch '{box.tmp}/cli-ran'\nexit 1\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    r = box.run([span(i) for i in range(10)], env={"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": url,
                                                    "PATH": f"{fake}:{box.env['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert client.xlen(f"nbus:{CHANNEL}") == 10
    assert not (box.tmp / "cli-ran").exists()


def test_redis_cli_pipe_is_the_fallback_when_the_python_module_is_missing(box, redis_server):
    if not shutil.which("redis-cli"):
        pytest.skip("redis-cli not installed")
    client, url = redis_server
    blocker = box.tmp / "blocker"
    blocker.mkdir()
    (blocker / "redis.py").write_text("raise ImportError('blocked for the test')\n")
    r = box.run([span(i) for i in range(10)],
                env={"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": url, "PYTHONPATH": str(blocker)})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["delivery"] == "redis"
    assert client.xlen(f"nbus:{CHANNEL}") == 10


def test_unreachable_redis_leaves_events_durable_and_strict_mode_fails(box):
    env = {"NERVOUS_NO_REDIS": "0", "NERVOUS_REDIS_URL": f"redis://127.0.0.1:{free_port()}"}
    r = box.run([span(1)], env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["delivery"] == "file-only" and len(box.events()) == 1
    strict = box.run([span(2)], env={**env, "NERVOUS_STRICT_REDIS": "1"})
    assert strict.returncode == 1 and len(box.events()) == 2


def test_helper_is_executable():
    assert os.access(REPO / "sdk" / "shell" / "publish_batch.py", os.X_OK) and stat.S_IMODE(NERVOUS.stat().st_mode) & 0o111


@pytest.mark.parametrize("args", [("--bogus",), ("--maxlen", "0"), ("--maxlen", "abc"), ("--max-bytes", "-1"),
                                  ("--max-bytes",), ("--maxlen",)])
def test_bad_flags_exit_two(box, args):
    r = box.run([span(1)], *args)
    assert r.returncode == 2 and "nervous publish-batch" in r.stderr
    assert box.events() == []


def test_a_second_positional_argument_exits_two(box):
    r = subprocess.run([str(NERVOUS), "publish-batch", CHANNEL, "extra"], input="", text=True, capture_output=True,
                       cwd=box.tmp, env=box.env)
    assert r.returncode == 2


def test_max_bytes_zero_disables_the_limit_and_the_exact_limit_passes(box):
    assert box.run([span(1, label="x" * 200)], "--max-bytes", "0").returncode == 0 and len(box.events()) == 1
    probe = box.run([span(2)], "--max-bytes", "10", env={"NERVOUS_OVERSIZE_POLICY": "warn"})
    size = int(re.search(r"payload is (\d+) bytes", probe.stderr).group(1))
    assert box.run([span(3)], "--max-bytes", str(size)).returncode == 0
    r = box.run([span(4)], "--max-bytes", str(size - 1))
    assert r.returncode == 2 and json.loads(r.stdout)["rejected"][0]["reason"] == "payload_too_large"


@pytest.mark.parametrize("channel", ["../schemas/agent.tool.span.v1", "a/b", ".hidden", "-x", "a b", ""])
def test_channel_names_outside_the_pattern_are_refused(box, channel):
    (box.tmp / "schemas").mkdir(exist_ok=True)
    r = box.run([span(1)], channel=channel)
    assert r.returncode == 2 and box.events() == []


def test_a_broken_allowlist_exits_two_with_a_message(box):
    (box.tmp / "nervous.toml").write_text("channels = [unterminated\n")
    r = box.run([span(1)])
    assert r.returncode == 2 and "allowlist" in r.stderr and box.events() == []


def test_oversize_policy_warn_publishes_and_warns(box):
    r = box.run([span(1)], "--max-bytes", "100", env={"NERVOUS_OVERSIZE_POLICY": "warn"})
    assert r.returncode == 0 and len(box.events()) == 1 and "over the 100 byte limit" in r.stderr
