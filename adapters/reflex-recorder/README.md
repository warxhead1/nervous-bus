# reflex-recorder

Reflexarc FLYWHEEL (b2): captures `bus.agent.activity.v1` events from `nbus:all`,
segments them into runs using the hardened composite-key model, persists to SQLite,
and emits `bus.agent.run.closed.v1` on each run close.

## Engine vs. adapters (the public/private split)

This directory is the **generic Reflexarc engine** and is PUBLIC. It ships:
- the pipeline (`recorder`/`segment`/`enrich`/`store`/`query`),
- outcome attribution (`label`/`git_outcome`),
- **generic** detectors (`worktree_leak`, `reread_same_file`, `repeated_question`,
  `file_reads_to_finding`) and **rust-ecosystem** detectors (`rebuild_cache_miss`,
  `edit_build_fail_revert` — they key on `cargo` alone, not any one project),
- **orchestration-quality** detectors (`red_baseline_dispatch`,
  `unverified_completion`, `directive_ground_truth_mismatch`,
  `inherited_rationalization`) built on the dispatch-lineage substrate
  (`detectors/dispatch_lineage.py`): they reason about how runs were *spawned* —
  fan-outs launched on a red/unestablished baseline (A1), a dispatch prompt
  asserting a clean baseline that reality contradicts (A2), delegated agents that
  shipped code edits with no build/test (grounded MAST "no-verification-step", F1),
  and sibling cohorts that converged on a seeded bad outcome (C1, session-scoped
  via the cohort→child-outcome join). The substrate is truncation-tolerant
  (bounded summaries are regex-recovered) and joins lineage at SESSION scope
  (delegated agents are folded into the parent session, split across idle runs).
  See the spec in the kb vault (`reflexarc-orchestration-detectors.md`),
- the inductive trajectory profiler (`tier2/trajectory_profile.py`),
- the **project-adapter contract** (`adapter_api.py`) + a scaffold
  (`templates/reflex-adapter/`).

Anything specific to ONE project — its build/run command taxonomy, bespoke
detectors, extra cost signals (build reports, GPU diagnostics) — is PRIVATE and
lives in the overlay at `$NERVOUS_HOME/adapters/reflex-<project>/adapter.py`
(default `~/.config/nervous-bus/adapters/`). The engine discovers them at runtime
via `adapter_api.load_adapters()`; with no overlay it runs generic-only. See
`templates/reflex-adapter/README.md` to build one. Reference: `reflex-tengine`.

## Segmentation model

**run_key_kind=session**: host main-tree work, no worktree context.
`run_key = conversation_id`

**run_key_kind=worktree**: dispatched subagent/workflow shard.
`run_key = conversation_id + '#' + worktree_slug`

Key insight: `conversation_id` alone is NOT a valid run key. One conversation_id
was observed spanning 26 parallel worktrees (shards all inherit the parent host
session ULID into `agent_id`/`session_id`/`conversation_id`). The worktree slug
is the only field in the live stream that separates parallel shards.

The `worktree` field in the output is the **reconstructed absolute path** (derived
from the activity `cwd` field), never the raw slug. The downstream worktree-leak
detector joins against `git worktree list` (absolute paths).

## Querying

`query.py` is the read-only query layer over the SQLite run-store.  It exposes a
`reflex` CLI and is importable as a library.  All queries open the DB in
`mode=ro` (no write locks, WAL-safe).

### Subcommands

```
reflex runs        [--project P] [--outcome O|unlabeled] [--since TS] [--days N] [--json]
reflex thrash      [--project P] [--since TS] [--days N] [--json]
reflex prevalence  [--project P] [--days N] [--json]
reflex issues      [--project P] [--since TS] [--days N] [--json]
reflex stats       [--project P] [--since TS] [--days N] [--json]
reflex time        [--project P] [--days N] [--top K] [--min-wall MIN] [--transcript PATH ...] [--json]
reflex sql  "<SELECT ...>"  [--json]
reflex schema      [--json]
```

