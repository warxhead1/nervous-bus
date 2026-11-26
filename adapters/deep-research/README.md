# deep-research (`dr`)

Deep research, claim-level citation audit, kb inbox. Python 3.11 stdlib only.

```
dr run "question"            # Gemini Deep Research via gemini.google.com in Orca's browser (engine gemini-web)
dr audit <run_id>            # fetch each cited URL, judge each claim: CONFIRMED|WEAKENED|REFUTED|UNREACHABLE
dr sink kb <run_id>          # markdown + frontmatter -> `kb ingest --tier inbox`
dr notebook <run_id>         # add the audited report to Gemini Notebook through Orca's browser
dr cost                      # per-run and cumulative usage
```

No API key is used or accepted. The engine drives gemini.google.com through `orca` browser subcommands, so Orca's browser (default profile) must be signed in to Google. Signed out, `dr run` exits 3 without submitting anything. `--delete-remote` is refused: conversations are never deleted.

## Runs

Each run lives in `--runs-dir` (default `$XDG_STATE_HOME/deep-research/runs/<run_id>/`):

- `run.json`: question, engine, agent, interaction id (the Gemini conversation id), started/finished, status, usage, cost_estimate, raw_path, report_md, citations `[{claim_span, url, title}]`.
- `interaction.json`: raw poll result (state, url, conversation). `report.md`, `audit.json`, `kb/<run_id>.md` follow.

`cost_estimate` is null for gemini-web runs (no metered usage). Legacy `gemini-interactions` runs keep their recorded usage.

## Config

`$XDG_CONFIG_HOME/deep-research/config.toml` or `--config`. Keys: `engine` (`gemini-web`), `orca_bin`, `step_timeout` (seconds to wait for each UI step), `poll_initial`, `poll_max`, `timeout` (seconds, then the tab is closed and the run marked timeout), `judge_cmd` (default `claude -p --model haiku --output-format json`, prompt on stdin), `judge_timeout`, `fetch_timeout`, `fetch_max_bytes`, `kb_bin`.

## Audit

Per (claim, URL) pair the page is fetched (public hosts only, http(s), size capped) and the judge command returns a verdict. A claim with CONFIRMED and REFUTED sources is WEAKENED. Unfetchable pages and judge failures are UNREACHABLE. Link resolution rates run above 94% while fact-check accuracy is 39 to 77%, so a resolving link proves nothing. Page text is untrusted input to the judge.

## kb sink

Frontmatter is validated before the file is written: `category: research`, `source_type: gemini-deep-research`, `source: gemini-web:<conversation-id>` (legacy `gemini-interactions:<id>` runs still validate), `status: inbox`, `confidence <= 0.4`, `needs_human_review: true`, `half_life_days: 90`, `sources [{url, fetched_at, verdict}]`. An un-audited run is refused unless `--allow-unaudited` (sources then carry verdict UNAUDITED). Without `--tier` in `kb ingest --help` the sink refuses, because kb would write the promoted tier. `--render-only` writes the markdown and skips kb.

## Gemini web UI selectors

All labels, roles and regexes live in the `UI` and `NB` tables at the top of the engine in `dr.py`. Entries are tagged OBSERVED (read from a live snapshot) or UNVERIFIED (needs a signed-in snapshot). When the UI changes, edit the table only.

## Notebook import

`dr notebook <run_id>` opens notebook.google.com in Orca's browser, creates a notebook (or `--notebook-id`), adds the report text as a Copied text source, then the CONFIRMED URLs as a Websites source. It needs `dr audit` first. The NB selectors are UNVERIFIED.

## Tests

`python3 -m pytest adapters/deep-research` (or `python3 tools/setup.py test deep-research`). `fixtures/orca/` holds Orca accessibility snapshots replayed by a fake runner (one real signed-out capture, the rest synthetic; see its README). No network, no browser.
