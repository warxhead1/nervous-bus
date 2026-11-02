# everos-watch

Feeds EverOS memory changes onto the bus so any system (hearth feed card, studio, kb) can react.

Polls the EverOS markdown memory root (source of truth), diffs entry-id sets per file, and publishes
through `sdk/shell/nervous publish` (never raw XADD):

| channel | meaning |
|---|---|
| `memory.node.created.v1` | new `<!-- entry:ID -->` block, new `user.md`, new `SKILL.md` |
| `memory.node.updated.v1` | same entry id, body changed (profile rewrite, skill overwrite, edited entry) |
| `memory.node.deleted.v1` | entry removed from its file, or file deleted |

Payload is ids and metadata ONLY: `app_id, project_id, owner_kind, owner_id, kind, entry_id,
session_id, md_path (root-relative), observed_at`. Never content. Content is hashed in the local
state file (mode 0600) solely to detect in-place updates.

## Privacy: private app scopes

`app_id=studio` (override with `--private-app`, repeatable) is a private data class. For those
events `owner_id` and `session_id` are omitted, the owner segment of `md_path` becomes
`_redacted_`, and `redacted: true` is set. Decision: omit rather than emit a hash-of-hash. The
owner id is already a salted fan hash; a second stable hash is still a stable per-fan key that any
bus consumer could correlate across events, which is exactly what the class forbids. Consumers get
counts per `(app_id, project_id)` from the subject and events, not identities. `session_id` is
withheld too because studio session ids are `thread:<id>` references.

## Behavior

- Polling (default 2 s) with a 1 s settle window: a file whose mtime is younger than the window is
  skipped and retried, so an atomic rename write (staged in `.tmp/`, `os.replace`d into place) is one
  event. `.index/`, `.tmp/` and `knowledge/` are never scanned.
- First run with no state file takes a silent baseline (no replay of history). `--emit-existing`
  treats the first scan as all-created. Later runs resume from `~/.local/state/everos-watch/state.json`
  and report changes made while the watcher was down.
- At-least-once: if `nervous publish` fails, that file's state is not advanced and the next poll retries.
- `default_app` / `default_project` on disk are reported as `default`.

## Run

```bash
python3 adapters/everos-watch/everos_watch.py --dry-run --once --emit-existing   # print, never publish
python3 adapters/everos-watch/everos_watch.py                                    # publish, loop
python3 -m pytest adapters/everos-watch -q
```

Env: `EVEROS_ROOT`, `NERVOUS_BIN` (default: repo `sdk/shell/nervous`).

## Install (operator, not done by the build)

`everos-watch.service` is a user unit with the same fail-closed egress env as `hearth-everos.service`:

```bash
cp adapters/everos-watch/everos-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now everos-watch
```
