# nervous-bus — agent context

**The substrate.** CloudEvents-lite pub/sub over Redis Streams + per-language SDKs
that turn isolated projects into an observable ecosystem. deer-flow consumes via
SSE and files beads; hearth-loom picks up those beads and opens PRs; consumers
that need at-least-once delivery use XREADGROUP consumer groups.

**Also read `AGENTS.md`** (autonomous-worker contract: machine-readable acceptance
criteria, file-scope escalation, cross-repo bead pairing) — it is a separate document,
not imported here, and its rules bind interactive sessions doing bead work too.

## Architecture

`sdk/shell/nervous publish <channel> <json>` writes the envelope to
`~/.cache/nervous-bus/debug.jsonl` first, then XADDs it to `nbus:<channel>` and `nbus:all`.
redis-mirror (`adapters/redis-mirror/`) mirrors file-only producers into Redis.
The shell SDK and redis-mirror validate against `schemas/`, and violations go to
`nbus:bus.dead_letter`.

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
  **Check `schemas/README.md` § "Known private prefixes" before `git add` on a schema.**
  `.gitignore` and the `no-private-schema-leak` CI gate (`tools/private_schema_prefixes.txt`)
  block only the listed prefixes. A new private subsystem gets added there first.
  When in doubt: overlay first, public repo never.
- **NEVER bypass `nervous publish`.** All publishers go through the SDK.
  Direct `zellij pipe` or raw `redis-cli XADD` calls break schema validation and
  observability. tachyonac-engine uses the Go nbus client (`internal/nbus/`), which
  wraps the same CloudEvents envelope format.

  SDKs: prefer `sdk/rust/` for new in-process producers. `sdk/python/nbus.py` is a
  back-compat shim kept because `~/.local/bin/deer-bus-listen` symlinks to it, so add
  no Python there. `sdk/ts/` is removed.
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

hearth-loom picks up nervous-bus beads. A bead with machine-readable acceptance,
file scope and verification becomes a PR autonomously.

## Standing dashboards (survive compaction — check before re-deriving)

- `~/.cache/nervous-bus/ci-watch/report.md` — CI health snapshot.
- `~/.cache/nervous-bus/staleness/report.md` — producer/schema staleness.
- `~/.cache/nervous-bus/dlq/summary.json` — dead-letter queue summary.

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
