# Build for human attention, not model attention

You read a whole screen at once. A person reads one thing at a time, so every
string you add to something you build costs them. The default is no text.

- A string ships only if a first-time user would miss it right now. Otherwise
  the design says it (shape, position, motion, feedback), they find it by
  trying, or it waits until they need it.
- One new thing at a time. A mechanic's first meeting is safe and teaches by
  what happens. Cutting the text is half; the design must carry what it said.
- Plain words. No subtitle restating its heading, no description in every card
  slot, no tagline, no drama.
- Stays: action names, errors, confirmations before destructive or costly
  steps, accessible names.
- Explanation the user asks for is wanted. This rule is about what ships.

Method: the `human-attention` skill. A hook reports the copy an edit adds to a
UI file (off: `COPY_AUDIT_DISABLE=1`); whether the design teaches is on you.
