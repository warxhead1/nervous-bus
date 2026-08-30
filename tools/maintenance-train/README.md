# maintenance-train

Scheduled sandboxed-MiniMax (mmx) workers that drain the cross-project
maintenance queue (beads_global `all_issues`, CI-red / dependabot / labelled
chores) and open PRs. Runs nightly at 05:00, sandwiched between the
tachyonac full-gate (03:30) and mmx-build-warnings (08:10) — a per-repo
`flock` (`~/.cache/nervous-bus/maintenance-train/locks/<repo>.lock`) also
makes the two jobs mutually exclusive on any repo they'd both touch.

## Pipeline

```
selector.py  -> ~/.cache/nervous-bus/maintenance-train/<date>/manifest.json
dispatch.sh <bead-id> [manifest-dir]
  -> data2 worktree (git worktree add, NOT mmx's own worktree machinery)
  -> bd update <bead-id> --claim
  -> brief-template.md rendered into the worktree
  -> claude-minimax-sandboxed launched in the BACKGROUND (CMMX_WORKDIR=<worktree>,
     CMMX_RW=1, CMMX_USE_WT=0, CMMX_NET=isolated)
finalize.sh <bead-id>
  -> blocks on the report file (or worker exit + grace period, or timeout)
  -> if no commits: release the bead with a bounded one-line note, done
  -> HOST re-runs build_cmd + test_cmd in the worktree (never trusts the
     sandbox's own claim)
  -> on pass: push branch, gh pr create, bd update --external-ref <pr-url>,
     publish bus.maintenance.pr.v1
  -> on fail: release the bead with a bounded failure note, worktree left
     for manual inspection
run-nightly.sh   -- systemd oneshot entrypoint: selector once, then
                    dispatch+finalize sequentially per entry (see below for why)
```

## Decision: plain `mmx` sandbox, not orca-native, for v1

Tested both paths before choosing (2026-08-30):

- **orca-native (`orca --agent mmx`)** needs Eric's pending orca relaunch —
  confirmed unavailable this session (per dispatch brief's ground facts).
  The alternative "works NOW" orca path (`orca worktree create` + `terminal
  create --command mmx-orca` + `terminal send`) requires a live orca UI
  session to drive the `terminal send` calls — it is built for an
  interactively-supervised dispatch, not a systemd oneshot with nobody
  watching. Making that path headless would mean scripting a `terminal send`
  choreography against a UI that assumes a human or a live supervising agent
  is present to react to its output. That is real future work, not a
  blocker to ship v1.
- **Plain `mmx` (`claude-minimax-sandboxed` directly)** has a genuine
  exit-code + report-file completion contract (`worker.exit`, the bead's
  contracted report path) that a oneshot systemd unit can poll
  synchronously with zero live supervision — exactly what a headless nightly
  timer needs. It also already has the `-push`/`-pr` host-side push
  contract documented (trust model: no credentials inside the sandbox,
  post-session host push uses the pre-bwrap-captured origin). We do NOT use
  `-push`/`-pr` directly, because that path pushes+PRs unconditionally on
  sandbox exit; finalize.sh needs to gate on a HOST-side build/test rerun
  first (a lane that trusts the sandbox's own "tests pass" claim is exactly
  the verification_failure bucket in `nervous-bus/adapters/reflex-recorder`).
  So dispatch.sh drives `claude-minimax-sandboxed` directly (RW, no -push),
  and finalize.sh owns push/PR after its own independent gate.

**v1 ships on plain mmx.** Upgrade path once Eric's orca relaunch lands:
swap `dispatch.sh`'s launch step for `orca orchestration worker-start
--agent mmx --worktree new-child`, keep `finalize.sh` unchanged (it only
cares about the worktree/branch/report-file contract, not who launched the
worker) — this is exactly the same shape as `orca-audit-dispatch`'s
run/task/worker-start/report-file wait.

## Sequential dispatch (not parallel) in v1

`run-nightly.sh` dispatches and finalizes one bead at a time, not the full
manifest concurrently. mmx sessions are heavy (bwrap + rootlesskit netns +
the model itself); running `MAX_REPOS_PER_NIGHT` (default 3) of them
concurrently on a nightly cron with nobody watching risks resource
contention with whatever else the box is doing at 05:00. Bounded blast
radius over throughput was the explicit tradeoff. Revisit if 3 sequential
mmx sessions can't fit inside `TimeoutStartSec=21600` in practice.

## Config

- `repo_config.py` — the allowlist. A repo not listed here is out of scope
  by construction; selector.py and dispatch.sh both fail closed on an
  unlisted repo rather than guessing a path or command.
- `MMTRAIN_MAX_REPOS` (env, default 3) — repos touched per night.
- `MMTRAIN_MAX_ESTIMATE_MIN` (env, default 60) — cap for the
  issue_type=chore + estimated_minutes maintenance class.

## Delivery status

Systemd units in `systemd/` are delivered, NOT enabled — per house rule,
Eric enables:
```
mkdir -p ~/.config/systemd/user
ln -s /home/eric/projects/nervous-bus/tools/maintenance-train/systemd/maintenance-train.service ~/.config/systemd/user/
ln -s /home/eric/projects/nervous-bus/tools/maintenance-train/systemd/maintenance-train.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now maintenance-train.timer
```

## KNOWN BLOCKER (2026-08-30): no MINIMAX_API_KEY on this box/session

`mmx`/`claude-minimax-sandboxed` require `MINIMAX_API_KEY` (env or
`~/.config/minimax/key` or `~/.minimax_key`). Checked all three at
2026-08-30T18:xx; none present in this session. This means the actual
sandboxed-worker step of the smoke test below could not be executed against
the real MiniMax endpoint from this session — the exact failure is captured
in the smoke-test section. Everything upstream (selector against live dolt,
dispatch.sh's worktree/claim/brief-render, the flock, and downstream
finalize.sh's host build/test/push/PR/bus-publish contract) is real code,
exercised against real infrastructure (live dolt at 127.0.0.1:39502, real
`gh`, a real bd bead, a real data2 worktree) — only the MiniMax API call
itself is blocked-on-key. See the smoke-test evidence at the bottom of the
dispatch report for the exact error and what unblocks it (drop a key at
`~/.config/minimax/key`, or export `MINIMAX_API_KEY`, then rerun
`dispatch.sh`/`finalize.sh` for the same bead — nothing else needs to change).
