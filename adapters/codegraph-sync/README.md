# codegraph-sync — keep opted-in codegraph indexes fresh

`codegraph` (the CLI, not its MCP server) answers symbol, caller and
blast-radius questions from a local `.codegraph/` index. The CLI does not
watch files, so an index goes stale until someone runs `codegraph sync`.
`kb brief` reads these indexes for its code-grounding section. This adapter
makes freshness automatic.

## Why a timer, not a bus event

The index tracks a **local checkout's working tree**. No bus event reports
that tree changing. Four days of `debug.jsonl` (2026-11-11 to 2026-11-14)
held no commit, merge, push or pull event. `bus.agent.activity.v1` tags a
git call as `cmd:git` without the subcommand. `ci.pipeline.status.v1` and
`hearth-loom.pr.merged.v1` describe GitHub, which never changes a local
tree by itself. A timer that fingerprints the checkout catches every path:
commits, pulls, merges, branch switches and uncommitted edits.

## How it works

```
codegraph-sync.timer (every 5 min)
  -> sync.py
       for each repo in ~/.config/kb/config.yaml `codegraph.repos` with .codegraph/codegraph.db:
         fingerprint = HEAD + sha256(git status --porcelain -z + dirty-path mtimes)
         unchanged since last sync            -> nothing
         changed, same as last tick           -> codegraph sync -q <repo>   (trigger: quiescent)
         still moving after 30 min pending    -> codegraph sync -q <repo>   (trigger: max_pending)
         -> publish codegraph.index.synced.v1
       state: ~/.cache/nervous-bus/codegraph-sync/state.json
```

An idle pass costs two `git` calls per opted-in repo. The debounce means a
burst of commits produces one sync, 5 to 10 minutes after the burst ends.

## Opt in

One list serves `kb brief` and this adapter: `codegraph.repos` in
`~/.config/kb/config.yaml`. A repo needs both the entry and an index:

```yaml
codegraph:
  repos:
    - name: kb                     # must equal the kb --project name
      path: /home/eric/projects/kb # the shared checkout, never a worktree
```

```bash
cd /home/eric/projects/kb && codegraph init -y   # builds .codegraph/, self-gitignored
```

Index only repos agents query repeatedly. `init` cost about 4.5 min and
139 MB on 1,570 files, and 4.3 s and 16 MB on kb's 148 files.

## Install

```bash
cp adapters/codegraph-sync/systemd/codegraph-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codegraph-sync.timer
```

The unit runs `sync.py` from its own clone of origin/main under
`~/.local/share/nervous-bus/codegraph-sync-src`, fetched on every tick. The
shared checkout can be on any branch, frozen or dirty and the timer is
unaffected. A failed fetch keeps the last good clone. Delete that directory to
force a fresh clone.

Resource policy is in the unit itself: `Slice=build.slice` (the batch slice),
`CPUWeight=10`, `Nice=19`, `IOSchedulingClass=idle`. It runs alongside
`game-priority-watch` and is not a second watcher.

## Run and test

```bash
python3 sync.py --dry-run          # decide, never sync or publish
python3 -m pytest -q test_sync.py
journalctl --user -u codegraph-sync -n 20
```
