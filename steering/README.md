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

## Adding one

A ruling belongs here when it reads correctly to someone who has never seen the
machine it was learned on: no host paths, no private project names.

```
steering/<name>/
  .claude-plugin/plugin.json
  skills/<name>/SKILL.md      the method
  hooks/hooks.json            the check, wired with ${CLAUDE_PLUGIN_ROOT}
  tests/                      pytest; collected by ci-portable
```

List it in `.claude-plugin/marketplace.json` at the repo root, then run
`claude plugin validate .` and `python -m pytest steering`.
