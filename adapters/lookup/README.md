# adapters/lookup/ — first-class session-metadata enrichment

Hook publishers (e.g. `~/.claude/hooks/lib/nbus-publish.sh`) emit MINIMAL
identification: `session_id`, `project`, `pane_id`, `event`,
`conversation_id`. That's enough to be discoverable on the bus.

Consumers (peer-list, cc-bus-dashboard, dlq drill-down, anything that
RENDERS session info to a human) need richer fields: branch, status,
running cost, context %, beads active, model, current task. Those live
in upstream services (CCM, Hearth API, zellij). **Don't duplicate the
gathering logic in every consumer.** Use a lookup adapter.

## Contract

Each adapter is an executable at `adapters/lookup/<source>/identify`.
Invoked as:

```
adapters/lookup/<source>/identify <session_id>
```

Output (stdout): one JSON object. MUST include:

```json
{
  "session_id": "<the input>",
  "found": true | false,
  "source":  "<adapter name>"
}
```

When `found` is true, SHOULD include any of these (omit fields not known):

| Field | Type | Meaning |
|-------|------|---------|
| `slug` | string | human-friendly handle (`keen-dove`) — from hearth-api when present |
| `project_slug` | string | short project name (`tengine`) |
| `project_path` | string | full path (disambiguates worktrees) |
| `branch` | string | git branch |
| `status` | string | `active` / `idle` / `stopped` / etc |
| `provider` | string | `claudecode` / `opencode` / `loomie` |
| `last_activity` | RFC3339 | UTC timestamp |
| `cost_usd` | number | running session cost |
| `context_percent` | number | context window usage (0-100, clamp overflow) |
| `message_count` | integer | turns so far |
| `beads_active` | array | bead IDs currently being worked |
| `current_task_preview` | string | first ~80 chars of current_task, free of XML tags |
| `worktree_path` | string | when present, full worktree path |

Exit code: `0` on success (including `found: false`); non-zero on
infrastructure failure (adapter crashed, network error, etc).

## Calling order

Consumers iterate adapters in priority order from
`~/.config/nervous-bus/lookup.toml` (or the env var
`NBUS_LOOKUP_ADAPTERS=ccm,hearth-api,zellij`). First adapter that
returns `found: true` wins; partial results from later adapters MAY be
merged in to fill gaps.

If no config exists, the default order is:

```
ccm
zellij
```

## Available adapters

- `ccm/`        — queries `http://localhost:8420/api/sessions` (CCM REST)
- (planned) `hearth-api/` — when CCM splits out
- (planned) `zellij/`     — env-var fallback for sessions CCM doesn't know

## Testing an adapter

```bash
./adapters/lookup/ccm/identify e99f0547-8864-48df-8069-360631cbae41 | jq
./adapters/lookup/ccm/identify nonexistent-id | jq .found   # → false
```
