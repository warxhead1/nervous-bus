#!/usr/bin/env python3
"""ecosystem_daily_report — bounded, read-only daily evidence over bus telemetry.

Reads three independent sources for one UTC window and emits ONE JSON
aggregate. Nothing is written back: no schema is touched, no database row is
modified, no service is contacted.

  1. The debug journal (``~/.cache/nervous-bus/debug.jsonl``) plus its rotated
     gzip generations. Every producer writes here first, so this is the
     widest surface available.
  2. The reflex-recorder durable store (``~/.cache/nervous-bus/reflex/runs.db``),
     opened read-only. This supplies the *segment* denominator and the
     recorder's own evaluation ledger (``run_evals``).
  3. Optionally, a memory-evaluation attempt ledger (``--ledger``), summarised
     through the EXISTING ``tools/memory_evaluation.py`` contract. No new
     ledger format is invented here.

Design rules this tool holds itself to
--------------------------------------

*Denominators, never bare rates.* Journal records, recorder segments and Orca
tasks are three different populations. Every coverage number names which one
it divides by, in a ``denominator_basis`` string.

*Unknown is not zero.* A missing token count, cost or identity is counted as
unknown in its own bucket. Zero is only ever reported when a producer actually
emitted zero.

*Deduplicate exactly, and never silently.* Records are collapsed on the
CloudEvents ``id`` only when the whole envelope is byte-identical after
canonicalisation. The same ``id`` carrying a *different* payload is an
identity collision: it is counted as a distinct record, and it is flagged.
This is not hypothetical — the codex session hook derives its envelope id from
a content hash that excludes the timestamp, so two genuinely different
``agent.session.v1`` compact events can share one id. Collapsing on id alone
would delete real events.

*Bounded resources.* Files are streamed line by line, group-by cardinality is
capped, the seen-id table is capped, and collision samples are capped. Every
cap that actually bites is reported as a ``*_truncated`` flag so a truncated
run can never be mistaken for a complete one.

*No fabricated acceptance.* The report carries
``evidence_level="observed_telemetry"`` and ``verified_acceptance="NOT_EVALUATED"``.
Observing that a worker reported success is not verifying that it succeeded.

*No invented correlation.* ``traceparent`` is reported exactly as observed. If
no producer stamps it, the report says so and stops; it never mints a
per-event trace id, because a random id shared by nothing correlates nothing.
The one derived linkage offered — joining recorder runs to
``orca.worker.lifecycle.v1`` identity by worktree path — is labelled
``authority: "observational_join_only"`` and reports its own ambiguity.

Usage::

    python3 tools/ecosystem_daily_report.py --hours 24
    python3 tools/ecosystem_daily_report.py --since 2026-09-05T00:00:00Z \\
                                            --until 2026-09-06T00:00:00Z
    python3 tools/ecosystem_daily_report.py --hours 24 --output report.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

REPORT_VERSION = "ecosystem-daily-report/v1"
EVIDENCE_LEVEL = "observed_telemetry"
VERIFIED_ACCEPTANCE = "NOT_EVALUATED"

DEFAULT_JOURNAL = Path("~/.cache/nervous-bus/debug.jsonl").expanduser()
DEFAULT_DB = Path("~/.cache/nervous-bus/reflex/runs.db").expanduser()

DEFAULT_MAX_CARDINALITY = 500
DEFAULT_MAX_TRACKED_IDS = 500_000
DEFAULT_MAX_SAMPLES = 20

OTHER_KEY = "<other>"
UNKNOWN_KEY = "<unknown>"

ACTIVITY_TYPE = "bus.agent.activity.v1"
WORKER_LIFECYCLE_TYPE = "orca.worker.lifecycle.v1"
RUN_CLOSED_TYPE = "bus.agent.run.closed.v1"

# Identity fields we account for on every activity-bearing envelope. The set is
# closed on purpose: a producer cannot make coverage look better by inventing a
# new field name, and a field that stops being emitted shows up as missing
# rather than disappearing from the report.
IDENTITY_FIELDS: tuple[str, ...] = (
    "session_id",
    "root_session_id",
    "conversation_id",
    "agent_id",
    "project",
    "worktree",
    "bead_id",
    "task_id",
    "dispatch_id",
    "run_id",
    "traceparent",
)

# Usage metrics whose absence means "not collected", never "zero used".
USAGE_METRICS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)

# An envelope only counts as fixture/test evidence when it says so. Absence of
# a marker is NOT proof of production traffic; see `limitations`.
FIXTURE_TYPE_PREFIXES = ("test.", "fixture.")

_ROTATION_RE = re.compile(r"\.(\d+)\.gz$")

# RFC3339 / ISO-8601 with an explicit offset. A timestamp without one is
# ambiguous on a machine whose local zone is not UTC, so it is refused rather
# than guessed.
_TZ_SUFFIX_RE = re.compile(r"(?:Z|z|[+-]\d{2}:?\d{2})$")


class ReportError(RuntimeError):
    """A fatal, user-facing problem with the inputs."""


# ── time ─────────────────────────────────────────────────────────────────────


def parse_utc(value: str, *, field: str = "timestamp") -> datetime:
    """Parse an RFC3339 timestamp that carries an explicit UTC offset.

    Returns a timezone-aware datetime normalised to UTC. Raises
    :class:`ValueError` for anything naive, malformed, or non-string.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if not _TZ_SUFFIX_RE.search(text):
        raise ValueError(f"{field} {value!r} has no UTC offset")
    normalised = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:  # pragma: no cover - guarded by the regex above
        raise ValueError(f"{field} {value!r} is naive")
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_window(
    *,
    hours: Optional[float],
    since: Optional[str],
    until: Optional[str],
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Resolve the half-open UTC window ``[start, end)``.

    ``--hours`` and ``--since`` are mutually exclusive. ``--until`` alone
    anchors the end of an ``--hours`` window so a daily timer can reproduce
    yesterday exactly.
    """
    now = now or datetime.now(timezone.utc)
    if since is not None and hours is not None:
        raise ReportError("--since and --hours are mutually exclusive")

    end = parse_utc(until, field="--until") if until is not None else now

    if since is not None:
        start = parse_utc(since, field="--since")
    else:
        span = 24.0 if hours is None else hours
        if span <= 0:
            raise ReportError("--hours must be positive")
        start = end - timedelta(hours=span)

    if start >= end:
        raise ReportError(f"empty window: start {_iso(start)} >= end {_iso(end)}")
    return start, end


# ── bounded counters ─────────────────────────────────────────────────────────


class BoundedCounter:
    """A Counter with a hard cap on distinct keys.

    Overflow is folded into ``<other>`` and flagged, so a producer that emits
    an unbounded number of distinct project names can inflate a count but can
    never blow the reporter's memory or hide the fact that it happened.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._counts: Counter[str] = Counter()
        self.truncated = False
        self.distinct_dropped = 0

    def add(self, key: Optional[str], amount: int = 1) -> None:
        label = key if isinstance(key, str) and key else UNKNOWN_KEY
        if label not in self._counts and len(self._counts) >= self.limit:
            self.truncated = True
            self.distinct_dropped += 1
            self._counts[OTHER_KEY] += amount
            return
        self._counts[label] += amount

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "counts": dict(sorted(self._counts.items())),
            "distinct": len(self._counts),
            "truncated": self.truncated,
        }
        if self.truncated:
            payload["distinct_keys_folded_into_other"] = self.distinct_dropped
            payload["limit"] = self.limit
        return payload