`reflex time` answers "what is taking each agent so long". For every run it
reads the run's transcript, which the bus does not carry, and splits wall time
into **tool** time (the union of tool-call intervals) and **model** time (wall
minus tool: generation, thinking, API latency). It splits tool time by the
project's command taxonomy and lists the slowest calls.
`--transcript` analyses a file directly, with no run-store. Token and per-lane
cost views live in `token_report.py` (`--lanes [--project P]` prints a local
per-subagent table; the digest carries a sanitized one).

### Transcript-reading detectors

`transcript_steps.py` turns a transcript into ordered tool calls (input, bounded
output, start/end) and resolves a run to its transcript (live root, then the
mirror). Detectors built on it, all rung `inform`:

| detector | fires when | lead's response |
|---|---|---|
| `param_sweep_thrash` | >= 15 reruns of one harness with only numeric edits/args between them over >= 45 min and no passing test/gate, OR >= 10 foreground calls > 60 s that run a harness serially (loop/chain) totalling >= 20 min | stop the lane, checkpoint, script the sweep (parallel/detached) and pin the value with a test |
| `gate_bound_loosened` | a test/gate bound widened after a failure of that gate in the same run (quotes the edit and the failure) | ask for the bound's derivation; accuses nothing |
| `foreground_wait` | >= 4 min in foreground sleep-poll loops / long sleeps | run the job in the foreground with a timeout, or in the background and do other work |

Fixtures are real lanes, trimmed and redacted (`tests/fixtures/transcripts/make_fixture.py`).

### Copy-paste examples

```bash
# All runs for tengine (default 50 most recent)
python3 query.py runs --project tengine

# Work that reached a clean outcome in the last 7 days
python3 query.py runs --project tengine --outcome clean --days 7 --json

# Work that was discarded (abandoned or reverted) this week
python3 query.py thrash --project hearth-loom --days 7

# Detector hit prevalence across all projects (last 7 days)
python3 query.py prevalence

# Per-project stats: run count, outcome breakdown, avg events, read/write ratio
python3 query.py stats --json

# Recurring issues per project
python3 query.py issues --project tachyonac-engine

# Worktree-leak prevalence for tachyonac-engine
python3 query.py prevalence --project tachyonac-engine --days 30
```

### Agent recipes

`reflex` is the **generic read-only behavioral-memory handle** for autonomous
agents and tools.  Shell out to `python3 .../query.py` to compose questions
about past agent behavior without reading store.py.

```bash
# 1. Self-describe the schema before composing a custom query
python3 query.py schema --json

# 2. Arbitrary SELECT — get the tool distribution for long runs
python3 query.py sql "SELECT project, tool_histogram, event_count FROM runs WHERE event_count > 50 ORDER BY event_count DESC" --json

# 3. Find worktree runs that ended cleanly (good candidates for leak audit)
python3 query.py sql "SELECT run_id, project, worktree_slug, git_branch FROM runs WHERE labeled_at IS NOT NULL AND outcome = 'clean' AND worktree_slug IS NOT NULL" --json

# 4. Count unlabeled runs per project (NOT-YET-LABELED; outcome NULL != clean)
python3 query.py sql "SELECT project, COUNT(*) as n FROM runs WHERE labeled_at IS NULL GROUP BY project" --json
```

**Null-vs-clean contract** (critical for agent self-composition): `outcome IS NULL`
means NOT-YET-LABELED, never "clean".  Any query filtering on `outcome` must
also gate on `labeled_at IS NOT NULL` — the `reflex sql` guard does NOT enforce
this for you.  Use `reflex schema` to get the full column semantics, including
the labeled_at caveat, before writing a custom query.

## File layout

