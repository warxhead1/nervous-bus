"""Contract tests for the planned kb.knowledge.gap.v2 producer migration."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "kb.knowledge.gap.v2.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=FormatChecker())


def _event(gap_type: str, *, entry_id: str | None = None) -> dict:
    event = {
        "project": "hearth",
        "question": "What evidence would resolve this knowledge gap?",
        "priority": 0.8,
        "reasoning": "Measured source path requires follow-up.",
        "gap_type": gap_type,
    }
    if entry_id is not None:
        event["entry_id"] = entry_id
    return event


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize(
    ("producer_path", "gap_type"),
    [
        ("enrich --pass gaps", "enrichment"),
        ("check --emit-gap", "agent-escalation"),
        ("overlap", "overlap"),
        ("watch scope_divergence", "scope-divergence"),
        ("watch loom.coord claim-overlap", "overlap"),
    ],
)
def test_each_measured_kb_producer_path_has_an_intended_v2_fixture(
    producer_path: str, gap_type: str
) -> None:
    event = _event(gap_type)

    _validator().validate(event)
    assert "entry_id" not in event, f"{producer_path} has no observed vault entry identity"


def test_optional_entry_identity_accepts_a_uuid_when_a_future_gap_is_entry_rooted() -> None:
    _validator().validate(_event("enrichment", entry_id="9e0c4be4-96f8-47d4-a9d9-997b24931a52"))


def test_scope_divergence_migration_rejects_legacy_null_entry_identity() -> None:
    event = _event("scope-divergence")
    event["entry_id"] = None

    with pytest.raises(ValidationError):
        _validator().validate(event)


@pytest.mark.parametrize("legacy_or_unknown_type", ["no_evidence", "low_confidence", "unknown-gap"])
def test_v1_and_unknown_gap_types_are_rejected(legacy_or_unknown_type: str) -> None:
    with pytest.raises(ValidationError):
        _validator().validate(_event(legacy_or_unknown_type))


def test_unmigrated_enrichment_payload_without_a_bounded_type_is_rejected() -> None:
    event = _event("enrichment")
    del event["gap_type"]

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_unexpected_payload_property_is_rejected() -> None:
    event = copy.deepcopy(_event("overlap"))
    event["operational_detail"] = "not part of the v2 contract"

    with pytest.raises(ValidationError):
        _validator().validate(event)
