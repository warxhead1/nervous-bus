# crucible CLI

`crucible <group> ...` runs `crucible-<group>` found beside the dispatcher, then on PATH. A new group is a new executable; nothing is registered.

## crucible ledger

Records are append-only JSONL in `design/ledger/<date>.<writer>.jsonl` under the design worktree, written under one file lock. State is a fold of the records. The SQLite index is a cache under `$CRUCIBLE_BULK/<game>/index/` (default `~/.cache/crucible`).

Root: `--root`, else `$CRUCIBLE_DESIGN_ROOT`, else the nearest ancestor holding `design/ledger`.

| Verb | Does |
|---|---|
| `init --game G [--adopt row.json] [--repo R]` | create the ledger, adopt a hearth row as record one, optionally add the `design` worktree |
| `get [id] [--kind K] [--state S]` | folded entity or record |
| `why id` | records behind an id, following question origin and supersedes |
| `build [--check\|--land]` | generate views; `--check` exits 1 on drift; `--land` also commits on the design branch |
| `doctor [--self-test]` | parse, schema, orphan decision, view drift, steward heartbeat |
| `conflicts` | live decisions whose `touches` hit another's `constrains` |
| `reindex`, `validate`, `append` | index rebuild, record check, validated append |

Exit: 0 pass, 1 negative result, 2 usage, 5 stale heartbeat or environment.

Views: the decisions view is built in. Another lane adds views by shipping `crucible_<group>/views.py` with `render(records, state) -> {relative_path: text}`.

Contract: `contract/envelope.v1.json`, per-type body schemas in `contract/bodies/`, golden records in `contract/golden/`, a rejected record in `contract/invalid/`.

Heartbeat: `$CRUCIBLE_BULK/<game>/steward.heartbeat`; older than `$CRUCIBLE_HEARTBEAT_MAX_S` (900) makes `doctor` exit 5.

Tests: `python3 -m unittest discover -s adapters/crucible/tests -p 'test_ledger*'`.
