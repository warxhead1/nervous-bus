# System resource history

`adapters/system-resource/watch.py` records host CPU, memory, physical-device I/O,
and pressure, plus at most eight selected cgroup-v2 scopes. The default configuration
selects Hearth API, Tachyonac, Orca execution scopes, and the Orca UI service on this
host. Edit a copy of `default-config.json` for another UID or service layout and
pass `--config`, or set `NERVOUS_SYSTEM_RESOURCE_CONFIG`.

The collector reads fixed kernel files, not process arguments, environments,
transcripts, or provider data. A selector may glob only its final cgroup path
component; selection is capped at eight. Missing cgroups remain explicit rows.
Physical whole-device counters exclude device-mapper layers and partitions so
the same I/O is not counted twice. Unrecognised-only cgroup I/O is unavailable.
Nested cgroups can overlap; rankings are not additive resource accounting.

Each batch has boot, cgroup inode, physical-device inventory, collector-code and
configuration identities. Rates use monotonic elapsed time, not lifetime averages.
First observations, missing data, a gap over 2.5 collection intervals, clock jumps,
counter decreases, or identity changes produce unavailable/reset intervals.
Zero, unavailable, and unlimited limits are distinct. Raw device counters retain
read/write bytes, I/O counts, busy time, and weighted I/O time for host diagnosis.

## Retention and queries

The default database is `~/.cache/nervous-bus/system-resource/history-v2.sqlite3`.
Typed raw measurements retain up to 24 hours / 50,000 rows; five-minute rollups
retain up to 30 days / 100,000 rows. Each completed row enters its rollup once in
the same transaction as its processed marker. Restart and late timestamps do not
recompute or double-count old history. Rollups preserve boot/inode/code identity,
counter deltas and their individual valid coverage, memory peaks, and CPU/memory/I/O
pressure maxima. Invalid observations remain counted. Intervals are assigned to
their end bucket; the first intersecting bucket may begin before the requested time.
Coverage describes observations, never an assumed uninterrupted recording.

```sh
python3 adapters/system-resource/watch.py --report --since 1789800000 --limit 120
```

Queries combine retained rollups with unrolled raw samples, rank observed cgroups
by CPU, I/O, and memory, and expose pressure history, missing entities, freshness,
delivery states, and cap evictions. Queries clamp to 30 days and 500 pressure rows;
the response says when that series is truncated. The latest `health.json` also
reports collection CPU/wall time and failures. SQLite uses WAL with `synchronous=NORMAL`
to avoid a disk flush for every sample; a machine power failure can lose the latest
transactions. Local retention survives bus failure, but is not a power-loss guarantee.

## Bus contract and diagnostics

One `bus.system.resource.sample.v2` event carries the host, selected cgroups and
any findings. The local transaction commits before publishing. `accepted_by_cli`
is only a publisher acknowledgement, not independent consumer-delivery proof.
Other outcomes are `receipt_only`, `failed`, `timed_out`, `unavailable`, and
`dry_run`. Publication has a 12-second deadline, and only its own process group
is terminated on timeout. Generic nervous-bus Redis/mirror and Hearth nbus
ingestion can retain the event without a separate provider or scraper.

V1 remains in `schemas/` for compatibility; it was not deployed by this work.
V2 is the first installed producer and corrects the original interval and typed
measurement contract. Consumers should select v2 explicitly; no v1 file is removed.

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
bounded query, service receipt, and independent Redis/Hearth ingestion evidence.