class UsageAccumulator:
    """Sums a usage metric while keeping unknown strictly separate from zero."""

    def __init__(self) -> None:
        self.known_sum: Decimal = Decimal(0)
        self.known_count = 0
        self.zero_count = 0
        self.unknown_count = 0
        self.invalid_count = 0
        self.integral = True

    def observe(self, present: bool, value: Any) -> None:
        if not present or value is None:
            self.unknown_count += 1
            return
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            self.invalid_count += 1
            return
        try:
            amount = Decimal(str(value))
        except Exception:
            self.invalid_count += 1
            return
        if not amount.is_finite():
            self.invalid_count += 1
            return
        if not isinstance(value, int):
            self.integral = False
        self.known_sum += amount
        self.known_count += 1
        if amount == 0:
            self.zero_count += 1

    def as_dict(self) -> dict[str, Any]:
        total: Any
        if self.integral:
            total = int(self.known_sum)
        else:
            total = str(self.known_sum)
        return {
            "known_sum": total,
            "known_count": self.known_count,
            "zero_count": self.zero_count,
            "unknown_count": self.unknown_count,
            "invalid_count": self.invalid_count,
        }


def coverage(present: int, denominator: int, basis: str) -> dict[str, Any]:
    """A coverage figure that always carries its own denominator."""
    return {
        "present": present,
        "missing": max(denominator - present, 0),
        "denominator": denominator,
        "denominator_basis": basis,
    }


# ── journal discovery ────────────────────────────────────────────────────────


def discover_journal_files(live: Path) -> list[Path]:
    """Return the journal generations oldest-first.

    Rotation names are ``debug.jsonl.<n>.gz`` with a HIGHER n meaning OLDER, so
    reading in descending-n order then the live file gives chronological order.
    A missing live file is fine — the rotations may still hold the window.
    """
    live = Path(live)
    rotations: list[tuple[int, Path]] = []
    parent = live.parent
    if parent.is_dir():
        for candidate in parent.iterdir():
            if not candidate.name.startswith(live.name + "."):
                continue
            match = _ROTATION_RE.search(candidate.name)
            if match:
                rotations.append((int(match.group(1)), candidate))
    ordered = [path for _, path in sorted(rotations, key=lambda item: -item[0])]
    if live.exists():
        ordered.append(live)
    return ordered


def _open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def iter_journal_lines(paths: Iterable[Path]) -> Iterator[tuple[Path, str]]:
    for path in paths:
        try:
            handle = _open_text(path)
        except OSError as exc:
            raise ReportError(f"cannot read journal {path}: {exc}") from exc
        with handle:
            for line in handle:
                yield path, line


# ── envelope canonicalisation ────────────────────────────────────────────────


