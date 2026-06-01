from __future__ import annotations
import re, subprocess, time
from queue import Queue
from threading import Event
from .helpers import infer_level

# Poll interval — one burst every N seconds, zero persistent subprocesses.
_POLL_INTERVAL_S = 30

_INSTANCE_SUFFIX = re.compile(r'(-[0-9a-f]{8,}|-\d{8,})$', re.IGNORECASE)

def _channel_name(container_name: str) -> str:
    """Strip ephemeral instance suffixes for stable channel grouping.

    hearth-loom-agent-17783819408 → hearth-loom-agent
    tengine-silo-runner-abc12345def → tengine-silo-runner
    deer-flow-gateway → unchanged (no long suffix)
    """
    return _INSTANCE_SUFFIX.sub('', container_name)
# How many lines to fetch per container per poll.
_TAIL_LINES = 50
# Only follow containers whose name matches at least one of these prefixes.
# Empty list = follow all containers (use sparingly on busy machines).
_NAME_PREFIXES: tuple[str, ...] = (
    "hearth-loom",
    "tengine",
    "deer-flow",
    "nervous",
    "hearth",
)

def normalize_docker_line(line: str, container_name: str) -> dict | None:
    # Docker --timestamps lines: "2026-05-09T22:10:46.123456789Z actual message"
    parts = line.split(" ", 1)
    message = parts[1].strip() if len(parts) == 2 and parts[0].endswith("Z") else line.strip()
    if not message:
        return None
    return {
        "log_source": "docker",
        "service": _channel_name(container_name),
        "level": infer_level(message),
        "message": message[:500],
        "raw": line[:1000],
        "parsed_fields": {"container_name": container_name},
    }

def _poll_container(cid: str, name: str, since: str) -> list[str]:
    """Fetch up to _TAIL_LINES lines since `since` timestamp. Returns raw lines."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--timestamps", "--since", since,
             "--tail", str(_TAIL_LINES), cid],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        return (result.stdout + result.stderr).splitlines()
    except Exception:
        return []

def _relevant(name: str) -> bool:
    if not _NAME_PREFIXES:
        return True
    return any(name.startswith(p) for p in _NAME_PREFIXES)

def docker_source(q: Queue, stop: Event, filters: list[re.Pattern]) -> None:
    """Poll each relevant container every _POLL_INTERVAL_S seconds.

    Zero persistent subprocesses between polls — brief `docker logs --since`
    bursts instead of `docker logs --follow` per container. Trades ~30s
    latency for dramatically lower steady-state CPU.
    """
    # Track last-poll timestamp per container so we only fetch new lines.
    last_poll: dict[str, str] = {}

    while not stop.is_set():
        poll_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Get running containers.
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            current: list[tuple[str, str]] = []
            for row in result.stdout.strip().splitlines():
                if "\t" in row:
                    cid, cname = row.split("\t", 1)
                    if _relevant(cname):
                        current.append((cid[:12], cname))
        except Exception:
            current = []

        for cid, cname in current:
            since = last_poll.get(cid, "1970-01-01T00:00:00Z")
            lines = _poll_container(cid, cname, since)
            for line in lines:
                line = line.rstrip("\n")
                if any(f.search(line) for f in filters):
                    continue
                entry = normalize_docker_line(line, cname)
                if entry:
                    try:
                        q.put_nowait(entry)
                    except Exception:
                        pass
            last_poll[cid] = poll_ts

        # Prune containers that are no longer running.
        alive = {cid for cid, _ in current}
        for cid in list(last_poll):
            if cid not in alive:
                del last_poll[cid]

        stop.wait(_POLL_INTERVAL_S)
