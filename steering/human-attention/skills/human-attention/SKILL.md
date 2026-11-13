---
name: human-attention
description: >
  Tunes what you build to human attention instead of model attention: fewer
  words on screen, meaning carried by the design, one new thing at a time. Use
  whenever building, reviewing, or briefing an agent on anything a person will
  look at or play: an app screen, web page, game, HUD, tutorial, onboarding,
  dashboard, empty state, landing copy. Also use when the user says a result is
  over-explained, wordy, cluttered, overwhelming, "AI slop", has subtitles,
  bylines or captions on everything, or tells instead of shows. Don't use to
  shorten an explanation the user asked for, or on briefs, docs and reports
  whose reader wants the detail.
license: MIT
---

# Human attention

You take in a whole screen at once. A person takes it one thing at a time, and
every string you add is one more thing. What feels thorough to you reads to
them as noise.

Text on a built surface is a cost. The default is none.

## Before you write a string

Ask in order. Stop at the first yes.

1. **Would a first-time user miss it right now?** No: leave it out.
2. **Can the thing say it itself?** Shape, position, colour, icon, motion, a
   sensible default, an example already in place. Change the thing.
3. **Can they find out by trying, safely?** Let them. Make the first try cheap
   to get wrong.
4. **Can it wait until they need it?** Show it then, where they are looking:
   on hover, on focus, on error, on first use.
5. **Only then write it.** The fewest plain words, once.

## What to stop doing

- A subtitle that restates its heading. "Settings. Manage your settings and
  preferences."
- A description on every card, tile, tab and section because the component has
  a slot for one. An empty slot is fine.
- Helper text under a control whose label already says it.
- A paragraph, modal or tooltip tour that explains a mechanic. If it needs a
  paragraph, the design is unfinished.
- Marketing voice inside a tool: taglines, "Welcome to", "Your all-in-one",
  "not just X, but Y", "seamless", "powerful", adjectives in threes, emoji as
  headers.
- Narrating what the screen already shows. "You have 3 items" above a list of
  three.
- Badges, legends and footnotes nobody asked about.

## Teach by design

For games, onboarding, and anything with a mechanic to learn.

- One new thing at a time. The next arrives after they have used the last.
- The first meeting is safe. It cannot kill them, and doing the obvious thing
  shows what it does.
- The thing looks like what it does, and stands out from what surrounds it.
  Shape or icon as well as colour, so it reads without colour vision.
- Feedback is the explanation: immediate, visible, on the thing that changed.
- Then a situation that needs it, a little harder.
- If a word is still needed: one or two, at the spot, at the moment, gone after
  the first success. Eight words on a game screen at once is already a lot.

**Example.** A blue floor tile cures fever. A popup explaining it is wrong.
Deleting the popup and leaving a meaningless blue tile is also wrong. Instead:
the first room gives the player a mild fever (the screen tints, a meter
pulses) and puts the tile between them and the door. They walk across, the
tint clears, the meter drains with a sound. No words. The next room puts the
tile off the path.

Removing text is half the job. The design has to pick up what the text was
carrying.

## Text that stays

- Action names on buttons, and a label on any icon that could mean two things.
- Errors: what happened, what to do next.
- A confirmation before anything destructive or costly.
- Money, legal, privacy.
- Units and axis labels on data.
- Accessible names and alt text. Not visible is not the same as optional.
- Density in a tool whose expert users asked for it.

## Check before calling it done

```bash
python3 scripts/copy-audit <files or dirs>   # scripts/ sits next to this file
```

It lists every string a person will see, and counts words, heading+subtitle
pairs, description slots, long strings and stock phrases. Put each listed
string through the five questions, cut, and run it again. Say the before and
after counts in one line of your report.

Then look at a screenshot for five seconds and look away. What you remember is
what the user gets.

## In a brief

Sub-agents do not load this unless told. For any lane that builds something a
person sees, add:

> Load the `human-attention` skill before writing UI. Default to no on-screen
> text; the design carries the meaning. Run `copy-audit` on what you built and
> report its counts.

## Boundary

This governs what a person's eyes land on in the thing you ship. It does not
shorten an explanation someone asked for. Wanting to understand is not
clutter.
