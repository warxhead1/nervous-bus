# Upstream

This directory is a copy of [Dodaios/ste80-humanizer](https://github.com/Dodaios/ste80-humanizer)
at commit `0d6f7994a581205aba615e0663a4c509d8a35c7d` (version 4.0.0, MIT).

Copied as they are: `SKILL.md`, `references/`, `scripts/ste_check.py`, `LICENSE`,
`THIRD_PARTY_NOTICES.md`. Left out: the README, the image, the evals and the
packaging for other tools.

Do not edit the copied files here. To update, copy them again from a newer
commit and change the commit above. The hook in `../../hooks/prose-guard` is
ours. It imports `ste_check.py` and keeps its own thresholds, so an update does
not touch it. Run `python -m pytest steering/plain-prose` after an update.
