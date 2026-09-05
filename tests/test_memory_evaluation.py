"""Behavioral tests for ``tools/memory_evaluation.py``.

These tests pin the public ``summarize`` contract and the CLI's
non-zero-on-invalid behaviour. They use only synthetic fixtures so
they stay transport-free and host-independent.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
TESTS_DIR = REPO_ROOT / "tests"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import memory_evaluation  # noqa: E402  (sys.path adjusted above)
from memory_evaluation import (  # noqa: E402
    ConflictingDuplicateError,
    MalformedRecordError,
    summarize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Fixed corpus / repository hashes that satisfy the validator.
CORPUS_SHA = "0" * 64
REPO_SHA_SHORT = "a" * 40
REPO_SHA_LONG = "b" * 64

# W3C trace-context v00 with a non-zero trace and span.
TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def _zero_costs() -> dict:
    """A fully-known zero-everywhere cost block (no ``null`` values)."""

    return {
        phase: {
            "duration_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0,
        }
        for phase in (
            "construction",
            "research",
            "retrieval",
            "execution",
            "retry",
            "review",
        )
    }


def _null_costs() -> dict:
    """A fully-unknown cost block (every metric is ``null``)."""

    return {
        phase: {
            "duration_ms": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_usd": None,
        }
        for phase in (
            "construction",
            "research",
            "retrieval",
            "execution",
            "retry",
            "review",
        )
    }


def _identities_minimal() -> dict:
    """Empty-but-present identity block (every field explicitly null)."""

    return {
        "kb_entry_id": None,
        "deer_run_id": None,
        "orca_run_id": None,
        "orca_task_id": None,
        "orca_dispatch_id": None,
        "provider_session_id": None,
        "repository_sha": None,
        "traceparent": None,
    }


def make_record(
    *,
    attempt_id: str = "attempt-1",
    execution_id: str = "exec-1",
    bead_id: str = "bead-1",
    project: str = "nervous-bus",
    model: str = "gpt-5",
    condition: str = "baseline",
    corpus_sha256: str = CORPUS_SHA,
    task_id: str = "task-1",
    status: str = "started",
    identities: dict | None = None,
    costs: dict | None = None,
    receipt: dict | None = None,
) -> dict:
    """Build a syntactically valid record; callers tweak what they need."""

    return {
        "attempt_id": attempt_id,
        "execution_id": execution_id,
        "bead_id": bead_id,
        "project": project,
        "model": model,
        "condition": condition,
        "corpus_sha256": corpus_sha256,
        "task_id": task_id,
        "status": status,
        "identities": identities if identities is not None else _identities_minimal(),
        "costs": costs if costs is not None else _zero_costs(),
        "receipt": receipt,
    }


def make_receipt(
    *,
    receipt_id: str = "receipt-1",
    event_id: str = "event-1",
    bead_id: str = "bead-1",
    execution_id: str = "exec-1",
    attempt_id: str = "attempt-1",
    attempt_number: int = 1,
    model: str = "gpt-5",
    completion_state: str = "runtime_complete",
    exit_code: int | None = 0,
    completed_at: str | None = "2026-08-23T12:01:00Z",
) -> dict:
    """Build a syntactically valid bus.exec.evidence.v1 envelope."""

    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "/hearth-loom",
        "type": "bus.exec.evidence.v1",
        "datacontenttype": "application/json",
        "time": "2026-08-23T12:00:00Z",
        "data": {
            "schema_version": "1",
            "receipt_id": receipt_id,
            "event_id": event_id,
            "bead_id": bead_id,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "dispatch_owner": "hearth-loom",
            "requested_execution": {"provider": "openai", "model": model, "backend": "codex"},
            "actual_execution": {"provider": "openai", "model": model, "backend": "codex"},
            "git_revisions": {
                "source_sha": "a" * 40,
                "main_sha": "b" * 40,
                "integration_sha": "c" * 40,
                "worktree_sha": "d" * 40,
            },
            "command": {"argv": ["pytest", "-q"], "rendered": "pytest -q", "working_directory": "/worktree"},
            "exit_code": exit_code,
            "completion_state": completion_state,
            "completed_at": completed_at,
            "artifacts": [],
            "denominators": {"tests_matched": {"status": "known", "value": 1}},
            "gates": {
                "test": {
                    "status": "passed",
                    "evidence_id": "test-1",
                    "evidence_kind": "test_result",
                    "completed_at": "2026-08-23T12:00:20Z",
                },
                "runtime": {
                    "status": "passed",
                    "evidence_id": "runtime-1",
                    "evidence_kind": "runtime_probe",
                    "completed_at": "2026-08-23T12:00:50Z",
                },
                "database": {"status": "not_applicable", "not_applicable_reason": "No database path."},
                "device": {"status": "not_applicable", "not_applicable_reason": "No device path."},
            },
        },
    }


# ---------------------------------------------------------------------------
# Happy-path: successful matching receipt counts as verified
# ---------------------------------------------------------------------------


def test_matching_runtime_complete_receipt_verifies_completed_record() -> None:
    receipt = make_receipt()
    record = make_record(status="completed", receipt=receipt)

    report = summarize([record])

    assert report["attempts"] == 1
    assert report["executions"] == 1
    assert report["verified_attempts"] == 1
    assert report["verified_executions"] == 1
    assert report["verified_completion_rate"] == 1.0
    assert report["status_counts"]["completed"] == 1
    assert report["evidence_level"] == "recorded_receipts"
    assert report["promotion"] == "NOT_EVALUATED"


def test_verified_attempt_does_not_require_explicit_verified_boolean() -> None:
    # Records that smuggle in a `verified: true` must be ignored — only the
    # receipt and the record's own status can promote a row.
    receipt = make_receipt()
    record = make_record(status="completed", receipt=receipt)
    record["verified"] = True  # type: ignore[assignment]

    report = summarize([record])

    assert report["verified_attempts"] == 1  # coincidence: also valid here


def test_record_verified_boolean_alone_does_not_count() -> None:
    record = make_record(status="completed", receipt=None)
    record["verified"] = True  # type: ignore[assignment]

    report = summarize([record])

    assert report["verified_attempts"] == 0


# ---------------------------------------------------------------------------
# Non-verified states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["started", "failed", "timed_out", "cancelled"])
def test_non_completed_status_never_counts_as_verified(status: str) -> None:
    # Even a runtime_complete receipt paired with a non-completed status
    # must be rejected — the receipt is the wrong shape for the record.
    receipt = make_receipt() if status != "started" else None
    record = make_record(status=status, receipt=receipt)

    if receipt is not None:
        with pytest.raises(MalformedRecordError, match="runtime_complete receipt"):
            summarize([record])
        return

    report = summarize([record])
    assert report["verified_attempts"] == 0
    assert report["verified_completion_rate"] == 0.0


@pytest.mark.parametrize(
    "completion_state",
    ["started", "background", "static_contract", "source_accepted", "partial", "incomplete"],
)
def test_non_runtime_complete_receipt_remains_unverified(completion_state: str) -> None:
    receipt = make_receipt(completion_state=completion_state, exit_code=None, completed_at=None)
    # Override gates for non-decisive states so the schema stays valid.
    if completion_state != "partial":
        receipt["data"]["gates"]["runtime"] = {
            "status": "unknown",
            "unavailable_reason": "Process has not reached a probeable runtime state.",
        }
    record = make_record(status="completed", receipt=receipt)

    report = summarize([record])

    assert report["verified_attempts"] == 0
    assert report["status_counts"]["completed"] == 1


# ---------------------------------------------------------------------------
# Mixed known / unknown costs
# ---------------------------------------------------------------------------


def test_mixed_known_and_unknown_costs_aggregate_separately() -> None:
    known = make_record(
        attempt_id="a-known",
        execution_id="exec-known",
        costs=_zero_costs(),
    )
    # Build a partially-unknown record by editing one metric in each phase.
    mixed_costs = _zero_costs()
    mixed_costs["execution"]["cost_usd"] = None
    mixed_costs["research"]["duration_ms"] = None
    mixed = make_record(attempt_id="a-mixed", execution_id="exec-mixed", costs=mixed_costs)
    unknown = make_record(attempt_id="a-unknown", execution_id="exec-unknown", costs=_null_costs())

    report = summarize([known, mixed, unknown])

    # ``mixed`` and ``unknown`` both contribute ``null`` to execution.cost_usd.
    exec_total = report["costs_by_phase"]["execution"]["cost_usd"]
    assert exec_total["known_total"] == Decimal("0")
    assert exec_total["unknown_count"] == 2
    assert exec_total["total"] is None

    # ``mixed`` and ``unknown`` both contribute ``null`` to research.duration_ms.
    research_duration = report["costs_by_phase"]["research"]["duration_ms"]
    assert research_duration["known_total"] == 0
    assert research_duration["unknown_count"] == 2
    assert research_duration["total"] is None

    # ``construction.duration_ms`` only sees the unknown record's null.
    construction_duration = report["costs_by_phase"]["construction"]["duration_ms"]
    assert construction_duration["known_total"] == 0
    assert construction_duration["unknown_count"] == 1
    assert construction_duration["total"] is None

    # Grand total aggregates across all six phases.
    cost_total = report["costs_total"]["cost_usd"]
    # mixed (1 null in execution) + unknown (1 null per phase * 6 phases) = 7
    assert cost_total["unknown_count"] == 7
    assert cost_total["total"] is None


def test_decimal_money_sums_without_floating_point_drift() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = Decimal("0.10")
    costs["execution"]["cost_usd"] = costs["execution"]["cost_usd"] + Decimal("0.20")
    record = make_record(attempt_id="a-dec", execution_id="exec-dec", costs=costs)

    report = summarize([record])

    assert report["costs_by_phase"]["execution"]["cost_usd"]["known_total"] == Decimal("0.30")


# ---------------------------------------------------------------------------
# Retries in the denominator and execution-level verified deduplication
# ---------------------------------------------------------------------------


def test_retries_are_counted_in_attempts_and_status_counts() -> None:
    # Two retries of the same execution_id, plus a third attempt on a
    # different execution. None of them are verified; all are in the
    # attempts denominator and the status counts.
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", status="failed")
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", status="started")
    r3 = make_record(attempt_id="a-3", execution_id="exec-2", status="timed_out")

    report = summarize([r1, r2, r3])

    assert report["attempts"] == 3
    assert report["executions"] == 2
    assert report["verified_attempts"] == 0
    assert report["verified_completion_rate"] == 0.0
    assert report["status_counts"]["failed"] == 1
    assert report["status_counts"]["started"] == 1
    assert report["status_counts"]["timed_out"] == 1


def test_execution_verified_dedupes_execution_id_with_multiple_verified_attempts() -> None:
    # Two retries of one execution, both verified. verified_executions
    # must count the execution once, verified_attempts must count both.
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", status="completed", receipt=make_receipt(attempt_id="a-1", event_id="event-1", receipt_id="receipt-1", attempt_number=1))
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", status="completed", receipt=make_receipt(attempt_id="a-2", event_id="event-2", receipt_id="receipt-2", attempt_number=2))

    report = summarize([r1, r2])

    assert report["attempts"] == 2
    assert report["executions"] == 1
    assert report["verified_attempts"] == 2
    assert report["verified_executions"] == 1
    assert report["verified_completion_rate"] == 1.0


def test_model_may_change_between_retries_of_the_same_execution() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", model="gpt-5", status="failed")
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", model="gpt-5-mini", status="completed",
                     receipt=make_receipt(attempt_id="a-2", event_id="event-2", receipt_id="receipt-2", model="gpt-5-mini"))

    report = summarize([r1, r2])

    assert report["executions"] == 1
    assert report["verified_attempts"] == 1


def test_execution_with_inconsistent_project_is_rejected() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", project="nervous-bus", status="started")
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", project="deer-flow", status="started")

    with pytest.raises(MalformedRecordError, match="disagrees on project"):
        summarize([r1, r2])


def test_execution_with_inconsistent_corpus_is_rejected() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", corpus_sha256="a" * 64, status="started")
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", corpus_sha256="b" * 64, status="started")

    with pytest.raises(MalformedRecordError, match="disagrees on corpus_sha256"):
        summarize([r1, r2])


def test_execution_with_inconsistent_condition_is_rejected() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", condition="baseline", status="started")
    r2 = make_record(attempt_id="a-2", execution_id="exec-1", condition="excerpts", status="started")

    with pytest.raises(MalformedRecordError, match="disagrees on condition"):
        summarize([r1, r2])


# ---------------------------------------------------------------------------
# Duplicated rows: identical collapse, conflicting raise
# ---------------------------------------------------------------------------


def test_identical_duplicate_rows_collapse() -> None:
    record = make_record(status="started")
    duplicate = copy.deepcopy(record)

    report = summarize([record, duplicate])

    assert report["attempts"] == 1


def test_conflicting_duplicate_raises() -> None:
    record = make_record(status="started")
    duplicate = copy.deepcopy(record)
    duplicate["status"] = "failed"

    with pytest.raises(ConflictingDuplicateError, match="reappears with changed content"):
        summarize([record, duplicate])


# ---------------------------------------------------------------------------
# Receipt mismatches
# ---------------------------------------------------------------------------


def test_receipt_with_wrong_attempt_id_is_rejected() -> None:
    receipt = make_receipt(attempt_id="attempt-different", event_id="event-1", receipt_id="receipt-1")
    record = make_record(attempt_id="attempt-1", status="completed", receipt=receipt)

    with pytest.raises(MalformedRecordError, match="receipt attempt_id does not match"):
        summarize([record])


def test_receipt_with_wrong_execution_id_is_rejected() -> None:
    receipt = make_receipt(execution_id="exec-different", attempt_id="attempt-1", event_id="event-1", receipt_id="receipt-1")
    record = make_record(attempt_id="attempt-1", execution_id="exec-1", status="completed", receipt=receipt)

    with pytest.raises(MalformedRecordError, match="receipt execution_id does not match"):
        summarize([record])


def test_receipt_with_wrong_bead_id_is_rejected() -> None:
    receipt = make_receipt(bead_id="bead-different", attempt_id="attempt-1", event_id="event-1", receipt_id="receipt-1")
    record = make_record(attempt_id="attempt-1", bead_id="bead-1", status="completed", receipt=receipt)

    with pytest.raises(MalformedRecordError, match="receipt bead_id does not match"):
        summarize([record])


def test_receipt_with_wrong_actual_model_is_rejected() -> None:
    receipt = make_receipt(model="gpt-5-mini", attempt_id="attempt-1", event_id="event-1", receipt_id="receipt-1")
    record = make_record(attempt_id="attempt-1", model="gpt-5", status="completed", receipt=receipt)

    with pytest.raises(MalformedRecordError, match="actual_execution.model does not match"):
        summarize([record])


def test_runtime_complete_receipt_paired_with_failed_status_is_rejected() -> None:
    receipt = make_receipt()
    record = make_record(status="failed", receipt=receipt)

    with pytest.raises(MalformedRecordError, match="runtime_complete receipt is paired"):
        summarize([record])


# ---------------------------------------------------------------------------
# Invalid numeric and identity values
# ---------------------------------------------------------------------------


def test_bool_cost_metric_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["input_tokens"] = True  # type: ignore[assignment]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative integer"):
        summarize([record])


def test_negative_cost_metric_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = -0.01
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative"):
        summarize([record])


def test_nan_cost_metric_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = float("nan")
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative"):
        summarize([record])


def test_infinite_cost_metric_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = float("inf")
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative"):
        summarize([record])


def test_unknown_condition_is_rejected() -> None:
    record = make_record(condition="nonsense")

    with pytest.raises(MalformedRecordError, match="condition must be one of"):
        summarize([record])


def test_unknown_status_is_rejected() -> None:
    record = make_record(status="unknown-state")

    with pytest.raises(MalformedRecordError, match="status must be one of"):
        summarize([record])


def test_invalid_repository_sha_is_rejected() -> None:
    identities = _identities_minimal()
    identities["repository_sha"] = "deadbeef"  # too short
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="repository_sha must be 40 or 64"):
        summarize([record])


def test_uppercase_repository_sha_is_rejected() -> None:
    identities = _identities_minimal()
    identities["repository_sha"] = ("A" * 40)
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="repository_sha must be 40 or 64"):
        summarize([record])


def test_all_zero_traceparent_is_rejected() -> None:
    identities = _identities_minimal()
    identities["traceparent"] = "00-" + ("0" * 32) + "-" + ("0" * 16) + "-01"
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="traceparent trace-id must not be all-zero"):
        summarize([record])


def test_uppercase_traceparent_is_rejected() -> None:
    identities = _identities_minimal()
    # uppercase version of the canonical traceparent
    identities["traceparent"] = TRACEPARENT.upper()
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="traceparent must be W3C v00"):
        summarize([record])


def test_invalid_traceparent_version_is_rejected() -> None:
    identities = _identities_minimal()
    identities["traceparent"] = "ff-" + ("0" * 31) + "1" + "-" + ("0" * 15) + "1" + "-01"
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="traceparent version must be 00"):
        summarize([record])


def test_unknown_identity_field_is_rejected() -> None:
    identities = _identities_minimal()
    identities["rogue_field"] = "x"  # type: ignore[assignment]
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="identities has unknown fields"):
        summarize([record])


def test_uppercase_corpus_sha256_is_rejected() -> None:
    record = make_record(corpus_sha256=("A" * 64))

    with pytest.raises(MalformedRecordError, match="corpus_sha256 must be 64 lowercase hex"):
        summarize([record])


# ---------------------------------------------------------------------------
# Group isolation
# ---------------------------------------------------------------------------


def test_groups_bucket_attempts_by_project_model_condition_corpus() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", project="nervous-bus", model="gpt-5", condition="baseline")
    r2 = make_record(attempt_id="a-2", execution_id="exec-2", project="nervous-bus", model="gpt-5", condition="baseline")
    r3 = make_record(attempt_id="a-3", execution_id="exec-3", project="nervous-bus", model="gpt-5-mini", condition="baseline")
    r4 = make_record(attempt_id="a-4", execution_id="exec-4", project="nervous-bus", model="gpt-5", condition="excerpts")
    r5 = make_record(attempt_id="a-5", execution_id="exec-5", project="deer-flow", model="gpt-5", condition="baseline")

    report = summarize([r1, r2, r3, r4, r5])

    groups = {(g["project"], g["model"], g["condition"]): g for g in report["groups"]}

    assert groups[("nervous-bus", "gpt-5", "baseline")]["attempts"] == 2
    assert groups[("nervous-bus", "gpt-5-mini", "baseline")]["attempts"] == 1
    assert groups[("nervous-bus", "gpt-5", "excerpts")]["attempts"] == 1
    assert groups[("deer-flow", "gpt-5", "baseline")]["attempts"] == 1

    # Each group's verified_attempts must be independent.
    assert groups[("nervous-bus", "gpt-5", "baseline")]["verified_attempts"] == 0


def test_groups_split_by_corpus_sha256() -> None:
    r1 = make_record(attempt_id="a-1", execution_id="exec-1", corpus_sha256="a" * 64)
    r2 = make_record(attempt_id="a-2", execution_id="exec-2", corpus_sha256="b" * 64)

    report = summarize([r1, r2])

    assert len(report["groups"]) == 2
    corpora = {g["corpus_sha256"] for g in report["groups"]}
    assert corpora == {"a" * 64, "b" * 64}


def test_group_verified_attempts_is_independent_of_other_groups() -> None:
    # One group has a verified attempt; the other does not.
    verified_record = make_record(
        attempt_id="a-verified",
        execution_id="exec-verified",
        bead_id="bead-verified",
        model="gpt-5",
        status="completed",
        receipt=make_receipt(
            attempt_id="a-verified",
            event_id="event-1",
            receipt_id="receipt-1",
            execution_id="exec-verified",
            bead_id="bead-verified",
            model="gpt-5",
        ),
    )
    plain_record = make_record(attempt_id="a-plain", execution_id="exec-plain", model="gpt-5-mini", status="started")

    report = summarize([verified_record, plain_record])

    groups_by_model = {g["model"]: g for g in report["groups"]}
    assert groups_by_model["gpt-5"]["verified_attempts"] == 1
    assert groups_by_model["gpt-5-mini"]["verified_attempts"] == 0


# ---------------------------------------------------------------------------
# Empty corpus
# ---------------------------------------------------------------------------


def test_empty_input_yields_zero_counts_and_null_rate() -> None:
    report = summarize([])

    assert report["attempts"] == 0
    assert report["executions"] == 0
    assert report["verified_attempts"] == 0
    assert report["verified_executions"] == 0
    assert report["verified_completion_rate"] is None
    assert sum(report["status_counts"].values()) == 0
    assert report["groups"] == []
    assert all(report["missing_identity_counts"][field] == 0 for field in report["missing_identity_counts"])


# ---------------------------------------------------------------------------
# Unicode JSONL
# ---------------------------------------------------------------------------


def test_unicode_identities_survive_round_trip() -> None:
    identities = _identities_minimal()
    identities["kb_entry_id"] = "知识条目-Ω-🐉"
    identities["provider_session_id"] = "café-セッション-01"
    record = make_record(attempt_id="a-uni", identities=identities)

    report = summarize([record])

    assert report["attempts"] == 1
    assert report["missing_identity_counts"]["kb_entry_id"] == 0
    assert report["missing_identity_counts"]["provider_session_id"] == 0


def test_unicode_project_name_is_preserved_in_groups() -> None:
    record = make_record(attempt_id="a-uni", project="プロジェクト-α")

    report = summarize([record])

    assert report["groups"][0]["project"] == "プロジェクト-α"


def test_blank_lines_in_ledger_are_rejected() -> None:
    # summarise() consumes already-parsed records, so blank lines must
    # be detected at the CLI boundary (see subprocess tests below). The
    # Python API still rejects records that are not dicts.
    with pytest.raises((MalformedRecordError, TypeError)):
        summarize([""])


# ---------------------------------------------------------------------------
# Missing identities are counted but do not crash
# ---------------------------------------------------------------------------


def test_missing_identities_are_counted_and_preserved_as_null() -> None:
    identities = _identities_minimal()
    # Only repository_sha is set; everything else remains null.
    identities["repository_sha"] = REPO_SHA_SHORT
    record = make_record(identities=identities)

    report = summarize([record])

    assert report["missing_identity_counts"]["kb_entry_id"] == 1
    assert report["missing_identity_counts"]["repository_sha"] == 0


# ---------------------------------------------------------------------------
# Subprocess CLI tests
# ---------------------------------------------------------------------------


def _run_cli(ledger_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOLS_DIR / "memory_evaluation.py"), str(ledger_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help_prints_usage(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "memory_evaluation.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "LEDGER" in result.stdout or "ledger" in result.stdout


def test_cli_reports_invalid_line_nonzero(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps(make_record(attempt_id="a-1")),
                "{not json",
                json.dumps(make_record(attempt_id="a-3")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert "invalid JSON" in result.stderr or "line 2" in result.stderr
    # No partial success — stdout must not contain a valid JSON summary.
    assert not result.stdout.strip().startswith("{")


def test_cli_reports_blank_line_nonzero(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(make_record(attempt_id="a-1")) + "\n\n",
        encoding="utf-8",
    )

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert "blank line" in result.stderr


def test_cli_reports_validation_error_nonzero(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    bad = make_record()
    bad["status"] = "bogus"
    ledger.write_text(json.dumps(bad) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert "status" in result.stderr


def test_cli_reports_missing_file_nonzero(tmp_path: Path) -> None:
    result = _run_cli(tmp_path / "does-not-exist.jsonl")

    assert result.returncode != 0
    assert result.stderr  # non-empty error


def test_cli_writes_valid_json_summary_on_success(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    record = make_record(status="completed", receipt=make_receipt())
    ledger.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["attempts"] == 1
    assert payload["verified_attempts"] == 1


def test_cli_handles_unicode_round_trip(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    record = make_record(attempt_id="a-uni", project="プロジェクト-α")
    ledger.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["groups"][0]["project"] == "プロジェクト-α"


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_summarize_exports_are_stable() -> None:
    # The contract names these explicitly. Pin them so a rename breaks
    # loudly here rather than in downstream consumers.
    assert callable(memory_evaluation.summarize)
    assert issubclass(MalformedRecordError, ValueError)
    assert issubclass(ConflictingDuplicateError, ValueError)


# ---------------------------------------------------------------------------
# Anchored-pattern regressions
#
# ``re.match(r"^...$", value)`` also matches a value with one trailing
# newline, so every hex/trace pattern must be a ``fullmatch``. A ledger
# writer that forgets to strip line endings would otherwise register a
# corpus that no other tool can reproduce.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\t"])
def test_corpus_sha256_with_trailing_whitespace_is_rejected(suffix: str) -> None:
    record = make_record(corpus_sha256="a" * 64 + suffix)

    with pytest.raises(MalformedRecordError, match="corpus_sha256 must be 64 lowercase hex"):
        summarize([record])


def test_corpus_sha256_with_leading_newline_is_rejected() -> None:
    record = make_record(corpus_sha256="\n" + "a" * 64)

    with pytest.raises(MalformedRecordError, match="corpus_sha256 must be 64 lowercase hex"):
        summarize([record])


def test_overlong_corpus_sha256_is_rejected() -> None:
    record = make_record(corpus_sha256="a" * 65)

    with pytest.raises(MalformedRecordError, match="corpus_sha256 must be 64 lowercase hex"):
        summarize([record])


@pytest.mark.parametrize("value", ["a" * 40 + "\n", "b" * 64 + "\n", "\n" + "a" * 40])
def test_repository_sha_with_stray_newline_is_rejected(value: str) -> None:
    identities = _identities_minimal()
    identities["repository_sha"] = value
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="repository_sha must be 40 or 64"):
        summarize([record])


def test_traceparent_with_trailing_newline_is_rejected() -> None:
    identities = _identities_minimal()
    identities["traceparent"] = TRACEPARENT + "\n"
    record = make_record(identities=identities)

    with pytest.raises(MalformedRecordError, match="traceparent must be W3C v00"):
        summarize([record])


# ---------------------------------------------------------------------------
# A missing cost metric is malformed, never an implicit unknown
# ---------------------------------------------------------------------------


COST_METRIC_NAMES = (
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)
COST_PHASE_NAMES = (
    "construction",
    "research",
    "retrieval",
    "execution",
    "retry",
    "review",
)


@pytest.mark.parametrize("metric", COST_METRIC_NAMES)
def test_absent_cost_metric_is_rejected_not_treated_as_unknown(metric: str) -> None:
    costs = _zero_costs()
    del costs["research"][metric]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="costs.research missing required metrics"):
        summarize([record])


@pytest.mark.parametrize("phase", COST_PHASE_NAMES)
def test_absent_cost_metric_is_rejected_in_every_phase(phase: str) -> None:
    costs = _zero_costs()
    del costs[phase]["cost_usd"]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match=f"costs.{phase} missing required metrics"):
        summarize([record])


def test_explicit_null_metric_is_still_accepted_as_unknown() -> None:
    # The rejection above must be about *absence*, not about unknown-ness:
    # an explicit null remains the supported way to say "not measured".
    costs = _zero_costs()
    costs["research"]["cost_usd"] = None
    record = make_record(costs=costs)

    report = summarize([record])

    assert report["costs_by_phase"]["research"]["cost_usd"]["unknown_count"] == 1
    assert report["costs_by_phase"]["research"]["cost_usd"]["total"] is None


def test_empty_phase_object_is_rejected() -> None:
    costs = _zero_costs()
    costs["review"] = {}
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="costs.review missing required metrics"):
        summarize([record])


# ---------------------------------------------------------------------------
# Receipt / event identity reuse must not inflate verified counts
# ---------------------------------------------------------------------------


def _verified_pair(receipt_id_a: str, event_id_a: str, receipt_id_b: str, event_id_b: str):
    """Two completed attempts on distinct executions, each with a receipt."""

    first = make_record(
        attempt_id="a-1",
        execution_id="exec-1",
        status="completed",
        receipt=make_receipt(
            attempt_id="a-1", execution_id="exec-1", receipt_id=receipt_id_a, event_id=event_id_a
        ),
    )
    second = make_record(
        attempt_id="a-2",
        execution_id="exec-2",
        status="completed",
        receipt=make_receipt(
            attempt_id="a-2", execution_id="exec-2", receipt_id=receipt_id_b, event_id=event_id_b
        ),
    )
    return first, second


def test_distinct_receipt_identities_verify_both_attempts() -> None:
    # Control for the two conflict tests below: with distinct identities
    # the same two rows are both verified.
    first, second = _verified_pair("receipt-1", "event-1", "receipt-2", "event-2")

    report = summarize([first, second])

    assert report["verified_attempts"] == 2
    assert report["verified_executions"] == 2


def test_receipt_id_reused_for_a_different_attempt_is_rejected() -> None:
    # One receipt cannot be evidence for two different execution attempts.
    # Accepting it would report two verified attempts from one measurement.
    first, second = _verified_pair("receipt-shared", "event-1", "receipt-shared", "event-2")

    with pytest.raises(memory_evaluation.ReceiptIdentityConflictError, match="receipt_id"):
        summarize([first, second])


def test_event_id_reused_for_a_different_receipt_is_rejected() -> None:
    first, second = _verified_pair("receipt-1", "event-shared", "receipt-2", "event-shared")

    with pytest.raises(memory_evaluation.ReceiptIdentityConflictError, match="event_id"):
        summarize([first, second])


def test_receipt_identity_reuse_is_rejected_across_retries_of_one_execution() -> None:
    # The same execution retried: both rows are legitimate attempts, but
    # they must not share a receipt identity.
    first = make_record(
        attempt_id="a-1",
        execution_id="exec-1",
        status="completed",
        receipt=make_receipt(attempt_id="a-1", receipt_id="receipt-shared", event_id="event-1"),
    )
    second = make_record(
        attempt_id="a-2",
        execution_id="exec-1",
        status="completed",
        receipt=make_receipt(
            attempt_id="a-2", receipt_id="receipt-shared", event_id="event-2", attempt_number=2
        ),
    )

    with pytest.raises(memory_evaluation.ReceiptIdentityConflictError):
        summarize([first, second])


def test_identical_duplicate_rows_with_receipts_do_not_trip_identity_conflict() -> None:
    # Redelivery of the identical row is a duplicate, not a conflict.
    record = make_record(status="completed", receipt=make_receipt())
    report = summarize([record, copy.deepcopy(record)])

    assert report["attempts"] == 1
    assert report["verified_attempts"] == 1


def test_receipt_identity_conflict_is_a_value_error() -> None:
    assert issubclass(memory_evaluation.ReceiptIdentityConflictError, ValueError)


# ---------------------------------------------------------------------------
# Same-execution retry: every attempt lands in the denominator
# ---------------------------------------------------------------------------


def test_timed_out_then_verified_retry_keeps_both_attempts_in_denominator() -> None:
    # The accounting question this tool exists to answer: a task that
    # succeeded on its second try cost two attempts, not one. A 1/1
    # completion rate here would be the exact failure mode the contract
    # forbids.
    first = make_record(
        attempt_id="a-1",
        execution_id="exec-1",
        status="timed_out",
        receipt=None,
    )
    second = make_record(
        attempt_id="a-2",
        execution_id="exec-1",
        status="completed",
        receipt=make_receipt(
            attempt_id="a-2",
            execution_id="exec-1",
            receipt_id="receipt-2",
            event_id="event-2",
            attempt_number=2,
        ),
    )

    report = summarize([first, second])

    assert report["attempts"] == 2
    assert report["executions"] == 1
    assert report["verified_attempts"] == 1
    assert report["verified_executions"] == 1
    assert report["verified_completion_rate"] == 0.5
    assert report["status_counts"]["timed_out"] == 1
    assert report["status_counts"]["completed"] == 1

    # The group for this execution sees both attempts too.
    (group,) = report["groups"]
    assert group["attempts"] == 2
    assert group["verified_attempts"] == 1


def test_retry_phase_overhead_is_separate_from_execution_phase() -> None:
    # The retry attempt's own execution cost belongs to `execution`; the
    # `retry` phase carries only retry-specific overhead. Both are summed
    # into the grand total, and neither is double-counted.
    first_costs = _zero_costs()
    first_costs["execution"]["duration_ms"] = 1000
    second_costs = _zero_costs()
    second_costs["execution"]["duration_ms"] = 2000
    second_costs["retry"]["duration_ms"] = 50

    first = make_record(attempt_id="a-1", execution_id="exec-1", status="timed_out", costs=first_costs)
    second = make_record(attempt_id="a-2", execution_id="exec-1", status="failed", costs=second_costs)

    report = summarize([first, second])

    assert report["costs_by_phase"]["execution"]["duration_ms"]["total"] == 3000
    assert report["costs_by_phase"]["retry"]["duration_ms"]["total"] == 50
    assert report["costs_total"]["duration_ms"]["total"] == 3050


# ---------------------------------------------------------------------------
# Non-finite and type-confused numerics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("metric", COST_METRIC_NAMES)
def test_nonfinite_value_is_rejected_for_every_metric(metric: str, bad: float) -> None:
    costs = _zero_costs()
    costs["execution"][metric] = bad  # type: ignore[assignment]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative"):
        summarize([record])


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_decimal_cost_is_rejected(bad: Decimal) -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = bad
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative finite"):
        summarize([record])


def test_negative_decimal_cost_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = Decimal("-0.01")
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative"):
        summarize([record])


def test_string_cost_is_rejected() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = "0.10"  # type: ignore[assignment]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="cost_usd must be a non-negative finite"):
        summarize([record])


def test_float_duration_is_rejected_as_a_non_integer() -> None:
    costs = _zero_costs()
    costs["execution"]["duration_ms"] = 1.5  # type: ignore[assignment]
    record = make_record(costs=costs)

    with pytest.raises(MalformedRecordError, match="must be a non-negative integer"):
        summarize([record])


def test_duplicate_differing_only_in_numeric_type_is_a_conflict() -> None:
    # 0 and 0.0 compare equal in Python but are not the same recorded
    # snapshot; collapsing them would hide a rewritten ledger row.
    record = make_record(status="started")
    duplicate = copy.deepcopy(record)
    duplicate["costs"]["execution"]["cost_usd"] = 0.0

    with pytest.raises(ConflictingDuplicateError, match="reappears with changed content"):
        summarize([record, duplicate])


def test_duplicate_differing_only_in_decimal_scale_is_a_conflict() -> None:
    record = make_record(status="started", costs=_zero_costs())
    record["costs"]["execution"]["cost_usd"] = Decimal("0.10")
    duplicate = copy.deepcopy(record)
    duplicate["costs"]["execution"]["cost_usd"] = Decimal("0.100")

    with pytest.raises(ConflictingDuplicateError, match="reappears with changed content"):
        summarize([record, duplicate])


# ---------------------------------------------------------------------------
# Group cost totals use the same unknown unit as the global totals
# ---------------------------------------------------------------------------


def test_group_cost_unknown_count_matches_global_unit() -> None:
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = None
    costs["review"]["cost_usd"] = None
    record = make_record(costs=costs)

    report = summarize([record])
    (group,) = report["groups"]

    assert group["costs_total"]["unknown_count"] == 2
    assert group["costs_total"]["unknown_count"] == report["costs_total"]["cost_usd"]["unknown_count"]
    assert group["costs_total"]["total"] is None


def test_group_cost_total_sums_all_phases_when_fully_known() -> None:
    costs = _zero_costs()
    costs["construction"]["cost_usd"] = Decimal("0.01")
    costs["execution"]["cost_usd"] = Decimal("0.02")
    record = make_record(costs=costs)

    report = summarize([record])
    (group,) = report["groups"]

    assert group["costs_total"]["known_total"] == Decimal("0.03")
    assert group["costs_total"]["total"] == Decimal("0.03")


# ---------------------------------------------------------------------------
# CLI: monetary precision, encoding, and IO failures
# ---------------------------------------------------------------------------


def test_cli_preserves_monetary_precision_across_a_sum(tmp_path: Path) -> None:
    # 0.1 + 0.2 is 0.30000000000000004 in binary floating point. Parsing
    # with Decimal and emitting decimal strings keeps the ledger honest.
    costs = _zero_costs()
    costs["execution"]["cost_usd"] = 0.1
    costs["review"]["cost_usd"] = 0.2
    record = make_record(costs=costs)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    total = payload["costs_total"]["cost_usd"]["total"]
    # Emitted as a decimal string so a consumer's JSON parser cannot
    # re-introduce the binary error we just avoided.
    assert isinstance(total, str)
    assert Decimal(total) == Decimal("0.3")


def test_cli_keeps_many_small_costs_exact(tmp_path: Path) -> None:
    records = []
    for index in range(10):
        costs = _zero_costs()
        costs["execution"]["cost_usd"] = 0.01
        records.append(
            make_record(attempt_id=f"a-{index}", execution_id=f"exec-{index}", costs=costs)
        )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert Decimal(payload["costs_total"]["cost_usd"]["total"]) == Decimal("0.10")


def test_cli_rejects_invalid_utf8_with_no_partial_stdout(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(b'{"attempt_id": "\xff\xfe"}\n')

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert "UTF-8" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_cli_rejects_nonfinite_json_literals(tmp_path: Path, token: str) -> None:
    ledger = tmp_path / "ledger.jsonl"
    raw = json.dumps(make_record())
    assert '"cost_usd": 0' in raw
    ledger.write_text(raw.replace('"cost_usd": 0', f'"cost_usd": {token}', 1) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert "non-finite" in result.stderr
    assert result.stdout == ""


def test_cli_rejects_a_directory_target(tmp_path: Path) -> None:
    result = _run_cli(tmp_path)

    assert result.returncode != 0
    assert result.stderr.strip()
    assert result.stdout == ""


def test_cli_rejects_an_unreadable_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "locked.jsonl"
    ledger.write_text(json.dumps(make_record()) + "\n", encoding="utf-8")
    ledger.chmod(0o000)
    try:
        if os.access(ledger, os.R_OK):  # running as root: the mode is not enforced
            pytest.skip("filesystem permissions are not enforced for this user")
        result = _run_cli(ledger)
    finally:
        ledger.chmod(0o600)

    assert result.returncode != 0
    assert result.stderr.strip()
    assert result.stdout == ""


def test_cli_emits_nothing_on_stdout_for_a_late_validation_failure(tmp_path: Path) -> None:
    # The failure is on the *last* line, after many valid rows. A streaming
    # writer would already have emitted a partial summary by now.
    good = [json.dumps(make_record(attempt_id=f"a-{i}", execution_id=f"exec-{i}")) for i in range(20)]
    bad = make_record(attempt_id="a-bad", execution_id="exec-bad")
    bad["corpus_sha256"] = "a" * 64 + "\n"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(good + [json.dumps(bad)]) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "corpus_sha256" in result.stderr


def test_cli_accepts_a_ledger_without_a_trailing_newline(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(make_record()), encoding="utf-8")

    result = _run_cli(ledger)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["attempts"] == 1


def test_cli_reports_promotion_not_evaluated(tmp_path: Path) -> None:
    # This tool counts attempts; it never runs a quality experiment. The
    # promotion field must stay pinned so a reader cannot mistake the
    # output for a benchmark result.
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(make_record(status="completed", receipt=make_receipt())) + "\n", encoding="utf-8")

    result = _run_cli(ledger)

    payload = json.loads(result.stdout)
    assert payload["promotion"] == "NOT_EVALUATED"
    assert payload["evidence_level"] == "recorded_receipts"
    assert payload["verified_attempts"] == 1
