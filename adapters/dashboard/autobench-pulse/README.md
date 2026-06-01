# autobench-pulse

Live Textual dashboard for autobench observability events.

Subscribes to four channels published by `autobench/observability.py`:

- `autobench.phase.v1`     — phase boundary (benchmark / improver / ...)
- `autobench.iteration.v1` — RSI iteration start/complete, aggregate score
- `autobench.sandbox.v1`   — per-case sandbox verdict & latency
- `autobench.improver.v1`  — improver model call boundaries

## Two flavours

| Flavour | Entry point | Use when |
|---|---|---|
| **v2 (default)** — Textual app | `python -m pulse_app` | You want the live 2-column dashboard with sparklines, scatter, histogram, gauge. |
| **legacy v1** — ANSI tree | `python pulse.py --legacy` | Smoke tests, headless dumps, or terminals that can't run Textual. |

## Install

```bash
pip install -e adapters/dashboard/autobench-pulse/
# or:
pip install -r adapters/dashboard/autobench-pulse/requirements.txt
```

Required deps: `textual>=1.0`, `textual-plotext>=1.0`. Tests need `pytest`.

## Run

```bash
# Live tail of the bus (preferred — needs `deer` CLI available)
python -m pulse_app --prefer-bus

# Offline replay from the debug file
python -m pulse_app --debug-file ~/.cache/nervous-bus/debug.jsonl

# Smoke test — one-shot synchronous dump, no TUI
python -m pulse_app --debug-file ~/.cache/nervous-bus/debug.jsonl --once
```

## Keybindings

| Key | Action |
|---|---|
| `q`     | Quit |
| `p`     | Toggle pause (events still buffered) |
| `/`     | Focus filter input (session-id substring) |
| `g`/`G` | Jump tree cursor to top / bottom |
| `j`/`k` | Move cursor down / up |
| `space` | Toggle tree node |
| `enter` | Select tree node |
| `?`     | Toggle modal help |

## Architecture (1-paragraph)

Bus worker thread → `PulseState` (single writer, single source of truth) →
10 Hz render tick reads a dirty flag → fans new state to widgets → Textual
diffs and writes minimal ANSI. Charts coalesce at ≤2 Hz. The bus listener
never touches widgets directly — per `autobench/research/terminal_rendering_2026.md`
§9 / probe p3, this is the k9s/btop "refresh-rate-from-data-rate decoupling"
pattern.

See `autobench/research/terminal_rendering_2026.md` for the full design.

## Tests

```bash
pytest adapters/dashboard/autobench-pulse/tests/ -q
```

Tests cover:

- `test_state.py`         — `PulseState` ingestion + verdict logic
- `test_source.py`        — `FileSource` round-trip
- `test_widgets_pilot.py` — Textual `Pilot` smoke tests for each widget
- `test_app_pilot.py`     — full `PulseApp` mount + pause toggle + help modal

## Legacy shim

`pulse.py` is now a thin wrapper:

- Default: prints a deprecation notice, then `exec`s `python -m pulse_app`
- `pulse.py --legacy` keeps the original ANSI renderer for one release
- `pulse.py --once` (without `--legacy`) routes to the new app's `--once` path,
  preserving orchestrator smoke-test compatibility
