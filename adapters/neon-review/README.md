# neon-review

Adversarial branch review through `agy -p` (default model `gemini-4-neon-medium`). Python 3 stdlib only. Usage, result fields, exit codes and the fix loop are in `SKILL.md`; the review standard is `rubric.md`.

Put it on PATH like `neon-run`: symlink `adapters/neon-review/neon-review` into a PATH directory.

Test: `python3 -m pytest adapters/neon-review` (uses a fake `agy` and a fake `ub-gate`).
