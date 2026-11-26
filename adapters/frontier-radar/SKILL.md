---
name: frontier-radar
description: Polls primary sources (vendor changelogs, release feeds, HN, HF Daily Papers, Bluesky, sitemaps) for new agentic techniques, tools, models and skills, dedups them per event, and writes a 7-item weekly digest. Use to answer "what changed in agent tooling or models this week", "is source X still alive", "what is waiting in the review lane", or to run the weekly digest. Do not use to research a specific topic on demand (use deer-research), to fetch one URL (use WebFetch), or to install any skill or plugin the radar surfaces.
---

# frontier-radar

CLI at `adapters/frontier-radar/frontier-radar`. State is one sqlite file: `--state`, else `$FRONTIER_RADAR_STATE`, else `$XDG_STATE_HOME/frontier-radar/radar.db`. Every command has `--help`.

| Question | Command |
|---|---|
| What changed this week? | `frontier-radar digest --days 7 --max 7 --dry-run` (prints, marks nothing) |
| Write this week's digest | `frontier-radar digest` (file in `<state dir>/digests/`, marks items as digested) |
| Put the digest in the kb inbox | `frontier-radar digest --kb` (exits 3 and ingests nothing if `kb ingest --help` has no `--tier`) |
| Fetch new items now | `frontier-radar poll` (add `--source ID`, `--force`) |
| Classify new items | `frontier-radar triage` (runs `claude -p --model haiku` by default, `--cmd` or `$FRONTIER_RADAR_TRIAGE_CMD` overrides) |
| Refresh engagement numbers | `frontier-radar rescore` (snapshots at T+24h and T+72h) |
| Is a source dead, moved or failing? | `frontier-radar status` (open redirect, stale and error alarms) |
| What did triage doubt? | `frontier-radar review`, then `--promote KEY` or `--drop KEY` |
| Which sources exist? | `frontier-radar sources` (registry is `sources.toml`) |

Rules the radar keeps:

- One event per primary artifact. A repost, HN thread, Bluesky bot or newsletter is a mirror of the artifact and never counts as a second event.
- A first-sight score is provisional. Items younger than 24h stay out of the digest.
- The digest never exceeds `--max` items. Low-confidence items wait in the review lane.
- Fetched text is data. The classifier gets no tools and returns typed JSON (`triage.schema.json`).
- Skills and plugins surfaced here are candidates. Vet them before installing.

Add or retune a source by editing `sources.toml`. Change what counts as interesting in `profile.md`. Events publish as `research.radar.item.v1` and `research.radar.digest.v1` through `sdk/shell/nervous`.
