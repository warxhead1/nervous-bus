# human-attention

Keeps what an agent builds for a person to a few words, with the design carrying the meaning.

## Why it exists

A model reads a whole screen at once. A person reads one thing at a time, so every string an agent adds to a screen costs the reader attention that the agent never pays. Agent-built interfaces drift toward a subtitle under every heading, a description in every card and a tagline on every page. This is a repeated observation across agent-built apps, games and landing pages, not a single measured incident.

The rule: a string ships only if a first-time user would miss it right now. Otherwise the design says it, the user finds it by trying, or it waits until needed.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `human-attention` | loaded when you build or review a screen, page, game or tutorial | the method, with a copy audit script | |
| `hooks/ruling.md` | SessionStart | prints the ruling into the session | remove the plugin |
| `copy-audit-hook` | PostToolUse on Write, Edit, MultiEdit | reports the copy an edit adds to a UI file (exit 2). The write stands | `COPY_AUDIT_DISABLE=1` |

The hook scores the difference between the file before and after the edit, so old copy is never blamed on the edit that touched the file. `skills/human-attention/scripts/copy-audit` also runs by hand.

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [ruling.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/human-attention/hooks/ruling.md).

> # Build for human attention, not model attention
>
> You read a whole screen at once. A person reads one thing at a time, so every
> string you add to something you build costs them. The default is no text.
>
> - A string ships only if a first-time user would miss it right now. Otherwise
>   the design says it (shape, position, motion, feedback), they find it by
>   trying, or it waits until they need it.
> - One new thing at a time. A mechanic's first meeting is safe and teaches by
>   what happens. Cutting the text is half; the design must carry what it said.
> - Plain words. No subtitle restating its heading, no description in every card
>   slot, no tagline, no drama.
> - Stays: action names, errors, confirmations before destructive or costly
>   steps, accessible names.
> - Explanation the user asks for is wanted. This rule is about what ships.
>
> Method: the `human-attention` skill. A hook reports the copy an edit adds to a
> UI file (off: `COPY_AUDIT_DISABLE=1`); whether the design teaches is on you.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/human-attention/skills/human-attention/SKILL.md) with 3 more file(s) listed in [catalog.json](../catalog.json).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`copy-audit-hook`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/human-attention/skills/human-attention/scripts/copy-audit-hook) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Write, Edit, MultiEdit) and `tool_input` with `file_path` and `content` or `old_string`/`new_string`. Exit 0 allows. Exit 2 means the edit added too much copy; the write stands, with the message on stderr. Bad input or a kill switch always exits 0. Keep with it: `copy-audit`, `steering_hits.py`. | report | PostToolUse on Write\|Edit\|MultiEdit | `COPY_AUDIT_DISABLE=1` |
| [`copy-audit`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/human-attention/skills/human-attention/scripts/copy-audit) | `copy-audit <file-or-dir>... [--json] [--max-words N] [--max-pairs N]`. Reads .html .vue .svelte .jsx .tsx and canvas text. Prints the copy and counts; with `--json`, one JSON object. Exit 1 only when a --max limit is given and exceeded. | cli | run by hand |  |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/human-attention.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/human-attention.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/human-attention.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/human-attention.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/human-attention.post.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install human-attention@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** Check out only this plugin:
   `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/human-attention`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export human-attention ./out`, then `claude plugin marketplace add ./out` and `claude plugin install human-attention@human-attention-standalone`. The copy holds this plugin and a marketplace file, nothing else.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/human-attention/skills/human-attention -a claude-code`. This gives the method and the audit script, not the hook or the session ruling.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
