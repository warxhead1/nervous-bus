#!/usr/bin/env python3
"""Batch publisher behind `nervous publish-batch <channel>`.

Reads one JSON payload per stdin line and publishes each as a CloudEvents envelope,
with the same checks as `nervous publish` run once per batch instead of once per event:
the nervous.toml allowlist, the byte guard, schema validation, one append to debug.jsonl
and one Redis connection. Prints one JSON summary line on stdout.

    publish_batch.py [--maxlen N] [--max-bytes N] <channel>

Exit codes: 0 every line published, 2 usage, allowlist denial or at least one rejected
line (the valid lines were still published; the summary names the rejected ones),
4 strict validation unavailable (nothing published), 1 strict Redis delivery failed
(events are durable in debug.jsonl).
A rejected line is dead-lettered as bus.dead_letter, like a single publish.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO_SCHEMAS = HERE.parent.parent / "schemas"
LOG_MAX = 524288000
DEAD_LETTER_MAXLEN = 10000
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}\Z")

DEDUP_SCRIPT = """
local dedup_key = KEYS[1]
if dedup_key ~= '' and redis.call('EXISTS', dedup_key) == 1 then
  return 0
end
local channel_type = redis.call('TYPE', KEYS[2])['ok']
local universal_type = redis.call('TYPE', KEYS[3])['ok']
if (channel_type ~= 'none' and channel_type ~= 'stream') or
   (universal_type ~= 'none' and universal_type ~= 'stream') then
  return redis.error_reply('nervous-bus stream key has incompatible type')
end
local fields = {}
for i = 4, #ARGV do
  fields[#fields + 1] = ARGV[i]
end
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', unpack(fields))
redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[3], '*', unpack(fields))
if dedup_key ~= '' then
  redis.call('SET', dedup_key, '1', 'EX', ARGV[1])
end
return 1
"""


def warn(msg: str) -> None:
    print(f"nervous: {msg}", file=sys.stderr)


def ulid() -> str:
    ms, out = int(time.time() * 1000), ""
    for _ in range(10):
        out, ms = CROCKFORD[ms & 31] + out, ms >> 5
    return out + "".join(CROCKFORD[b & 31] for b in secrets.token_bytes(16))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class UsageError(Exception):
    """A bad command line; the caller exits 2."""


def valid_int(raw: Optional[str], minimum: int) -> bool:
    return bool(raw) and re.fullmatch(r"[0-9]{1,15}", raw) is not None and int(raw) >= minimum


def int_limit(flag: Optional[str], env: str, default: int, minimum: int) -> int:
    """Flag (already validated), then a valid env value, then the default."""
    for raw in (flag, os.environ.get(env)):
        if valid_int(raw, minimum):
            return int(raw)
    return default


def debug_log() -> Path:
    return Path(os.environ.get("NERVOUS_DEBUG_LOG") or Path.home() / ".cache" / "nervous-bus" / "debug.jsonl")


def channel_allowed(channel: str) -> bool:
    """The nearest nervous.toml's [channels] allowlist; no file or no list means allowed."""
    if os.environ.get("NERVOUS_SKIP_ALLOWLIST") == "1":
        return True
    for d in (Path.cwd(), *Path.cwd().parents):
        toml = d / "nervous.toml"
        if toml.is_file():
            import tomllib
            try:
                patterns = tomllib.loads(toml.read_text()).get("channels", [])
            except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
                raise UsageError(f"cannot read the allowlist {toml}: {exc}") from exc
            if not isinstance(patterns, list) or not all(isinstance(x, str) for x in patterns):
                raise UsageError(f"the allowlist {toml}: channels must be a list of strings")
            return not patterns or any(fnmatch(channel, p) for p in patterns)
    return True


def schema_for(channel: str) -> Optional[dict]:
    home = Path(os.environ.get("NERVOUS_HOME") or Path.home() / ".config" / "nervous-bus")
    for d in (home / "schemas", REPO_SCHEMAS):
        f = d / f"{channel}.json"
        if f.is_file():
            return json.loads(f.read_text())
    return None


class Validator:
    """Schema check for one channel. `available` is False when jsonschema is missing."""

    def __init__(self, channel: str):
        self.schema = None if os.environ.get("NBUS_SKIP_VALIDATION") == "1" else schema_for(channel)
        self.available, self.check = True, None
        if self.schema is None:
            return
        try:
            import jsonschema
        except ImportError:
            self.available = False
            return
        self.envelope_schema = "specversion" in self.schema.get("required", [])
        self.check = jsonschema.validators.validator_for(self.schema)(self.schema)

    def violation(self, envelope: dict) -> Optional[str]:
        if self.check is None:
            return None
        subject = envelope if self.envelope_schema else envelope.get("data", {})
        err = next(iter(self.check.iter_errors(subject)), None)
        return None if err is None else str(err.message)[:500]


