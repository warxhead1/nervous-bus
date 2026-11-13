# Judging a screen

Use this after a change to anything a person looks at. Judge from the
screenshot of the running build and the code together; either alone misses
things. Score each line 0 (fails), 1 (partly), 2 (holds), and quote what you
saw for every 0 or 1. Do not round up.

## Take the evidence first

- The real build, not a mock or a storybook frame, at the size it ships at.
- One narrow width and one wide, if it is responsive. Dark and light, if both
  exist.
- The states that matter: empty, loading, full, error, long text.
- Read the screenshot. A file that exists is not a screen you looked at.

## Does it do what was asked

1. Everything in the request is on the screen, and nothing asked to be removed
   is still there.
2. The data shown is real or realistic. No lorem ipsum, no "Item 1", no zero
   counts caused by a missing fixture.
3. Nothing is broken: no overflow, overlap, clipped text, missing image,
   unstyled flash, or console error.

## Can a person use it

4. One thing leads. The eye lands on the main action or content first.
5. Text is readable: contrast, size, line length. Check the smallest text.
6. Controls look like controls, show their state, and are big enough to hit.
7. Spacing and alignment follow one scale. Edges line up.
8. It holds at the narrow width without sideways scrolling.

## Is it finished work

9. Few words. No heading restated by a subtitle, no description in every
   slot, no explanatory text the layout could carry.
10. It belongs to this product: its colours, type, and components, not a
    default template.
11. Motion and feedback exist where an action needs them, and nowhere else.

## Report

Give the scores, the quotes for anything under 2, and what you did not look
at. A model judging a screenshot agrees with people about two times in three,
so a clean score is not approval: say "looked, found nothing" and let the
person decide.