```
adapters/reflex-recorder/
  recorder.py          # Main entrypoint — XREADGROUP consumer, shutdown handler
  segment.py           # Segmentation logic: run_key computation, OpenRun, Segmenter
  store.py             # SQLite persistence (abstracted for future dolt swap-in)
  query.py             # CLI/library query layer (reflex subcommands + sql passthrough)
  skill_usage.py       # Skill-load + dispatch-tiering signals, folded into runs.features; backfill CLI
  reflex-recorder.toml # Configuration
  systemd/
    reflex-recorder.service  # User systemd unit (do not enable without orchestrator review)
  tests/
    test_segment.py    # 28 unit tests for segmentation logic
    test_query.py      # 69 unit tests for query layer
```

## Skill & dispatch usage

`skill_usage.py` folds three feature keys into every run at fold time (and
`python3 skill_usage.py backfill [--since ISO] [--apply]` recomputes them for
stored runs from `run_events`; dry-run and read-only unless `--apply`):

- `skills` — `{name: {mechanism: count}}`. `skill_tool` = Claude's `Skill`
  tool; `file_read` = a Read or shell command that references
  `.../skills/<name>/SKILL.md`. Codex has no Skill tool, so `file_read` is how
  Codex usage shows up. It also counts agents opening a SKILL.md to edit it.
- `dispatch` — Agent/Task calls: `by_model`, `by_subagent_type`,
  `model_missing`, `model_unknown`, `isolated`.
- `workflow_calls` — Workflow tool calls.

```bash
reflex skills [--by project|month|agent_kind]   # per-skill usage + harness split
reflex skills --inventory                       # on-disk skills vs usage; never-seen
reflex skills --lift [--min-n 20]               # outcome rate with vs without (correlational)
reflex dispatch [--by month|project]            # model mix, missing-model rate
```

`tool_summary` is bounded by the emitting hook, so read the counts as lower
bounds: a SKILL.md path past the bound is not seen. On dispatches, JSON keys
sort alphabetically and `prompt` pushes later keys past the bound, so model and
subagent_type carry a tri-state (`explicit`/`missing`/`unknown`) — `missing` is
only asserted when the summary parsed as complete JSON. Slash commands and
Workflow-internal `agent()` calls are not in the live stream
(`workflow_dispatch.py` recovers the latter from transcripts).

## Running

```bash
# Continuous (live mode)
python3 recorder.py

# Drain current stream entries and exit
python3 recorder.py --once

# Offline replay from debug.jsonl
python3 recorder.py --replay ~/.cache/nervous-bus/debug.jsonl
```

## Storage

Default DB: `~/.cache/nervous-bus/reflex/runs.db`

Tables:
- `runs` — one row per closed run (the bus.agent.run.closed.v1 payload)
- `run_events` — ordered raw activity events per run_id (for b3 feature/label backfill)

## Enable systemd service (after orchestrator review)

```bash
cp adapters/reflex-recorder/systemd/reflex-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reflex-recorder.service
systemctl --user status reflex-recorder.service
```

## Run close triggers

1. Explicit `ended` event from the activity stream
2. Idle timeout (default 15 min, configurable) with no new events on the run_key
3. Graceful recorder shutdown (`close_reason=recorder_shutdown`)

Idle-split runs set `continues_run_id` to the prior run_id so fragments can be
re-stitched downstream.

## Transcript snapshotter

`transcript_snapshot.py` incrementally mirrors Claude Code's per-session
`*.jsonl` transcripts from `~/.claude/projects/` into a durable cache at
`~/.cache/nervous-bus/reflex/transcripts/`. Worktree dirs (munged names
containing the literal substring `worktrees`) are processed first because
Claude Code deletes them whole when the worktree is reaped, losing the
delegated agent's full transcript.

The mirror is *append-only with rotation recovery* — a 2-line src file is
appended to byte-for-byte; a rotation (inode change or shrink) is detected
and re-copied; the destination is *never* shrunk or pruned (a vanished src
file is a normal event, not an error and not a deletion in dst).

