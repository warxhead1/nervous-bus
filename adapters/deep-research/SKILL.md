---
name: deep-research
description: Run programmatic deep research (Gemini Deep Research via the Interactions API), audit every cited claim against its source page, and file the result in the kb inbox tier for human review. Use when a question needs a cited multi-source report and you want claim-level verification, per-run cost tracking, or an inbox entry in the vault. Not for quick lookups.
---

# deep-research (`dr`)

Run `dr` from `adapters/deep-research/`. JSON on stdout, errors on stderr, `dr <cmd> --help` is the reference.

| Question | Command |
|---|---|
| Research this question | `dr run "<question>"` (or `--question-file Q.md`; add `--delete-remote` to delete the server copy after download) |
| Which cited claims hold up | `dr audit <run_id>` |
| File it in the vault | `dr sink kb <run_id> --project <p>` |
| What did runs cost | `dr cost` |
| Feed a notebook (unofficial, off by default) | `dr sink notebook <run_id> --enable-unofficial` |

Rules:

- Key comes from env `GEMINI_API_KEY` only: inject it from your secrets manager. Never pass it as an argument.
- The question leaves the machine. Keep it generic: no private names or data. Use a paid-tier key.
- A run takes 5 to 60 minutes. Run it detached (`setsid nohup ... &`) and read the run dir. The interaction id is saved before polling starts.
- Always `dr audit` before `dr sink kb`. Link resolution is not verification: CONFIRMED means a judge found the claim on the fetched page.
- `dr sink kb` writes `status: inbox`, `confidence <= 0.4`, `needs_human_review: true`. It refuses when `kb ingest --help` has no `--tier`. Never promote without `kb vet`.
- Judge failures count as UNREACHABLE, never CONFIRMED.
