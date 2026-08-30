# deploy-staleness — watch the deploys, not just the code

Detects long-running processes (systemd `--user` units, and unmanaged
processes like AppImages) that are executing OLDER code than what is
currently on disk. Measured motivation (one sweep, 2026-08-30):

1. `kb-watch.service` ran the Aug 24 `kb` binary for ~6h after
   `~/.local/bin/kb` was replaced with a critical fix.
2. The running `orca` AppImage (pid started Sat 2026-08-29 13:44 from
   `dist/orca-linux.AppImage`) predated both a same-day dist rebuild (13:15)
   and every fix merged that day — the fuse mount pins the OLD build's
   contents even after the underlying file is overwritten.
3. Generally: any systemd user service whose `ExecStart` source changed
   after `ExecMainStartTimestamp` is quietly serving stale code, and neither
   "merged to main" nor "tests green" can ever catch this — both checks are
   about the repo, and this failure mode lives entirely in the gap between
   the repo and the process.

## Architecture

```
systemctl --user show / /proc  →  check.py  →  bus.deploy.stale.v1  (via `nervous publish`)
                                        ↓
                               ~/.cache/nervous-bus/deploy-staleness/
                                 state.json     (per-target verdict + alert cooldown)
                                 report.md      (human-readable standing report)
                                 summary.json   (machine-readable, same data as report.md)
```

## Two passes

1. **systemd `--user` units.** `Type=oneshot` units are always skipped — a
   oneshot re-execs fresh on every activation, so "staleness" doesn't apply.
   For everything else, `ExecStart` is parsed to find the real code target
   (the script path for an interpreter-backed unit, e.g. `python3
   dlq.py`; the binary itself for a binary-backed unit, e.g. `kb watch
   --live`). That target's freshness is:
   - **script-backed** (target lives inside a git repo): `max(file mtime,
     last commit touching that path)` — a checkout mtime alone can lie
     (e.g. after a fresh clone or worktree reset), so a later git commit
     timestamp always wins.
   - **binary-backed** (no git repo, e.g. `~/.local/bin/kb`): file mtime,
     UNLESS `/proc/<pid>/exe` resolves to a `(deleted)` link — a running
     process holding an unlinked (replaced) inode open — which is **stale**
     regardless of any timestamp math.
   Verdict: stale iff the code's freshest timestamp is AFTER
   `ExecMainStartTimestamp`.

2. **AppImage / unmanaged processes** (`roster.json` `appimage_matchers`,
   matched against `/proc/<pid>/cmdline` directly — never `pgrep`, which
   would match its own subprocess wrapper). For each match, `ps -o lstart=
   -p <pid>` gives the process start time; the launch file named in
   argv[0] of that same `/proc/<pid>/cmdline` gives the code mtime.
   File-newer-than-process-start is stale: this is the fuse-mount case, and
   it can never be caught by pass 1 since it's not a systemd unit.

## Roster (`roster.json`) — auto-discovery + overrides

`check.py` never hardcodes a unit list. It auto-discovers every
`Type=simple/exec/notify/forking/dbus` user unit whose resolved code target
lives under a configured `project_roots` or `local_bin_roots` prefix, then
applies `include_units` (roster additions outside those roots) and
`exclude_units` (roster exclusions, e.g. this adapter's own oneshot unit —
belt-and-suspenders since oneshots are already always skipped).
`appimage_matchers` is a list of `{"name", "cmdline_contains"}` for
unmanaged long-running processes.

## Publish + dedupe

One `bus.deploy.stale.v1` event per **transition** — first-seen-stale, or a
previously-stale target recovering to fresh — plus a persistent-stale
reminder at most once per 24h (matches the `staleness`/`ci-watch` cooldown
convention), never one event per 15-minute timer tick for a target that has
correctly stayed stale. Dedup key: `unit:<name>` or
`process:<matcher-name>:<pid>` (a fresh process replacing a stale one gets a
new pid, so it starts a clean transition history rather than inheriting the
old process's stale state).

## Usage

```bash
python3 check.py                 # real run: evaluate, publish transitions, write report + summary
python3 check.py --dry-run       # compute + report, never call `nervous publish`
```

Outputs:
- `~/.cache/nervous-bus/deploy-staleness/state.json` — per-target verdict + alert cooldown state
- `~/.cache/nervous-bus/deploy-staleness/report.md` — human-readable standing report (stale / fresh / unknown tables)
- `~/.cache/nervous-bus/deploy-staleness/summary.json` — same data, machine-readable
- `bus.deploy.stale.v1` events on the bus (schema: `schemas/bus.deploy.stale.v1.json`)

## Running as a systemd user timer

```bash
mkdir -p ~/.config/systemd/user
cp systemd/deploy-staleness.service ~/.config/systemd/user/nervous-deploy-staleness.service
cp systemd/deploy-staleness.timer   ~/.config/systemd/user/nervous-deploy-staleness.timer
systemctl --user daemon-reload
# NOT enabled by this adapter — opt in explicitly:
# systemctl --user enable --now nervous-deploy-staleness.timer
```

## Tests

```bash
python3 test_check.py -v
```

19 tests, all I/O (`systemctl`, `ps`, `/proc`) stubbed via injected functions
— covers stale-by-mtime, stale-by-git-commit, stale-by-deleted-exe,
oneshot-skip, auto-discovery root filtering + roster overrides, AppImage
file-newer-than-process, and transition dedupe (first-seen, cooldown-
suppressed repeat, recovery, persistent-stale reminder after cooldown).

## Real run, 2026-08-30 (measured, not simulated)

A real `--dry-run` sweep against this box found exactly the incidents that
motivated this adapter, plus two more nobody had flagged yet:

| target | verdict | reason | running since | code newest |
|---|---|---|---|---|
| `kb-watch.service` | **fresh** | mtime | 2026-08-30T20:47:05Z | 2026-08-30T19:50:18Z |
| `appimage:orca` (pid 1500947) | **stale** | file_newer_than_process | 2026-08-29T17:44:55Z | 2026-08-30T17:15:04Z |
| `opencode-serve.service` | **stale** | deleted_exe | 2026-08-24T15:31:26Z | 2026-08-30T20:53:20Z |
| `app-orca\x2dide@...service` (x2) | **stale** | mtime | 2026-08-28/29 | 2026-08-30T17:15:04Z |

`kb-watch.service` correctly reads fresh (restarted 16:47 local time same
day the fix landed). The orca AppImage's long-running GUI process (pid
1500947, started Aug 29) reads stale exactly as predicted; two newer orca
CLI invocations from the SAME binary (pids started after the Aug 30 rebuild)
correctly read fresh in the same run, confirming the verdict tracks the
process, not just the binary. `opencode-serve.service` and the
`app-orca-ide@` sessiond units were not in the motivating incident list —
found by auto-discovery, not the roster.
