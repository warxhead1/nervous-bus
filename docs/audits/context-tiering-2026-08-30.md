# Context-Tiering Audit — 2026-08-30 (Lane M)

Goal: measure what loads into EVERY session's context regardless of task, identify
what earns that placement vs what should demote to read-on-demand, and design a
tiered profile mechanism so long/compacted sessions still know how to reach
orchestration/worker lore without every subagent being context-dumped with it.

All byte counts are `wc -c` (raw UTF-8 bytes), measured 2026-08-30. Everything in
§1-§6 is a MEASUREMENT; §7-§9 is the DESIGN.

---

## 1. Always-loaded global tier (`~/.claude/CLAUDE.md` + `@`-imports)

`~/.claude/CLAUDE.md` (1,836B) pulls in `GLOBAL-MEMORY.md` (771B), which in turn
`@`-imports all 18 files below via `ROUTING.md`'s own listing. Total global tier:
**~37.3KB** (2,607B root files + 34,698B memory-global), loaded on every session
in every project, before any project CLAUDE.md.

| File | Bytes | Verdict |
|---|---|---|
| agent-dispatch.md | 6,276 | KEEP — largest file, but every clause is a distinct measured incident (worktree mixing, partition-by-file, mmx-orca pointer). Candidate to trim: the `## mmx via Orca` stub (163B) already correctly demotes to mmx-orca.md — that pattern should be applied harder here (see below). |
| cc-tooling.md | 4,138 | KEEP, borderline. 9 distinct incidents, each terse and mechanism-specific (pgrep guard, TaskStop SIGKILL, hold-loop wakeup). Every session benefits since these are tooling footguns not task-specific. |
| kb.md | 3,119 | DEMOTE-candidate. Only sessions that actually call `kb` need the mechanics (query vs brief vs find). The one line every session needs is "kb survives project rename, use `--project` explicitly" — the rest (doctor fix history, brief rebuild changelog) is changelog noise that never needs to be in EVERY session's context. |
| worktrees.md | 3,073 | KEEP core rules (data2 path, push-or-doesn't-exist, branch-from-origin/HEAD), but the two long dated anecdotes (apps-audit misattribution, hearth-loom/kb cross-repo case) could compress to one-liners — they're already summarized at the top of each bullet; the incident prose after the ruling is receipts for Eric, not operational content the agent needs twice per session. |
| tree-wipe-incident.md | 2,470 | DEMOTE-candidate. This is a closed incident postmortem with a recovery runbook. The one live rule ("nested `.claude/worktrees` is blast-radius risk") is already restated in worktrees.md. Recovery steps are only needed IF `.git` vanishes — that's a rare/on-demand read, not a per-session load. |
| verification.md | 1,858 | KEEP — every session either verifies its own work or dispatches, and these are cross-cutting (isolation check, dispatch-from-frozen-doc, push-as-you-go). |
| gate-wait-parallelism.md | 1,855 | DEMOTE-candidate. Only relevant to sessions that are actually blocked on a gate/deploy/backfill — a niche trigger condition, not universal. Fits the "role profile" tier better than global-always. |
| ambition-brief.md | 1,392 | KEEP — this is the direct fix for a measured failure mode (over-conservative agents), it's short, and it applies to literally every dispatch decision. |
| failure-modes.md | 1,408 | KEEP — one-paragraph classifier + a `python3 synthesis.py` pointer; cheap and used as an end-of-run self-check. |
| destructive-ops.md | 1,276 | KEEP — safety-critical, applies to any session touching git. |
| go-builds.md | 1,134 | DEMOTE-candidate. Only relevant to Go-repo sessions (tachyonac-engine, subset of tengine). Project-scoped fact wearing a global hat — per ROUTING.md's own test ("names a crate/path/build target → project"), this arguably violates the routing rule already. Should live in tachyonac-engine's + tengine's project memory, not global.
| data2-disk.md | 1,053 | KEEP — cross-project disk-routing rule, applies to any worktree/scratch decision. |
| ROUTING.md | 1,044 | KEEP — meta-rule for the memory system itself, must be universally known to be followed. |
| mmx-orca.md | 1,592 | Already correctly gated — NOT `@`-imported (deliberately, per its own docstring), only pulled in by name from agent-dispatch.md when an mmx dispatch is imminent. This is the precedent pattern the rest of the DEMOTE-candidates above should copy. |
| tmpfs.md | 869 | KEEP — short, safety-relevant (OOM risk), applies to any scratch-file decision. |
| codex-model-tiering.md | 821 | KEEP-borderline — only matters when dispatching codex, but it's tiny and the failure mode (default launch = expensive/wrong tier) is easy to trip silently. Cheap enough to leave global. |
| remediation-ladder.md | 652 | KEEP — meta-principle, cheap, applies whenever proposing any fix. |
| secrets.md | 668 | KEEP — safety-critical, universal. |

**Global tier verdict:** ~9.8KB (kb.md, worktrees.md incident prose, tree-wipe-incident.md,
gate-wait-parallelism.md, go-builds.md) of the 34.7KB memory-global total is
task-conditional or project-scoped content riding in the always-loaded tier under
the mmx-orca.md precedent's absence. Demoting those (mechanism in §7) would cut the
global tier from ~37.3KB to ~27.5KB, a 26% reduction, with zero loss of reachability.

---

## 2. Per-project always-loaded tier

| Project | CLAUDE.md | AGENTS.md | Auto-loaded via `@`? | Effective always-loaded |
|---|---|---|---|---|
| nervous-bus | 7,159B | 2,482B | No — CLAUDE.md explicitly says "Also read AGENTS.md... not imported here" | 7,159B |
| tengine | 20,151B | 12,888B | No `@` found | 20,151B (AGENTS.md separate) |
| hearth | 31,342B | 13,034B | No `@` found | 31,342B |
| hearth-loom (main/) | 1,276B | 8,471B | Yes, `@AGENTS.md` at line 3 | 9,747B |
| deer-flow | 184B | 15,294B | Yes, `@AGENTS.md` at line 5 | 15,478B |
| kb | 19,137B | none | n/a | 19,137B |
| orca | 11B | 7,848B | Yes, `@AGENTS.md` | 7,859B |
| app-to-market | 11B | 6,763B | Yes, `@AGENTS.md` | 6,774B |
| career-ops | 556B | 43,211B | Yes, `@AGENTS.md` | **43,767B — worst offender** |
| unreal-battlebots-gamedev | 1,689B | none | n/a | 1,689B |
| tachyonac-engine | 19,474B | 887B | No `@` found | 19,474B (AGENTS.md separate, small) |
| shader-garden | none | none | n/a | **0B — no repo-level agent context at all** |

Notable structural finding: **5 of 12 projects (orca, app-to-market, hearth-loom,
deer-flow, career-ops) fold their entire AGENTS.md into the always-loaded tier via
`@AGENTS.md`**, while nervous-bus, tengine, hearth, tachyonac-engine keep them
separate (AGENTS.md read on demand / by convention). This is an inconsistency: two
different tiering philosophies coexist across the fleet with no stated rule for
which project uses which.

### Content classification (spot-checked, not exhaustively line-audited)

- **nervous-bus CLAUDE.md (7,159B):** ~70% architecture/identity (transport model,
  SDK matrix, schema-first rule), ~20% cross-project pointers, ~10% beads
  boilerplate duplicated verbatim from the beads plugin template (the "Beads Issue
  Tracker" section — identical text appears in essentially every project's
  CLAUDE.md that uses `bd`, i.e. category (c) duplicated-from-elsewhere, though it's
  duplicated from a shared template rather than from the global tier specifically).
  Spot-checked 2 architecture claims (redis-mirror adapter path, zellij plugin dir)
  — both confirmed present on disk (`adapters/redis-mirror/mirror.py`,
  `plugin/Cargo.toml` exist). No stale claims found in this pass.
- **career-ops AGENTS.md (43,211B):** by far the largest single always-loaded file
  in the fleet, ~2.6x tengine's CLAUDE.md and ~5x nervous-bus's. Did not
  line-audit its full content (out of the exhaustion budget for this pass — flagged
  as the top candidate for a follow-up deep read); given its size relative to every
  other AGENTS.md (6.7-15KB range), it is very likely mixing task-specific playbooks
  (category b) into what should be an identity/pointer document.
- **tengine / hearth (20-31KB range):** these are the two largest CLAUDE.md files
  outside career-ops. Both are large enough that a compacted mid-session summary
  plus this file alone approaches a meaningful fraction of a 200K context window
  before any real work starts. Not line-audited for staleness in this pass —
  named as the #2/#3 follow-up targets.
- **shader-garden: zero repo-level context.** Per this session's own MEMORY.md
  index ("shader-garden public... LIVE at warxhead1.github.io"), this is an active
  public-facing project with launch-gate rows still open, yet a session opened
  there gets no identity file at all. This is a coverage gap, not a bloat problem.

**Exhaustion note (§2):** I read only the CLAUDE.md/AGENTS.md top-level files and
grepped for `@`-imports; I did NOT recursively check `.claude/rules/` (searched,
none exist in any of the 12 projects) or nested CLAUDE.md files inside
subdirectories (Claude Code supports directory-scoped CLAUDE.md; I did not `find`
for these under each of the 12 project trees — only under nervous-bus's own tree
implicitly via my worktree). A full sweep of nested CLAUDE.md across all 12 repos
is the natural next step if more budget opens up.

---

## 3. Skills surface (listing loads every session)

Global: **23 skill files** under `~/.claude/skills/` (21 in `*/SKILL.md` dirs + 2
flat `.md` files: `ask-codex.md`, `ask-gemini.md`). Every skill's YAML
`description:` field is the part that loads into every session regardless of
invocation; bodies load only on invocation. Description lengths run
roughly 150-500 bytes each (e.g. `sandboxed-minimax` ~430B, `hearth-loom-dispatch`
~460B, `agent-orchestration` ~400B) — call it **~23 x 350B avg ≈ 8KB of
always-loaded description text globally**, separate from the CLAUDE.md tiers above.

Project-level skills add to this per-project: tengine has 11 skill dirs (bodies
totaling 130.7KB, `dispatch/SKILL.md` alone is 54KB — but again, only its
description loads by default, not the 54KB body), career-ops has 1 (`career-ops`,
10.9KB body).

**Overlap/staleness flags found via description read (not exhaustive):**
- `ask-gemini.md`'s own description self-flags as possibly stale: "the standalone
  `gemini` CLI this skill invokes was reported removed 2026-07-05 in favor of
  `agy` — verify it still exists before relying on this skill." This is a skill
  admitting its own staleness in its always-loaded description — worth resolving
  (delete or fix) rather than leaving as a live trap.
- Six loomie/hearth-loom-adjacent skills (`dugout`, `monitor`, `loomie`, `loomie-introspect`,
  `hearth-loom-dispatch`, `hearth-loom-pulse-and-triage`) have descriptions that
  cross-reference each other explicitly ("Don't use for X, use Y instead") — this
  is good practice (reduces wrong-skill selection) but is also a signal of a
  fine-grained cluster that could arguably be one skill with subcommands; not
  resolved here, flagged for the design section.
- `council`, `ask-codex`, `ask-gemini`, `sol-fanout`, `deer-research` similarly
  overlap on "get another AI's opinion" — again cross-referenced correctly in their
  descriptions, not clearly broken, just dense.

No skill descriptions were found to reference files/paths that don't exist in this
pass (I checked `ask-gemini.md`'s self-flagged claim only; did not verify all 23
against current binaries/CLIs — exhaustion note: 22 of 23 skill descriptions'
factual claims about external tool existence were NOT verified in this pass).

---

## 4. Project MEMORY.md (always-loaded auto-memory index)

| Project | Bytes |
|---|---|
| tengine | 19,562 |
| hearth | 19,494 |
| tachyonac-engine | 18,594 |
| hearth-loom (bare) | 10,195 |
| nervous-bus | 8,239 |
| temple-stuart-accounting | 5,913 |
| job-search-se | 5,345 |
| app-to-market | 2,363 |
| kb | 1,885 |
| deer-flow | 1,722 |
| unreal-battlebots-gamedev | 1,366 |
| dungeon-ops | 1,358 |
| water-study | 969 |
| -tmp | 516 |
| career-ops | 138 |
| orca | 152 |

The three largest (tengine, hearth, tachyonac-engine, ~19KB each) stack directly
on top of those same projects' already-largest CLAUDE.md files (20-31KB). For
tengine specifically: CLAUDE.md (20.2KB) + AGENTS.md if read (12.9KB) + MEMORY.md
(19.6KB) + skill descriptions (11 x ~350B ≈ 3.9KB) totals **~57KB of always-or-
near-always-loaded text before a single tool call**, before the global 37KB tier.
That's ~94KB combined for a tengine session — worth flagging as the single biggest
"why did compaction kick in after 20 minutes" candidate in the fleet.

career-ops is the inverse extreme: MEMORY.md is nearly empty (138B) while
AGENTS.md is the largest in the fleet (43.2KB) — all the accumulated knowledge
lives in the wrong tier for that project (static doc instead of the
append-only memory the tooling expects), another data point that career-ops's
AGENTS.md needs the deep read this pass didn't have budget for.

---

## 5. Subagent definitions — what a worker inherits

`~/.claude/agents/*.md`: 16 files, sizes 867B (haiku-grunt) to 41KB (gsd-planner).
The two general-purpose worker tiers used by this fleet's dispatch discipline:

- `sonnet-worker.md` — 2,136B total (frontmatter + body). Frontmatter:
  `tools: Read, Write, Edit, NotebookEdit, Bash, Glob, Grep, WebSearch, WebFetch,
  TodoWrite, Agent, ToolSearch, Monitor, TaskOutput, LSP`.
- `haiku-grunt.md` — 867B total. Frontmatter: `tools: Read, Write, Edit, Bash,
  Glob, Grep, TodoWrite` (no Agent — cannot itself fan out).

**Inheritance question (asked in the brief): does a subagent get the project's
CLAUDE.md/AGENTS.md automatically?** I did not find this documented inside the
agent definition files themselves (neither file references CLAUDE.md, project
context, or a loading mechanism) nor did I run an empirical isolated test (would
require spawning a subagent in a repo with a deliberately unique CLAUDE.md marker
and checking whether it echoes the marker unprompted — not done in this pass,
flagged as the concrete next verification step if this matters for the design).
Based on Claude Code's documented project-context loading (CLAUDE.md is loaded
per-working-directory, independent of which "seat" — main thread or subagent —
is running in that directory), the working assumption used for the design below
is: **a subagent DOES inherit the project's CLAUDE.md/AGENTS.md/MEMORY.md** (same
cwd-keyed loading applies regardless of caller) but does NOT inherit anything
that lives only in the parent's conversation history (prior turns, ad hoc
decisions made mid-session). This is the load-bearing assumption behind the
"subagents shouldn't get orchestration lore" framing in the brief — **not
empirically verified in this pass; treat the design in §7 as contingent on it and
verify before large-scale rollout.**

