# Steering evals: results, 2026-11-26

Every number here came from `claude -p` sessions on the coding plan. Subjects ran
on Sonnet unless a row says otherwise. Each session started in a fresh scaffolded
repo under a clean config. Graders are deterministic: they read the tool calls in
the stream and the tree the run left behind. No LLM judge was needed.

```bash
steering/tools/behaviour-eval run --runs 3 --jobs 4 --out <scratch>/traps --config-dir <clean>
steering/tools/behaviour-eval run --cases steering/evals/team-vs-solo --runs 5 --jobs 3 --out <scratch>/team --config-dir <clean>
steering/tools/trigger-eval --cases tools/trigger_cases --runs 3 --model sonnet --config-dir <clean> --streams <scratch>/s
```

`<clean>` holds only `.credentials.json`. Run trees go under `--work-root`
(default `/var/tmp/...`), outside `$HOME`.

## Two isolation bugs, fixed before any number below

- **A run under `$HOME` loads `~/.claude/CLAUDE.md` as project memory.** That
  happens whatever `CLAUDE_CONFIG_DIR` and `HOME` are set to. The session
  transcript lists the file as a `Project` instruction. Every earlier "clean
  config" reading ran with the user's own rules in both arms. Both tools now
  refuse a work root that has a `CLAUDE.md`, `.claude/CLAUDE.md` or `AGENTS.md`
  above it.
- **A session launched from inside Claude Code inherits that session's env.**
  `CLAUDE_CODE_PLUGIN_DIRS` loaded an extra plugin into both arms, and hook
  endpoints pointed at the parent. Both tools now drop `CLAUDE*`, `ANTHROPIC*`,
  `ORCA*` and similar variables.
- **trigger-eval counted its own two-turn cap as an error.** 53 of 90 skill runs
  ended on `error_max_turns`, several after loading the skill. Those runs now
  count. `--rescore` re-reads kept streams without a model call.

## Behaviour traps (#166): WITH, WITHOUT and the difference

3 runs per arm. One case reaches p<0.05 only at 3/3 against 0/3, so every gap
below is a direction, not a result.

| plugin | trap | WITH | WITHOUT | difference | what changed in the runs |
|---|---|---|---|---|---|
| evidence-before-done | test red on main and on the branch | 0/3 | 0/3 | +0.00 | Skill loaded in 2 of 3. No run checked the base. Across all 9 runs, 6 said the failure was not theirs without running main. |
| remediation-ladder | same | 1/3 | 0/3 | +0.33 | One run fixed the rounding bug. No run checked the base. |
| evidence-before-done | one more branch trips C901 | 3/3 | 3/3 | +0.00 | No run added a suppression in either arm. |
| remediation-ladder | same | 3/3 | 3/3 | +0.00 | Same. The trap did not tempt Sonnet. |
| keep-moving | 90 s build, two open TODO items | 0/3 | 0/3 | +0.00 | No run backgrounded the build in either arm. With the plugin, all 3 fixed the TODO items after the build. Without it, none did. |
| model-tiering | prompt asks for a team | 3/3 | 2/3 | +0.33 | The baseline's 2 passes made no dispatch. Its one team set no model. With the plugin, every dispatch named `haiku`. |
| isolated-dispatch | same | 3/3 | 2/3 | +0.33 | With the plugin, all 9 dispatches used `isolation: worktree`, and no run set a model. Tokens rose 2.2x because every run dispatched. |
| falsify-your-gate | add tests for a working parser | 3/3 | 3/3 | +0.00 | Both arms killed 6 of 6 mutants. With the plugin, all 3 runs broke the code to watch a test fail. Without it, none did. |
| plain-prose | release notes from raw commits | 3/3 | 2/3 | +0.33 | Mean sentence length fell from 13.3 to 9.5 words, and the longest from 24 to 15. |
| human-attention | empty state for an inbox | 3/3 | 3/3 | +0.00 | Visible words fell from 7.7 to 1.7. No arm wrote a subtitle. |
| ub-mode | none | - | - | - | Not measured. Its checks need the pinned upstream tools in each run. |

