# deep-research (`dr`)

Deep research, claim-level citation audit, kb inbox. Python 3.11 stdlib only.

```
dr run "question"            # Gemini Deep Research, background create + poll, raw JSON saved
dr audit <run_id>            # fetch each cited URL, judge each claim: CONFIRMED|WEAKENED|REFUTED|UNREACHABLE
dr sink kb <run_id>          # markdown + frontmatter -> `kb ingest --tier inbox`
dr sink notebook <run_id> --enable-unofficial
dr cost                      # per-run and cumulative usage
```

Run it under your secrets manager so `GEMINI_API_KEY` is injected (for example `hearth-vault exec --prefix ai/ --redact -- dr run ...`; any manager that sets the env var works). The key is read from the environment, sent as the `x-goog-api-key` header, and never logged or accepted as an argument.

## Runs

Each run lives in `--runs-dir` (default `$XDG_STATE_HOME/deep-research/runs/<run_id>/`):

- `run.json`: question, engine, agent, interaction id, started/finished, status, usage, cost_estimate, raw_path, report_md, citations `[{claim_span, url, title}]`.
- `interaction.json`: raw API response. `report.md`, `audit.json`, `kb/<run_id>.md` follow.

`cost_estimate` is null unless `prices_per_mtok` (`input`, `output`, `cached`) is set in config. No prices are built in.

## Config

`$XDG_CONFIG_HOME/deep-research/config.toml` or `--config`. Keys: `agent` (pinned id, default `deep-research-preview-04-2026`; `latest` is refused), `api_revision`, `poll_initial`, `poll_max`, `timeout` (seconds, then the job is cancelled), `judge_cmd` (default `claude -p --model haiku --output-format json`, prompt on stdin), `judge_timeout`, `fetch_timeout`, `fetch_max_bytes`, `prices_per_mtok`, `kb_bin`.

## Audit

Per (claim, URL) pair the page is fetched (public hosts only, http(s), size capped) and the judge command returns a verdict. A claim with CONFIRMED and REFUTED sources is WEAKENED. Unfetchable pages and judge failures are UNREACHABLE. Link resolution rates run above 94% while fact-check accuracy is 39 to 77%, so a resolving link proves nothing. Page text is untrusted input to the judge.

## kb sink

Frontmatter is validated before the file is written: `category: research`, `source_type: gemini-deep-research`, `source: gemini-interactions:<id>`, `status: inbox`, `confidence <= 0.4`, `needs_human_review: true`, `half_life_days: 90`, `sources [{url, fetched_at, verdict}]`. An un-audited run is refused unless `--allow-unaudited` (sources then carry verdict UNAUDITED). Without `--tier` in `kb ingest --help` the sink refuses, because kb would write the promoted tier. `--render-only` writes the markdown and skips kb.

## Notebook sink (unofficial)

Off by default. It needs `--enable-unofficial` and the third-party `notebooklm-py` package, which drives undocumented Google endpoints with browser cookies. It can break without notice and may violate Google terms. Use a dedicated account. It adds the report text and the CONFIRMED URLs to a new or existing notebook (`--notebook-id`). Without the package it prints the manual steps. Gemini Notebook's own Deep Research covers the human path.

## Tests

`python3 -m pytest adapters/deep-research` (or `python3 tools/setup.py test deep-research`). Fixtures in `fixtures/` follow the documented Interactions API shape. No network.