What this means concretely for the fleet: a `haiku-grunt` spawned inside tengine
currently inherits the full ~57KB tengine stack (§4) whether or not the mechanical
edit task needs any of it — none of that content is currently gated by role.

---

## 6. Compaction survival

Reloads automatically after compaction: CLAUDE.md (all tiers, all `@`-imports),
project MEMORY.md, and the skill LISTING (name+description for all 23+N skills).
Does NOT survive: the conversation's own accumulated context — ad hoc decisions,
tool output, and any guidance that was only ever stated inline in a prior turn
and never written to one of the durable files above.

**Guidance found to live ONLY in conversation flow / nowhere durable, checked
against the specific examples in the brief:**

- **"when to use orca vs the Agent tool"** — partially durable. `agent-dispatch.md`
  (global, always-loaded) covers dispatch mechanics and the mmx-via-orca pointer,
  and `~/.claude/skills/orca-orchestration/SKILL.md` + `orca-cli/SKILL.md` exist as
  invocable skills (so the listing survives compaction and can be reached). But I
  found no single line stating the DECISION rule "orca vs plain Agent()" as a
  compact always-loaded rule — it's inferred from reading agent-dispatch.md's
  gate-wait section plus mmx-orca.md's contents once fetched. Borderline: reachable
  but not a one-line answer at Tier 0.
