# needs-you

One cross-project queue of what agent sessions need from the operator, so nobody has to read
full turn output.

Every Claude Code turn ends with a `**Needs you:**` block (rule in `~/.claude/CLAUDE.md`). The
Stop hook `needs_you.py hook` reads it, stores the latest block per session under
`~/.local/state/needs-you/sessions/`, and publishes `agent.attention.v1` on the bus. A turn that
used tools but ended without the block is sent back once to add it. A later
`**Needs you:** nothing` clears that session's earlier asks.

```bash
needs-you            # open items, grouped by session
needs-you ack 2      # mark entry 2 handled; `ack all` clears the queue
needs-you --json     # raw entries for dashboards
```

Kill switch: `NEEDS_YOU_DISABLE=1`.
