# Upstream

This directory is a copy of [Dodaios/ste80-humanizer](https://github.com/Dodaios/ste80-humanizer)
at commit `dc94757522792c4af70371a81a01465a6079ec28` (version 4.1.0, MIT).

Copied as they are: `SKILL.md`, `references/`, `scripts/ste_check.py`, `LICENSE`,
`THIRD_PARTY_NOTICES.md`. Left out: the README, the image, the evals and the
packaging for other tools.

Do not edit the copied files here. To update, copy them again from a newer
commit and change the commit above. The hook in `../../hooks/prose-guard` is
ours. It imports `ste_check.py` and keeps its own thresholds, so an update does
not touch it. Run `python -m pytest steering/plain-prose` after an update.
