---
name: grill
description: >
  Routes an open game design question through `crucible grill ask`, and drafts
  the card when the question must reach the owner. Use in a repo that has a
  crucible ledger when you are about to guess a design choice, about to ask
  the owner a design question in chat or in a Needs-you bullet, when you class
  a question as fact, measurable, lead, owner or money, when you draft or
  review a card and its options, or when the owner says "grill me". Don't use
  for a step inside work the owner already asked for, or in a repo with no
  crucible ledger.
---

# Grill

The owner's attention is the scarcest resource in the project. Ask hard
questions, and ask few. A question reaches the owner only when the answer is
taste, money or direction, and a wrong guess costs more than that attention.

## Send the question to the ledger

When you meet an open design question, run `crucible grill ask`. Its `--help`
is the reference.

- Do not guess. A guess is a decision that nobody logged and nobody can flip.
- Do not ask in chat or in a Needs-you bullet. That answer depends on a live
  session and never reaches the ledger.
- A lane never asks the owner directly.

The command returns one of three states.

| State | Meaning | You do |
|---|---|---|
| `settled` | An answer exists, or an agent took the call and logged it | Use it and cite the decision id |
| `routed` | A lookup, a research run or a lab experiment has the question | Take other work until it settles |
| `asked` | A card is with the owner | See the next two lines |

On a two-way card, proceed on the recommended option and mark the PR
`Design: q-<id> pending`. On a one-way card, the node is not ready to build,
so take other work.

## Settle the question before the owner sees it

Run six checks, cheapest first. The full text with clause ids is
`adapters/crucible/rubric/triage.v1.toml`.

1. **Search for an existing answer.** Read the ledger, then kb with
   `--project`. A hit drops the question with a ref. A decision about a
   neighbouring thing is evidence and leaves the question open.
2. **Class the question** and cite the clause you used.
3. **Set the stakes**: door, weight and radius.
4. **Draft** two to four options, and what changes under each.
5. **Recommend** one option, with only the pips it earned.
6. **Apply the gate.**

### Class

| Class | The answer | Ends as |
|---|---|---|
| fact | exists outside the game and is the same for any owner | a lookup or research. Never a card |
| measurable | comes from a run, under a rule written before the run | a lab experiment |
| lead | follows by reasoning from settled decisions and evidence | a logged agent decision |
| owner | depends on what the owner wants the game to be | taken, or a card |
| money | spends money or quota, or sets what the game costs or earns | a card, alone |

Three tests separate the classes.

- **Name the evidence that would change the answer.** A lookup means fact. A
  run means measurable. No evidence means lead or owner.
- **Picture a different owner with the same ledger.** If the answer stays the
  same, the class is lead.
- **Ask what the owner would notice in the finished game.** If the answer is
  nothing, the class is lead.

Split a question that mixes classes. Look up the fact part and measure the
measurable part. Only the taste residue keeps the owner class.

Not knowing the answer does not make an owner question. A sonnet job classes
an off-tree question. Haiku never does, because no mechanical check catches a
wrong class.

### Stakes

- **Door.** One-way means that undoing the answer needs code changes across
  areas, breaks stored data, leaves the repo or spends what does not come
  back. Two-way means that undoing it is a data change or one lane of work.
  If you cannot count the cost of undoing, the door is one-way.
- **Weight.** Outcome-critical means that a wrong answer breaks a pillar,
  loses data or money, or fails the first playable slice. Divergent means
  that a player would tell the options apart. Benign means that the options
  differ little, or the ledger predicts the pick.
- **Radius.** Count the downstream nodes, label uses and lanes. Do not
  estimate.

### Gate

| Class | Door and weight | End |
|---|---|---|
| owner | two-way and benign | taken on the recommendation. One tap flips it |
| owner | two-way, divergent or outcome-critical | a card in a batch, with no default |
| owner | one-way | a card, alone, with no default, after a sealed two-seat proposal |
| money | any | a card, alone, with no default |

Six things are never taken: a pillar, a spend, the look baseline, a public
name, scope and a save format. A guard can raise a class, a door or a weight.
It never lowers one.

A recommendation earns up to three pips. Independent evidence earns the
first. Agreement between sealed seats from two model families earns the
second. A measured record of accepted recommendations in that class earns the
third. Your own confidence earns none.

## Draft a card

The owner reads a card on a phone in a few seconds. The full rules are
`adapters/crucible/rubric/cards.v1.toml`. `crucible grill post` exits 1 on a
card that breaks a shape rule.

- **Ask one idea in at most 90 characters.** Ask about the game the player
  gets, never about code or tools.
- **Ask the upstream question.** When one answer decides three smaller
  questions, ask that one and take the three.
- **Ask for a choice.** Never ask the owner to supply facts, to list things or
  to explain.
- **Write real options.** Each is one that a reasonable owner could pick, each
  excludes the others, and together they cover the live choices. If the owner
  must type to answer, the draft missed an option.
- **Name each option in at most three words** and 48 characters.
- **Give each option one "what changes" line** of at most 96 characters.
  Compute it where you can: "data only" or "2 lanes". State what is built,
  removed or delayed, not a virtue.
- **Post the recommended option first.** "Yes" accepts it.
- **Keep the rationale one tap down.** The brief says what was looked up, what
  was measured and what could not be found out.
- **Show a look question as renders of one test scene.** Make a feel question
  two slices that the owner plays. Never describe either.
- **Put no number on a card** unless the number is in the evidence.
- **Give a one-way card or a money card no default.** A default would commit
  your own pick.
- **Batch only parts whose options explain themselves**: names, image picks,
  yes or no. A batch holds eight parts at most.

## Read the answer words

These phrases match by exact rule. Any other words are stored as typed.

| The owner says | Result |
|---|---|
| "yes", "first", "the recommended one" | picks the recommended option |
| "send" | submits the picks made by voice |
| "call it X" | renames the option to X, then picks it |
| "you decide" | the lead takes the call and logs it |
| "later" | defers the question, with a wake condition |
| "test it" | starts an investigation, and the question waits |
| "why" | opens the brief. Nothing is recorded |
| "wrong question" plus a note | withdraws the card for a redraft |
| "harder" | raises the pressure on that subtree |
| "enough" | closes the sitting |

"Grill me" opens a sitting. "Grill me on" plus a topic opens one subtree.

## Attack the answer, not the owner

Every answer is tested after the owner gives it. The number of cards stays
under the sitting's cap.

- List the testable claims that the answer rests on. Send each one to the lab.
- A refuted claim, or a conflict with an earlier owner decision, returns as
  one challenge card: keep, switch or look deeper.
- A contradiction that one model suspects is a lead. It becomes a card only
  when a seat from another model family agrees.
- A pillar is not ruled until the owner accepts one cost of it.
- Never send the owner back to answer the same question again.
