"""Contract tests for Deer Flow's enrichment-feedback bus events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _schema(channel: str) -> dict:
    return json.loads((SCHEMAS / f"{channel}.json").read_text())


@pytest.mark.parametrize(
    ("channel", "payload"),
    [
        (
            "deer-flow.bead.reenrichment_hint.v1",
            {
                "bead_id": "deer-flow-ltfs",
                "event_type": "container_died",
                "reason": "container died (exit_code=137)",
            },
        ),
        (
            "deer-flow.enrichment_feedback.error.v1",
            {
                "consumer": "enrichment-feedback-consumer",
                "event_channel": "bus.bead.lifecycle.v1",
                "subject": None,
                "error_type": "missing_bead_id",
                "message": "bead_failed event without a bead_id",
            },
        ),
        (
            "deer-flow.design_request.error.v1",
            {
                "entity_key": "deer-flow-ltfs",
                "error_type": "parse_error",
                "message": "invalid request",
                "request_event_id": "evt-123",
            },
        ),
        (
            "deer-flow.trading_research.error.v1",
            {
                "ticker": "SPY",
                "error_type": "RuntimeError",
                "message": "pipeline failed",
                "run_id": "run-123",
            },
        ),
    ],
)
def test_emitted_payload_is_valid_draft_2020_12(channel: str, payload: dict) -> None:
    schema = _schema(channel)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


def test_reenrichment_hint_rejects_unknown_failure_type() -> None:
    validator = Draft202012Validator(_schema("deer-flow.bead.reenrichment_hint.v1"))
    with pytest.raises(ValidationError):
        validator.validate(
            {"bead_id": "deer-flow-ltfs", "event_type": "unknown", "reason": "x"}
        )


def test_enrichment_feedback_error_requires_consumer_error_shape() -> None:
    validator = Draft202012Validator(_schema("deer-flow.enrichment_feedback.error.v1"))
    with pytest.raises(ValidationError):
        validator.validate({"consumer": "enrichment-feedback-consumer", "message": "x"})
