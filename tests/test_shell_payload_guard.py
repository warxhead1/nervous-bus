"""Byte-bounded publishing and per-publish MAXLEN for the `nervous` shell SDK.

Redis streams are capped by entry COUNT, but an entry's cost is its size. A
production Valkey died at 2.5 GB of streams from ~60 KB entries at exactly
10,000 entries each. `nervous publish` therefore refuses an envelope over a
byte limit (writing a bus.dead_letter instead of the original) and lets a
caller pick the typed stream's MAXLEN.

These tests run the real script. Anything that needs Redis talks to a private
throwaway server on a loopback port; every other test sets NERVOUS_NO_REDIS=1,
so nothing here can reach a live broker.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

NERVOUS = Path(__file__).resolve().parents[1] / "sdk" / "shell" / "nervous"

LARGE = 20_000  # well over the 16 KiB default, well under argv limits


def _payload(n: int) -> str:
    return json.dumps({"snapshot": "x" * n})


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "NERVOUS_DEBUG_LOG": str(tmp_path / "debug.jsonl"),
        "NERVOUS_NO_ZELLIJ": "1",
        "NERVOUS_SKIP_ALLOWLIST": "1",
        "NBUS_SKIP_VALIDATION": "1",
        "NERVOUS_SOURCE": "/guard-test",
        "NERVOUS_PYTHON": sys.executable,
        "NERVOUS_NO_REDIS": "1",
    }
    env.update(extra)
    return env


def _publish(
    tmp_path: Path,
    *args: str,
    stdin: str | None = None,
    **env: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(NERVOUS), "publish", *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=_env(tmp_path, **env),
    )


def _log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "debug.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _envelope(channel: str, n: int) -> str:
    return json.dumps(
        {
            "specversion": "1.0",
            "id": "01GUARDTEST",
            "source": "/passthrough-producer",
            "type": channel,
            "time": "2026-11-08T00:00:00Z",
            "datacontenttype": "application/json",
            "data": {"snapshot": "x" * n},
        }
    )


# ── Byte guard, no Redis ─────────────────────────────────────────────────────


@pytest.mark.parametrize("via", ["arg", "json"])
def test_oversize_envelope_is_refused_and_only_a_dead_letter_is_logged(
    tmp_path: Path, via: str
) -> None:
    if via == "arg":
        proc = _publish(tmp_path, "big.snapshot.v1", _payload(LARGE))
    else:
        proc = _publish(tmp_path, "--json", stdin=_envelope("big.snapshot.v1", LARGE))

    assert proc.returncode == 5, (proc.returncode, proc.stderr)
    assert "big.snapshot.v1" in proc.stderr and "bytes" in proc.stderr

    # The original must not reach debug.jsonl: redis-mirror replays that file
    # into Redis, which would defeat the guard.
    events = _log(tmp_path)
    assert [e["type"] for e in events] == ["bus.dead_letter"]
    data = events[0]["data"]
    assert data["failure_reason"] == "payload_too_large"
    assert data["original_type"] == "big.snapshot.v1"
    assert data["payload_bytes"] > data["limit_bytes"]
    assert len(data["original_payload_excerpt"]) <= 500
    if via == "json":
        assert data["original_source"] == "/passthrough-producer"


def test_notification_sized_payload_is_published_untouched(tmp_path: Path) -> None:
    proc = _publish(tmp_path, "bus.notify.v1", json.dumps({"id": "01J", "kind": "changed"}))

    assert proc.returncode == 0, proc.stderr
    events = _log(tmp_path)
    assert [e["type"] for e in events] == ["bus.notify.v1"]


def test_multibyte_payload_is_measured_in_bytes_not_characters(tmp_path: Path) -> None:
    # 9,000 two-byte characters: ~9 K characters but ~18 K bytes.
    payload = json.dumps({"s": "é" * 9_000}, ensure_ascii=False)
    proc = _publish(tmp_path, "big.snapshot.v1", payload, LC_ALL="C.UTF-8", LANG="C.UTF-8")

    assert proc.returncode == 5, (proc.returncode, proc.stderr)
    assert [e["type"] for e in _log(tmp_path)] == ["bus.dead_letter"]


def test_warn_policy_publishes_anyway_and_says_so(tmp_path: Path) -> None:
    proc = _publish(
        tmp_path, "big.snapshot.v1", _payload(LARGE), NERVOUS_OVERSIZE_POLICY="warn"
    )

    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and "big.snapshot.v1" in proc.stderr
    assert [e["type"] for e in _log(tmp_path)] == ["big.snapshot.v1"]


def test_max_bytes_flag_and_env_resolution(tmp_path: Path) -> None:
    big = _payload(LARGE)
    # Flag raises the limit for one publish.
    assert _publish(tmp_path, "--max-bytes", "100000", "a.v1", big).returncode == 0
    # 0 disables the check.
    assert _publish(tmp_path, "--max-bytes", "0", "a.v1", big).returncode == 0
    # Env lowers the limit for a small payload.
    small = _payload(500)
    assert _publish(tmp_path, "a.v1", small, NERVOUS_MAX_PAYLOAD_BYTES="300").returncode == 5
    # Env disables.
    assert _publish(tmp_path, "a.v1", big, NERVOUS_MAX_PAYLOAD_BYTES="0").returncode == 0
    # Flag beats env, both directions.
    assert (
        _publish(
            tmp_path, "--max-bytes", "100000", "a.v1", big, NERVOUS_MAX_PAYLOAD_BYTES="300"
        ).returncode
        == 0
    )
    assert (
        _publish(
            tmp_path, "--max-bytes", "300", "a.v1", small, NERVOUS_MAX_PAYLOAD_BYTES="0"
        ).returncode
        == 5
    )


def test_invalid_env_limit_is_ignored_with_a_warning(tmp_path: Path) -> None:
    proc = _publish(
        tmp_path, "bus.notify.v1", json.dumps({"id": "1"}), NERVOUS_MAX_PAYLOAD_BYTES="lots"
    )

    assert proc.returncode == 0, proc.stderr
    assert "NERVOUS_MAX_PAYLOAD_BYTES" in proc.stderr


@pytest.mark.parametrize(
    "flag,value", [("--maxlen", "0"), ("--maxlen", "abc"), ("--max-bytes", "-1"), ("--max-bytes", "x")]
)
def test_invalid_flag_value_exits_2(tmp_path: Path, flag: str, value: str) -> None:
    proc = _publish(tmp_path, flag, value, "a.v1", json.dumps({"id": "1"}))

    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert flag in proc.stderr and "integer" in proc.stderr
    assert _log(tmp_path) == []


def test_schema_violation_still_dead_letters_without_logging_the_original(
    tmp_path: Path,
) -> None:
    # Characterises the pre-existing path that now shares the dead-letter
    # helper with the size guard.
    pytest.importorskip("jsonschema")
    proc = _publish(
        tmp_path,
        "agent.message.v1",
        json.dumps({"hello": "world"}),
        NBUS_SKIP_VALIDATION="0",
    )

    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    events = _log(tmp_path)
    assert [e["type"] for e in events] == ["bus.dead_letter"]
    assert events[0]["data"]["failure_reason"] == "schema_violation"
    assert events[0]["data"]["original_type"] == "agent.message.v1"
    assert events[0]["data"]["schema_violation_detail"]


# ── MAXLEN and dead-letter delivery against a private Redis ──────────────────


@pytest.fixture()
def redis_server(tmp_path: Path):
    """A private throwaway server. One entry per stream node makes the `~`
    (approximate) trim exact, so MAXLEN assertions are not hostage to
    macro-node granularity."""
    binary = shutil.which("valkey-server") or shutil.which("redis-server")
    if binary is None:
        pytest.skip("no redis-server/valkey-server on PATH")
    redis = pytest.importorskip("redis")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [
            binary,
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--stream-node-max-entries", "1",
            "--dir", str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    deadline = time.time() + 10
    while True:
        try:
            client.ping()
            break
        except redis.RedisError:
            if time.time() > deadline:
                proc.kill()
                pytest.fail("private redis-server did not start")
            time.sleep(0.05)
    client.url = f"redis://127.0.0.1:{port}"  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(params=["python", "redis-cli"])
def delivery(request, tmp_path: Path) -> dict[str, str]:
    """Env that forces one of the two XADD delivery paths in the script."""
    if request.param == "python":
        return {}
    if shutil.which("redis-cli") is None:
        pytest.skip("redis-cli not on PATH")
    # Shadow the redis module so the python path fails and the script falls
    # back to redis-cli.
    stub = tmp_path / "noredis"
    stub.mkdir()
    (stub / "redis.py").write_text("raise ImportError('redis hidden for test')\n")
    return {"PYTHONPATH": str(stub)}


def _publish_n(tmp_path, server, delivery, channel, n, *args, **env) -> None:
    for i in range(n):
        proc = _publish(
            tmp_path,
            *args,
            channel,
            json.dumps({"i": i}),
            NERVOUS_NO_REDIS="0",
            NERVOUS_REDIS_URL=server.url,
            **delivery,
            **env,
        )
        assert proc.returncode == 0, proc.stderr


def test_maxlen_flag_caps_the_typed_stream_only(tmp_path, redis_server, delivery) -> None:
    _publish_n(tmp_path, redis_server, delivery, "capped.v1", 8, "--maxlen", "3")

    assert redis_server.xlen("nbus:capped.v1") == 3
    assert redis_server.xlen("nbus:all") == 8


def test_default_typed_cap_does_not_trim_small_streams(tmp_path, redis_server, delivery) -> None:
    _publish_n(tmp_path, redis_server, delivery, "plain.v1", 8)

    assert redis_server.xlen("nbus:plain.v1") == 8


def test_maxlen_env_default_and_flag_precedence(tmp_path, redis_server, delivery) -> None:
    _publish_n(tmp_path, redis_server, delivery, "env.v1", 8, NERVOUS_TYPED_MAXLEN="2")
    _publish_n(
        tmp_path, redis_server, delivery, "flag.v1", 8, "--maxlen", "5", NERVOUS_TYPED_MAXLEN="2"
    )

    assert redis_server.xlen("nbus:env.v1") == 2
    assert redis_server.xlen("nbus:flag.v1") == 5


def test_fanout_maxlen_env_caps_nbus_all(tmp_path, redis_server, delivery) -> None:
    _publish_n(tmp_path, redis_server, delivery, "fan.v1", 8, NERVOUS_FANOUT_MAXLEN="4")

    assert redis_server.xlen("nbus:all") == 4
    assert redis_server.xlen("nbus:fan.v1") == 8


def test_refused_payload_never_reaches_redis_but_its_dead_letter_does(
    tmp_path, redis_server, delivery
) -> None:
    proc = _publish(
        tmp_path,
        "big.snapshot.v1",
        _payload(LARGE),
        NERVOUS_NO_REDIS="0",
        NERVOUS_REDIS_URL=redis_server.url,
        **delivery,
    )

    assert proc.returncode == 5, proc.stderr
    assert redis_server.exists("nbus:big.snapshot.v1") == 0
    assert redis_server.xlen("nbus:bus.dead_letter") == 1
    # nbus:all holds the dead letter and nothing of the oversize original.
    fanout = redis_server.xrange("nbus:all")
    assert len(fanout) == 1
    assert fanout[0][1]["type"] == "bus.dead_letter"
    assert len(fanout[0][1]["_raw"]) < 4096


def test_dead_letter_stream_is_not_trimmed_by_a_producers_typed_maxlen(
    tmp_path, redis_server, delivery
) -> None:
    # bus.dead_letter is shared by every producer; one producer's own
    # NERVOUS_TYPED_MAXLEN must not trim it.
    for _ in range(8):
        proc = _publish(
            tmp_path,
            "big.snapshot.v1",
            _payload(LARGE),
            NERVOUS_NO_REDIS="0",
            NERVOUS_REDIS_URL=redis_server.url,
            NERVOUS_TYPED_MAXLEN="2",
            **delivery,
        )
        assert proc.returncode == 5, proc.stderr

    assert redis_server.xlen("nbus:bus.dead_letter") == 8
