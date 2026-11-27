# neon-run

Blocking-free CLI so any orchestrator (Claude Code workflows, Codex, cron, or a plain shell) can drive one Orca agy worker lane. Python 3 stdlib only.

```sh
neon-run start --repo <path> --spec <file> [--base origin/main] [--name N] [--model gemini-4-neon-medium] [--agent antigravity] [--objective TEXT]
neon-run wait <run> [--timeout 9m]
neon-run reply <run> --text TEXT
neon-run status <run>
```

Every command emits one JSON object on stdout and writes diagnostics to stderr. Each command reads `ORCA_CLI_COMMAND` (default `orca`).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (`start`, `reply`, `status`, `wait` returning `done` with `outcome: "succeeded"`, `pending`, or `question`) |
| `1` | Negative result (`wait` returning `done` with `outcome: "failed"` or `escalation`; missing worker/run) |
| `2` | Usage error (bad flags, invalid timeout, missing spec file, repo not registered in Orca) |
| `3` | Orca unreachable (CLI missing, daemon not reachable, non-JSON response) |

## Commands

### `start`

Creates a dedicated Orca run (`orca orchestration run-create`) and launches one worker on a new top-level worktree (`orca orchestration worker-start --worktree new-top-level --run <run>`).

Output:

```json
{"run": "run_abc123", "dispatch": "ctx_def456", "terminal": "term_789", "worktree": "/path/to/worktree", "branch": "neon-task"}
```

### `wait`

Blocks at most `--timeout` (default `9m`, hard-capped at `9m50s` / `590000` ms so calling shell tools with a 10-minute deadline never kill the process) on:

```sh
orca orchestration check --run <run> --wait --types worker_done,question,escalation --timeout-ms <ms> --full --json
```

Only messages belonging to `<run>` are consumed. Output is one of:

```json
{"state": "pending"}
{"state": "done", "outcome": "succeeded", "branch": "neon-task", "sha": "abc1234", "files": ["a.py"], "body": "..."}
{"state": "question", "id": "msg_123", "body": "Which base branch?"}
{"state": "escalation", "body": "Blocked: missing dependency"}
```

### `reply`

Looks up the run's active dispatch via `orca orchestration worker-list --run <run>` and delivers `--text` through `orca orchestration send --to dispatch:<id>`.

Output:

```json
{"ok": true, "run": "run_abc123", "dispatch": "ctx_def456", "messageId": "msg_456"}
```

### `status`

Reads `worker-list --run <run>` and `check --run <run> --peek --full` so the caller can inspect worker liveness and unread messages without consuming mail.

## Using with a Claude Code workflow

A lightweight Claude Code workflow can run one `haiku` subagent per lane in a simple loop:

1. The `haiku` lane agent calls `neon-run start --repo <repo> --spec <spec>` and captures `run`.
2. It loops `neon-run wait <run> --timeout 9m` while `state == "pending"`, returning the JSON payload to the orchestrating script as soon as `state` is `done`, `question`, or `escalation`.
3. When `state == "question"`, the script hands the question body to an `opus` agent, passes the answer back via `neon-run reply <run> --text "<answer>"`, and resumes the `haiku` wait loop until `done` or `escalation`.

## Tests

```sh
python3 -m pytest adapters/neon-run -q
```

Tests run against a fake `orca` executable placed on `PATH` that records every argv invocation and returns canned JSON. No live Orca runtime is required.
