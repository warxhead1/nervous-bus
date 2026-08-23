"""Behavioral reference tests for bus.exec.evidence.v1, without Redis."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from exec_evidence_receipts import (  # noqa: E402
    ReceiptValidationError,
    ReferenceReceiptConsumer,
    ReferenceReceiptProducer,
    UnsupportedReceiptVersion,
)


def receipt_fixture() -> dict:
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
            "requested_execution": {"provider": "openai", "model": "gpt-5.6-terra", "backend": "codex"},
            "actual_execution": {"provider": "openai", "model": "gpt-5.6-terra", "backend": "codex"},
            "git_revisions": {
                "source_sha": "a" * 40,
                "main_sha": "b" * 40,
                "integration_sha": "c" * 40,
                "worktree_sha": "d" * 40,
            },
            "command": {"argv": ["pytest", "-q"], "rendered": "pytest -q", "working_directory": "/worktree"},
            "exit_code": 0,
            "completion_state": "runtime_complete",
            "completed_at": "2026-08-23T12:01:00Z",
            "artifacts": [],
            "denominators": {"tests_matched": {"status": "known", "value": 1}},
            "gates": {
                "test": {"status": "passed", "evidence_id": "test-01", "evidence_kind": "test_result", "completed_at": "2026-08-23T12:00:20Z"},
                "runtime": {"status": "passed", "evidence_id": "runtime-01", "evidence_kind": "runtime_probe", "completed_at": "2026-08-23T12:00:50Z"},
                "database": {"status": "not_applicable", "not_applicable_reason": "No database path."},
                "device": {"status": "not_applicable", "not_applicable_reason": "No device path."},
            },
        },
    }


def test_completed_receipt_is_published_and_consumed_once() -> None:
    event = receipt_fixture()
    producer = ReferenceReceiptProducer()
    consumer = ReferenceReceiptConsumer()

    produced = producer.publish(event)
    result = consumer.consume(event)

    assert produced.is_runtime_complete
    assert result.status == "accepted"
    assert result.receipt.is_runtime_complete
    assert consumer.accepted_receipt_count == 1


def test_known_zero_tests_matched_fails_closed() -> None:
    event = receipt_fixture()
    event["data"]["denominators"]["tests_matched"]["value"] = 0

    with pytest.raises(ReceiptValidationError):
        ReferenceReceiptProducer().publish(event)


def test_unknown_tests_matched_cannot_support_a_passed_runtime_complete_gate() -> None:
    event = receipt_fixture()
    event["data"]["denominators"]["tests_matched"] = {"status": "unknown", "value": None}

    with pytest.raises(ReceiptValidationError):
        ReferenceReceiptProducer().publish(event)


def test_partial_receipt_preserves_unavailable_evidence_without_completion() -> None:
    event = receipt_fixture()
    event["data"].update({"completion_state": "partial", "exit_code": None, "completed_at": None, "artifacts": []})
    event["data"]["gates"]["runtime"] = {
        "status": "unavailable",
        "unavailable_reason": "Process has not reached a probeable runtime state.",
    }

    receipt = ReferenceReceiptProducer().publish(event)

    assert not receipt.is_runtime_complete


@pytest.mark.parametrize("state", ["started", "background", "static_contract", "source_accepted"])
def test_progress_and_static_states_never_become_runtime_complete(state: str) -> None:
    event = receipt_fixture()
    event["data"]["completion_state"] = state
    event["data"]["exit_code"] = None
    event["data"]["completed_at"] = None
    event["data"]["gates"]["runtime"] = {"status": "unknown", "unavailable_reason": "No runtime probe completed."}

    receipt = ReferenceReceiptProducer().publish(event)

    assert not receipt.is_runtime_complete


def test_same_event_delivery_is_idempotent() -> None:
    event = receipt_fixture()
    consumer = ReferenceReceiptConsumer()

    assert consumer.consume(event).status == "accepted"
    assert consumer.consume(copy.deepcopy(event)).status == "duplicate_event"
    assert consumer.accepted_receipt_count == 1


def test_redelivery_with_a_new_event_id_deduplicates_by_receipt() -> None:
    event = receipt_fixture()
    redelivery = copy.deepcopy(event)
    redelivery["id"] = redelivery["data"]["event_id"] = "evt-02"
    consumer = ReferenceReceiptConsumer()

    assert consumer.consume(event).status == "accepted"
    assert consumer.consume(redelivery).status == "duplicate_receipt"
    assert consumer.accepted_receipt_count == 1


def test_next_attempt_is_not_deduplicated_as_the_prior_attempt() -> None:
    first = receipt_fixture()
    retry = copy.deepcopy(first)
    retry["id"] = retry["data"]["event_id"] = "evt-02"
    retry["data"].update({"receipt_id": "receipt-02", "attempt_id": "attempt-02", "attempt_number": 2})
    retry["data"]["prior_attempt_receipt_ids"] = ["receipt-01"]
    consumer = ReferenceReceiptConsumer()

    assert consumer.consume(first).status == "accepted"
    assert consumer.consume(retry).status == "accepted"
    assert consumer.accepted_receipt_count == 2


def test_unsupported_future_major_is_rejected_explicitly() -> None:
    event = receipt_fixture()
    event["data"]["schema_version"] = "2"

    with pytest.raises(UnsupportedReceiptVersion, match="unsupported bus.exec.evidence major"):
        ReferenceReceiptProducer().publish(event)


def test_envelope_and_data_event_ids_must_agree() -> None:
    event = receipt_fixture()
    event["data"]["event_id"] = "evt-different"

    with pytest.raises(ReceiptValidationError, match="envelope id must equal"):
        ReferenceReceiptProducer().publish(event)
