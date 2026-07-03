# nervous-bus

A lightweight, per-user event bus that turns a collection of sibling projects into a living ecosystem — every tool, agent, and background service can observe and react to what the others are doing in real time.

**Transport:** Redis Streams + CloudEvents-lite envelopes.  
**Schema discipline:** every channel has a JSON Schema before any producer emits.  
**Zero server code:** the bus is a convention + a shell script. Nothing to deploy beyond Redis.

```
                         nervous-bus ecosystem
                        ──────────────────────

  producers          tengine  │  deer-flow  │  hearth-loom  │  kb  │  your-project
                                         │
                              nervous publish <channel> <json>
                                         │
                              ┌──────────▼──────────┐
                              │   debug.jsonl        │  ← durable, always written
                              │   ~/.cache/nbus/     │
                              └──────────┬──────────┘
                                         │  redis-mirror (tail → XADD)
                              ┌──────────▼──────────┐
                              │   Redis Streams      │  ← live fan-out
                              │   nbus:<channel>     │
                              │   nbus:all           │
                              └──────────┬──────────┘
                                         │
  consumers          cc-bus-dashboard  │  Prometheus exporter  │  signal-router
                     silo-watcher      │  pattern-bundler      │  your-consumer
```

The [zellij](https://zellij.dev) WASM plugin (`plugin/`) provides optional in-terminal fan-out for pane-to-pane routing but is not required — Redis Streams is the primary path.

---

## Why

Projects that live side-by-side on a developer's machine generate rich internal state, but that state is invisible across project boundaries. Nervous-bus exposes a typed, named-channel pub/sub so:

- A dashboard shows live cross-project activity in one pane
- An autonomous agent (hearth-loom) sees lifecycle events and triggers work
- A knowledge base (kb) ingests events and builds a persistent context layer
- An eval harness (autobench) publishes progress events visible to everything else

The design constraint: **single user, single machine, no auth, no persistence beyond what consumers choose to store**. The bus is a fire-hose. Keep it simple.

---

## Quick start

### 1. Install Redis

```bash
# macOS
brew install redis && brew services start redis

# Arch / Manjaro
sudo pacman -S redis && sudo systemctl enable --now redis

# Ubuntu / Debian
sudo apt install redis-server && sudo systemctl enable --now redis
```

### 2. Install the shell SDK

```bash
git clone https://github.com/warxhead1/nervous-bus
cd nervous-bus
chmod +x sdk/shell/nervous
cp sdk/shell/nervous ~/.local/bin/nervous   # or symlink

nervous setup    # bootstraps ~/.config/nervous-bus/
```

### 3. Start redis-mirror

redis-mirror tails `debug.jsonl` and fans events into Redis Streams so any consumer can XREAD/XREADGROUP without touching the log file.

```bash
pip install redis jsonschema tomli
python adapters/redis-mirror/mirror.py &
```

Or install the systemd unit:

```bash
cp adapters/redis-mirror/systemd/redis-mirror.service ~/.config/systemd/user/
systemctl --user enable --now redis-mirror
```

### 4. Publish your first event

```bash
nervous publish hello.world.v1 '{"msg": "first event"}'
```

Verify it landed in Redis:

```bash
redis-cli XRANGE nbus:hello.world.v1 - + COUNT 5
```

---

## Wire format

Every event is a single JSON line — a CloudEvents-lite envelope:

```json
{
  "specversion": "1.0",
  "id": "01JVMC3P4NRFQ2T8XABCDE1234",
  "source": "/tengine/silo/racing",
  "type": "tengine.session.frame.v1",
  "subject": "silo_racing_20260501_203715",
  "time": "2026-05-01T23:51:00Z",
  "datacontenttype": "application/json",
  "data": {"fps": 47.3, "hot_wt": 164, "hot_ms": 2.4}
}
```

| Field | Convention |
|---|---|
| `specversion` | always `"1.0"` |
| `id` | ULID — sortable, monotonic per source |
| `source` | URI-path style: `/<project>/<subsystem>` |
| `type` | `<project>.<channel>.<event>.v<n>` — dot-separated, versioned |
| `time` | RFC3339 UTC |
| `data` | channel-specific payload; schema in `schemas/<type>.json` |

---

## Schema-first discipline

Every channel requires a JSON Schema **before** any producer emits. Breaking changes bump the major version (`v1` → `v2`) as a new file; old schemas remain.

**Where schemas live:** the `schemas/` directory at the repo root is the authoritative store — one file per channel, flat, named `<type>.json` (the version is part of the type, e.g. `tengine.session.frame.v1.json`). This directory is the complete source of truth for the *public* bus, so listing `schemas/` is a valid way to see every public channel. At runtime the validator also overlays private channels from `$NERVOUS_HOME/schemas/` (see [NERVOUS_HOME](#nervous_home--private-schema-and-adapter-overlay)); user schemas win on a name conflict. Use `nervous schemas` to enumerate the full merged set rather than assuming `schemas/` is everything a given machine has loaded.

```bash
nervous schemas          # list all known channels (repo schemas/ + NERVOUS_HOME overlay)
nervous schema install path/to/my.channel.v1.json   # add a private schema to NERVOUS_HOME
```

The shell SDK validates every `nervous publish` call against the resolved schema automatically. Violations are dead-lettered to `nbus:bus.dead_letter` — not silently dropped.

---

## SDKs

### Shell (universal)

Works from any language or script. Zero dependencies beyond bash + python3 (for Redis XADD and validation).

```bash
nervous publish tengine.silo.verify.v1 '{"silo":"racing","success":true,"fps_p50":61.2}'
nervous tail tengine.silo.verify.v1     # subscribe (tails debug.jsonl)
nervous dlq                             # show recent dead-letter events
nervous dlq --since 1h --reason schema_violation
```

### Rust

```toml
# Cargo.toml
nbus = { path = "sdk/rust" }
```

```rust
use nbus::publish;
use serde_json::json;

publish("tengine.silo.verify.v1", &json!({
    "silo": "racing",
    "success": true,
}))?;
```

The default (`subprocess`) feature shells out to `nervous publish`. The `native` feature appends the envelope straight to `debug.jsonl` (and pipes to the Zellij plugin when running inside Zellij), skipping the `nervous` CLI; Redis delivery still happens via the redis-mirror adapter that tails `debug.jsonl`. See `sdk/rust/src/lib.rs` for the v1 status.

For real consumer-group Redis Streams support (`XREADGROUP`/`XACK`/`XAUTOCLAIM`, not just publish), enable the `streams` feature — see `sdk/rust/README.md`.

### Python

The Python shim (`sdk/python/nbus.py`) wraps the shell SDK and is kept for backwards compatibility. For new consumers, shell out to `nervous publish` directly or use the `deer obs bus` subcommand if deer-flow is in your stack.

---

## Adapters

| Adapter | What it does |
|---|---|
| `adapters/redis-mirror/` | Tails `debug.jsonl` → XADD to `nbus:<channel>` and `nbus:all`. Primary fan-out path. |
| `adapters/silo-watcher/` | Watches `~/.tengine/sessions/` and emits `tengine.silo.started.v1` / `tengine.silo.verify.v1` without modifying tengine. |
| `adapters/exporter/` | Prometheus `/metrics` endpoint — event rates, session activity, autobench scores. Pair with Grafana for long-haul charts. |
| `adapters/pattern-bundler/` | Windowed stats over `nbus:all` → `nbus:bundles`. Drives the signal-router anomaly pipeline. |
| `adapters/signal-router/` | Consumes `bus.pattern.signal.v1`, routes by confidence tier, optionally auto-files beads. |
| `adapters/pattern-watchdog/` | Health check for consumer-group lag; emits `bus.intrinsic.marker.v1` on stall. |
| `adapters/log-normalizer/` | Normalises raw log lines from multiple sources into typed bus events. |
| `adapters/zjstatus/` | Format bus events for zjstatus (zellij status bar). |
| `adapters/dlq/` | Dead-letter queue inspector and replay tool. |

---

## Dashboards

### cc-bus-dashboard

A btop-style terminal dashboard showing live bus traffic across all channels.

```bash
python adapters/dashboard/cc-bus-dashboard
```

Tabs: **bus** (channel rates, top talkers, live stream, alerts) · **sysmap** (containers, runners, GPU, host vitals grouped by project) · **agents** (active Claude Code sessions) · **loomies** (hearth-loom agent state).

Press `1`–`4` to switch tabs. Source: `~/.cache/nervous-bus/debug.jsonl`.

### cc-sysmap

Standalone system-map panel — Docker containers, systemd services, GPU utilisation, and bus event rates per project. Can run embedded in cc-bus-dashboard tab 2 or standalone.

```bash
python adapters/dashboard/cc_sysmap.py
python adapters/dashboard/cc_sysmap.py --tick 3   # slower refresh
```

### autobench-pulse

Live Textual dashboard for autobench (FunSearch / RSI eval harness) observability. Shows iteration scores, sandbox verdicts, improver calls, AHE prediction outcomes.

```bash
pip install -e autobench/          # installs with the submodule checked out
python -m pulse_app --prefer-bus   # live tail
python -m pulse_app --once         # one-shot dump
```

### Prometheus + Grafana

```bash
python adapters/exporter/prometheus_exporter.py   # :9418/metrics (--port to change)
```

Import `adapters/exporter/dashboards/` for pre-built Grafana panels covering event throughput, session counts, and autobench scoring trends.

---

## NERVOUS_HOME — private schema and adapter overlay

`nervous-bus` is designed to be used with project-specific private schemas (channels that describe your internal infrastructure) alongside the public schemas in this repo. Private schemas live in `~/.config/nervous-bus/schemas/` and are loaded transparently by both the shell SDK and redis-mirror — same channel-name resolution, user schemas win on conflict.

```bash
nervous setup                              # creates ~/.config/nervous-bus/
nervous schema install path/to/schema.json # add a private schema
nervous schema list                        # list installed private schemas
nervous adapter list                       # list private adapters in NERVOUS_HOME
```

Set `NERVOUS_HOME` to override the default location. This pattern also applies to adapters that bridge private services — source lives in `~/.config/nervous-bus/adapters/`, not in the public repo.

---

## Sister projects

### kb

[`kb`](https://github.com/warxhead1/kb) is a knowledge base CLI written in Rust that integrates with nervous-bus as a first-class citizen. It emits structured events when entries are created, vetted, and cited — making your knowledge base observable alongside all your other tools.

```bash
kb check "redis streams backpressure"    # emits kb.knowledge.gap.v1 if coverage is low
kb landmark "latent heat oracle fix"     # emits kb.entry.created.v1
kb vet <entry-id>                        # emits kb.entry.vetted.v1
```

Both repos are designed to be cloned side-by-side. kb consumes the bus for cross-session continuity; the bus consumes kb schemas for its dead-letter enrichment pipeline.

### autobench (submodule)

The `autobench/` directory is a git submodule pointing at [`nervous-autobench`](https://github.com/warxhead1/nervous-autobench) — a FunSearch + RSI evaluation harness for evolving GPU kernels (SDF, TSP, SPH, Allen-Cahn PDE, terrain). It publishes dozens of event types to the bus (see the `autobench.*` schemas in `schemas/`) covering iteration progress, sandbox verdicts, and evolved kernel candidates.

The submodule is optional — the core bus works without it.

---

## Conventions

| Thing | Convention |
|---|---|
| Channel type | `<project>.<subsystem>.<event>.v<n>` — lowercase, dot-separated |
| Schema filename | `<channel-type>.json` in `schemas/` |
| Source URI | `/<project>/<subsystem>` — path style, never a URL with host |
| IDs | ULID — 26 chars, sortable, monotonic |
| Timestamps | RFC3339 UTC, never local time |

---

## What's intentionally not here

- **Auth** — single user, single machine. The bus is in-process by design.
- **Persistence** — `debug.jsonl` is the durable log; the bus is a fire-hose. Consumers store what they care about.
- **Replay** — consumers hold their own history. The bus doesn't journal.
- **Schema validation in the WASM plugin** — keep the plugin routing-only and tiny. Validation happens at the SDK edge.
- **Multi-host** — Redis Streams can fan across hosts trivially, but the schema contracts and source URIs assume a single-user local setup.

---

## Build

```bash
# WASM plugin (optional — Redis path works without it)
cd plugin && cargo build --release --target wasm32-wasi
cp target/wasm32-wasi/release/nervous_bus.wasm ~/.config/zellij/plugins/

# Rust SDK
cd sdk/rust && cargo test

# Schema validation smoke test
python3 -c "
import json, jsonschema, pathlib
for s in pathlib.Path('schemas').glob('*.json'):
    jsonschema.Draft202012Validator.check_schema(json.loads(s.read_text()))
print('all schemas valid')
"
```

---

## License

MIT
