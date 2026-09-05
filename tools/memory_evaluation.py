#!/usr/bin/env python3
"""Memory-evaluation ledger accounting.

This tool turns an attempt-by-attempt JSONL ledger into a deterministic,
transport-free summary. It deliberately does not look at Redis, the bus,
or any out-of-band state. Every fact it reports must come from a record
in the ledger or from a receipt carried in that record.

Design contract (v1):

* Public entry point: :func:`summarize`. CLI is a thin wrapper.
* Every JSONL line must decode to a complete, immutable attempt snapshot.
  Malformed lines, missing required keys, or out-of-enum values abort the
  run with a non-zero exit and a stderr message. There is no partial
  success.
* Identical duplicate rows collapse. Same ``attempt_id`` with any changed
  content raises :class:`ValueError`.
* Every cost phase must carry every metric key. A metric may be
  ``null`` (unknown) but it may not be absent: an absent key is a
  malformed record, not an implicit unknown.
* Costs are aggregated per phase as ``known_total + unknown_count``
  pairs. Unknown means ``null``; zero is never used as a stand-in for
  unknown. Monetary values are summed with :class:`decimal.Decimal` and
  serialised to JSON as decimal *strings* so no cent is lost to binary
  floating point. The CLI parses JSON floats with ``parse_float=Decimal``
  for the same reason.
* Receipt and event identities are globally unique across the ledger. A
  ``receipt_id`` or ``event_id`` reused for a different execution
  attempt is rejected via the reference consumer in
  ``tools/exec_evidence_receipts.py`` rather than allowed to inflate the
  verified counts.
* Receipts are validated against the existing ``bus.exec.evidence.v1``
  reference. A receipt is counted as verified only when the record's own
  ``status`` is ``completed`` AND the receipt reports
  ``is_runtime_complete``. A runtime-complete receipt paired with a
  non-completed status is rejected as a mismatch.
* This is accounting, not a quality benchmark. The output explicitly
  reports ``evidence_level="recorded_receipts"`` and
  ``promotion="NOT_EVALUATED"`` so downstream consumers cannot
  misread the result as an independently run experiment.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

# Import the existing receipt reference so we share validation rules.
# The receipt tool is intentionally transport-free, so importing it does
# not pull in Redis or the wider bus surface.
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from exec_evidence_receipts import (  # noqa: E402  (sys.path adjusted above)
    ReceiptIdentityConflict,
    ReceiptValidationError,
    ReferenceReceiptConsumer,
    UnsupportedReceiptVersion,
    validate_receipt,
)


SCHEMA_VERSION = 1
EVIDENCE_LEVEL = "recorded_receipts"
PROMOTION_STATUS = "NOT_EVALUATED"

REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "attempt_id",
    "execution_id",
    "bead_id",
    "project",
    "model",
    "task_id",
)

REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    *REQUIRED_STRING_FIELDS,
    "condition",
    "corpus_sha256",
    "status",
    "identities",
    "costs",
    "receipt",
)

ALLOWED_CONDITIONS: frozenset[str] = frozenset({"baseline", "excerpts", "packet"})
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"started", "failed", "timed_out", "cancelled", "completed"}
)

# All seven causal identities that an attempt may carry. The set is
# closed: an attempt cannot smuggle in a fresh identity by inventing a
# new field name, and it cannot omit one silently — missing fields are
# preserved as ``None`` so the summary can report them explicitly.
IDENTITY_FIELDS: tuple[str, ...] = (
    "kb_entry_id",
    "deer_run_id",
    "orca_run_id",
    "orca_task_id",
    "orca_dispatch_id",
    "provider_session_id",
    "repository_sha",
    "traceparent",
)

# Six disjoint cost phases. ``retry`` records retry-specific overhead;
# every actual retry execution attempt is attributed to ``execution``.
COST_PHASES: tuple[str, ...] = (
    "construction",
    "research",
    "retrieval",
    "execution",
    "retry",
    "review",
)

COST_METRICS: tuple[str, ...] = (
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)

INTEGER_COST_METRICS: frozenset[str] = frozenset(
    {"duration_ms", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
)

# W3C trace-context v00: ``00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>``.
# Lowercase only and the trace/span ids must not be all-zero — a fresh
# root span is required before any event is allowed to claim context.
#
# These are matched with ``re.fullmatch``: ``re.match`` with a trailing
# ``$`` also accepts a single trailing newline, so ``"a" * 64 + "\n"``
# would sneak through as a valid corpus digest.
TRACEPARENT_RE = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
HEX40_RE = re.compile(r"[0-9a-f]{40}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")


class MemoryEvaluationError(ValueError):
    """Base class for ledger-validation failures."""


class MalformedRecordError(MemoryEvaluationError):
    """A record failed a structural or semantic check."""


class ConflictingDuplicateError(MemoryEvaluationError):
    """The same attempt_id reappears with different content."""


class ReceiptIdentityConflictError(MemoryEvaluationError):
    """A receipt_id or event_id was reused across different attempts."""


@dataclass(frozen=True)
class _ValidatedRecord:
    """The validated, normalised form of one attempt row."""

    raw: Mapping[str, Any]
    attempt_id: str
    execution_id: str
    bead_id: str
    project: str
    model: str
    task_id: str
    condition: str
    corpus_sha256: str
    status: str
    identities: dict[str, str | None]
    costs: dict[str, dict[str, int | float | Decimal | None]]
    receipt: Mapping[str, Any] | None
    receipt_runtime_complete: bool

    @property
    def is_verified(self) -> bool:
        return self.receipt_runtime_complete and self.status == "completed"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MalformedRecordError(message)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        # NaN is excluded by ``value == value``; infinities are excluded
        # by the explicit finite comparisons.
        return value == value and value != float("inf") and value != float("-inf")
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def _is_nonnegative(value: Any) -> bool:
    if not _is_finite_number(value):
        return False
    if isinstance(value, Decimal):
        return value >= 0
    return value >= 0


def _validate_traceparent(value: Any) -> str:
    _require(isinstance(value, str), "traceparent must be a string")
    _require(
        TRACEPARENT_RE.fullmatch(value) is not None,
        "traceparent must be W3C v00 lowercase hex",
    )
    version, trace_id, span_id, _flags = value.split("-")
    _require(
        version == "00",
        "traceparent version must be 00",
    )
    _require(
        not all(ch == "0" for ch in trace_id),
        "traceparent trace-id must not be all-zero",
    )
    _require(
        not all(ch == "0" for ch in span_id),
        "traceparent span-id must not be all-zero",
    )
    return value


def _validate_repository_sha(value: Any) -> str:
    _require(isinstance(value, str), "repository_sha must be a string when present")
    _require(
        HEX40_RE.fullmatch(value) is not None or HEX64_RE.fullmatch(value) is not None,
        "repository_sha must be 40 or 64 lowercase hex characters",
    )
    return value


def _validate_identities(identities: Any) -> dict[str, str | None]:
    _require(isinstance(identities, dict), "identities must be an object")

    extras = set(identities) - set(IDENTITY_FIELDS)
    _require(not extras, f"identities has unknown fields: {sorted(extras)}")

    normalized: dict[str, str | None] = {}
    for field in IDENTITY_FIELDS:
        if field not in identities:
            normalized[field] = None
            continue
        value = identities[field]
        if value is None:
            normalized[field] = None
            continue
        _require(isinstance(value, str), f"{field} must be a string when present")
        _require(value != "", f"{field} must be a nonempty string when present")
        if field == "traceparent":
            normalized[field] = _validate_traceparent(value)
        elif field == "repository_sha":
            normalized[field] = _validate_repository_sha(value)
        else:
            normalized[field] = value

    return normalized


def _validate_cost_metric(field: str, value: Any) -> int | float | Decimal | None:
    if value is None:
        return None
    if field in INTEGER_COST_METRICS:
        _require(
            isinstance(value, int) and not isinstance(value, bool),
            f"{field} must be a non-negative integer or null",
        )
        _require(value >= 0, f"{field} must be non-negative")
        return value
    # cost_usd — accept int, float, or Decimal. ``_is_nonnegative`` rejects
    # bools, NaN, and both infinities; Decimal is preferred because the CLI
    # parses JSON floats with ``parse_float=Decimal``.
    _require(
        _is_nonnegative(value),
        "cost_usd must be a non-negative finite number or null",
    )
    return value


def _validate_costs(costs: Any) -> dict[str, dict[str, int | float | Decimal | None]]:
    _require(isinstance(costs, dict), "costs must be an object")

    extras = set(costs) - set(COST_PHASES)
    _require(not extras, f"costs has unknown phases: {sorted(extras)}")

    normalized: dict[str, dict[str, int | float | Decimal | None]] = {}
    for phase in COST_PHASES:
        phase_value = costs.get(phase)
        _require(isinstance(phase_value, dict), f"costs.{phase} must be an object")
        extras_phase = set(phase_value) - set(COST_METRICS)
        _require(
            not extras_phase,
            f"costs.{phase} has unknown metrics: {sorted(extras_phase)}",
        )
        # An absent metric is NOT an implicit unknown. A producer that
        # cannot measure a metric must say so with an explicit ``null``;
        # silently treating a missing key as null would let a truncated
        # writer masquerade as a complete snapshot.
        missing_metrics = [metric for metric in COST_METRICS if metric not in phase_value]
        _require(
            not missing_metrics,
            f"costs.{phase} missing required metrics: {missing_metrics}",
        )
        normalized_phase: dict[str, int | float | Decimal | None] = {}
        for metric in COST_METRICS:
            normalized_phase[metric] = _validate_cost_metric(metric, phase_value[metric])
        normalized[phase] = normalized_phase
    return normalized


def _validate_record_shape(record: Any) -> _ValidatedRecord:
    _require(isinstance(record, dict), "record must be an object")

    missing = [field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in record]
    _require(not missing, f"record missing required fields: {missing}")

    for field in REQUIRED_STRING_FIELDS:
        value = record[field]
        _require(isinstance(value, str), f"{field} must be a string")
        _require(value != "", f"{field} must be nonempty")

    condition = record["condition"]
    _require(isinstance(condition, str), "condition must be a string")
    _require(condition in ALLOWED_CONDITIONS, f"condition must be one of {sorted(ALLOWED_CONDITIONS)}")

    corpus_sha256 = record["corpus_sha256"]
    _require(isinstance(corpus_sha256, str), "corpus_sha256 must be a string")
    _require(
        HEX64_RE.fullmatch(corpus_sha256) is not None,
        "corpus_sha256 must be 64 lowercase hex characters",
    )

    status = record["status"]
    _require(isinstance(status, str), "status must be a string")
    _require(status in ALLOWED_STATUSES, f"status must be one of {sorted(ALLOWED_STATUSES)}")

    identities = _validate_identities(record["identities"])
    costs = _validate_costs(record["costs"])
    receipt_raw = record["receipt"]
    receipt_runtime_complete = False

    if receipt_raw is not None:
        _require(isinstance(receipt_raw, dict), "receipt must be an object or null")
        try:
            receipt_obj = validate_receipt(receipt_raw)
        except UnsupportedReceiptVersion as exc:
            raise MalformedRecordError(str(exc)) from exc
        except ReceiptValidationError as exc:
            raise MalformedRecordError(f"receipt failed validation: {exc}") from exc

        receipt_data = receipt_raw["data"]
        actual_model = receipt_data["actual_execution"]["model"]
        _require(
            receipt_obj.attempt_id == record["attempt_id"],
            "receipt attempt_id does not match record",
        )
        _require(
            receipt_obj.execution_id == record["execution_id"],
            "receipt execution_id does not match record",
        )
        _require(
            receipt_obj.bead_id == record["bead_id"],
            "receipt bead_id does not match record",
        )
        _require(
            actual_model == record["model"],
            "receipt actual_execution.model does not match record",
        )

        if receipt_obj.is_runtime_complete:
            _require(
                status == "completed",
                "runtime_complete receipt is paired with non-completed status",
            )
            receipt_runtime_complete = True

    return _ValidatedRecord(
        raw=record,
        attempt_id=record["attempt_id"],
        execution_id=record["execution_id"],
        bead_id=record["bead_id"],
        project=record["project"],
        model=record["model"],
        task_id=record["task_id"],
        condition=condition,
        corpus_sha256=corpus_sha256,
        status=status,
        identities=identities,
        costs=costs,
        receipt=receipt_raw,
        receipt_runtime_complete=receipt_runtime_complete,
    )


def _content_key(value: Any) -> Any:
    """Return a hashable, *type-tagged* view of a decoded JSON value.

    Plain ``==`` treats ``0``, ``0.0``, ``Decimal("0")``, ``Decimal("0.00")``
    and ``False`` as the same value, so an attempt could be re-emitted with
    materially different content and still collapse as an "identical
    duplicate". Tagging the type (and comparing decimals by their exact
    string form) makes the dedup check see what a reader would see.
    """

    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, Decimal):
        return ("decimal", str(value))
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, Mapping):
        return ("map", tuple(sorted((key, _content_key(item)) for key, item in value.items())))
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_content_key(item) for item in value))
    return ("other", repr(value))


def _records_are_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if two records are content-identical for ledger purposes.

    We deliberately skip a JSON round-trip here so :class:`decimal.Decimal`
    values survive the dedup check intact; :func:`_content_key` supplies the
    type discipline that a round-trip would otherwise have provided.
    """

    return _content_key(left) == _content_key(right)