def envelope_for(channel: str, source: str, data, traceparent: str) -> dict:
    env = {"specversion": "1.0", "id": ulid(), "source": source, "type": channel}
    if traceparent:
        env["traceparent"] = traceparent
    env.update(time=now_iso(), datacontenttype="application/json", data=data)
    return env


def dead_letter(source: str, data: dict) -> dict:
    return envelope_for("bus.dead_letter", source, data, "")


def stream_fields(env: dict, raw: str) -> list:
    return ["_raw", raw, "type", env["type"], "source", env.get("source", ""),
            "timestamp", env.get("time", ""), "event_id", env.get("id", "")]


def eval_args(env: dict, raw: str, typed: int, fanout: int) -> list:
    eid = env.get("id", "")
    cap = DEAD_LETTER_MAXLEN if env["type"] == "bus.dead_letter" else typed
    return [("nbus:dedup:" + eid) if eid else "", f"nbus:{env['type']}", "nbus:all",
            600, cap, fanout, *stream_fields(env, raw)]


def resp(parts: list) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    for p in parts:
        b = str(p).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(b), b))
    return b"".join(out)


def xadd_python(url: str, events: list, typed: int, fanout: int) -> bool:
    try:
        import redis
    except ImportError:
        return False
    try:
        r = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=30)
        script = r.register_script(DEDUP_SCRIPT)
        pipe = r.pipeline(transaction=False)
        for env, raw in events:
            a = eval_args(env, raw, typed, fanout)
            script(keys=a[:3], args=a[3:], client=pipe)
        pipe.execute()
        return True
    except Exception as exc:
        warn(f"python redis batch failed: {exc}")
        return False