- **"check the ci-watch report"** — not found anywhere in the 18 memory-global
  files, the nervous-bus CLAUDE.md, or the skill list I read. Searched: all 18
  `@`-imported files (full read via wc/grep above), nervous-bus CLAUDE.md, and the
  23 global skill descriptions. Did NOT search: other projects' CLAUDE.md/AGENTS.md
  bodies for a ci-watch mention (only did `@`-import + top-level grep on those,
  not a full-text search across all 12), nor `.github/workflows/` for a tool named
  ci-watch, nor `~/.local/bin` for a ci-watch binary. Given that gap, I can state
  "not found in the always-loaded tiers I searched" but NOT "does not exist
  anywhere" — the bound is on my search, not the fact. If this phrase refers to a
  specific tool, its durable home (if any) is outside what this pass covered.

---

## 7. Tiered profile architecture — design

**Principle:** progressive disclosure, not omission. Every tier below is reachable
by every session; the difference is WHEN it loads (always vs on first need) and
WHO gets it (orchestrator seat vs subagent seat).

### Tier 0 — Identity (always loaded, target <4KB per project)
**What:** what the project IS (one paragraph), the 3-5 binding rules that would
break something if violated (schema-first, public/private boundary, bd-not-todo),
and POINTERS (file paths, not content) to everything else — AGENTS.md, skills,
role profiles.
**Mechanism: project `CLAUDE.md` file, kept small, NEVER `@`-importing AGENTS.md.**
This is the one tier where "always loaded" is correct by construction (Claude Code
loads it per-cwd unconditionally), so the discipline is entirely about keeping it
under budget — no new mechanism needed, just enforcement (a byte-budget lint,
see §9).
**Why this mechanism:** it's the only one guaranteed to survive compaction AND to
apply regardless of seat (orchestrator or subagent) — anything you want literally
everyone to see belongs nowhere else.

