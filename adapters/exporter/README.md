# nervous-bus Prometheus exporter

Tails `~/.cache/nervous-bus/debug.jsonl` and exposes a `/metrics` endpoint
in Prometheus exposition format. Pair with Prometheus + Grafana for
ops-style time-series visibility on bus traffic. (Pulse is the live
RSI-introspection TUI; this exporter is the long-haul charts story.)

Bead: **nervous-bus-qy8n**. Read-only — the exporter never publishes
back to the bus, never mutates `debug.jsonl`.

## Exposed metrics

| Metric                                | Type    | Labels                          |
|---------------------------------------|---------|---------------------------------|
| `nbus_events_total`                   | Counter | `channel`, `source`, `project`  |
| `nbus_events_per_second`              | Gauge   | `channel`                       |
| `nbus_session_active`                 | Gauge   | `agent_type`, `project`         |
| `nbus_autobench_iteration_score`      | Gauge   | `session_id`                    |
| `nbus_autobench_ahe_outcome_total`    | Counter | `outcome` (hit/miss/refuted/pending) |
| `nbus_exporter_dropped_total`         | Counter | (none) — backpressure dropouts  |

`nbus_events_per_second` is computed from a 60-second rolling window,
refreshed once per second by a background ticker.

## Run

```bash
pip install prometheus_client
python -m adapters.exporter.prometheus_exporter --port 9182
```

Flags:

- `--port` (default `9182`) — chosen from the Grafana-exporter range
  (9100-9999); `9180-9189` is mostly unassigned.
- `--path` (default `~/.cache/nervous-bus/debug.jsonl`) — alternate JSONL.
- `--from-start` — replay the whole file rather than tail from EOF.
- `--once` — read existing file, print metrics to stdout, exit. Good
  for smoke tests and scripted scrapes.

## Prometheus scrape config

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: nervous-bus
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:9182']
        labels:
          host: workstation
```

## Grafana dashboard import

1. Grafana → **Dashboards** → **New** → **Import**.
2. Paste the contents of `dashboards/nervous-bus-overview.json` (or upload).
3. Pick your Prometheus datasource for the `DS_PROMETHEUS` variable.
4. Save. Default refresh is 10s, default window 1h.

Panels:

1. **Top 10 channels by ev/s** — timeseries, `topk(10, nbus_events_per_second)`.
2. **Active sessions by agent_type** — stat, `sum by (agent_type) (nbus_session_active)`.
3. **Autobench iteration score** — timeseries, `nbus_autobench_iteration_score`.
4. **AHE prediction outcomes** — donut piechart, `sum by (outcome) (nbus_autobench_ahe_outcome_total)`.

## Performance notes

Rough numbers from synthetic benchmarks (ingest_iterable, no HTTP):

| Event rate | RSS    | CPU (single core) | Notes                               |
|-----------:|-------:|------------------:|-------------------------------------|
| 100 ev/s   | ~25 MB | <1%               | Steady-state, all channels seen.    |
| 1 000 ev/s | ~30 MB | ~5%               | Rate-window deques dominate memory. |
| 10 000 ev/s| ~40 MB | ~30%              | Queue starts to back up; expect drops. |

Memory is bounded: each channel keeps at most `RATE_WINDOW_SECS * peak_rate`
timestamps. Backpressure is FIFO-drop with `nbus_exporter_dropped_total`
incrementing — Prometheus will pick up the gap on its next scrape.

## Co-existence with pulse

Pulse (`adapters/dashboard/autobench-pulse/`) and this exporter both tail
the same `debug.jsonl`. There is no contention: both open the file
read-only with independent file descriptors. Run both simultaneously
without conflict.
