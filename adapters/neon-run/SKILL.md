---
name: neon-run
description: Drive one Orca agy worker lane without long-blocking shell calls using a harness-agnostic JSON CLI. Use when an orchestrator (Claude Code workflow, Codex, cron, or shell script) needs to start a worker on a fresh worktree, wait in bounded slices under 10 minutes, answer worker questions, or peek at status without consuming messages.
---

# neon-run

Run `neon-run` from `adapters/neon-run/`. JSON on stdout, errors on stderr, `neon-run <cmd> --help` is the reference.

| Question | Command |
|---|---|
| Start a worker lane on a new worktree | `neon-run start --repo <path> --spec <file> [--base origin/main] [--name N]` |
| Wait for done, question, or escalation | `neon-run wait <run> [--timeout 9m]` |
| Answer a worker's open question | `neon-run reply <run> --text "<answer>"` |
| Check worker state without consuming mail | `neon-run status <run>` |

Exit codes: `0` ok (including `pending` and `question` on `wait`), `1` negative result (`outcome: "failed"`, `escalation`, or missing worker), `2` usage error, `3` Orca unreachable.

Rules:

- `wait` always passes `--run <run>` so it never consumes another run's messages.
- `wait` enforces a hard cap of `9m50s` (`590000` ms) so a 10-minute shell tool timeout never kills the command mid-wait; loop on `{"state": "pending"}`.
- `status` uses `--peek` and never consumes unread messages.
- Set `ORCA_CLI_COMMAND` to point at a non-default `orca` binary (defaults to `orca` on `PATH`).
- Keep specs and objectives free of private paths or secrets when targeting public repos.