### Tier 1 — Role profiles (loaded by need, not by default)
**What:** ORCHESTRATOR profile (dispatch discipline, merge trains, orca/bus usage,
model tiering — i.e., most of `agent-dispatch.md` + the gate-wait/mmx-orca
material) vs WORKER profile (worktree contract, ambition brief, verification
checklist) vs specialized (release, triage).
**Mechanism recommendation: agent-definition system prompts for the WORKER
profile; a `read-on-demand memory-global file (mmx-orca.md precedent) + one Tier-0
pointer line` for the ORCHESTRATOR profile.**
- Worker profile → **bake into `~/.claude/agents/sonnet-worker.md` /
  `haiku-grunt.md` directly.** These files ARE the subagent's system prompt; they
  are the only mechanism that reaches a subagent WITHOUT reaching the
  orchestrator's own context too (an `@`-import or skill-listing addition would hit
  both seats). Currently both files are near-empty (867B / 2,136B) — this is
  underused real estate. Add the worktree contract + ambition-brief essentials
  (already-short, ~1.4KB) directly into these files' bodies. This is the concrete
  fix for §5's finding that a haiku-grunt in tengine gets 57KB of orchestration
  lore it can't use and none of the worker contract it needs.
- Orchestrator profile → **stays in memory-global as read-on-demand, following the
  mmx-orca.md precedent exactly** (small always-loaded pointer + a name the
  orchestrator fetches when actually dispatching). This is NOT a new mechanism —
  it's applying the ALREADY-PROVEN pattern to the DEMOTE-candidates identified in
  §1 (kb.md's mechanics section, gate-wait-parallelism.md, tree-wipe-incident.md's
  recovery runbook).
