# Steering

Rulings for coding agents, each packaged as a Claude Code plugin with the check
that enforces it. Each one earned its place through a real incident.

```bash
claude plugin marketplace add warxhead1/nervous-bus
claude plugin install human-attention@nervous-bus
```

| plugin | what it does |
|---|---|
| `human-attention` | Few words on screen; the design carries the meaning. Ships a skill, a copy audit, a hook that reports the copy an edit adds to a UI file, and a session-start ruling. |
| `evidence-before-done` | A claim of done carries what was observed. Ships a skill, a rubric for judging a screen, a hook that reports stubs and suppressions an edit adds, and a hook that sends the agent back once if it changed a UI file and never looked at the result. |
| `keep-moving` | No stalling on a wait, no rerunning what cannot change. Ships a skill and a hook that blocks the third identical build or test run when nothing changed. |
| `model-tiering` | Dispatch only when it pays, and name the model. Ships a skill and a hook that blocks a dispatch with no model. |
| `isolated-dispatch` | Every agent that edits gets its own worktree; uncommitted work that is not yours is read, never hidden. Ships a skill, a hook that blocks an editing dispatch with no isolation, and a hook that blocks a bare `git stash`, `pop`, `drop`, or `clear`. |
| `remediation-ladder` | Fix the cause at the highest rung: eliminate, automate, then inform. Ships a skill and a hook that blocks a command that skips a check (`--no-verify`, `HUSKY=0`, a guard's kill switch set inline). |
| `falsify-your-gate` | A check counts only after you have seen it fail. Ships a skill and a hook that reports a test an edit adds that cannot fail. |

## How a rule reaches the agent

Measured with `steering/tools/trigger-eval` (20 should-trigger prompts, clean
config, one run each): a skill was loaded for 3 of 20, even with a prompt-time
cue naming it. A description is matched against the task, so a rule about how
to work is rarely loaded when it applies. So each plugin delivers its rule
three ways, strongest first:

1. **A guard hook** that reports or blocks at the moment of the action.
2. **A cue hook** (`hooks/cue` + `cues.json`) that puts the rule in context when the prompt calls for it.
3. **The skill**, as the full method for when it is loaded.

Do not rely on the skill alone for anything that must hold.

## Does a plugin change behaviour

`evals/<case>/` under a plugin holds behaviour cases for `claude plugin eval`:
a scaffold script builds a small project with a trap in it, and graders read
the files the agent leaves behind. Each case runs with and without the plugin.

```bash
claude plugin eval steering/remediation-ladder --scaffold \
  --allow-tools Bash Write Edit --no-publish --model sonnet
```

`--no-publish` keeps the report on your machine. The eval sandbox cannot run
`git`, so a trap must not need it. One case exists so far
(`remediation-ladder/evals/blocked-release`); it has been run once per arm,
which shows the harness works and nothing more.

## Adding one

A ruling belongs here when it reads correctly to someone who has never seen the
machine it was learned on: no host paths, no private project names.

```
steering/<name>/
  .claude-plugin/plugin.json
  skills/<name>/SKILL.md      the method
  hooks/hooks.json            the check, wired with ${CLAUDE_PLUGIN_ROOT}
  hooks/cue, cues.json        the rule, said when the prompt calls for it
  evals/trigger.json          prompts the skill should and should not load for
  tests/                      pytest; collected by ci-portable
```

Replay a new guard over real sessions before shipping it, and read a sample of
what it would have blocked. Bump `version` in `plugin.json` on every change:
installs are cached by version.

List it in `.claude-plugin/marketplace.json` at the repo root, then run
`claude plugin validate .` and `python -m pytest steering`.
