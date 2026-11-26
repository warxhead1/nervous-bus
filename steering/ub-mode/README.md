# ub-mode

Makes an agent measure whether code is right, with deterministic gates instead of opinion: CRAP score, structural duplication and mutation testing.

## Why it exists

"Looks fine" is not a measurement. unclebob's crapper, dryer and mutator answer three questions with numbers: is a function too complex for its tests, does new code copy code that already exists, and would the tests notice a broken line. Run unattended, those tools also install packages and fetch a parser bundle on their own. Run on a large Go repository (1.7M lines), stock dryer was stopped unfinished at a 30-minute cap, and the exact prefix-filter join added here took 50 seconds with byte-identical output on every tree where both finished.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `ub-mode` | loaded when writing, refactoring or reviewing code in Go, Python, Rust, TypeScript, Java or Clojure | the method: gate changed code, not the repo | |
| `bin/ub-gate` | run by the agent | installs the three tools at pinned commits and hash-locked dependencies, then runs them with no network | `UB_GATE_NETWORK=allow` lifts the sandbox, for macOS |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt asks for CRAP, duplication or mutation testing | `STEERING_CUES_DISABLE=1` |

This plugin has no blocking or reporting hook. What it adds to upstream (dependency cooldown, the faster exact duplicate search, a no-network sandbox, and the rewritten skill) is in [UPSTREAM.md](UPSTREAM.md).

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [ub-mode.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/ub-mode.md).

> # ub-mode
>
> Measure it instead of judging it: `ub-gate crap`, `ub-gate dry --since <base> --against-repo`, `ub-gate mutate <file>`, run on the code you changed.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/ub-mode/skills/ub-mode/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`ub-gate`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/ub-mode/bin/ub-gate) | `ub-gate install | doctor | crap | dry | mutate | summary | sandbox -- CMD`. `install` is the only networked step. `doctor` prints JSON. Needs Python 3.11 or newer and git. Keep with it: `ub_gate_dry.py`, `simjoin.py`, `pins.json`, `requirements.lock`. | cli | run by hand |  |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/ub-mode/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/ub-mode.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/ub-mode.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/ub-mode.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/ub-mode.json).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install ub-mode@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/ub-mode`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export ub-mode ./out`, then `claude plugin marketplace add ./out` and `claude plugin install ub-mode@ub-mode-standalone`.
4. **Skill only.** The skill alone does nothing without `bin/ub-gate`, so use paths 1 to 3, or the manual steps below.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin). The steps below are for any other agent that reads `SKILL.md` skills (Kiro CLI, Codex) or for running `ub-gate` by hand.

## Get it

Clone only this folder (git also checks out the top-level files):

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/warxhead1/nervous-bus ~/.local/share/ub-mode-src
git -C ~/.local/share/ub-mode-src sparse-checkout set steering/ub-mode
```

Put `ub-gate` on PATH. It finds `lock/` next to its own `bin/` through the
link, so keep the folder in one piece:

```bash
ln -s ~/.local/share/ub-mode-src/steering/ub-mode/bin/ub-gate ~/.local/bin/ub-gate
```

Link the skill where your agent looks for skills. For Kiro CLI:

```bash
ln -s ~/.local/share/ub-mode-src/steering/ub-mode/skills/ub-mode ~/.kiro/skills/ub-mode
```

## Install the tools

```bash
ub-gate install
```

This is the only step that uses the network. It needs Python 3.11 or newer
and `git`. It fetches crapper, dryer, and mutator from GitHub at the commits in
`lock/pins.json`. It installs their Python packages from the hash-locked
`lock/requirements.lock` and checks the parser bundle against its pinned
sha256. Nothing is vendored in this repo. A host that cannot reach
github.com and pypi.org cannot run this step.

Every other command runs in a network namespace with only loopback. macOS has
none, so set `UB_GATE_NETWORK=allow` there.

To update, run `git -C ~/.local/share/ub-mode-src pull`, then `ub-gate
install`. A changed pin installs into a new venv.

Provenance and licenses: `UPSTREAM.md`.
