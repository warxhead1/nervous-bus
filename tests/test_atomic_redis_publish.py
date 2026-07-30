"""Live Redis falsifiers for atomic nervous-bus publication.

The tests use unique keys and never touch production stream names.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import redis


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "redis_mirror", ROOT / "adapters" / "redis-mirror" / "mirror.py"
)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def redis_client() -> redis.Redis:
    client = redis.Redis.from_url(
        os.environ.get("NERVOUS_TEST_REDIS_URL", "redis://127.0.0.1:6379"),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis unavailable: {exc}")
    return client


def publish(client: redis.Redis, event_id: str, stream: str, universal: str) -> bool:
    return mirror.atomic_publish(
        client,
        event_id=event_id,
        stream_name=stream,
        universal_stream=universal,
        fields={
            "_raw": f'{{"id":"{event_id}","type":"atomic.test.v1"}}',
            "type": "atomic.test.v1",
            "event_id": event_id,
        },
        trim_strategy="MAXLEN",
        maxlen=100,
        min_idle_ms=0,
        universal_stream_maxlen=100,
    )


def test_concurrent_same_id_creates_exactly_one_row_per_stream() -> None:
    client = redis_client()
    suffix = uuid.uuid4().hex
    event_id = f"atomic-concurrent-{suffix}"
    dedup = f"nbus:dedup:{event_id}"
    stream = f"test:nbus:channel:{suffix}"
    universal = f"test:nbus:all:{suffix}"
    keys = [dedup, stream, universal]
    client.delete(*keys)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: publish(client, event_id, stream, universal),
                    range(2),
                )
            )
        assert sorted(results) == [False, True]
        assert client.xlen(stream) == 1
        assert client.xlen(universal) == 1
        assert client.get(dedup) == "1"
    finally:
        client.delete(*keys)


def test_failed_second_stream_write_cannot_publish_dedup_marker() -> None:
    client = redis_client()
    suffix = uuid.uuid4().hex
    event_id = f"atomic-failure-{suffix}"
    dedup = f"nbus:dedup:{event_id}"
    stream = f"test:nbus:channel:{suffix}"
    invalid_universal = f"test:nbus:not-a-stream:{suffix}"
    keys = [dedup, stream, invalid_universal]
    client.delete(*keys)
    client.set(invalid_universal, "wrong-type")
    try:
        with pytest.raises(redis.RedisError):
            publish(client, event_id, stream, invalid_universal)
        assert client.get(dedup) is None
        assert client.exists(stream) == 0
        assert client.type(invalid_universal) == "string"
    finally:
        client.delete(*keys)