def _parse_records(records: Iterable[Mapping[str, Any]]) -> list[_ValidatedRecord]:
    """Parse, validate, deduplicate, and cross-check every ledger line."""

    validated: list[_ValidatedRecord] = []
    seen: dict[str, Mapping[str, Any]] = {}

    for index, parsed in enumerate(records, start=1):
        if not isinstance(parsed, dict):
            raise MalformedRecordError(f"record {index}: record must be a JSON object")

        record = _validate_record_shape(parsed)

        prior = seen.get(record.attempt_id)
        if prior is not None:
            if not _records_are_equal(prior, parsed):
                raise ConflictingDuplicateError(
                    f"attempt_id {record.attempt_id!r} reappears with changed content"
                )
            continue

        seen[record.attempt_id] = parsed
        validated.append(record)

    _check_receipt_identity_conflicts(validated)

    # Cross-attempt invariants: each execution must agree on project,
    # task_id, condition, and corpus_sha256 across its retries.
    execution_to_project: dict[str, str] = {}
    execution_to_task: dict[str, str] = {}
    execution_to_condition: dict[str, str] = {}
    execution_to_corpus: dict[str, str] = {}
    for record in validated:
        existing_project = execution_to_project.setdefault(record.execution_id, record.project)
        _require(
            existing_project == record.project,
            f"execution_id {record.execution_id!r} disagrees on project "
            f"({existing_project!r} vs {record.project!r})",
        )
        existing_task = execution_to_task.setdefault(record.execution_id, record.task_id)
        _require(
            existing_task == record.task_id,
            f"execution_id {record.execution_id!r} disagrees on task_id",
        )
        existing_condition = execution_to_condition.setdefault(record.execution_id, record.condition)
        _require(
            existing_condition == record.condition,
            f"execution_id {record.execution_id!r} disagrees on condition",
        )
        existing_corpus = execution_to_corpus.setdefault(record.execution_id, record.corpus_sha256)
        _require(
            existing_corpus == record.corpus_sha256,
            f"execution_id {record.execution_id!r} disagrees on corpus_sha256",
        )

    return validated


