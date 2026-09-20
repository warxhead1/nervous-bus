# System resource history

`adapters/system-resource/watch.py` records host CPU, DRAM, physical-device I/O,
and pressure, plus at most eight selected cgroup-v2 scopes and at most eight NVIDIA
GPU inventory readings. GPU VRAM is a separate top-level measurement: it is never
added to host/cgroup `memory_current`, DRAM aggregates, CPU counters, or cgroup
rankings. The default configuration selects Hearth API, Tachyonac, Orca execution
scopes, and the Orca UI service on this host. Edit a copy of `default-config.json`
for another UID or service layout and pass `--config`, or set
`NERVOUS_SYSTEM_RESOURCE_CONFIG`.

The collector reads fixed kernel files, not process arguments, environments,
transcripts, or provider data. At the same cadence it makes one shell-free
`nvidia-smi` inventory query for UUID, driver version, total/used/free VRAM, and
GPU utilization; it never asks for processes, arguments, environments, model data,
or prompts. The direct child has a 1.9-second execution window plus bounded reaping
inside the two-second query budget, stdout is capped at 64 KiB, stderr is discarded,
and no more than eight device rows are retained. Missing executables, empty results,
timeouts, output overflow, malformed/duplicate UUIDs, partial fields, and cleanup
failure are typed GPU states; none prevents host/cgroup retention or publication.
A selector may glob only its final cgroup path component; selection is capped at
eight. Missing cgroups remain explicit rows.
Physical whole-device counters exclude device-mapper layers and partitions so
the same I/O is not counted twice. Unrecognised-only cgroup I/O is unavailable.
Nested cgroups can overlap; rankings are not additive resource accounting.

Each batch has boot, cgroup inode, physical-device inventory, collector-code and
configuration identities. GPU rows additionally retain the stable device UUID,
driver version, boot, and collector identity; device indices are not identities.
Rates use monotonic elapsed time, not lifetime averages.
First observations, missing data, a gap over 2.5 collection intervals, clock jumps,
counter decreases, or identity changes produce unavailable/reset intervals.
Zero, unavailable, and unlimited limits are distinct. Raw device counters retain
read/write bytes, I/O counts, busy time, and weighted I/O time for host diagnosis.

## Retention and queries

The default database remains `~/.cache/nervous-bus/system-resource/history-v2.sqlite3`.
Typed raw measurements retain up to 24 hours / 50,000 rows; five-minute rollups
retain up to 30 days / 100,000 rows. Each completed row enters its rollup once in
the same transaction as its processed marker. Restart and late timestamps do not
recompute or double-count old history. Rollups preserve boot/inode/code identity,
counter deltas and their individual valid coverage, memory peaks, and CPU/memory/I/O
pressure maxima. Invalid observations remain counted. Intervals are assigned to
their end bucket; the first intersecting bucket may begin before the requested time.
Coverage describes observations, never an assumed uninterrupted recording.

GPU query status and per-device gauges use additive SQLite tables in that same
transaction, so an upgrade keeps existing host history. GPU rollups preserve sampled
min/max/sum/count for total, used, free, and utilization separately, plus query
attempt, observed-device-sample, and missing-sample counts. Those are sampled
observations: a sampled maximum is not an instantaneous peak and counts do not
claim continuous duration. Each device/driver identity uses all query attempts
in its window as the denominator, including attempts before it was first observed;
an unobserved sample is not proof that the device physically disappeared.
UUID, driver, boot, and collector boundaries remain
separate; replacement, reboot, observed loss, reset, and long gaps are never used
to infer a GPU rate or an unseen reset. On a continuous matching boot/collector
chain, a previously observed UUID that is absent from a complete inventory, or cannot
be reread because the query is partial/unavailable, receives a local unavailable
gauge with null values; that records an observed loss or query gap rather than
inventing a VRAM value.

```sh
python3 adapters/system-resource/watch.py --report --since 1789800000 --limit 120
```

Queries combine retained rollups with unrolled raw samples, rank observed cgroups
by CPU, I/O, and memory, and expose pressure history, missing entities, freshness,
delivery states, and cap evictions. The separate `gpu` report object contains query
status history, latest query status and devices, raw samples, five-minute sampled
history, and explicit coverage; it is not part of `top_consumers`. Queries clamp to
30 days and 500 rows per bounded series; the response says when a series is
truncated. The latest `health.json` reports GPU query wall time and collector
overhead as well as collection CPU/wall time and failures. SQLite uses WAL with
`synchronous=NORMAL` to avoid a disk flush for every sample; a machine power failure
can lose the latest transactions. Local retention survives bus failure, but is not a
power-loss guarantee.

## Bus contract and diagnostics

One `bus.system.resource.sample.v3` event carries the host, selected cgroups, the
separate GPU measurement, and any findings. The local transaction commits before
publishing. `accepted_by_cli` is only a publisher acknowledgement, not independent
consumer-delivery proof. Other outcomes are `receipt_only`, `failed`, `timed_out`,
`unavailable`, and `dry_run`. Publication has a 12-second deadline, and only its own
process group is terminated on timeout. The GPU query never kills a process group:
it targets and reaps only its direct owned child, exposing `cleanup_failed` if bounded
reaping cannot be confirmed. Generic nervous-bus Redis/mirror and Hearth nbus
ingestion can retain the event without a separate provider or scraper.

V1 and V2 remain byte-for-byte frozen in `schemas/` for compatibility. The installed
producer now emits v3; `watch.py` and this document were the exact in-repository v2
consumer references, while generic nervous-bus ingestion is version-agnostic.
Consumers that selected v2 must migrate explicitly to v3 rather than treating
"latest" as interchangeable. No v1/v2 file is removed, and the existing v2 database
filename is retained because the new tables are additive.

Findings require three distinct consecutive valid observations spanning at least
two configured intervals, with no restart or long gap. CPU/memory/I/O `some.avg10`
thresholds are 50/10/20 percent. Each entity/resource/boot/inode has a one-hour
default cooldown. Host pressure never becomes an attributed cgroup finding.
The evidence is stored in `latest-diagnostic.json` and included in the bus event.
These are observations of sustained pressure, not proven causes. Automation does
not restart workloads, change their limits, kill their processes, or notify people.

## Installation and validation

The user service invokes the canonical `~/projects/nervous-bus` checkout. Create
the cache directory, copy `systemd/system-resource.{service,timer}` into
`~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then
`systemctl --user enable --now system-resource.timer`. The one-minute timer uses
a non-overlap lock, 10% of one CPU, 128 MiB memory, low I/O weight, a 30-second
deadline, and a read-only home except the nervous-bus cache needed for receipts.
Stop it with `systemctl --user disable --now system-resource.timer`.
The publisher's interpreter must have `jsonschema`; if system Python does not,
set `Environment=NERVOUS_PYTHON=/absolute/path/to/python3` in a local service
override. Publication fails visibly when validation is unavailable.

Tests need `jsonschema`; the runtime collector uses only Python's standard library
plus the existing nervous publisher:

```sh
python3 -m unittest discover -s adapters/system-resource -p 'test_*.py'
```

Runtime acceptance additionally requires a real kernel sample, valid interval,
two bounded GPU records with measured query/collector overhead where NVIDIA hardware
is present, service receipt, and independent Redis/Hearth ingestion evidence. Unit
tests inject GPU reads and do not require hardware; prior GPU history cannot be
reconstructed from host DRAM history.
