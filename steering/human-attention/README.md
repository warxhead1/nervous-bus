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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install human-attention@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** Check out only this plugin:
   `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/human-attention`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export human-attention ./out`, then `claude plugin marketplace add ./out` and `claude plugin install human-attention@human-attention-standalone`. The copy holds this plugin and a marketplace file, nothing else.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/human-attention/skills/human-attention -a claude-code`. This gives the method and the audit script, not the hook or the session ruling.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