def _as_decimal(value: int | float | Decimal) -> Decimal:
    """Coerce a validated monetary value to :class:`Decimal` losslessly.

    Floats go through ``str`` (never ``Decimal(float)``) so ``0.1`` stays
    ``0.1`` instead of becoming its binary expansion. Values that arrive
    from the CLI are already ``Decimal`` — see ``parse_float`` below.
    """

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _check_receipt_identity_conflicts(records: list[_ValidatedRecord]) -> None:
    """Reject receipt/event identities reused across different attempts.

    Receipt and event ids are the consumer-side deduplication keys. If two
    ledger rows carry the same ``receipt_id`` (or the same ``event_id``)
    while describing different execution attempts, then at most one of them
    can be true — counting both would inflate ``verified_attempts`` from a
    single piece of evidence. We delegate the decision to the reference
    consumer in ``tools/exec_evidence_receipts.py`` rather than restating
    its rules here, so the ledger can never be more permissive than the
    bus edge. That module is used as-is; nothing in it is modified.

    Rows are fed in ledger order, after identical-duplicate collapse, so
    every receipt reaching the consumer belongs to a distinct ``attempt_id``.
    """

    consumer = ReferenceReceiptConsumer()
    for record in records:
        if record.receipt is None:
            continue
        try:
            consumer.consume(record.receipt)
        except ReceiptIdentityConflict as exc:
            raise ReceiptIdentityConflictError(
                f"attempt_id {record.attempt_id!r}: {exc}"
            ) from exc