def canonical_digest(envelope: Mapping[str, Any]) -> str:
    """A stable digest of the whole envelope, used only for exact-dup detection.

    ``sort_keys`` makes key order irrelevant, so a re-serialised mirror of the
    same event still collapses; anything else — one differing byte of payload,
    a different ``time`` — produces a different digest and is treated as a
    distinct record.
    """
    blob = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _lookup(envelope: Mapping[str, Any], data: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    """Find ``field`` on the envelope extension first, then inside ``data``.

    CloudEvents extensions (``traceparent``) sit at the envelope root; producer
    payload sits under ``data``. Returning ``(present, value)`` keeps an
    explicit ``null`` distinguishable from an absent key.
    """
    if field in envelope:
        return True, envelope[field]
    if field in data:
        return True, data[field]
    return False, None


def _is_fixture(envelope: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
    event_type = envelope.get("type")
    if isinstance(event_type, str) and event_type.startswith(FIXTURE_TYPE_PREFIXES):
        return True
    for key in ("fixture", "is_fixture", "synthetic"):
        if data.get(key) is True:
            return True
    return False


# ── journal aggregation ──────────────────────────────────────────────────────


def aggregate_journal(
    paths: list[Path],
    start: datetime,
    end: datetime,
    *,
    max_cardinality: int = DEFAULT_MAX_CARDINALITY,
    max_tracked_ids: int = DEFAULT_MAX_TRACKED_IDS,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> dict[str, Any]:
    per_file: list[dict[str, Any]] = []
    file_stats: dict[Path, dict[str, int]] = {}

    lines_total = 0
    json_errors = 0
    non_object = 0
    time_missing = 0
    time_malformed = 0
    malformed_samples: list[dict[str, Any]] = []
    out_of_window = 0

    records_in_window = 0            # raw in-window lines, before dedup
    unique_records = 0               # records counted once (dup-collapsed)
    exact_duplicates = 0             # byte-identical redeliveries collapsed
    collisions = 0                   # same id, DIFFERENT payload — kept, flagged
    records_without_id = 0
    collision_samples: list[dict[str, Any]] = []

    # id -> (canonical digest, type, time). Only a compact fingerprint is
    # retained, never the payload: holding envelopes to diff them later is
    # exactly the unbounded growth this tool promises not to have.
    seen: dict[str, tuple[str, Any, Any]] = {}
    id_tracking_truncated = False

    by_type = BoundedCounter(max_cardinality)
    by_source = BoundedCounter(max_cardinality)
    by_project = BoundedCounter(max_cardinality)
    by_worktree = BoundedCounter(max_cardinality)
    by_agent_kind = BoundedCounter(max_cardinality)
    by_model = BoundedCounter(max_cardinality)

    source_first: dict[str, datetime] = {}
    source_last: dict[str, datetime] = {}
    source_seen_any_time: dict[str, datetime] = {}   # includes out-of-window
    source_truncated = False

    fixture_counts = Counter()

    identity_present = {field: 0 for field in IDENTITY_FIELDS}
    identity_denominator = 0

    usage = {metric: UsageAccumulator() for metric in USAGE_METRICS}
    usage_denominator = 0

    trace_ids: set[str] = set()
    trace_types: dict[str, set[str]] = defaultdict(set)
    traceparent_count = 0
    trace_malformed = 0

    worker_task_ids: set[str] = set()
    worker_dispatch_ids: set[str] = set()
    worker_run_ids: set[str] = set()
    worker_bead_ids: set[str] = set()
    worker_events = 0
    worker_outcomes = BoundedCounter(max_cardinality)
    worktree_to_identity: dict[str, set[tuple[Optional[str], ...]]] = defaultdict(set)
    worktree_slug_projects: dict[str, set[str]] = defaultdict(set)

    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None

    current_path: Optional[Path] = None

    def _stats(path: Path) -> dict[str, int]:
        if path not in file_stats:
            file_stats[path] = {
                "lines": 0,
                "in_window": 0,
                "json_errors": 0,
                "time_malformed": 0,
            }
        return file_stats[path]

    for path, raw in iter_journal_lines(paths):
        current_path = path
        stats = _stats(path)
        stats["lines"] += 1
        lines_total += 1

        line = raw.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            json_errors += 1
            stats["json_errors"] += 1
            continue
        if not isinstance(envelope, dict):
            non_object += 1
            continue

        raw_time = envelope.get("time")
        if raw_time is None:
            time_missing += 1
            continue
        try:
            moment = parse_utc(raw_time, field="time")
        except ValueError as exc:
            time_malformed += 1
            stats["time_malformed"] += 1
            if len(malformed_samples) < max_samples:
                malformed_samples.append(
                    {
                        "file": str(path),
                        "type": envelope.get("type"),
                        "time": raw_time if isinstance(raw_time, str) else repr(raw_time),
                        "reason": str(exc),
                    }
                )
            continue

        source = envelope.get("source")
        source_label = source if isinstance(source, str) and source else UNKNOWN_KEY
        if len(source_seen_any_time) < max_cardinality or source_label in source_seen_any_time:
            known = source_seen_any_time.get(source_label)
            if known is None or moment > known:
                source_seen_any_time[source_label] = moment
        else:
            source_truncated = True

        if not (start <= moment < end):
            out_of_window += 1
            continue

        records_in_window += 1
        stats["in_window"] += 1

        # ── exact dedup + collision detection ────────────────────────────
        envelope_id = envelope.get("id")
        counts_as_new = True
        if not isinstance(envelope_id, str) or not envelope_id:
            records_without_id += 1
        else:
            digest = canonical_digest(envelope)
            previous = seen.get(envelope_id)
            if previous is None:
                if len(seen) >= max_tracked_ids:
                    id_tracking_truncated = True
                else:
                    seen[envelope_id] = (digest, envelope.get("type"), envelope.get("time"))
            elif previous[0] == digest:
                exact_duplicates += 1
                counts_as_new = False
            else:
                # Same id, different bytes. This is a REAL distinct event that
                # id-only dedup would have deleted. Keep it; flag it.
                collisions += 1
                if len(collision_samples) < max_samples:
                    collision_samples.append(
                        {
                            "id": envelope_id,
                            "file": str(path),
                            "first_type": previous[1],
                            "first_time": previous[2],
                            "second_type": envelope.get("type"),
                            "second_time": envelope.get("time"),
                        }
                    )

        if not counts_as_new:
            continue
        unique_records += 1

        if earliest is None or moment < earliest:
            earliest = moment
        if latest is None or moment > latest:
            latest = moment

        event_type = envelope.get("type")
        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {}

        by_type.add(event_type if isinstance(event_type, str) else None)
        by_source.add(source_label)

        first = source_first.get(source_label)
        if first is None or moment < first:
            source_first[source_label] = moment
        last = source_last.get(source_label)
        if last is None or moment > last:
            source_last[source_label] = moment

        fixture_counts["explicit_fixture" if _is_fixture(envelope, data) else "production_or_unclassified"] += 1

        # `project` is the logical project; `worktree` is a filesystem
        # location. They are deliberately NOT merged: one project has many
        # worktrees, and a worktree slug can repeat across projects.
        project = data.get("project")
        by_project.add(project if isinstance(project, str) else None)
        worktree = data.get("worktree")
        if isinstance(worktree, str) and worktree:
            by_worktree.add(worktree)
            if isinstance(project, str) and project:
                bucket = worktree_slug_projects[os.path.basename(worktree.rstrip("/"))]
                if len(bucket) < 64:
                    bucket.add(project)

        # traceparent: reported exactly as observed, never synthesised.
        has_trace, trace_value = _lookup(envelope, data, "traceparent")
        if has_trace and isinstance(trace_value, str) and trace_value:
            traceparent_count += 1
            pieces = trace_value.split("-")
            if len(pieces) == 4 and len(pieces[1]) == 32:
                trace_ids.add(pieces[1])
                if isinstance(event_type, str):
                    types = trace_types[pieces[1]]
                    if len(types) < 32:
                        types.add(event_type)
            else:
                trace_malformed += 1

        if event_type == ACTIVITY_TYPE:
            identity_denominator += 1
            for field in IDENTITY_FIELDS:
                present, value = _lookup(envelope, data, field)
                if present and value is not None and value != "":
                    identity_present[field] += 1

            usage_denominator += 1
            for metric in USAGE_METRICS:
                present, value = _lookup(envelope, data, metric)
                usage[metric].observe(present, value)

            model = data.get("model")
            by_model.add(model if isinstance(model, str) else None)
            agent_kind = data.get("agent_kind")
            by_agent_kind.add(agent_kind if isinstance(agent_kind, str) else None)

        elif event_type == WORKER_LIFECYCLE_TYPE:
            worker_events += 1
            task_id = data.get("task_id")
            dispatch_id = data.get("dispatch_id")
            run_id = data.get("run_id")
            bead_id = data.get("bead_id")
            worktree_path = data.get("worktree")
            if isinstance(task_id, str) and task_id:
                worker_task_ids.add(task_id)
            if isinstance(dispatch_id, str) and dispatch_id:
                worker_dispatch_ids.add(dispatch_id)
            if isinstance(run_id, str) and run_id:
                worker_run_ids.add(run_id)
            if isinstance(bead_id, str) and bead_id:
                worker_bead_ids.add(bead_id)
            outcome = data.get("outcome")
            worker_outcomes.add(outcome if isinstance(outcome, str) and outcome else None)
            if isinstance(worktree_path, str) and worktree_path:
                bucket = worktree_to_identity[worktree_path.rstrip("/")]
                if len(bucket) < 64:
                    bucket.add(
                        (
                            run_id if isinstance(run_id, str) else None,
                            task_id if isinstance(task_id, str) else None,
                            dispatch_id if isinstance(dispatch_id, str) else None,
                            bead_id if isinstance(bead_id, str) else None,
                        )
                    )

    for path in paths:
        stats = _stats(path)
        per_file.append(
            {
                "path": str(path),
                "compressed": path.name.endswith(".gz"),
                "lines_read": stats["lines"],
                "records_in_window": stats["in_window"],
                "json_errors": stats["json_errors"],
                "time_malformed": stats["time_malformed"],
            }
        )

    source_counts = by_source.as_dict()["counts"]
    stale_sources = []
    for label, last_any in sorted(source_seen_any_time.items()):
        in_window_last = source_last.get(label)
        entry = {
            "source": label,
            "records_in_window": 0,
            "last_seen_utc": _iso(last_any),
            "staleness_seconds": int((end - last_any).total_seconds()),
            "emitted_in_window": in_window_last is not None,
        }
        if in_window_last is not None:
            entry["records_in_window"] = source_counts.get(label, 0)
            entry["first_seen_in_window_utc"] = _iso(source_first[label])
            entry["last_seen_in_window_utc"] = _iso(in_window_last)
        stale_sources.append(entry)

    cross_type_traces = sum(1 for types in trace_types.values() if len(types) > 1)

    ambiguous_slugs = {
        slug: sorted(projects)
        for slug, projects in worktree_slug_projects.items()
        if len(projects) > 1
    }

    return {
        "files": per_file,
        "files_read": len(paths),
        "lines_read": lines_total,
        "parse": {
            "json_errors": json_errors,
            "non_object_records": non_object,
            "time_field_missing": time_missing,
            "time_malformed": time_malformed,
            "time_malformed_samples": malformed_samples,
            "records_outside_window": out_of_window,
        },
        "records": {
            "raw_in_window": records_in_window,
            "unique": unique_records,
            "exact_duplicates_collapsed": exact_duplicates,
            "id_collisions_kept": collisions,
            "records_without_id": records_without_id,
            "id_tracking_truncated": id_tracking_truncated,
            "distinct_ids_tracked": len(seen),
            "earliest_utc": _iso(earliest) if earliest else None,
            "latest_utc": _iso(latest) if latest else None,
        },
        "id_collision_samples": collision_samples,
        "id_collision_note": (
            "Same envelope id, different bytes. Both records are counted; "
            "neither is dropped. Only a type/time fingerprint of the first "
            "sighting is retained, so a sample names what collided rather "
            "than diffing the payloads."
        ),
        "by_type": by_type.as_dict(),
        "by_source": by_source.as_dict(),
        "by_project": by_project.as_dict(),
        "by_worktree_slug": by_worktree.as_dict(),
        "by_agent_kind": by_agent_kind.as_dict(),
        "by_model": by_model.as_dict(),
        "project_worktree_distinction": {
            "note": (
                "project is the logical repository/product; worktree is a "
                "filesystem checkout. They are counted separately and never "
                "summed together."
            ),
            "worktree_slugs_seen_under_multiple_projects": ambiguous_slugs,
        },
        "source_freshness": {
            "sources": stale_sources,
            "truncated": source_truncated,
            "note": (
                "Freshness is measured across every generation read, so a "
                "source with zero in-window records still reports when it was "
                "last seen. Absence from this list means absence from the "
                "files read, not absence from the bus."
            ),
        },
        "classification": {
            "counts": dict(fixture_counts),
            "note": (
                "explicit_fixture requires a self-declared marker. "
                "production_or_unclassified is NOT proof of production traffic."
            ),
        },
        "identity_coverage": {
            field: coverage(
                identity_present[field],
                identity_denominator,
                f"{ACTIVITY_TYPE} records in window",
            )
            for field in IDENTITY_FIELDS
        },
        "usage": {
            "denominator": usage_denominator,
            "denominator_basis": f"{ACTIVITY_TYPE} records in window",
            "metrics": {metric: acc.as_dict() for metric, acc in usage.items()},
            "note": "unknown_count is absent/null collection; zero_count is a measured zero.",
        },
        "trace": {
            "envelopes_with_traceparent": traceparent_count,
            "malformed_traceparent": trace_malformed,
            "distinct_trace_ids": len(trace_ids),
            "trace_ids_spanning_multiple_types": cross_type_traces,
            "producer_status": "observed" if traceparent_count else "no_producer_observed",
            "note": (
                "The shell SDK stamps traceparent only when NERVOUS_TRACEPARENT "
                "is set in the producing process. This tool reports what it "
                "observes and never synthesises a trace id: a per-event random "
                "id would correlate nothing while looking like it did."
            ),
        },
        "orca_worker_lifecycle": {
            "records": worker_events,
            "distinct_run_ids": len(worker_run_ids),
            "distinct_task_ids": len(worker_task_ids),
            "distinct_dispatch_ids": len(worker_dispatch_ids),
            "distinct_bead_ids": len(worker_bead_ids),
            "outcomes": worker_outcomes.as_dict(),
        },
        "_worktree_identity_index": {
            path: sorted(values) for path, values in worktree_to_identity.items()
        },
    }


# ── recorder DB aggregation ──────────────────────────────────────────────────


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # Belt and braces: mode=ro already refuses writes, query_only makes a
    # would-be write fail loudly inside this connection rather than at the VFS.
    conn.execute("PRAGMA query_only = 1")
    conn.row_factory = sqlite3.Row
    return conn


def _group_counts(
    conn: sqlite3.Connection, sql: str, params: tuple, limit: int
) -> dict[str, Any]:
    rows = conn.execute(sql + f" LIMIT {limit + 1}", params).fetchall()
    truncated = len(rows) > limit
    counts = {
        (row[0] if row[0] is not None else UNKNOWN_KEY): row[1] for row in rows[:limit]
    }
    return {"counts": counts, "distinct": len(counts), "truncated": truncated}


def aggregate_recorder_db(
    db_path: Path,
    start: datetime,
    end: datetime,
    *,
    max_cardinality: int = DEFAULT_MAX_CARDINALITY,
) -> dict[str, Any]:
    db_path = Path(db_path)
    if not db_path.exists():
        return {
            "status": "absent",
            "path": str(db_path),
            "note": "No recorder database on this host; segment counts are unavailable, not zero.",
        }

    start_s, end_s = _iso(start), _iso(end)
    try:
        conn = _connect_readonly(db_path)
    except sqlite3.Error as exc:
        return {"status": "unreadable", "path": str(db_path), "error": str(exc)}

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        report: dict[str, Any] = {"status": "read", "path": str(db_path)}

        if "runs" in tables:
            # Overlap, not containment: a run that starts before the window and
            # is still open inside it is part of the window's work.
            where = "started < ? AND ended >= ?"
            params = (end_s, start_s)
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n,
                       SUM(bead_id IS NOT NULL AND bead_id != '') AS bead,
                       SUM(outcome IS NOT NULL AND outcome != '') AS outcome,
                       SUM(worktree IS NOT NULL AND worktree != '') AS worktree,
                       SUM(worktree_slug IS NOT NULL AND worktree_slug != '') AS slug,
                       SUM(git_branch IS NOT NULL AND git_branch != '') AS branch,
                       SUM(session_id IS NOT NULL AND session_id != '') AS session,
                       SUM(host_conversation_id IS NOT NULL AND host_conversation_id != '') AS conv,
                       SUM(event_count) AS events
                FROM runs WHERE {where}
                """,
                params,
            ).fetchone()
            segments = row["n"] or 0
            basis = "recorder runs overlapping window"
            report["segments"] = {
                "count": segments,
                "window_basis": "overlap (started < end AND ended >= start)",
                "folded_event_count_sum": row["events"] or 0,
                "identity_coverage": {
                    "bead_id": coverage(row["bead"] or 0, segments, basis),
                    "outcome": coverage(row["outcome"] or 0, segments, basis),
                    "worktree_absolute_path": coverage(row["worktree"] or 0, segments, basis),
                    "worktree_slug": coverage(row["slug"] or 0, segments, basis),
                    "git_branch": coverage(row["branch"] or 0, segments, basis),
                    "session_id": coverage(row["session"] or 0, segments, basis),
                    "host_conversation_id": coverage(row["conv"] or 0, segments, basis),
                },
                "by_project": _group_counts(
                    conn,
                    f"SELECT project, COUNT(*) FROM runs WHERE {where} GROUP BY project ORDER BY COUNT(*) DESC",
                    params,
                    max_cardinality,
                ),
                "by_agent_kind": _group_counts(
                    conn,
                    f"SELECT agent_kind, COUNT(*) FROM runs WHERE {where} GROUP BY agent_kind ORDER BY COUNT(*) DESC",
                    params,
                    max_cardinality,
                ),
                "by_run_key_kind": _group_counts(
                    conn,
                    f"SELECT run_key_kind, COUNT(*) FROM runs WHERE {where} GROUP BY run_key_kind ORDER BY COUNT(*) DESC",
                    params,
                    max_cardinality,
                ),
                "by_close_reason": _group_counts(
                    conn,
                    f"SELECT close_reason, COUNT(*) FROM runs WHERE {where} GROUP BY close_reason ORDER BY COUNT(*) DESC",
                    params,
                    max_cardinality,
                ),
            }

        if "run_events" in tables:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT run_id) FROM run_events WHERE event_ts >= ? AND event_ts < ?",
                (start_s, end_s),
            ).fetchone()
            report["run_events"] = {
                "count": row[0] or 0,
                "distinct_run_ids": row[1] or 0,
                "by_event_type": _group_counts(
                    conn,
                    "SELECT event_type, COUNT(*) FROM run_events WHERE event_ts >= ? AND event_ts < ? GROUP BY event_type ORDER BY COUNT(*) DESC",
                    (start_s, end_s),
                    max_cardinality,
                ),
            }

        if "run_evals" in tables:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT issue_signature) FROM run_evals WHERE synthesis_pass_at >= ? AND synthesis_pass_at < ?",
                (start_s, end_s),
            ).fetchone()
            report["evaluation_ledger"] = {
                "source": "reflex-recorder run_evals",
                "count": row[0] or 0,
                "distinct_issue_signatures": row[1] or 0,
                "by_decision": _group_counts(
                    conn,
                    "SELECT decision, COUNT(*) FROM run_evals WHERE synthesis_pass_at >= ? AND synthesis_pass_at < ? GROUP BY decision ORDER BY COUNT(*) DESC",
                    (start_s, end_s),
                    max_cardinality,
                ),
                "by_rung": _group_counts(
                    conn,
                    "SELECT rung, COUNT(*) FROM run_evals WHERE synthesis_pass_at >= ? AND synthesis_pass_at < ? GROUP BY rung ORDER BY COUNT(*) DESC",
                    (start_s, end_s),
                    max_cardinality,
                ),
                "note": (
                    "A recorded decision is a proposal from the synthesis pass. "
                    "It is not evidence that a remediation was applied or worked."
                ),
            }

        if "detector_hits" in tables:
            row = conn.execute(
                "SELECT COUNT(*) FROM detector_hits WHERE ts >= ? AND ts < ?",
                (start_s, end_s),
            ).fetchone()
            report["detector_hits"] = {
                "count": row[0] or 0,
                "by_detector": _group_counts(
                    conn,
                    "SELECT detector, COUNT(*) FROM detector_hits WHERE ts >= ? AND ts < ? GROUP BY detector ORDER BY COUNT(*) DESC",
                    (start_s, end_s),
                    max_cardinality,
                ),
            }

        return report
    finally:
        conn.close()


def _segment_worktrees(db_path: Path, start: datetime, end: datetime) -> Optional[list[dict[str, Any]]]:
    """Return (run_id, worktree, worktree_slug, project) for window segments."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        conn = _connect_readonly(db_path)
    except sqlite3.Error:
        return None
    try:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone():
            return None
        rows = conn.execute(
            "SELECT run_id, worktree, worktree_slug, project FROM runs "
            "WHERE started < ? AND ended >= ?",
            (_iso(end), _iso(start)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ── derived correlation (labelled, never authoritative) ──────────────────────


def correlate_segments_to_workers(
    segments: Optional[list[dict[str, Any]]],
    worktree_index: Mapping[str, list],
) -> dict[str, Any]:
    """Join recorder segments to Orca worker identity by worktree path.

    This is the only correlation available without new producer work, because
    ``bus.agent.activity.v1`` carries no bead/task/dispatch id at all. It is a
    join on a shared observable key, not a trace: a worktree reused by two
    dispatches is genuinely ambiguous and is reported as such rather than
    resolved by guessing.
    """
    base = {
        "basis": "worktree_path_join",
        "authority": "observational_join_only",
        "caveat": (
            "A worktree path reused by successive dispatches yields more than "
            "one candidate identity. Ambiguous matches are reported, never "
            "collapsed to a single answer, and nothing here is written back "
            "to the recorder database."
        ),
    }
    if segments is None:
        base["status"] = "unavailable"
        base["reason"] = "recorder database absent or unreadable"
        return base

    by_path = {path: list(values) for path, values in worktree_index.items()}
    by_slug: dict[str, list] = defaultdict(list)
    for path, values in by_path.items():
        by_slug[os.path.basename(path)].extend(values)

    matched_unique = 0
    matched_ambiguous = 0
    unmatched = 0
    matched_paths: set[str] = set()

    for segment in segments:
        candidates: list = []
        absolute = segment.get("worktree")
        slug = segment.get("worktree_slug")
        if isinstance(absolute, str) and absolute:
            key = absolute.rstrip("/")
            candidates = by_path.get(key, [])
            if candidates:
                matched_paths.add(key)
        if not candidates and isinstance(slug, str) and slug:
            candidates = by_slug.get(slug, [])
            for path in by_path:
                if os.path.basename(path) == slug:
                    matched_paths.add(path)
        distinct = {tuple(candidate) for candidate in candidates}
        if not distinct:
            unmatched += 1
        elif len(distinct) == 1:
            matched_unique += 1
        else:
            matched_ambiguous += 1

    base.update(
        {
            "status": "computed",
            "segments": len(segments),
            "matched_unique": matched_unique,
            "matched_ambiguous": matched_ambiguous,
            "unmatched": unmatched,
            "worker_worktrees_in_window": len(by_path),
            "worker_worktrees_unmatched_by_any_segment": len(set(by_path) - matched_paths),
        }
    )
    return base


# ── optional memory-evaluation ledger ────────────────────────────────────────


def summarize_ledger(path: Path) -> dict[str, Any]:
    """Summarise an attempt ledger through the existing v1 contract.

    ``tools/memory_evaluation.py`` owns this format. We import it rather than
    reimplementing it so a ledger can never be scored two different ways, and
    we honour its all-or-nothing rule: a malformed ledger is an error, not a
    partial number.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import memory_evaluation  # type: ignore
    except Exception as exc:  # pragma: no cover - import environment specific
        return {
            "status": "unavailable",
            "path": str(path),
            "reason": f"tools/memory_evaluation.py could not be imported: {exc}",
        }

    path = Path(path)
    if not path.exists():
        raise ReportError(f"--ledger {path} does not exist")

    records: list[Mapping[str, Any]] = []
    with open(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                raise ReportError(f"--ledger line {number}: blank line in ledger")
            try:
                records.append(json.loads(stripped, parse_float=Decimal))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ReportError(f"--ledger line {number}: {exc}") from exc

    try:
        report = memory_evaluation.summarize(records)
    except ValueError as exc:
        raise ReportError(f"--ledger rejected by memory_evaluation v1: {exc}") from exc

    return {
        "status": "summarized",
        "path": str(path),
        "contract": "tools/memory_evaluation.py summarize() v1",
        "report": report,
    }


# ── report assembly ──────────────────────────────────────────────────────────


LIMITATIONS = [
    "Counts are metadata aggregates. No prompt, payload body or transcript "
    "content is read or emitted.",
    "Fixture classification is self-declared. 'production_or_unclassified' is "
    "the absence of a fixture marker, not proof of production traffic.",
    "A missing usage metric means the producer did not report it. It is never "
    "counted as zero cost or zero tokens.",
    "Worker lifecycle outcomes are what an agent reported about itself. This "
    "report never treats a reported success as verified acceptance.",
    "Journal coverage is bounded by rotation retention. Records older than the "
    "oldest retained generation are absent from every count here.",
    "The recorder database is read with mode=ro; nothing in this tool writes "
    "to it, backfills it, or restarts a service.",
]


def build_report(
    *,
    start: datetime,
    end: datetime,
    journal: Path,
    db: Path,
    ledger: Optional[Path] = None,
    max_cardinality: int = DEFAULT_MAX_CARDINALITY,
    max_tracked_ids: int = DEFAULT_MAX_TRACKED_IDS,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    files = discover_journal_files(journal)
    journal_report = aggregate_journal(
        files,
        start,
        end,
        max_cardinality=max_cardinality,
        max_tracked_ids=max_tracked_ids,
        max_samples=max_samples,
    )
    worktree_index = journal_report.pop("_worktree_identity_index", {})

    db_report = aggregate_recorder_db(db, start, end, max_cardinality=max_cardinality)
    segments = _segment_worktrees(db, start, end)
    correlation = correlate_segments_to_workers(segments, worktree_index)

    records_unique = journal_report["records"]["unique"]
    segment_count = db_report.get("segments", {}).get("count")
    task_count = journal_report["orca_worker_lifecycle"]["distinct_task_ids"]

    report: dict[str, Any] = {
        "version": REPORT_VERSION,
        "evidence_level": EVIDENCE_LEVEL,
        "verified_acceptance": VERIFIED_ACCEPTANCE,
        "generated_at_utc": _iso(generated_at or datetime.now(timezone.utc)),
        "window": {
            "start_utc": _iso(start),
            "end_utc": _iso(end),
            "bounds": "half-open [start, end)",
            "duration_hours": round((end - start).total_seconds() / 3600.0, 6),
        },
        "denominators": {
            "records": {
                "value": records_unique,
                "basis": "unique journal envelopes in window (exact duplicates collapsed)",
            },
            "segments": {
                "value": segment_count,
                "basis": "reflex-recorder runs overlapping window",
            },
            "tasks": {
                "value": task_count,
                "basis": f"distinct task_id on {WORKER_LIFECYCLE_TYPE} in window",
            },
            "note": (
                "These three populations are not interchangeable. A ratio is "
                "only meaningful against the denominator named beside it."
            ),
        },
        "journal": journal_report,
        "recorder_db": db_report,
        "identity_correlation": correlation,
        "limitations": list(LIMITATIONS),
    }

    if ledger is not None:
        report["attempt_ledger"] = summarize_ledger(ledger)
    else:
        report["attempt_ledger"] = {
            "status": "not_supplied",
            "note": "Pass --ledger to summarise a memory-evaluation attempt ledger.",
        }

    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecosystem_daily_report.py",
        description=(
            "Emit one bounded, read-only JSON aggregate of nervous-bus "
            "telemetry for a UTC window."
        ),
    )
    parser.add_argument("--hours", type=float, default=None, help="Window length in hours (default 24).")
    parser.add_argument("--since", default=None, help="Window start, RFC3339 with offset (e.g. 2026-09-05T00:00:00Z).")
    parser.add_argument("--until", default=None, help="Window end, RFC3339 with offset (default: now).")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL, help="Live journal path; rotations are discovered automatically.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="reflex-recorder SQLite database (opened read-only).")
    parser.add_argument("--ledger", type=Path, default=None, help="Optional memory-evaluation attempt ledger (JSONL).")
    parser.add_argument("--max-cardinality", type=int, default=DEFAULT_MAX_CARDINALITY, help="Cap on distinct keys per group-by.")
    parser.add_argument("--max-tracked-ids", type=int, default=DEFAULT_MAX_TRACKED_IDS, help="Cap on envelope ids held for dedup.")
    parser.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES, help="Cap on collision / malformed-time samples.")
    parser.add_argument("--output", type=Path, default=None, help="Write JSON here instead of stdout.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact).")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        start, end = resolve_window(hours=args.hours, since=args.since, until=args.until)
        report = build_report(
            start=start,
            end=end,
            journal=args.journal,
            db=args.db,
            ledger=args.ledger,
            max_cardinality=args.max_cardinality,
            max_tracked_ids=args.max_tracked_ids,
            max_samples=args.max_samples,
        )
        # Serialise fully before touching the output stream so a failure
        # mid-encode cannot leave a half-written report behind.
        payload = json.dumps(report, indent=args.indent or None, sort_keys=False, default=str)
    except ReportError as exc:
        sys.stderr.write(f"ecosystem_daily_report: {exc}\n")
        return 2
    except ValueError as exc:
        sys.stderr.write(f"ecosystem_daily_report: {exc}\n")
        return 2

    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