```bash
# One pass (used by the systemd timer):
python3 adapters/reflex-recorder/transcript_snapshot.py --once

# Manifest summary (mirrored files + total bytes):
python3 adapters/reflex-recorder/transcript_snapshot.py --stats

# Poll loop (foreground; the timer unit replaces this in production):
python3 adapters/reflex-recorder/transcript_snapshot.py --watch 60
```

State on disk:

- `<dst>/<munged-dir>/<sessionid>.jsonl` — byte-for-byte mirror of the src file.
- `<dst>/.manifest.json` — `relpath -> {"inode": int, "size": int}`; the
  incremental copy baseline. Atomic write (temp file + `os.replace`).

Enable the systemd user timer (every 2 min, catches up after sleep):

```bash
cp adapters/reflex-recorder/systemd/nervous-transcript-snapshot.{service,timer} \
    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nervous-transcript-snapshot.timer
systemctl --user list-timers nervous-transcript-snapshot.timer
journalctl --user -u nervous-transcript-snapshot.service -n 50
```

Stdlib only (no `pip`), no network. The destination is a durable archive
that retains content even after the worktree is reaped.

## Struggle Ledger

`struggle_ledger.py` answers a different question than the quality detectors: not
"did the agent verify its work?" but **"what are the agents fighting, is it shared,
and has it been fixed?"** — the lived friction the verified-% scorecards throw away.

It reads the durable transcript archive (mirrored by `transcript_snapshot.py`),
classifies records against `StruggleClass` patterns (generic friction shipped by the
engine — cargo build-lock, address-in-use, OOM — plus project-specific ones a private
adapter contributes via `ProjectAdapter.struggle_classes()`, so proprietary tool names
stay private), and for each struggle builds a longitudinal record:

- events, distinct sessions/agents, first/last seen, daily sparkline
- **status** — `open` (still happening) / `dormant` / `resolved`
- **fix verdict** — correlates the friction curve against remediation events
  (fix-commits, bead closes) to score whether a claimed fix actually dropped it:
  `fixed` / `partial_still_open` / `unfixed_open` / `unfixed_no_attempt` /
  `resolved_no_fix_found`. Picks the remediation that best explains the decline
  (largest before→after drop), not merely the latest.

```bash
python3 struggle_ledger.py                      # whole-fleet ledger
python3 struggle_ledger.py --project tengine     # one project
python3 struggle_ledger.py --struggle gpu_lock_wait --project tengine  # drill-in: timeline + examples + fix
python3 struggle_ledger.py --json                # machine-readable
```

The fix-correlation is an honest heuristic (keyword-matched remediations, ±7-day
before/after windows, edge-truncation flagged) — a starting signal for "is this wall
still standing", not a proof.

## Nightly Analysis Loop

Capture (`recorder.py`) has run as a systemd service since the engine's first bead.
Outcome labeling (`label.py`), the 12 detectors (`synthesis.py`), and the struggle
ledger (`struggle_ledger.py`) were code-complete but only ever invoked BY HAND —
a three-quarter loop: capture shipped without a schedule to consume it. The
ecosystem charter's rule is that nothing ships as "done" until it closes the full
capture → analyze → surface → consume loop; `nightly_analysis.py` + its timer is
what closes the analyze/surface half for reflex-recorder.

`nightly_analysis.py` runs three steps in order, each subprocess-wrapped with a
hard per-step timeout (default 300s) and a logged-but-non-fatal failure — a slow
or broken step never blocks the ones after it, and a bad night never wedges the
timer:

1. `label.py --since-days 30 --unlabeled-only` — incremental outcome labeling.
   `--unlabeled-only` means a settled outcome (landed/reverted/abandoned/clean/
   thrashed) is never re-verified; only runs still sitting at `outcome=NULL` get
   another attempt (their PR may have merged, their bead may have closed since).
   `--since-days` bounds cost against an ever-growing run history. A first pass
   against a mostly-unlabeled DB may not finish inside the timeout — that's fine,
   `apply_label` autocommits per-row, so the next night resumes where this one
   left off.