def _aggregate_cost_metrics(
    records: list[_ValidatedRecord],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Aggregate the per-phase cost metrics into known/unknown/total triples.

    Each metric returns ``known_total`` (the sum of measured values),
    ``unknown_count`` (the number of ``null`` entries seen), and ``total``
    (equal to ``known_total`` when every entry was known, otherwise
    ``null``). ``unknown_count`` is counted per phase/metric entry, which
    is the same unit :func:`_aggregate_cost_totals` uses.
    """

    by_phase: dict[str, dict[str, dict[str, Any]]] = {}

    for phase in COST_PHASES:
        phase_view: dict[str, dict[str, Any]] = {}
        for metric in COST_METRICS:
            if metric in INTEGER_COST_METRICS:
                known_total: int = 0
            else:
                known_total = Decimal("0")
            unknown_count = 0
            for record in records:
                value = record.costs[phase][metric]
                if value is None:
                    unknown_count += 1
                    continue
                if metric in INTEGER_COST_METRICS:
                    known_total = known_total + value  # type: ignore[operator]
                else:
                    known_total = known_total + _as_decimal(value)  # type: ignore[operator]

            total_value: int | Decimal | None = known_total if unknown_count == 0 else None
            phase_view[metric] = {
                "known_total": known_total,
                "unknown_count": unknown_count,
                "total": total_value,
            }
        by_phase[phase] = phase_view

    return by_phase


def _aggregate_cost_totals(records: list[_ValidatedRecord]) -> dict[str, dict[str, Any]]:
    """Aggregate every metric across all six phases for the grand total."""

    out: dict[str, dict[str, Any]] = {}
    for metric in COST_METRICS:
        known_total: int | Decimal = 0 if metric in INTEGER_COST_METRICS else Decimal("0")
        unknown_count = 0
        for record in records:
            for phase in COST_PHASES:
                value = record.costs[phase][metric]
                if value is None:
                    unknown_count += 1
                elif metric in INTEGER_COST_METRICS:
                    known_total = known_total + value  # type: ignore[operator]
                else:
                    known_total = known_total + _as_decimal(value)  # type: ignore[operator]
        total_value: int | Decimal | None = known_total if unknown_count == 0 else None
        out[metric] = {
            "known_total": known_total,
            "unknown_count": unknown_count,
            "total": total_value,
        }
    return out


def _summarize_groups(records: list[_ValidatedRecord]) -> list[dict[str, Any]]:
    """Bucket attempts by project/model/condition/corpus_sha256."""

    buckets: dict[tuple[str, str, str, str], list[_ValidatedRecord]] = defaultdict(list)
    for record in records:
        buckets[(record.project, record.model, record.condition, record.corpus_sha256)].append(record)

    groups: list[dict[str, Any]] = []
    for (project, model, condition, corpus_sha256), bucket in sorted(buckets.items()):
        verified_attempts = sum(1 for record in bucket if record.is_verified)
        # ``unknown_count`` counts unknown phase entries, matching the unit
        # used by ``costs_by_phase``/``costs_total`` so the three fields of
        # the same name are never read against different denominators.
        known_total_cost = Decimal("0")
        unknown_cost_entries = 0
        for record in bucket:
            for phase in COST_PHASES:
                value = record.costs[phase]["cost_usd"]
                if value is None:
                    unknown_cost_entries += 1
                    continue
                known_total_cost = known_total_cost + _as_decimal(value)
        cost_entry: dict[str, Any] = {
            "known_total": known_total_cost,
            "unknown_count": unknown_cost_entries,
            "total": known_total_cost if unknown_cost_entries == 0 else None,
        }

        groups.append(
            {
                "project": project,
                "model": model,
                "condition": condition,
                "corpus_sha256": corpus_sha256,
                "attempts": len(bucket),
                "verified_attempts": verified_attempts,
                "costs_total": cost_entry,
            }
        )
    return groups


def _missing_identity_counts(records: list[_ValidatedRecord]) -> dict[str, int]:
    counts = {field: 0 for field in IDENTITY_FIELDS}
    for record in records:
        for field in IDENTITY_FIELDS:
            if record.identities[field] is None:
                counts[field] += 1
    return counts


def summarize(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize a sequence of raw JSONL records into the v1 report shape.

    The caller passes a list of already-decoded JSON objects (one per
    line of the ledger). ``summarize`` keeps the original Python values
    intact so :class:`decimal.Decimal` costs survive dedup and
    aggregation without round-tripping through ``str``.
    """

    materialised: list[Mapping[str, Any]] = list(records)
    validated = _parse_records(materialised)

    verified_attempts = sum(1 for record in validated if record.is_verified)
    distinct_executions = {record.execution_id for record in validated}
    verified_executions: set[str] = set()
    for record in validated:
        if record.is_verified:
            verified_executions.add(record.execution_id)

    status_counts: dict[str, int] = {status: 0 for status in sorted(ALLOWED_STATUSES)}
    for record in validated:
        status_counts[record.status] += 1

    by_phase = _aggregate_cost_metrics(validated)
    costs_total = _aggregate_cost_totals(validated)

    verified_completion_rate: float | None
    if not validated:
        verified_completion_rate = None
    else:
        verified_completion_rate = verified_attempts / len(validated)

    report = {
        "version": SCHEMA_VERSION,
        "evidence_level": EVIDENCE_LEVEL,
        "promotion": PROMOTION_STATUS,
        "attempts": len(validated),
        "executions": len(distinct_executions),
        "verified_attempts": verified_attempts,
        "verified_executions": len(verified_executions),
        "verified_completion_rate": verified_completion_rate,
        "status_counts": status_counts,
        "costs_by_phase": by_phase,
        "costs_total": costs_total,
        "missing_identity_counts": _missing_identity_counts(validated),
        "groups": _summarize_groups(validated),
    }
    return report


def _reject_nonfinite_constant(token: str) -> Any:
    """Refuse the ``NaN``/``Infinity`` JSON extensions at the parse boundary.

    ``json`` accepts these by default. A non-finite number is never an
    admissible cost, duration, or count, and rejecting it here means no
    part of the pipeline has to carry a poisoned value.
    """

    raise MalformedRecordError(f"non-finite JSON constant {token!r} is not admissible")


def _load_ledger_text(path: Path) -> str:
    """Read the ledger as strict UTF-8, mapping decode failures to our error."""

    raw_bytes = path.read_bytes()  # OSError propagates to the CLI wrapper
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedRecordError(
            f"ledger is not valid UTF-8 at byte {exc.start}: {exc.reason}"
        ) from exc


def _summarize_from_path(path: Path) -> dict[str, Any]:
    text = _load_ledger_text(path)

    lines = text.split("\n")
    if lines and lines[-1] == "":
        # A single trailing newline terminates the last record; it is not
        # an extra (blank) line. Any other blank line is an error below.
        lines.pop()

    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            # Empty lines are not allowed; surface them rather than skip.
            raise MalformedRecordError(f"line {line_number}: blank line in ledger")
        try:
            # ``parse_float=Decimal`` keeps monetary literals exact: a
            # ``cost_usd`` of 0.1 stays 0.1 rather than becoming the nearest
            # binary double, so summing a ledger cannot drift by cents.
            parsed = json.loads(
                stripped,
                parse_float=Decimal,
                parse_constant=_reject_nonfinite_constant,
            )
        except json.JSONDecodeError as exc:
            raise MalformedRecordError(
                f"line {line_number}: invalid JSON ({exc.msg})"
            ) from exc
        except MalformedRecordError as exc:
            raise MalformedRecordError(f"line {line_number}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MalformedRecordError(f"line {line_number}: record must be a JSON object")
        records.append(parsed)

    return summarize(records)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_evaluation",
        description=(
            "Summarize a memory-evaluation attempt ledger into a v1 report. "
            "Reads one JSON object per line from LEDGER.jsonl and writes a "
            "JSON summary to stdout. Any invalid record aborts the run."
        ),
    )
    parser.add_argument(
        "ledger",
        type=Path,
        help="Path to a JSONL ledger of attempt snapshots.",
    )
    return parser


def _json_default(value: Any) -> Any:
    """Serialise :class:`decimal.Decimal` losslessly for CLI output.

    Monetary values are emitted as decimal *strings*, not JSON numbers: a
    JSON number would be re-read as a binary double by most consumers and
    silently lose the precision the Decimal arithmetic preserved. Token
    and duration totals stay plain integers.
    """

    if isinstance(value, Decimal):
        if not value.is_finite():  # defence in depth; inputs are pre-screened
            raise ValueError(f"refusing to serialise non-finite total {value!r}")
        # ``str(Decimal)`` preserves precision and never adopts scientific
        # notation for the magnitudes we expect (cost_usd).
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        report = _summarize_from_path(args.ledger)
        # Serialise fully before touching stdout. ``json.dump`` streams, so a
        # failure partway through would leave a truncated object on stdout
        # next to a non-zero exit — exactly the partial success the contract
        # forbids. ``allow_nan=False`` refuses to emit NaN/Infinity.
        payload = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
            allow_nan=False,
        )
    except OSError as exc:
        # Missing file, unreadable file, a directory in place of a file.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MemoryEvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Non-finite value reaching serialisation, or an unencodable total.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
