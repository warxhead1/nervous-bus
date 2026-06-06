# nervous-bus — agent context

**The substrate.** CloudEvents-lite pub/sub over Redis Streams + per-language SDKs
that turn isolated projects into an observable ecosystem. deer-flow consumes via
SSE and files beads; hearth-loom picks up those beads and opens PRs; consumers
that need at-least-once delivery use XREADGROUP consumer groups.

## Architecture (one paragraph)

**Primary transport: Redis Streams.** `sdk/shell/nervous publish <channel> <json>`
XADD's a CloudEvents-lite envelope to `nbus:<channel>` (e.g. `nbus:bus.notify.v1`)
and to the fan-in stream `nbus:all`. The redis-mirror adapter (`adapters/redis-mirror/`)
tails `~/.cache/nervous-bus/debug.jsonl` and mirrors file-only producers (tengine,
home-automation.vision) into Redis. The zellij WASM plugin remains active for
plugin-to-pane fan-out (zjstatus, nervous-board) but is best-effort — it never
blocks the Redis write path. All producers write to `debug.jsonl` first (durable
history), then fire-and-forget to Redis (sub-ms live delivery). Schema validation
runs in both the shell SDK (on publish) and redis-mirror (on mirror); violations
are dead-lettered to `nbus:bus.dead_letter`. Schemas in `schemas/<type>.v<n>.json`
are authoritative and hot-reloaded by redis-mirror every 5 minutes.

## Critical rules

- **Schema first, code second.** Every channel needs `schemas/<type>.v<n>.json` BEFORE
  any publisher emits. Breaking changes bump the major version, never silent edits.
  **Enforced by:** `.github/workflows/schema-coverage.yml` — scans producers in
  hearth-loom + deer-flow on every PR and fails if any emitted channel lacks a
  schema. Add new channels to `tools/schema_coverage_allowlist.txt` ONLY if
  they're internal-only and never cross the bus.
- **This repo is PUBLIC — private contracts go in `$NERVOUS_HOME/schemas/`, NOT here.**
  Anything sensitive (trading PnL/positions/orders, account/venue data, proprietary
  params, private integrations, internal diagnostics) belongs in the local overlay
  (`~/.config/nervous-bus/schemas/`, loaded alongside repo schemas and precedence-wins
  for the same channel). Install via `nervous schema install <path>`.
  **NEVER `git add` a schema without checking `schemas/README.md` § "Known private
  prefixes".** The `.gitignore` blocks the listed prefixes, but new private subsystems
  are not blocked until added. When in doubt: overlay first, public repo never.
  Current private prefixes: `tachyonos.*` (trading engine), `tengine.diag.*`
  (internal diagnostics), `hearth.market.state.*` (tachyonos bridge, raw_signal
  leak vector). Full list and rationale in `schemas/README.md`.
- **NEVER bypass `nervous publish`.** All publishers go through the SDK.
  Direct `zellij pipe` or raw `redis-cli XADD` calls break schema validation and
  observability. tachyonac-engine uses the Go nbus client (`internal/nbus/`), which
  wraps the same CloudEvents envelope format.

  **SDK matrix:**
  - **`sdk/shell/nervous`** — universal escape hatch. Any language can shell out.
    All adapters in `adapters/*/` use this pattern. Always available.
  - **`sdk/rust/`** — typed library for compiled producers/consumers. Scaffolded
    (v0 wraps the shell SDK); v1 (native pipe + Listener, no subprocess hop)
    tracked in nervous-bus-xnn. Prefer Rust for new in-process producers.
  - **`sdk/python/nbus.py`** — back-compat shim; the listener moved into
    deer-flow's `deer obs bus` subcommand. The file remains as a bash shim
    because `~/.local/bin/deer-bus-listen` symlinks to it. Don't add new
    Python code here — call `deer obs bus` or shell out to `nervous publish`.
  - **`sdk/ts/`** — removed (no producers required it).
- **The plugin is dumb.** Routing only — no business logic, no schema validation
  in v1, no state beyond a small ring buffer. Validation happens at adapter edges.
- **deer-flow is a consumer, not a peer.** It subscribes; it does not authenticate
  the bus. The bus has no auth in v1 (it's per-user, in-process via zellij).
- **OSC 133 emission, if added, is opt-in.** A pane-tagging adapter (`aid=<project>`
  prompt marks) must never inject without explicit shell config. Not yet shipped.

## Build / test

```bash
# Plugin
cd plugin && cargo build --release --target wasm32-wasi

# Shell SDK (no build, just chmod)
chmod +x sdk/shell/nervous && sdk/shell/nervous --help

# Rust SDK (when implemented)
cd sdk/rust && cargo test

# Schema validation
python3 -c "import json,jsonschema; jsonschema.Draft202012Validator(json.load(open('schemas/<name>.v1.json'))).validate(json.load(open('sample.json')))"
```

## Beads

All work is tracked in `.beads/`. Open work:

```bash
bd ready          # what's ready
bd list --status=open
```

hearth-loom is configured to pick up nervous-bus beads. Sophisticated beads
(machine-readable acceptance + file scope + verification) become PRs autonomously.

## Cross-project links

- **tengine** — emits `tengine.session.*` from shadergen + silo_tester
- **kb** — emits events for knowledge base lifecycle (entry.created, session.context,
  guidance.provided, knowledge.gap, review lifecycle). Private schemas live in
  `$NERVOUS_HOME/schemas/` if you have kb installed.
- **hearth-loom** — emits `loom.lifecycle.*` AND consumes `bus.bead.*`
- **deer-flow** — consumes everything via `/api/bus/sse`, emits `deer-flow.thread.*`
- Private integrations (trading engines, home automation, etc.) bridge via
  `$NERVOUS_HOME/adapters/` — source and service units stored there, not in this repo.

## Conventions

- Channel naming: `<project>.<subsystem>.<event>` (lowercase, dot-separated)
- Schema filenames: `<channel>.v<n>.json` (semver major only)
- Source URIs: `/<project>/<subsystem>` (path-style, never URL with host)
- IDs: ULID (sortable, monotonic, 26 chars)
- All times: RFC3339 UTC, never local

## What's intentionally NOT here

- **Auth** — single-user, single-machine in v1
- **Persistence** — events are ephemeral; consumers durably store what they care about
- **Replay** — deer-flow's `/api/threads/search` already provides agent-level replay;
  the bus is a fire-hose, not a journal
- **Schema validation in plugin** — keep the plugin tiny; validate at adapter edges


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

<!-- END BEADS INTEGRATION -->
