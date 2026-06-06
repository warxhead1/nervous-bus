# Event schemas

Authoritative CloudEvents-lite contracts for the **public** nervous-bus
ecosystem. One file per channel: `<channel>.v<major>.json`
(e.g. `agent.session.v1.json`). redis-mirror hot-reloads this directory every
5 minutes; the shell SDK and redis-mirror both validate publishes against it.

## Public vs. private — read this before adding a schema

This repo is **public**. Only schemas for the open ecosystem belong here
(`agent.*`, `autobench.*`, `bus.*`, `deer-flow.*` public channels,
`loom.*`, `tengine.session.*`, `tengine.shadergen.*`, `tengine.race.*`,
`tengine.gpu.*`, `tengine.frame.*`, `tengine.silo.*`, `tengine.code.*`,
kernel channels, etc.). **Not all `tengine.*` subspaces are public** —
see the private-prefix table below.

**Private / org-specific contracts do NOT go in this repo.** They live in the
local overlay `$NERVOUS_HOME/schemas/` (default `~/.config/nervous-bus/schemas/`),
which redis-mirror loads *alongside* these and which takes precedence for the
same channel name. Install one with:

```bash
nervous schema install path/to/<channel>.v1.json
```

If a schema describes anything that should not be world-readable — trading
positions/PnL, order or venue data, account identifiers, proprietary strategy
parameters, anything from a private integration — it is **private**. Put it in
the overlay, never here.

### Known private prefixes (never commit to this repo)

| Prefix | Owner | Why private |
|---|---|---|
| `tachyonos.*` | tachyonac-engine | Trading / prediction-market: PnL, positions, trades, venue & order data |
| `tengine.diag.*` | tengine | Internal diagnostics: WT telemetry, scheduling internals, TSDL activation details |

**All entries in this table are `.gitignore`d.** They cannot be staged accidentally.
When adding a new private subsystem: (1) add the glob to `.gitignore`, (2) add a
row here, (3) install the schema into the overlay with `nervous schema install`.

If you find a private schema already tracked here: move it to the overlay
(`cp schemas/X.json ~/.config/nervous-bus/schemas/`) then `git rm` it and push.
The bus keeps working — the overlay copy serves the same channel.

## Conventions

- Channel naming: `<project>.<subsystem>.<event>` (lowercase, dot-separated)
- Filename: `<channel>.v<n>.json` (semver **major** only)
- Breaking changes bump the major version — never silently edit a published `v<n>`
- Draft 2020-12 JSON Schema; prefer `additionalProperties: false`
