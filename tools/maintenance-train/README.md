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

## LIVE SMOKE TEST (2026-08-30) — full end-to-end pass, two real bugs found+fixed

Filed a real bead (`nervous-bus-rp9a`, "fix gofmt drift on 15 top-level
files" — the shared `claude-hook-fast` checkout was verified clean via
`git status --porcelain` before dispatch), ran `selector.py --dry-run`
against it (correctly deprioritized behind three older CI-red beads under
the `MMTRAIN_MAX_REPOS=3` cap — that's the ranking working as designed, not
a bug), then manually seeded a 1-entry manifest and ran `dispatch.sh` /
`finalize.sh` for real. `MINIMAX_API_KEY` turned out to be present in the
session environment the whole time (a prior file-only check
`~/.config/minimax/key` came up absent and an earlier attempt to also check
`$MINIMAX_API_KEY` got cut off by an unrelated sandbox guard before it could
print — that gap in verification, not an actual missing key, is why this
section originally read "blocked"). Two real defects surfaced and were
fixed in this same commit:

1. **Report path was outside the sandbox mount.** `claude-minimax-sandboxed`
   only bind-mounts `CMMX_WORKDIR` (+ claude binary/cargo/`/bus`) — nothing
   under `~/.cache` is visible inside the sandbox. The first dispatch had
   the worker "successfully" write its completion report to
   `$SESSION_DIR/report.md` (outside the worktree); the worker's own
   transcript claimed success, the file never existed on host, and
   `finalize.sh` correctly failed the run rather than trusting the claim.
   Fixed: `REPORT_PATH` now lives at `$WORKTREE/.maintenance-report.md`
   (inside the mount); dispatch.sh's background subshell copies it to the
   stable `$SESSION_DIR/report.md` only after the worker exits.
2. **`release_bead` cleared assignee but left status `in_progress`.**
   `bd update --claim` sets `status=in_progress`; clearing only the
   assignee left the bead unclaimable (`issue not claimable: status
   in_progress`) on the very next dispatch attempt. Fixed: `release_bead`
   now also passes `--status open`.
3. **The per-repo flock is held for the FULL worker lifetime, including
   orphaned `rootlesskit`/`slirp4netns` helper processes** that outlive the
   `claude-minimax-sandboxed` parent exiting — this is correct/intentional
   (the worktree is being mutated for that whole window) but means a
   `flock -n` probe from `finalize.sh`/manual debugging can look "stuck"
   for the true duration of an mmx session (~4-6 min observed), not a hang.
   Documented here so it isn't mistaken for a deadlock on the next run.

After the fixes, a clean rerun went fully end-to-end for real:
- Worker committed `9c3c672` on `maint/nervous-bus-rp9a` (`gofmt -w` on the
  17 actually-drifted files — the bead said 15, the worker's own report
  flagged and corrected the discrepancy rather than silently matching the
  bead's wrong count).
- Host re-verification (independent of the sandbox's own claim):
  `go build ./...` exit 0, `go vet ./...` exit 0, `go test ./...` — all
  packages `ok` (`cmd/hearth-loom-hook` 7.0s, `tools/claude-hooks` 6.0s,
  rest cached/instant), `gofmt -l .` (worktrees excluded) empty.
- **PR opened**: https://github.com/warxhead1/claude-hook-fast/pull/2
  ("[maint] nervous-bus-rp9a: chore(claude-hook-fast): fix gofmt drift on
  15 top-level files", state OPEN, real commit `9c3c6725` authored by
  `MiniMax Sandbox <mmx-sandbox@hearth-loom>`).
- Bead `nervous-bus-rp9a` updated: `External: https://github.com/warxhead1/
  claude-hook-fast/pull/2`, notes carry the PR-opened timestamp.
- `bus.maintenance.pr.v1` publish attempt logged `[nbus warn] no schema for
  type: bus.maintenance.pr.v1` — expected and harmless: the shared
  `nervous-bus` checkout's schema dir doesn't have this PR's own
  `schemas/bus.maintenance.pr.v1.json` yet (it ships in the same PR that
  adds this pipeline); resolves once this branch merges to main.

No blocker remains for the mmx dispatch path itself; the two real defects
found by exercising it are fixed above.
