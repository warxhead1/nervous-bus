"""Strict migration contract tests for ``kb.entry.created.v2``.

These are normalized migration fixtures, not claims that v2 is already on the
bus. They cover every measured current v1 emitter class and show the fields
the companion KB migration must add or normalize.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "kb.entry.created.v2.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=FormatChecker())


def _event(producer: str, source_type: str, category: str, confidence: dict) -> dict:
    return {
        "entry_id": "9e0c4be4-96f8-47d4-a9d9-997b24931a52",
        "title": "Normalized KB entry",
        "project": "hearth",
        "category": category,
        "source_type": source_type,
        "provenance": {
            "producer": producer,
            "source_kind": "derived",
            "source_uri": "kb://migration/current-producer",
        },
        "confidence": confidence,
        "occurred_at": "2026-08-23T14:30:00Z",
        "file": "/tmp/vault/hearth/normalized-kb-entry.md",
        "tags": ["migration"],
    }


def _inferred_confidence() -> dict:
    return {
        "value": 0.7,
        "basis": "inferred",
        "evidence_uri": "deer-flow://cycle/research-42",
        "rationale": "Derived from the source record during entry creation.",
    }


def _default_confidence() -> dict:
    return {"value": 0.6, "basis": "default", "default_policy": "new-entry-fallback"}


def _measured_confidence() -> dict:
    return {
        "value": 1.0,
        "basis": "measured",
        "evidence_uri": "test-result://run/42",
        "method": "Recorded passing test result.",
    }


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize(
    ("producer", "source_type", "category", "confidence"),
    [
        # graphify.rs: id -> entry_id; title, confidence, provenance, timestamp added.
        ("kb.graphify", "codemap-derived", "reference", _inferred_confidence()),
        # ingest.rs: empty optional frontmatter values must be normalized before v2 emission.
        ("kb.ingest", "manual", "concept", _default_confidence()),
        # watch.rs deer-flow authored entry.
        ("kb.watch.deer-flow-authored", "deer-flow-research", "research", _inferred_confidence()),
        # watch.rs application-submitted entry.
        ("kb.watch.application-submitted", "bus-event", "log", _default_confidence()),
        # kb_ops/entry.rs programmatic create.
        ("kb.entry.create", "manual", "decision", _measured_confidence()),
        # landmark.rs writer.
        ("kb.landmark", "agent-generated", "log", _inferred_confidence()),
    ],
)
def test_every_measured_current_producer_class_has_a_valid_normalized_fixture(
    producer: str, source_type: str, category: str, confidence: dict
) -> None:
    _validator().validate(_event(producer, source_type, category, confidence))


@pytest.mark.parametrize("confidence", [_inferred_confidence(), _default_confidence(), _measured_confidence()])
def test_confidence_basis_variants_validate(confidence: dict) -> None:
    _validator().validate(_event("kb.entry.create", "manual", "concept", confidence))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda event: event.update({"id": event.pop("entry_id")}), "legacy graphify id is not entry_id"),
        (lambda event: event.pop("title"), "identity title is required"),
        (lambda event: event.update({"category": ""}), "empty legacy category is not canonical"),
        (lambda event: event.update({"trace_uri": ""}), "empty optional strings must be omitted"),
        (lambda event: event.update({"confidence": 0.6}), "scalar confidence has no basis"),
        (lambda event: event["confidence"].pop("rationale"), "inferred confidence needs rationale"),
        (lambda event: event.update({"extra": "legacy spillover"}), "extra legacy fields are rejected"),
    ],
)
def test_legacy_or_malformed_shapes_are_rejected(mutation, reason: str) -> None:
    event = _event("kb.graphify", "codemap-derived", "reference", _inferred_confidence())
    mutation(event)
    with pytest.raises(ValidationError, match=".*"):
        _validator().validate(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_id", "not-a-uuid"),
        ("occurred_at", "not-a-timestamp"),
        ("title", 7),
        ("project", ""),
        ("category", "application"),
        ("source_type", "unknown-source"),
        ("file", ""),
    ],
)
def test_identifier_timestamp_type_enum_and_required_value_guards(field: str, value: object) -> None:
    event = _event("kb.entry.create", "manual", "concept", _default_confidence())
    event[field] = value
    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_missing_required_provenance_field_fails() -> None:
    event = _event("kb.entry.create", "manual", "concept", _default_confidence())
    del event["provenance"]["source_uri"]
    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_additive_content_hash_uses_the_canonical_hash_shape() -> None:
    event = copy.deepcopy(_event("kb.entry.create", "manual", "concept", _default_confidence()))
    event["content_hash"] = "0123456789abcdef"
    _validator().validate(event)
    event["content_hash"] = "legacy-hash"
    with pytest.raises(ValidationError):
        _validator().validate(event)
