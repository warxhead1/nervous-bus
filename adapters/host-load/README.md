# host-load

Read-only attribution of host CPU, memory, paging and pressure to PROJECTS and AGENT SESSIONS, plus
detectors for wasted load. Separate from `system-resource` on purpose: that adapter's contract is
"no process inspection". Never signals or reweights anything; `~/.local/bin/game-priority-watch`
owns policy (this adapter only reads its state).

    who-is-loading                       # sample 3s: top projects + flagged inefficiencies
    who-is-loading --json
    who-is-loading --agents [--hours N]  # per agent session; --hours reads history
    who-is-loading --who TEXT            # every process of an agent/project/command
    who-is-loading --record [--emit]     # append to ~/.cache/nervous-bus/host-load/history.sqlite3
    who-is-loading --tune [--hours N]    # history distributions + fire rate of each threshold
    who-is-loading --html out.html --hours 6   # PSI lines + stacked CPU/RSS by project
    cc-bus-dashboard --tab 5             # live tab over the same history

History comes from `systemd/host-load.timer` (copy both units to `~/.config/systemd/user/`,
`systemctl --user daemon-reload`, `systemctl --user enable --now host-load.timer`). 7-day retention.
The unit runs `--record --emit`; one run takes about 6 s wall, 1 s CPU (CPUQuota 15%).

## CPU: NOW vs AVG

NOW is the short sample window. AVG integrates every process, keyed by (pid, start time), over the whole
gap since the previous `--record` run, children's reaped time included (cutime+cstime, subtracted from
the parent for children already seen so nothing is counted twice; reapers such as systemd, dockerd and
containerd-shim get no child credit). `coverage` is observed CPU over host busy CPU from /proc/stat:
the shortfall is processes that started and exited between runs without ever being seen. Gaps over
15 minutes, or across a reboot, fall back to NOW.

## Attribution

Order: docker compose label (verbatim) > cwd under `~/projects/<p>`, `~/data2/worktrees/<p>/...`,
`~/data2/orca/<p>` > such a path in argv > cgroup unit (`unit:<name>`) > `kernel` (kernel threads) >
`user-session` > `unknown`. A process whose cwd is unreadable falls back to its cgroup, and docker scope
members get a bounded `docker inspect` lookup. The residual `unknown` is printed with its reasons.

Agent sessions: key `<project>/<worktree>` or `claude@<project>#<pid>`. Resolved from a live claude/codex
ancestor, else the worktree path in cwd/argv, else (flagged pids only) an allow-listed environ read:
ORCA_WORKTREE_ID, ORCA_PANE_KEY, CODEX_COMPANION_SESSION_ID. Tokens in the environment are never parsed.

## Paging

Swap churn is attributed to who CAUSES it, not just who holds swap: per-process major-fault deltas, leaf
cgroup `memory.stat` deltas (`workingset_refault_anon` is swap thrash, `workingset_refault_file`,
`memory.events high` throttling, `memory.current` growth) and vmstat pswpin/pswpout. A memory_pressure
finding carries these under `evidence.causes`. Per-process `/proc/<pid>/io` read_bytes is recorded as
disk reads and is NOT swap; do not read it as paging.

## Detectors

`orphan_cpu` (busy shell/worktree process reparented to `systemd --user` or pid 1; excused when it is a
unit MainPID, holds a listening socket, leads a small dedicated scope, or has a pidfile newer than the
process; a shell spin loop is excused only as a unit MainPID), `stale_cwd`, `cpu_loop` (>=0.8 core for
>=10 min, ~no io), `duplicate_build` (same tool+verb+project, >=2 top-level; "same target" only when cwd
and args match), `memory_pressure`, `cpu_pressure`. Thresholds: `hl_detect.DEFAULTS`, tuned with
`--tune` against recorded history.

## Outputs

- Bus: `bus.host.load.finding.v1` on level change only (2-sample confirm, 6 h reminder, recovery event,
  retry on publish failure). Critical orphan_cpu / memory_pressure also publish `bus.notify.v1`.
  Publishing goes through `nervous publish`, never raw XADD.
- Prometheus/Grafana: `nbus_host_load_*` gauges from `adapters/exporter` (reads this history read-only);
  dashboard JSON and scrape/rule files are under `adapters/exporter/`.
- game-priority-watch: POLICY column and mode header from `systemctl --user show` and the last `mode=`
  journal line. Read-only.

Tests: `python3 -m unittest discover -s adapters/host-load -p 'test_*.py'`
