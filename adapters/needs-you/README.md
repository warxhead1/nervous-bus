# needs-you

One cross-project queue of what agent sessions need from the operator, so nobody has to read
full turn output.

Every Claude Code turn ends with a `**Needs you:**` block (rule in `~/.claude/CLAUDE.md`). The
Stop hook `needs_you.py hook` reads it, upserts an `errand` decision (`ny-<session_id>`) in Hearth
via `POST /api/decisions`, and publishes `agent.attention.v1` on the bus. A turn that used tools
but ended without the block is sent back once to add it. A choice-shaped bullet without a Hearth
decision id is also sent back once so the agent posts a real choice card with `hearth-ask post`;
if it still harvests as an errand, local image/video paths (`png`, `jpg`, `webp`, `gif`, `mp4`)
named in the bullet or final reply are uploaded via `hearth-ask media` and attached to `media`
(up to 8). A later `**Needs you:** nothing` withdraws the session's decision.

The local JSON under `~/.local/state/needs-you/sessions/` is an offline cache: written after a
successful post, replayed on the next reachable run if Hearth was down, and pruned once the
decision settles. If Hearth does not yet support the `errand` kind (4xx), the hook falls back to
local storage and `agent.attention.v1` without interrupting the turn.

Goal loop breaker: when the `/goal` checker re-opens a session 3 turns in a row and none of those
turns makes a tool call, the hook returns `continue: false`. Claude Code then ends the turn and
pauses the goal ("a hook that ended the turn"). The ask "Goal checker is looping" goes to the top
of the queue. A goal turn that uses a tool, or any prompt from a person, resets the count.

```bash
needs-you              # open errand decisions from Hearth (offline cache fallback)
needs-you ack 2        # settle entry 2 via /api/decisions/{id}/respond (done: true, via: terminal)
needs-you --json       # raw open entries for dashboards
needs-you migrate      # one-time migration of local sessions (<=7d posted, >7d withdrawn)
```

Items that name a decision id (`dec-...`, `src-...`) get ` <hearth://decisions/{id}>` appended; an
inline `hearth://` URL moves to the end in the same form (Android renders it as a button). An item
saying `Hearth ->` with no link prints a one-time stderr hint per session (never blocks).

Kill switch: `NEEDS_YOU_DISABLE=1`.
