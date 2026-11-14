# everos-sink (pilot, #115)

Feeds successful agent runs to EverOS so it can distill them into `SKILL.md` proposals.

## Status

Built so far:

- `mapping.py`: Claude transcript JSONL to EverOS MessageItems. Tool calls are kept as
  `tool_calls`, and their results become `role: tool` rows. No network.
- `schemas/memory.agent.skill.proposed.v1.json`: the event a proposal will publish (ids and
  metadata only).

Not built yet:

- the session-end trigger, gated on reflex-recorder's success label;
- the `POST /api/v2/memory/add` + `/flush` client;
- the watcher that turns a new `agent_skill` into an agent-skills PR and the event above.

## Mapping

```bash
python3 adapters/everos-sink/mapping.py <session>.jsonl          # summary: messages, tool_rounds, requests
python3 adapters/everos-sink/mapping.py <session>.jsonl --dump   # the items themselves
python3 -m pytest adapters/everos-sink -q
```

- Assistant lines that share a message id merge into one item.
- Thinking blocks, meta lines and non-message lines are dropped.
- A tool row whose call is not in the session is dropped, because EverOS answers it with a 422.
- Tool output is capped at 4,000 characters.
- Requests are cut at 500 messages, and a call is never split from its results.
- `case_eligible` means at least 3 answered tool rounds. Below that, EverOS builds no agent_case.

Request fields: `app_id` is the project, and the assistant and tool rows carry
`sender_id=claude-code`.