def xadd_rediscli(url: str, events: list, typed: int, fanout: int) -> bool:
    cli = shutil.which("redis-cli")
    if not cli:
        return False
    body = b"".join(resp(["EVAL", DEDUP_SCRIPT, 3, *eval_args(env, raw, typed, fanout)]) for env, raw in events)
    try:
        r = subprocess.run([cli, "-u", url, "--pipe"], input=body, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and f"errors: 0, replies: {len(events)}".encode() in r.stdout


def deliver(events: list, typed: int, fanout: int) -> str:
    """Append every envelope to debug.jsonl in one write, then XADD them over one connection."""
    if not events:
        return "none"
    log = debug_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.is_file() and log.stat().st_size > LOG_MAX:
        os.replace(log, str(log) + ".1")
    with open(log, "a", encoding="utf-8") as f:
        f.write("".join(raw + "\n" for _, raw in events))
    if os.environ.get("NERVOUS_NO_REDIS") == "1":
        return "file-only"
    url = os.environ.get("NERVOUS_REDIS_URL") or "redis://localhost:6379"
    if xadd_python(url, events, typed, fanout) or xadd_rediscli(url, events, typed, fanout):
        return "redis"
    warn("WARNING: Redis delivery failed for publish-batch (events durable in debug.jsonl)")
    return "file-only"


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def too_big(raw: str, limit: int) -> bool:
    """True when the envelope is over `limit` bytes and must be refused; policy warn lets it through."""
    size = len(raw.encode())
    if limit == 0 or size <= limit:
        return False
    if os.environ.get("NERVOUS_OVERSIZE_POLICY") == "warn":
        warn(f"WARNING: payload is {size} bytes, over the {limit} byte limit; publishing anyway (NERVOUS_OVERSIZE_POLICY=warn)")
        return False
    return True


def classify_line(n: int, text: str, ctx: dict) -> tuple[Optional[dict], Optional[dict]]:
    """(envelope, None) for an accepted line, (None, rejection) otherwise."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, {"line": n, "reason": "invalid_json"}
    env = envelope_for(ctx["channel"], ctx["source"], data, ctx["traceparent"])
    raw = dumps(env)
    if too_big(raw, ctx["max_bytes"]):
        return None, {"line": n, "reason": "payload_too_large", "excerpt": raw[:500], "bytes": len(raw.encode())}
    detail = ctx["validator"].violation(env)
    if detail is not None:
        return None, {"line": n, "reason": "schema_violation", "detail": detail, "excerpt": raw[:500]}
    return env, None


def dead_letter_for(rej: dict, ctx: dict) -> dict:
    data = {"failure_reason": rej["reason"], "original_type": ctx["channel"], "original_source": ctx["source"],
            "original_payload_excerpt": rej.get("excerpt", "")}
    if "detail" in rej:
        data["schema_violation_detail"] = rej["detail"]
    if "bytes" in rej:
        data.update(payload_bytes=rej["bytes"], limit_bytes=ctx["max_bytes"])
    return dead_letter(ctx["source"], data)


CHANNEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def flag_value(name: str, it, minimum: int) -> str:
    raw = next(it, None)
    if not valid_int(raw, minimum):
        raise UsageError(f"{name} needs an integer >= {minimum}")
    return raw


def parse_args(argv: list) -> tuple[Optional[str], Optional[str], Optional[str]]:
    maxlen = max_bytes = channel = None
    it = iter(argv)
    for a in it:
        if a == "--maxlen":
            maxlen = flag_value(a, it, 1)
        elif a == "--max-bytes":
            max_bytes = flag_value(a, it, 0)
        elif a.startswith("--") or channel is not None:
            raise UsageError(f"unexpected argument {a}")
        else:
            channel = a
    if channel is not None and not CHANNEL_RE.match(channel):
        raise UsageError(f"channel name {channel!r} must match [A-Za-z0-9][A-Za-z0-9._-]*")
    return channel, maxlen, max_bytes


def ctx_validator_missing_schema(channel: str) -> bool:
    return os.environ.get("NBUS_SKIP_VALIDATION") != "1" and schema_for(channel) is None


def warn_raw(msg: str) -> None:
    print(msg, file=sys.stderr)


def build_ctx(channel: str, maxlen: Optional[str], max_bytes: Optional[str]) -> dict:
    tp = os.environ.get("NERVOUS_TRACEPARENT", "")
    if tp and not TRACEPARENT.match(tp):
        warn("WARNING: NERVOUS_TRACEPARENT is not a valid W3C traceparent, dropping it")
        tp = ""
    if ctx_validator_missing_schema(channel):
        warn_raw(f"[nbus warn] no schema for type: {channel}")
    return {"channel": channel, "source": os.environ.get("NERVOUS_SOURCE") or "/" + Path.cwd().name,
            "traceparent": tp, "validator": Validator(channel),
            "max_bytes": int_limit(max_bytes, "NERVOUS_MAX_PAYLOAD_BYTES", 16384, 0)}


def collect(lines, ctx: dict) -> tuple[int, list, list]:
    """(accepted count, rejections, events to deliver) for the stdin lines; each rejection adds a dead letter."""
    accepted, rejected, events = 0, [], []
    for n, text in enumerate(lines, 1):
        if not text.strip():
            continue
        env, rej = classify_line(n, text, ctx)
        if rej:
            rejected.append(rej)
            env = dead_letter_for(rej, ctx)
        else:
            accepted += 1
        events.append((env, dumps(env)))
    return accepted, rejected, events


def preflight(ctx: dict) -> int:
    """Exit code that stops the batch before any line is read, or 0."""
    if not channel_allowed(ctx["channel"]):
        print(f"ERROR: channel '{ctx['channel']}' not in allowlist", file=sys.stderr)
        return 2
    if not ctx["validator"].available and os.environ.get("NERVOUS_STRICT_VALIDATION") == "1":
        print(f"[nbus schema] strict mode: aborting publish for {ctx['channel']} (validation unavailable)", file=sys.stderr)
        return 4
    return 0


def strict_redis_failed(delivery: str) -> bool:
    return delivery == "file-only" and os.environ.get("NERVOUS_NO_REDIS") != "1" and os.environ.get("NERVOUS_STRICT_REDIS") == "1"


def run(channel: str, maxlen: Optional[str], max_bytes: Optional[str], lines) -> int:
    ctx = build_ctx(channel, maxlen, max_bytes)
    stop = preflight(ctx)
    if stop:
        return stop
    accepted, rejected, events = collect(lines, ctx)
    delivery = deliver(events, int_limit(maxlen, "NERVOUS_TYPED_MAXLEN", 10000, 1), int_limit(None, "NERVOUS_FANOUT_MAXLEN", 50000, 1))
    print(dumps({"channel": channel, "published": accepted, "delivery": delivery,
                 "validation": "ok" if ctx["validator"].available else "skipped",
                 "rejected": [{k: v for k, v in r.items() if k in ("line", "reason")} for r in rejected]}))
    if strict_redis_failed(delivery):
        return 1
    return 2 if rejected else 0


def main(argv: list) -> int:
    usage = "usage: nervous publish-batch [--maxlen N] [--max-bytes N] <channel>  (JSONL payloads on stdin)"
    try:
        channel, maxlen, max_bytes = parse_args(argv)
        if not channel:
            raise UsageError(usage)
        return run(channel, maxlen, max_bytes, sys.stdin)
    except UsageError as exc:
        print(f"nervous publish-batch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
