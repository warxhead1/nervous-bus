---
name: spans
description: Find where agent time goes (tool classes, wait targets, slowest agents per project) from session transcripts, and record or query short lessons learned. Use when asked why agent work is slow, what agents wait on, or to save a durable lesson for a project. Works for any harness that can run a shell command.
---

# spans and lessons

JSON on stdout. `--help` on each command is the reference.

| Question | Command |
|---|---|
| Where does agent wall time go? | `adapters/spans/spans top [--project P] [--since 24h] [--limit N]` |
| Same, as a table | `adapters/spans/spans top --text` |
| Publish new tool spans to the bus | `adapters/spans/spans scan [--since 2d] [--dry-run]` |
| Record a lesson | `adapters/lessons/lessons record --project P --area A --lesson "..." --evidence "..." [--rule "..."] [--supersedes les_x]` |
| What did we learn about X? | `adapters/lessons/lessons query --project P [--area A] [--all]` |

## spans

`top` prints three rankings by wall time: `classes` (what kind of call), `wait_targets` (what a waiting call waited on: lock, build, e2e, test, gate, ci, background_task, time, unknown) and `agents` (workflow lane or session). It reads transcripts directly and needs no bus.

`scan` pairs each tool call with its result in Claude Code transcripts (`$CLAUDE_CONFIG_DIR/projects`, default `~/.claude/projects`, including subagent and workflow lanes) and publishes `agent.tool.span.v1` through `nervous publish-batch`, 500 spans per call (`--batch-size`). The cursor (`~/.cache/nervous-bus/spans/cursor.json`) holds a byte offset per file and is saved after every batch, so a rerun publishes nothing twice. A crash between a batch's publish and its cursor save re-sends at most that one batch. Every span carries a stable `span_id` for consumers that dedupe. A call without a result stays open until it finishes, or counts as lost after 6 h. Spans carry the class and wait target only, never command text.

A call is a wait when it sleeps, polls (`until`, `while ... sleep`), holds a lock (`flock`), or blocks on a background task or watcher. Its target comes from the whole command. The word "wait" in a commit message or heredoc decides nothing.

The project comes from the call's cwd through `adapters/transcripts/project.py` (`NBUS_PROJECT_ROOTS`, `NBUS_PROJECT_ALIASES`).

Install the timer by hand: `systemd/spans.timer`.

## lessons

A lesson is one sentence of at most 300 characters, with evidence (a measurement, a link or a commit) and the rule or fix it produced. It is not a session log. `record` appends to `~/.local/state/nervous-bus/lessons/lessons.jsonl` (`NERVOUS_LESSONS_FILE` overrides) and publishes `agent.lesson.v1`. `--supersedes` names the lesson being replaced, and `query` hides replaced lessons unless `--all`. A failed publish exits 1 with the lesson stored.