2. `synthesis.py` (default dry-run) — runs every built-in + private-overlay
   detector and persists hits/issues to `detector_hits`/`issues`. Dry-run only
   gates the `nervous publish` calls; the local persistence that `reflex
   prevalence`/`reflex issues` read happens either way.
3. `struggle_ledger.py --days 14 --json` — captured (not persisted, it has no
   store of its own) to feed the digest below.

Then it writes/updates a rolling digest at
`~/knowledge/indexed/shared/reflex-digest.md` (kb vault; a fixed id in the
frontmatter means each run rewrites the same entry rather than forking a new
file) with: run counts + labeled-outcome distribution (7d), top detector
prevalence (7d), and top OPEN struggle-ledger items (14d). The digest is a
rolling log, not a decision record — this script does **not** git-commit it;
commit a snapshot by hand if you want history of one.

```bash
# Manual run (same as the timer):
python3 nightly_analysis.py

# Custom DB / windows / timeout:
python3 nightly_analysis.py --db /path/to/runs.db --step-timeout 300 \
    --label-since-days 30 --window-days 7 --struggle-window-days 14
```

Enable the systemd user timer (daily, ~05:30 local, catches up after sleep):

```bash
cp adapters/reflex-recorder/systemd/reflex-analysis.{service,timer} \
    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reflex-analysis.timer
systemctl --user list-timers reflex-analysis.timer
journalctl --user -u reflex-analysis.service -n 100
```

## Token ledger and the daily token audit

The bus carries tool activity, not model requests. Per-request usage (input,
cache read, cache write 5m/1h, output) lives in the session transcripts, so it
gets its own ledger:

- `token_ledger.py` ingests transcripts incrementally into
  `~/.cache/nervous-bus/reflex/tokens.db` (tables `sessions`, `requests`,
  `tool_calls`, `instructions`). Sources: the transcript mirror, live
  `~/.claude/projects` (incl. `<session>/subagents/`), and `~/.mmx/projects`.
  Per-file byte offsets; a full backfill of ~420k requests takes about 2 min.
- `token_report.py [--days 7] [--md]` ranks waste candidates in
  input-equivalent tokens (IET, API price ratios): `long_context`,
  `cache_rewrites` (split by idle gap, TTL, compaction), `big_tool_results`,
  `startup_context` (incl. instruction-file sizes), `redundant_reruns`,
  `uncached_input`, plus IET per commit and the effect of prior proposals. The
  digest is sanitized by construction: counts, model ids, project names, agent
  types, command families, instruction-file paths. Never prompts, tool output,
  full commands, or paths of files agents read.
- The nightly batch runs both and writes `token-digest/latest.{md,json}`.
- `automation/token-audit.md` is the `mmx-automation` recipe: a read-only
  MiniMax run (`mmx-automation@nervous-bus--token-audit`, 06:30) reads ONLY the
  digest and proposes ONE action. `automation/token_audit_publish.py` is the
  host-side gate (ExecStartPost): it validates the proposal against the digest
  (known candidate, saving <= measured IET, evidence rows verbatim, no duplicate
  title), files a bead plus an issue on the private `claude-hook-fast` repo, and
  appends to `proposals.jsonl`, which the next digest reads back to show whether
  the fix moved its number.

Install (after merge, from the shared checkout):

```bash
ln -sfn ~/projects/nervous-bus/adapters/reflex-recorder/automation/token-audit.md \
  ~/.config/mmx-automations/recipes/token-audit.md
for d in ~/projects/nervous-bus/adapters/reflex-recorder/automation/systemd/*.d; do
  ln -sfn "$d" ~/.config/systemd/user/; done
systemctl --user daemon-reload
systemctl --user enable --now mmx-automation@nervous-bus--token-audit.timer
```

