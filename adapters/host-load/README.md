# host-load

Read-only attribution of host CPU, memory and pressure to PROJECTS, plus detectors for wasted load.
Separate from `system-resource` on purpose: that adapter's contract is "no process inspection".
Never signals or reweights anything; `~/.local/bin/game-priority-watch` owns policy.

    who-is-loading                       # sample 3s: top projects + flagged inefficiencies
    who-is-loading --json
    who-is-loading --record              # append to ~/.cache/nervous-bus/host-load/history.sqlite3
    who-is-loading --html out.html --hours 6   # PSI lines + stacked CPU/RSS by project
    cc-bus-dashboard --tab 5             # live tab over the same history

History comes from `systemd/host-load.timer` (copy both units to `~/.config/systemd/user/`,
`systemctl --user enable --now host-load.timer`). 7-day retention.

Attribution order: docker compose label (verbatim) > cwd under `~/projects/<p>`,
`~/data2/worktrees/<p>/...`, `~/data2/orca/<p>` > such a path in argv > cgroup unit
(`unit:<name>`, pid digits stripped) > `unknown`. A process the sampler cannot read (other uid's cwd)
falls through to cgroup or `unknown`; the `unknown` share is printed.

Detectors: `orphan_cpu` (busy shell/worktree process reparented to `systemd --user` or pid 1, not a
service main process), `stale_cwd`, `cpu_loop` (>=0.8 core for >=10 min, ~no io in window),
`duplicate_build` (same tool+verb+project, >=2 top-level; "same target" only when cwd and args match),
`memory_pressure` (swap rate / kswapd / mem PSI; swap shown is VmSwap currently held, not who caused
paging), `cpu_pressure`. Thresholds: `hl_detect.DEFAULTS`.

Tests: `python3 -m unittest discover -s adapters/host-load -p 'test_*.py'`
