#!/usr/bin/env python3
"""log-normalizer — collect Docker/journald/app/kernel/Redis logs → nbus:logs stream."""
from __future__ import annotations
import argparse, json, queue, re, sys, threading, time
from pathlib import Path
import redis as redis_lib

DEFAULT_CONFIG = Path(__file__).parent / "config.toml"

def _load_config(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    with open(path, "rb") as f:
        return tomllib.load(f)

def _build_filters(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]

def _xadd_batch(r: redis_lib.Redis, entries: list[dict], maxlen: int) -> None:
    pipe = r.pipeline(transaction=False)
    for entry in entries:
        fields: dict[str, str] = {}
        for k, v in entry.items():
            if k == "parsed_fields":
                fields["parsed_fields"] = json.dumps(v)
            else:
                fields[k] = str(v)[:2000]
        pipe.xadd("nbus:logs", fields, maxlen=maxlen, approximate=True)
    pipe.execute()

def main() -> int:
    parser = argparse.ArgumentParser(description="log-normalizer daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    cfg = _load_config(args.config)
    redis_url = cfg.get("redis", {}).get("url", "redis://localhost:6379")
    redis_db = int(cfg.get("redis", {}).get("db", 0))
    maxlen = cfg.get("streams", {}).get("maxlen", 100000)
    app_paths = cfg.get("sources", {}).get("app", {}).get("paths", [])

    r = redis_lib.Redis.from_url(redis_url, db=redis_db, decode_responses=True,
                                  socket_timeout=5.0, socket_connect_timeout=5.0)

    q: queue.Queue = queue.Queue(maxsize=10000)
    stop = threading.Event()

    sys.path.insert(0, str(Path(__file__).parent))
    from sources.docker_source import docker_source
    from sources.journal_source import journal_source
    from sources.app_source import app_source
    from sources.kernel_source import kernel_source
    from sources.redis_source import redis_source

    filters_cfg = cfg.get("filters", {})
    threads = [
        threading.Thread(target=docker_source,
            args=(q, stop, _build_filters(filters_cfg.get("docker", []))), daemon=True),
        threading.Thread(target=journal_source,
            args=(q, stop, _build_filters(filters_cfg.get("journal", []))), daemon=True),
        threading.Thread(target=app_source,
            args=(q, stop, app_paths, _build_filters(filters_cfg.get("app", []))), daemon=True),
        threading.Thread(target=kernel_source,
            args=(q, stop, _build_filters(filters_cfg.get("kernel", []))), daemon=True),
        threading.Thread(target=redis_source,
            args=(q, stop, r), daemon=True),
    ]
    for t in threads:
        t.start()

    batch: list[dict] = []
    last_flush = time.time()
    total = 0

    try:
        while True:
            try:
                entry = q.get(timeout=0.5)
                batch.append(entry)
            except queue.Empty:
                pass
            now = time.time()
            if len(batch) >= 50 or (batch and now - last_flush >= 2.0):
                try:
                    _xadd_batch(r, batch, maxlen)
                    total += len(batch)
                    batch.clear()
                except Exception as e:
                    sys.stderr.write(f"log-normalizer xadd error: {e}\n")
                    if len(batch) > 500:
                        batch = batch[-500:]  # cap retained entries during outage
                last_flush = now
    except KeyboardInterrupt:
        stop.set()
    return 0

if __name__ == "__main__":
    sys.exit(main())