Non-LLM producers (`agent_kinds.NON_AGENT_KINDS`: tachyonac `cron`/`ingestor`
job ticks) are dropped by the recorder; `retire_non_agent_runs.py` moves any
backlog into `runs_non_agent.db`.

## Steering audit: our guidance vs the published practice

The steering surface (the `~/.claude/CLAUDE.md` import chain, project
CLAUDE.md/AGENTS.md, auto-memory indexes, skills, agent definitions, dispatch
briefs) is audited against external guidance that is fetched, hashed, and
diffed every night. Same shape as the token audit: measure, digest, propose,
gate, read back.

- `guidance_sources.toml` — the watchlist (Claude Code docs pages + its
  `llms.txt`, the changelog, Claude prompting best practices, the Agent Skills
  spec, AGENTS.md) and the `[[threshold]]` regexes that READ every numeric limit
  out of a fetched page. Nothing is typed in; a regex that stops matching is
  reported as `threshold_unresolved`.
- `guidance_watch.py` — fetches the watchlist into
  `~/.cache/nervous-bus/reflex/guidance/sources/<id>.md`, sha256 per source in
  `state.json`. A change writes `diffs/<date>/<id>.diff` and a line in
  `changes.jsonl`; an `llms.txt` change also lists pages added/removed (a new
  feature page nobody has audited against). A fetch error keeps the old snapshot.
- `steering_audit.py` — zero-LLM measurement into
  `~/.cache/nervous-bus/reflex/steering-digest/latest.{md,json}`: per-file
  lines/tokens/loads (loads from tokens.db, worktree copies folded onto their
  `~/projects` original), emphasis/dated/ENFORCED line counts, findings against
  the fetched limits (CLAUDE.md chain over target, MEMORY.md past the load
  cutoff, SKILL.md and listing-description limits, import depth), the doc's
  "Avoid common failure patterns" crosswalked to detector hits
  (`failure_crosswalk.toml`; a pattern the doc adds is `failure_pattern_unmapped`),
  and prior proposals with the target's line count then vs now. Only roots that
  loaded in the window are judged. Paths, counts, line numbers; never rule text.
- `automation/workflows/steering-audit.js` — the saved workflow (judgment half).
  Ground (haiku: refetch + digest) -> Claude Code's own `/doctor prompt-audit`
  per loaded root, headless (sonnet runner, opus auditor) in parallel with
  practice extraction + mapping from changed sources (sonnet) -> judge (fable,
  `judge_model` overrides; at most 3 proposals) -> two opus refuters per proposal -> gate (haiku).
  Dry run unless `{publish: true}`. `{full: true}` audits the core source set
  instead of only what changed.
- `automation/steering_audit_publish.py` — the gate. A proposal files only if
  its target excerpt is in the cited lines of a real file under `~/.claude` or
  `~/projects`, every guidance quote is verbatim in a fetched snapshot, and the
  title/target is not a duplicate (7-day cooldown per target). Files an issue in
  the repo that owns the target (`~/.claude` -> claude-memory-global, skills ->
  agent-skills, projects -> their origin) plus a bead labelled
  `<repo>,steering-audit`, and appends `proposals.jsonl`.

The nightly batch runs `guidance_watch.py` and `steering_audit.py` (step 2c).
Install the workflow (after merge, from the shared checkout):

```bash
mkdir -p ~/.claude/workflows
ln -sfn ~/projects/nervous-bus/adapters/reflex-recorder/automation/workflows/steering-audit.js \
  ~/.claude/workflows/steering-audit.js
# then, in any session: run the steering-audit workflow with {full: true}
```

## champion

`champion.py` is the deterministic half of the MASS champion-brief theory test
(design: `MASS-integration-design.md` §3.1-3.3). Zero LLM calls; every database
opens `mode=ro`; JSON on stdout, `--help` is the reference.

