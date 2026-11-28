# deep-research (`dr`)

Deep research, claim-level citation audit, kb inbox. Python 3.11 stdlib only.

```
dr run "question"            # Gemini Deep Research via gemini.google.com in Orca's browser (engine gemini-web)
dr audit <run_id>            # fetch each cited URL, judge each claim: CONFIRMED|WEAKENED|REFUTED|UNREACHABLE
dr sink kb <run_id>          # markdown + frontmatter -> `kb ingest --tier inbox`
dr notebook <run_id>         # add the audited report to Gemini Notebook through Orca's browser
dr cost                      # per-run and cumulative usage
```

No API key is used or accepted. The engine drives gemini.google.com through `orca` browser subcommands, so the Orca browser profile `dr` opens tabs in must be signed in to Google. Tabs open in Orca's own default for the worktree. Set `browser_profile = "<id>"` in `~/.config/deep-research/config.toml` (ids from `orca tab profile list`) to pin another profile. Signed out, `dr run` exits 3 without submitting anything. `--delete-remote` is refused: conversations are never deleted.

## Runs

Each run lives in `--runs-dir` (default `$XDG_STATE_HOME/deep-research/runs/<run_id>/`):

- `run.json`: question, engine, agent, interaction id (the Gemini conversation id), started/finished, status, usage, cost_estimate, raw_path, report_md, citations `[{claim_span, url, title}]`.
- `interaction.json`: raw poll result (state, url, conversation). `report.md`, `audit.json`, `kb/<run_id>.md` follow.

`cost_estimate` is null for gemini-web runs (no metered usage). Legacy `gemini-interactions` runs keep their recorded usage.

## Config

`$XDG_CONFIG_HOME/deep-research/config.toml` or `--config`. Keys: `engine` (`gemini-web`), `orca_bin`, `step_timeout` (seconds to wait for each UI step), `poll_initial`, `poll_max`, `timeout` (seconds, then the tab is closed and the run marked timeout), `judge_cmd` (default `claude -p --model haiku --output-format json`, prompt on stdin), `judge_timeout`, `fetch_timeout`, `fetch_max_bytes`, `kb_bin`.

## Audit

Each citation's `claim_span` is the sentence or bullet containing the link (link label kept, capped at 400 chars). Per (claim, URL) pair the page is fetched (public hosts only, http(s), size capped) and the judge command evaluates whether the page body supports the claim sentence (a matching title alone is not support). PDF responses (`application/pdf` or `%PDF` magic) are text-extracted with `pdftotext` when on `PATH`, and pages whose readable text is under 200 chars after stripping tags are marked UNREACHABLE (`no readable text`) without calling the judge. A claim with CONFIRMED and REFUTED sources is WEAKENED. Unfetchable pages, unreadable pages, and judge failures are UNREACHABLE. Link resolution rates run above 94% while fact-check accuracy is 39 to 77%, so a resolving link proves nothing. Page text is untrusted input to the judge.

## kb sink

Frontmatter is validated before the file is written: `category: research`, `source_type: gemini-deep-research`, `source: gemini-web:<conversation-id>` (legacy `gemini-interactions:<id>` runs still validate), `status: inbox`, `confidence <= 0.4`, `needs_human_review: true`, `half_life_days: 90`, `sources [{url, fetched_at, verdict}]`. An un-audited run is refused unless `--allow-unaudited` (sources then carry verdict UNAUDITED). Without `--tier` in `kb ingest --help` the sink refuses, because kb would write the promoted tier. `--render-only` writes the markdown and skips kb.

## Gemini web UI selectors

All labels, roles and regexes live in the `UI` and `NB` tables at the top of the engine in `dr.py`. Every entry was read from a live signed-in snapshot (2026-11-26). When the UI changes, edit the table only.

## Notebook import

`dr notebook <run_id>` opens gemini.google.com/notebooks/create in Orca's browser, creates a notebook named `--title` (default `dr: <question>`), adds the report text as a Copied text source, then the CONFIRMED URLs as Websites sources, three per Insert because one Insert keeps only the first few. `--notebook-id` adds to an existing notebook, and `--no-report` adds only the URLs. It needs `dr audit` first.

`dr run --attach <conversation-id> "<question>"` skips submission and polls a Gemini conversation that was already started.

## Display off

After opening the tab, `dr run` checks that it draws a frame (a `requestAnimationFrame` callback within 3 s). On a sleeping display Orca's tab never paints: `Page.captureScreenshot` reports "did not draw a frame", `innerText` is empty and Gemini shows no messages. `dr` then exits 5 with `dr: Orca's browser tab is not painting`, without polling. Wake the display and rerun.

Orca's Linux launcher passes `--ozone-platform=wayland`. Untested candidate for unattended runs: add `--disable-renderer-backgrounding --disable-backgrounding-occluded-windows --disable-background-timer-throttling` to the Exec line of Orca's desktop entry, or switch to `--ozone-platform=x11`, then restart Orca. `orca tab create --headless` and `orca serve` use Orca's offscreen browser backend, which does not depend on the display, but they need a headless Orca runtime separate from the desktop app, and that profile has no Google sign-in.

## Tests

`python3 -m pytest adapters/deep-research` (or `python3 tools/setup.py test deep-research`). `fixtures/orca/` holds Orca accessibility snapshots replayed by a fake runner (real signed-in captures, scrubbed, plus one synthetic failure page; see its README). No network, no browser.
