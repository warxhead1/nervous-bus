# redis-mirror — Redis Streams adapter for nervous-bus

Opt-in Redis Streams transport that mirrors selected bus channels for durable replay, cross-process fan-out, and history.

## Architecture

redis-mirror is a **pure consumer** — it subscribes to nervous-bus via the same JSONL-tail pattern as `cc-bus-dashboard`. It never publishes to the bus or modifies plugin behavior.

```
nervous-bus (debug.jsonl)  →  redis-mirror  →  Redis Streams (nbus:<channel>)
                                     ↑
                           Optional XREAD subscribers
                           (cc-news-headlines, downstream consumers)
```

## Behavior

1. **JSONL tailing** — follows `~/.cache/nervous-bus/debug.jsonl` with inode-rotation awareness (survives log rotation).

2. **Channel filtering** — only events whose `type` starts with a configured prefix are mirrored. All other events are silently ignored.

3. **Stream writing** — `XADD nbus:<type> MAXLEN ~N * key value...` where `type` is the full event type (e.g. `home-automation.news.article.v1`).

4. **Retention** — `MAXLEN ~N` trims older entries as the stream grows. `MINID` strategy with `min_idle_ms` is also supported.

5. **Fail-soft** — when Redis is unreachable or config is empty, the adapter stays quiet and continues retrying.

6. **Offset persistence** — tail position is saved to `~/.cache/nervous-bus/redis-mirror-offset.json` after each batch, enabling restart resilience.

## Configuration

Edit `config.toml`:

```toml
[channels]
types = [
    "home-automation.news.article",
    "bus.bead",
    "loom.lifecycle",
]

[redis]
url = "redis://localhost:6379"
db = 0
connect_timeout_s = 5.0

[streams]
maxlen = 10000           # MAXLEN ~N: keep last N entries
trim_strategy = "MAXLEN"  # or "MINID" for time-based trimming
min_idle_ms = 0           # for MINID strategy

[offset_file]
path = "~/.cache/nervous-bus/redis-mirror-offset.json"

[metrics]
interval_s = 60.0  # metrics log interval
```

## Usage

```bash
# Default: uses config.toml in the same directory
python mirror.py

# Explicit config
python mirror.py --config /path/to/config.toml

# Test mode: tail existing log, process, exit
python mirror.py --once

# Custom log path
python mirror.py --log /path/to/debug.jsonl
```

## Redis Stream Layout

Each mirrored event produces:

```
XADD nbus:home-automation.news.article.v1 MAXLEN ~10000 *
  event_id     "<uuid>"
  timestamp    "<RFC3339>"
  data.id      "<uuid>"
  data.title   "<article headline>"
  data.url     "<canonical url>"
  data.source  "<publisher>"
  data.published_at "<RFC3339>"
  data.classification_tier "1"
  data.relevance_score "0.85"
  _raw         "<original JSON, truncated to 2000 chars>"
```

## XREAD Subscriber Idiom

Downstream consumers use `XREAD` (or `XREADGROUP` for consumer groups) to subscribe:

```bash
# Block for 30s waiting for new events on a specific channel
redis-cli XREAD BLOCK 30000 STREAMS nbus:home-automation.news.article.v1 $

# Read all existing events
redis-cli XRANGE nbus:home-automation.news.article.v1 - +

# Consumer group for distributed processing
redis-cli XGROUP CREATE nbus:home-automation.news.article.v1 mygroup $ MKSTREAM
redis-cli XREADGROUP GROUP mygroup consumer1 STREAMS nbus:home-automation.news.article.v1 >
```

## Metrics

Every `interval_s` (default 60s), the adapter logs to stderr:

```
[2026-05-03T12:00:00Z] redis-mirror metrics: mirrored=1234 dropped=5 redis_errors=0 rate=20.57/s
```

Fields:
- `mirrored` — total events successfully XADD'd
- `dropped` — events skipped (Redis unreachable or filtered)
- `redis_errors` — connection/publish failures
- `rate` — mirrored events per second (cumulative average)

## Running as a user service

The shipped unit (`systemd/redis-mirror.service`) is a **user** service — it expands `%h` to your home directory and does not need root. Install:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/redis-mirror.service ~/.config/systemd/user/nervous-redis-mirror.service
systemctl --user daemon-reload
systemctl --user enable --now nervous-redis-mirror.service
```

Inspect:

```bash
systemctl --user status nervous-redis-mirror.service
journalctl --user -u nervous-redis-mirror.service -f
```

Disable:

```bash
systemctl --user disable --now nervous-redis-mirror.service
```

If you prefer not to use systemd, run it from a tmux/zellij pane:

```bash
python3 ~/projects/nervous-bus/adapters/redis-mirror/mirror.py
```

## Adding / removing mirrored channels

Edit the `[channels].types` list in `config.toml` and restart the service:

```bash
$EDITOR ~/projects/nervous-bus/adapters/redis-mirror/config.toml
systemctl --user restart nervous-redis-mirror.service
```

Each entry is a *prefix* match against the event `type` field — `bus.bead` mirrors `bus.bead.created`, `bus.bead.closed`, etc. Pre-existing streams in Redis are not affected when you add or remove channels; remove unwanted streams manually with `redis-cli DEL nbus:<channel>`.

## Verification

```bash
# Publish a test event (after home-automation.news.article schema bead lands)
nervous publish home-automation.news.article.v1 '{"id":"test-1","title":"Test","url":"http://example.com","source":"test","published_at":"2026-05-03T12:00:00Z","classification_tier":1,"relevance_score":0.5}'

# Check stream within 1s
redis-cli XRANGE nbus:home-automation.news.article.v1 - + COUNT 1

# Verify channels not in config never produce XADD
redis-cli MONITOR | grep XADD  # should only see configured channels
```

## Schema

Configuration shape is defined by `schemas/bus.redis-mirror.config.v1.json`.