```
champion.py extract [--since-days N] [--project P] [--out PATH]   # briefs.jsonl, counts on stdout
champion.py pairs   --population retry|cohort|recipe|similar|all [--near-miss]
champion.py bundle  <run_id|brief_sha|brief_id>                    # <=50k tokens, diff stat <=10k
champion.py lift    [--window 800|0] [--md PATH]                   # Experiment B + verdict
```

Brief sources: Claude subagent transcripts (line 0, `.meta.json` read from the
mirror or `~/.claude/projects`), mmx sessions, bounded bus dispatches recovered
from parent `run_events` (the mirror only reaches back to 2026-08-31, explicit
abandoned labels start in July), Orca `tasks.spec`, and `~/.mmx/runs` recipes.
`briefs.jsonl` holds private brief text and lives ONLY under
`~/.cache/nervous-bus/reflex/phase0/`, never in the repo.

Labels: explicit means `label._source_tier(label_history[-1].source) >= 2`;
positive is `landed|clean`. Orca `worker_done` follows `orca_outcome`
(succeeded is a positive, failed abstains), so Orca rows are excluded from
`lift` and can never form a usable pair. `lift` applies the §3.3 rule: cells
are project x agent_kind, valid with both arms >= 20 and both outcomes
present; testable means valid cells in >= 2 projects; load-bearing means a
project-weighted delta >= 10 points in >= 2 projects; "kill" needs >= 3
testable enforced features with none clearing. Features run on the first
`--window` chars of every brief so full and bounded text share one lens
(`length_chars` is dropped there). `model_explicit`, `isolation_worktree`,
`has_acceptance_cmd`, `has_file_scope` and `has_base_branch` OR the prose regex
with structured fields (dispatch args, mmx meta, cwd under a worktree); the
`_fired` map in each row records which arm fired.

### Phase 0d pilot: calibration pairs and pairwise judges

```
champion.py calibrate-sample [--n 30] [--flip-extra 70] [--seed 20261012]
champion.py judge --judge mmx|sonnet --both-orderings [--limit N] [--bundle-only]
champion.py calibrate-score --truth truth.json
```

Labels are not required: `calibrate-sample` draws same project + agent_type pairs with brief
similarity >= 0.8 from runs since 2026-08-31 (full brief text, transcript present), keeps rows with
at least two kinds of workspace evidence (diff stat, an existing report file, files written or
edited per the transcript, a last message of >= 300 chars), prefers pairs that differ in inferred
outcome, detector count or diff size, round-robins across projects and agent types, and caps any
project at 30%. Private projects never enter: any of `project`, `run_project`, `cwd`,
`run_worktree`, `transcript`, branch names or brief text matching tachyonac, tachyonos, market-ops,
hearth.market or tengine.diag drops the row (mmx ships content to MiniMax).

Output under `~/.cache/nervous-bus/reflex/phase0/calibration/`: `pairs.json` (ids, run ids, bundle
paths, random A/B), `packet.md` (30 pairs, blind: no labels, outcomes, detector names or judge
verdicts), `truth.template.json`, and `sealed/` (selection features that carry labels, the judge
call ledger, `verdicts.jsonl`). Packet and judges read the SAME rendered evidence files.

`judge` runs the pair in both orderings (ordering 2 swaps the workspaces; winners are mapped back to
the packet frame). One call ledger caps all judges at 400 calls on disk, failed calls included, and
reruns skip finished (pair, judge, ordering) triples. sonnet uses a credentials-only
`CLAUDE_CONFIG_DIR`; mmx runs read-only from a fresh empty directory. Only counts and flip_rate
print; verdicts stay in `sealed/`.

`calibrate-score` reads `{pair_id: {"winner": "A"|"B"|"tie", "by": ..., "note": ...}}`. Agreement
is taken over order-consistent calibration pairs whose truth is A or B (truth ties and null truth
are counted separately); flip_rate over all pairs with both orderings parsed. The verdict applies
design section 3.2 verbatim and states that n=30 is a pilot below the 80-pair floor.
