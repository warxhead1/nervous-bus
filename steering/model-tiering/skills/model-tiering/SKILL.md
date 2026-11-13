---
name: model-tiering
description: >
  Chooses the model for a subagent or workflow agent by what would catch a
  wrong answer, and decides whether to dispatch at all. Use before any Agent
  or Task call, before writing a workflow script, when planning a fan-out, or
  when the user mentions cost, limits, tokens, or "too many agents". Don't use
  for work you are doing yourself in this session.
---

# Model tiering

## First: should this be a dispatch?

One agent with the right skill usually matches a team on quality at lower cost,
because every subagent starts cold and pays for its context again. Dispatch
only when one of these holds:

- Independent pieces can run in parallel, each in a context you can throw away.
- The reading would flood your context and you only need the conclusion.
- You want a judgment that is not anchored on yours.

A build with tight handoffs (plan, build, review, test) is not on this list. Do
it in one session.

## Then: what catches a wrong answer?

Ask this of every dispatch.

| if a subtle miss is caught by | use |
|---|---|
| a test, compiler, gate, or diff you will read | a small model; a wrong answer costs one retry |
| nothing mechanical (design, copy, naming, pricing, sequencing, a final review) | the top model, or write it yourself |

A page can pass every test and still read as generic. Nothing reports that
error until a person sees it, so there is no cheap retry.

## Rules

- Set `model` on every dispatch. An unset model inherits the session's, so a
  fan-out from a top-tier session runs every agent at top-tier cost.
- Before more than three agents, state the plan in one line: how many, which
  tier, why.
- Retry failed items alone. Do not rerun the batch.

The hook blocks a dispatch with no `model`. Off: `MODEL_TIERING_DISABLE=1`.
Agent types that pin their own model: list them in `MODEL_TIERING_ALLOW_TYPES`.