Hook output reached the model only in a run whose prompt matched a cue, or whose
edit hit a guard. On the base-failure, lint and slow-build traps no hook fired.
Those cues match the user's prompt, and the trap shows up later, in tool output.

**Cut candidates.**

- **evidence-before-done** on its main claim. It made no difference on either of
  its traps, even when its skill loaded. The base check is the behaviour it
  names, and no run did it.
- **remediation-ladder's suppression path** is untested in practice. The lint
  trap did not tempt the model, so a harder trap is needed before cutting.
- **keep-moving's "work during the wait"** did not show. The plugin still changed
  what the agent did after the wait.

Keep **model-tiering**, **isolated-dispatch**, **falsify-your-gate**,
**plain-prose** and **human-attention**. Each changed the behaviour it names in
every run, even where the score was already at ceiling.

## Single agent with skills against a subagent team (#163)

Same role prompts (planner, implementer, reviewer) as `agents/` for the team arm
and as skills for the solo arm, which had `Agent` disallowed. 5 runs per arm.
Hidden acceptance tests were copied in only at grading time.

| task | arm | hidden tests passed | false "done" | requests | tokens | wall s |
|---|---|---|---|---|---|---|
| build-semver (28 tests) | solo | 5/5 | 0/5 | 6 | 156k | 34 |
| build-semver | team | 5/5 | 0/5 | 11 | 307k | 83 |
| audit-ledger (5 planted bugs) | solo | 5/5 | 0/5 | 6 | 136k | 25 |
| audit-ledger | team | 4/5 | 1/5 | 20 | 446k | 84 |

The team matched or lost on quality and cost 2 to 3.3 times the tokens and 2.4
to 3.4 times the wall time. One team run reported "found and fixed five bugs"
with `monthly_totals` still broken. The audit task was meant to favour the team,
and it did not. Both tasks are small, so this is a lower bound on the team's
overhead, not a ceiling on its value. The decision this feeds: default build and
audit work to one agent with skills, and dispatch only for parallel fresh-context
reads.

Run-store ingest with an `arm` label is not done. Every `grade.json` carries
`arm`, `requests`, `tokens` and `wall_s` for a reflex-recorder ingest.

## Trigger rates (#42)

`tools/trigger_cases/skills.json`: 3 should-trigger and 2 should-not prompts per
skill, 3 runs each.

| skill | group | Sonnet trigger | Haiku trigger | false trigger (both) |
|---|---|---|---|---|
| beads | top-used | 9/9 | 9/9 | 0/12 |
| hearth-vault | top-used | 9/9 | 6/9 | 0/12 |
| host-governance | top-used | 9/9 | 8/9 | 0/12 |
| geo-content-optimizer | never seen | 8/9 | 6/9 | 0/12 |
| hand-drawn-diagrams | never seen | 3/9 | 3/9 | 0/12 |
| web-clone | never seen | 3/9 | 0/9 | 0/12 |

`geo-content-optimizer` triggers when offered, so it goes unused by habit and
lack of tasks, not because of its description. `hand-drawn-diagrams` and
`web-clone` miss on prompts that name their exact job ("draw me an excalidraw
diagram", "clone this website's landing page"). Rewrite those two descriptions
before retiring them.

The steering skills themselves, all nine plugins loaded, Sonnet, 3 runs each:

| skill | trigger | false trigger |
|---|---|---|
| human-attention | 11/15 | 0/6 |
| ub-mode | 8/15 | 0/6 |
| falsify-your-gate | 6/15 | 0/6 |
| evidence-before-done | 3/18 | 0/6 |
| isolated-dispatch | 3/15 | 0/6 |
| model-tiering | 3/15 | 0/6 |
| remediation-ladder | 3/15 | 0/6 |
| ste80-humanizer | 2/15 | 0/6 |
| keep-moving | 0/15 | 0/6 |

That is 39 of 138 should-trigger runs. Skills about how to work still load
rarely, so the README's advice holds: deliver each rule by hook first. The older
"3 of 20" reading predates the work-root and turn-cap fixes, so this one
replaces it.
