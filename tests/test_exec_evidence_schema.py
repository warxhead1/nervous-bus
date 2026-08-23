"""Contract tests for the append-only bus.exec.evidence.v1 receipt.

These fixtures deliberately validate the schema alone: publishing, consuming,
and receipt storage belong to later producer/consumer beads.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "bus.exec.evidence.v1.json"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def receipt_fixture() -> dict:
    """A complete runtime receipt with explicit zero and inapplicable gates."""
    return {
        "specversion": "1.0",
        "id": "evt-01",
        "source": "/hearth-loom",
        "type": "bus.exec.evidence.v1",
        "datacontenttype": "application/json",
        "time": "2026-08-23T12:00:00Z",
        "data": {
            "schema_version": "1",
            "receipt_id": "receipt-01",
            "event_id": "evt-01",
            "bead_id": "hearth-53rbk.3",
            "execution_id": "exec-01",
            "attempt_id": "attempt-01",
            "attempt_number": 1,
            "dispatch_owner": "hearth-loom",
            "requested_execution": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "backend": "codex",
                "sleeve_image": "ghcr.io/openai/codex:2026-08-23",
            },
            "actual_execution": {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "backend": "codex",
                "sleeve_image": "ghcr.io/openai/codex:2026-08-23",
            },
            "git_revisions": {
                "source_sha": "a" * 40,
                "main_sha": "b" * 40,
                "integration_sha": "c" * 40,
                "worktree_sha": "d" * 40,
            },
            "command": {
                "argv": ["python", "-m", "pytest", "-q"],
                "rendered": "python -m pytest -q",
                "working_directory": "/worktrees/hearth-53rbk.3",
            },
            "exit_code": 0,
            "completion_state": "runtime_complete",
            "completed_at": "2026-08-23T12:01:00Z",
            "artifacts": [
                {
                    "path": "artifacts/pytest.txt",
                    "sha256": "e" * 64,
                    "size_bytes": 0,
                    "mtime": "2026-08-23T12:01:00Z",
                    "media_type": "text/plain",
                }
            ],
            "denominators": {
                "tests_matched": {"status": "known", "value": 1, "basis": "selected test cases"},
                "tests_discovered": {"status": "known", "value": 0, "basis": "optional test category"},
                "database_rows": {"status": "unknown", "value": None},
                "device_checks": {"status": "not_applicable", "value": None},
            },
            "gates": {
                "test": {
                    "status": "passed",
                    "evidence_id": "test-01",
                    "evidence_kind": "test_result",
                    "completed_at": "2026-08-23T12:00:30Z",
                },
                "runtime": {
                    "status": "passed",
                    "evidence_id": "runtime-01",
                    "evidence_kind": "runtime_probe",
                    "completed_at": "2026-08-23T12:00:50Z",
                },
                "database": {
                    "status": "not_applicable",
                    "not_applicable_reason": "This schema-only bead has no database path.",
                },
                "device": {
                    "status": "not_applicable",
                    "not_applicable_reason": "This schema-only bead has no device path.",
                },
            },
        },
    }


def test_complete_runtime_receipt_passes(validator: Draft202012Validator) -> None:
    validator.validate(receipt_fixture())


@pytest.mark.parametrize(
    ("status", "value"),
    [("known", None), ("unknown", 0), ("not_applicable", 0)],
)
def test_denominator_zero_is_not_unknown_or_not_applicable(
    validator: Draft202012Validator, status: str, value: object
) -> None:
    event = receipt_fixture()
    event["data"]["denominators"]["tests_discovered"] = {"status": status, "value": value}
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_known_zero_denominator_is_valid(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["denominators"]["tests_discovered"] = {"status": "known", "value": 0}
    validator.validate(event)


@pytest.mark.parametrize("non_completion", ["started", "source_accepted", "static_contract", "background", "partial"])
def test_non_decisive_progress_states_are_representable(
    validator: Draft202012Validator, non_completion: str
) -> None:
    event = receipt_fixture()
    event["data"]["completion_state"] = non_completion
    event["data"]["gates"]["runtime"] = {
        "status": "unavailable",
        "unavailable_reason": "Process state is progress, not a completed runtime probe.",
    }
    validator.validate(event)


def test_partial_receipt_needs_no_fabricated_terminal_or_artifact_evidence(
    validator: Draft202012Validator,
) -> None:
    event = receipt_fixture()
    event["data"]["completion_state"] = "partial"
    event["data"]["exit_code"] = None
    event["data"]["completed_at"] = None
    event["data"]["artifacts"] = []
    event["data"]["gates"]["runtime"] = {
        "status": "unknown",
        "unavailable_reason": "The background process has not reached a probeable state.",
    }
    validator.validate(event)


@pytest.mark.parametrize("gate_name", ["test", "runtime", "database", "device"])
def test_runtime_complete_rejects_failed_applicable_gate(
    validator: Draft202012Validator, gate_name: str
) -> None:
    event = receipt_fixture()
    event["data"]["gates"][gate_name] = {
        "status": "failed",
        "evidence_id": f"{gate_name}-failure",
        "evidence_kind": {
            "test": "test_result",
            "runtime": "runtime_probe",
            "database": "database_query",
            "device": "device_observation",
        }[gate_name],
        "completed_at": "2026-08-23T12:00:59Z",
    }
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_runtime_complete_rejects_lifecycle_only_evidence(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["gates"]["runtime"]["evidence_kind"] = "source_accepted"
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_runtime_complete_requires_a_decisive_runtime_gate(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["gates"]["runtime"] = {
        "status": "not_applicable",
        "not_applicable_reason": "A lifecycle marker is not runtime evidence.",
    }
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_runtime_complete_rejects_known_zero_tests_matched(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["denominators"]["tests_matched"] = {"status": "known", "value": 0}
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize("field", ["exit_code", "completed_at"])
def test_runtime_complete_requires_terminal_exit_and_completion_time(
    validator: Draft202012Validator, field: str
) -> None:
    event = receipt_fixture()
    event["data"][field] = None
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_unavailable_gate_records_missing_infra_but_cannot_complete_runtime(
    validator: Draft202012Validator,
) -> None:
    event = receipt_fixture()
    event["data"]["gates"]["database"] = {
        "status": "unavailable",
        "unavailable_reason": "Database endpoint was not provisioned for this attempt.",
    }
    with pytest.raises(ValidationError):
        validator.validate(event)


def test_distinct_attempts_remain_distinguishable(validator: Draft202012Validator) -> None:
    first = receipt_fixture()
    retry = copy.deepcopy(first)
    retry["id"] = retry["data"]["event_id"] = "evt-02"
    retry["data"]["receipt_id"] = "receipt-02"
    retry["data"]["attempt_id"] = "attempt-02"
    retry["data"]["attempt_number"] = 2
    retry["data"]["prior_attempt_receipt_ids"] = ["receipt-01"]
    validator.validate(first)
    validator.validate(retry)
    assert (first["data"]["execution_id"], first["data"]["attempt_id"]) != (
        retry["data"]["execution_id"], retry["data"]["attempt_id"]
    )


def test_duplicate_receipts_are_identifiable_by_receipt_and_event_ids(
    validator: Draft202012Validator,
) -> None:
    first = receipt_fixture()
    duplicate_delivery = copy.deepcopy(first)
    duplicate_delivery["id"] = duplicate_delivery["data"]["event_id"] = "evt-redelivery-01"
    validator.validate(first)
    validator.validate(duplicate_delivery)
    by_receipt: dict[str, list[str]] = defaultdict(list)
    for event in (first, duplicate_delivery):
        by_receipt[event["data"]["receipt_id"]].append(event["data"]["event_id"])
    assert by_receipt == {"receipt-01": ["evt-01", "evt-redelivery-01"]}


def test_additive_optional_v1_field_remains_valid(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["producer_annotation"] = {"migration_window": "2026-Q3"}
    validator.validate(event)


def test_unknown_major_fixture_version_is_rejected(validator: Draft202012Validator) -> None:
    event = receipt_fixture()
    event["data"]["schema_version"] = "2"
    with pytest.raises(ValidationError):
        validator.validate(event)
