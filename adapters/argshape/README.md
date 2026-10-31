# argshape

Learns how each CLI and database wants to be called, from the agent's own
failed Bash calls, and **corrects** the next call before it runs. It never
blocks a command.

```
fail:  tail -2 a.log b.log      tail: option used in invalid context -- 2
       -> argshape: `tail` (GNU coreutils 9.12) does not accept `-2` here. Use `-n 2`.
next:  tail -3 c.log d.log      rewritten to  tail -n 3 c.log d.log  before it runs
```

## Hooks (Claude Code, `Bash` matcher)

| Event | Command | What it does |
| :- | :- | :- |
| PreToolUse | `argshape hook pre` | rewrites known-bad tokens via `updatedInput` (no `permissionDecision`: permission rules still apply to the rewritten command), or adds advice |
| PostToolUse | `argshape hook post` | settles corrections (ok), learns session pairs, SQL columns, value shapes |
| PostToolUseFailure | `argshape hook fail` | records rejections, learns `--help` once per binary build, tells the agent what is valid |

`argshape install` adds the three hooks to `~/.claude/settings.json` (backup
written next to it). `argshape uninstall` removes them.

## The golden rule

A token is only rewritten if **this build of the binary** (or this database
connection) has already rejected exactly that token, and a replacement is
known with enough confidence:

| source | meaning | needed |
| :- | :- | :- |
| `tool-hint` | the tool printed the suggestion (clap tip, cobra "Did you mean", git "most similar") | 1 |
| `pg-hint` | postgres `HINT: Perhaps you meant ...` | 1 |
| `spec-unique` | learned `--help` has exactly one obvious match (Damerau 1, unique prefix, dash slip) | 1 |
| `catalog` | learned column list has exactly one close match | 1 |
| `session-pair` | an agent's next successful call swapped the token | 2 sessions, or 1 when `--help` agrees |
| `shape` | literal compared to a column whose sampled values have one shape (MAC, UUID, hex case) | 1 |

Binary identity is `(device, inode, mtime, size)`; an upgrade or rebuild
resets what was learned for it. When a binary's `--help` lists the
replacement, it must exist there and the bad token must not.

## Tiers and turning it off

```
argshape mode                       # show
argshape mode correct|advise|observe|off
argshape mode advise --bin git      # per-binary ceiling
argshape mode advise --sql          # ceiling for SQL identifier/value corrections
ARGSHAPE_MODE=off                   # env kill switch
```

- `observe` learns only. `advise` adds context, never rewrites. `correct` rewrites eligible tokens.
- **Agents**: append `# argshape:off` to run a command verbatim. If it follows a
  correction, that correction is demoted to advice permanently.
- A correction whose command then fails on the same token is demoted.
- **Circuit breaker**: if at least 4 corrections in 24h were followed by a related
  failure or an opt-out, and those are half or more, all corrections drop to
  advice for 24h (`argshape breaker reset` to close it).

## SQL

Any word that reads as a statement (`select ...`, `with ...`), passed via
`-c/--command` or a heredoc / here-string, is checked. Connections are keyed
by program + connection arguments (credentials stripped), never by a secret.

- Column errors from postgres, sqlite, duckdb, and mysql are recorded per connection.
- **Active catalog learning** (`sql_active_learning`, default on): on a column
  error, argshape re-runs **the same connector command** with its SQL replaced
  by a read-only catalog query (`information_schema.columns`, or
  `pragma_table_info` for sqlite). This only happens when the program looks
  like a SQL client (`psql`, `sqlite3`, `duckdb`, `mysql`, `*db*.sh`, `*sql*.sh`),
  the SQL was an argument (not stdin), and the segment has no `sudo`. Turn it off
  with `{"sql_active_learning": false}` in `~/.config/argshape/config.json`.
- **Value shapes**: from a successful query's printed rows (at most 20 sampled),
  each column gets a shape such as `mac colon/lower` or `uuid dashed/lower`.
  Only the shape is stored, never a value.

## Operator commands

```
argshape status            mode, breaker, table counts, recent corrections
argshape explain '<cmd>'   segments, binary identity, and what pre would do
argshape fixes [--all]     learned replacements and their applied/ok/bad record
argshape rejects | sigs | columns | shapes
argshape learn <bin> [sub] learn a binary's --help now
argshape demote|promote <bin> <bad>
```

State: `~/.local/state/argshape/argshape.db` (SQLite, counts and learned facts,
low hundreds of KB). Errors inside the hook go to `errors.log` there, and the
hook exits 0.

## Bus

`agent.cli.correction.v1` (`schemas/agent.cli.correction.v1.json`): rejection,
correction, demotion, and breaker events. These carry tokens and binary names,
never output.

## Safety of `--help`

Running `X --help` executes X. Help is requested only from programs that just
printed a usage error (so they parse arguments), or from ELF binaries and scripts
that visibly use an arg parser. Never from a deny-list (`rm`, `dd`, `mkfs`,
`kill`, `sudo`, ...). It always runs with stdin closed, pagers off, no TTY, and a
2s timeout.

## Measured (14-day replay, 101,402 shell calls)

`python3 backtest.py --days 14 --state <scratch dir> [--agent claude]` replays shell calls from
every agent `adapters/transcripts` can read (sub-agent sessions included) into a scratch state dir.
Result (2026-10-31): 101,236 Claude Code, 163 Codex and 3 opencode calls; 65 rewrites, all multi-file
`tail -N` → `tail -n N`, each of which would have failed; 3 SQL column advisories; no false positives
and no exceptions. `plan()` averages 1.5 ms. Live, the SQL catalog path does the most useful work.
Real flag misuse is rare; most recorded failures are harness ones (see `argshape status`).