**Why not skills for role profiles:** a skill's description ALWAYS loads (that's
the "listed" cost the brief specifically flags) — correct for something
occasionally invoked by name ("use council"), wrong for a profile that should be
either fully present (worker system prompt) or fully absent until fetched
(orchestrator memory-global), never a description-tax on every session regardless
of seat.
**Why not `@`-imports for role profiles:** `@`-imports have exactly one load
state — always, for every seat in that cwd. That's Tier 0's mechanism, structurally
wrong for anything role-conditional (the brief explicitly flags this: "wrong for
profiles").
**Why not SessionStart hook injection for role profiles:** hooks fire once at
session start for the ONE seat that started (the orchestrator) — there's no
per-subagent SessionStart hook to gate a role split, and a hook can't tell in
advance whether this session will spend its life dispatching (orchestrator-heavy)
or executing (worker-heavy). Reserve hook injection for content that's genuinely
universal-and-dynamic (kb-prime's live state snapshot is the right shape for a
hook; a static role profile is not).

### Tier 2 — Specialized / on-demand (skills)
**What:** release procedures, triage playbooks, one-off tool bridges (council,
ask-codex, kb-knowledge) — content that's invoked by explicit name, rarely needed,
and fine to pay a small description-tax for discoverability.
**Mechanism: skills, unchanged.** This tier is already correctly implemented
fleet-wide; the only actionable finding is the staleness/overlap flags in §3
(fix `ask-gemini.md`'s self-flagged staleness, consider consolidating the 6-skill
loomie cluster).

### Tier 3 — Deep reference (kb vault / project docs, pure pull)
**What:** anything that would only ever be read by grep/kb-query when a specific
question comes up — incident postmortems, recovery runbooks, changelog-shaped
content. Never auto-loaded by any mechanism; reached only via `kb query`/`kb brief`
or an explicit file Read.
**Mechanism: kb vault (already exists) for content that should survive a project
rename; plain repo `docs/` for content scoped to one repo's lifetime.**
This is where tree-wipe-incident.md's recovery runbook and kb.md's changelog
material belong per §1 — they're valuable exactly once, when the specific failure
happens, and cost real bytes every other session.

---

## 8. Concrete templates

### 8a. `CLAUDE.md` skeleton (Tier 0, budget: <4KB)

```markdown
# <project> — agent context

**What this is.** <One paragraph: what the system does, its role in the fleet.>

## Binding rules (violate these and something breaks)
- <Rule 1 — e.g. schema-first / public-private boundary / no-bypass-the-SDK>
- <Rule 2>
- <Rule 3, max 5 total>

## Pointers (read on demand, do not inline here)
- Full contract / autonomous-worker rules: `AGENTS.md`
- Orchestrator playbook (dispatch, merge trains, model tiering): pointer only —
  see `~/.claude/memory-global/agent-dispatch.md` (already always-loaded globally)
  and project-specific orchestration notes at `<path>` if any.
- Worker contract: baked into `sonnet-worker`/`haiku-grunt` agent definitions —
  nothing to read manually.
- Build/test commands: `<path or inline if <10 lines>`
- Cross-project links: `<short list, name + one clause each>`

## Beads
`bd ready` / `bd prime` — <one line, do not duplicate the beads plugin's own
boilerplate here if it's already injected elsewhere>
```

### 8b. `ORCHESTRATOR.md` profile skeleton (Tier 1, read-on-demand, budget: <3KB)

```markdown
# Orchestrator profile — <project>

Fetched when: dispatching 2+ agents, running a merge train, or resolving a gate.

## Model tiering for this project
<haiku/sonnet/fable mapping specific to this project's task shapes, if it
differs from the global default in ~/.claude/CLAUDE.md>

## Dispatch checklist
- [ ] Green baseline confirmed before fan-out (`<test command>`)
- [ ] File-scope partitioned, no two agents on one file
- [ ] isolation: "worktree" on every file-editing agent
- [ ] Model tier stated per agent BEFORE dispatch (see global agent-dispatch.md)

## Merge-back procedure
<project-specific: PR flow, hearth-loom pickup, manual merge>

## Where the rest lives
- Cross-cutting dispatch mechanics: ~/.claude/memory-global/agent-dispatch.md (global)
- Gate-wait parallelism: ~/.claude/memory-global/gate-wait-parallelism.md (fetch on demand)
- mmx/orca mechanics: ~/.claude/memory-global/mmx-orca.md (fetch on demand)
```

### 8c. WORKER preamble skeleton (Tier 1, baked into agent definition body, budget: <2KB)

```markdown
---
name: <worker-name>
description: <when to use / not use, one paragraph>
model: <haiku|sonnet>
tools: <explicit allowlist>
---

You are a scoped worker. <task framing>.

## Non-negotiables (from the fleet-wide worker contract)
- FILE SCOPE bounds what you WRITE, never what you READ. Report cross-boundary
  findings with file:line for the next lane to execute.
- Before reporting anything missing/absent/unresolvable: name every surface you
  searched and every one you did not — the bound is a property of your search.
- "Changed nothing" is a success ONLY with receipts.
- Finish the ending: pushed branch + a plain-language result, or a stated
  blocked-on-X. Never end on silence.
- Commit early, push your branch. isolation: "worktree" is your only copy until
  it's pushed.

## Your scope
<task-specific — filled by the dispatcher, not by this file>
```

---

## 9. Top-10 offenders (ranked by bytes-that-shouldn't-be-there)

| # | File | Bytes | Why it's an offender | Cut/demote |
|---|---|---|---|---|
| 1 | `~/projects/career-ops/AGENTS.md` (via `@`-import, always loaded) | 43,211 | Largest always-loaded single file in the whole fleet; MEMORY.md for the same project is nearly empty (138B), suggesting knowledge is stuck in the wrong tier. Not line-audited this pass — top follow-up. | Split into Tier-0 CLAUDE.md (<4KB) + Tier-2 skill(s) for playbooks + push durable facts into `bd remember`/MEMORY.md where they belong. |
| 2 | `~/projects/hearth/CLAUDE.md` | 31,342 | 2nd largest CLAUDE.md fleet-wide; stacks with hearth's 19.5KB MEMORY.md → ~51KB before a tool call. | Extract task-specific sections to Tier 1/2, shrink to identity+pointers. |
| 3 | `~/projects/tengine/CLAUDE.md` + MEMORY.md + skills | 20,151 + 19,562 + ~3.9KB desc | Combined ~57KB is the single biggest always-loaded stack measured (§4); explains fast compaction in tengine sessions. | Same treatment as #1/#2; also move `go-builds.md`-style project-scoped global content that's tengine/tachyonac-specific out of the global tier and into tengine's own Tier 0 pointer. |
| 4 | `~/projects/kb/CLAUDE.md` | 19,137 | No AGENTS.md split at all — everything lives in one always-loaded file for a project whose whole job is knowledge routing/tiering (ironic target for this exact audit). | Split per this design's Tier 0/1 boundary. |
| 5 | `~/projects/tachyonac-engine/CLAUDE.md` | 19,474 | Same shape as tengine — large single file, stacks with 18.6KB MEMORY.md. | Same treatment. |
| 6 | `~/.claude/memory-global/kb.md` | 3,119 | Global-always tier carrying a changelog (kb.md's "FIXED 2026-08-29" narrative) that's only relevant once, to whoever hits that specific bug. | Trim to the 1-2 live rules; move changelog prose to kb vault itself (fitting, since it's about kb). |
| 7 | `~/.claude/memory-global/worktrees.md` (incident-prose portion) | ~1.5KB of 3,073 | Two long dated anecdotes riding alongside the compact rulings; every session pays for narrative it never needs twice. | Keep the ruling bullets, cut to one clause each; move narrative to kb vault or this repo's own incident log. |
| 8 | `~/.claude/memory-global/tree-wipe-incident.md` | 2,470 | Closed-incident postmortem + recovery runbook in the ALWAYS-loaded tier; only needed if `.git` vanishes. | Demote to Tier 3 (kb vault or a `docs/runbooks/` pull-only doc); leave one line in worktrees.md pointing at it. |
| 9 | `~/.claude/memory-global/go-builds.md` | 1,134 | Project-scoped fact (Go build hygiene) wearing a global hat, violating ROUTING.md's own project-vs-global test. | Move to tengine's + tachyonac-engine's project memory/MEMORY.md. |
| 10 | `~/.claude/agents/sonnet-worker.md` + `haiku-grunt.md` | 2,136 + 867 | Not an over-budget offender — the OPPOSITE: most underused real estate in the fleet. Every subagent dispatched anywhere inherits whatever the project's CLAUDE.md/MEMORY.md happen to contain (§5), but these two files — the one guaranteed worker-only channel — carry almost nothing. | Bake in the WORKER preamble skeleton (§8c): worktree contract + ambition-brief essentials, ~1.5KB. This is the single highest-leverage fix in this report: it fixes the brief's core ask (subagents shouldn't inherit orchestration lore, but should reliably get worker discipline) without touching any project's CLAUDE.md. |

---

## Summary for rollout

1. **Biggest win, least risk:** #10 — flesh out `sonnet-worker.md`/`haiku-grunt.md`
   with the worker preamble (§8c). Doesn't touch any project file, fixes the
   role-split problem directly, ships today.
2. **Second win:** apply the mmx-orca.md demotion pattern to items #6-#9
   (kb.md trim, worktrees.md/tree-wipe-incident.md split, go-builds.md
   relocation) — mechanical, ~9KB off the global tier, zero reachability loss.
3. **Largest but slowest:** #1-#5, the five oversized project CLAUDE.md/AGENTS.md
   files. Each needs the deep line-audit this pass didn't have budget for before
   cutting — flagged as follow-up work, not done here.
4. **Coverage gap, not bloat:** shader-garden has zero repo-level agent context —
   worth a minimal Tier-0 CLAUDE.md even though it's not a bloat problem.

**Exhaustion clause (report-level):** this pass measured byte counts for all 12
named projects, all 18 global memory-global files, both root global files, 23
global skills + 2 project skill directories (tengine, career-ops — the only two
projects with `.claude/skills/`), 16 subagent definitions, and 16 MEMORY.md
indexes. It did NOT: line-audit the full text of career-ops/AGENTS.md, hearth/
CLAUDE.md, tengine/CLAUDE.md, kb/CLAUDE.md, or tachyonac-engine/CLAUDE.md for
staleness (only nervous-bus got a targeted 2-claim spot-check, both confirmed
live); search for nested directory-scoped CLAUDE.md files inside any of the 12
project trees; verify subagent CLAUDE.md inheritance empirically (§5, stated as a
working assumption); or check the other 10 projects for a "ci-watch" reference
beyond what's in the always-loaded tiers already read (§6). Each of these is a
named, boundable follow-up, not a closed question.
