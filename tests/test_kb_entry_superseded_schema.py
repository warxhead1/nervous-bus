"""Contract tests for the kb.entry.superseded.v1 event payload."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "kb.entry.superseded.v1.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _valid_event() -> dict:
    return {
        "entry_id": "9e0c4be4-96f8-47d4-a9d9-997b24931a52",
        "superseded_by": "external/replacement-reference",
        "reason": "The replacement has current production evidence.",
        "file": "/tmp/vault/hearth/old-entry.md",
        "ts": "2026-08-23T14:30:00Z",
    }


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=FormatChecker())


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


def test_valid_emitted_payload_passes() -> None:
    _validator().validate(_valid_event())


def test_missing_required_payload_field_fails() -> None:
    event = _valid_event()
    del event["reason"]

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_bad_timestamp_fails() -> None:
    event = _valid_event()
    event["ts"] = "not-a-timestamp"

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_malformed_entry_id_fails() -> None:
    event = _valid_event()
    event["entry_id"] = "not-a-uuid"

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_wrong_payload_type_fails() -> None:
    event = _valid_event()
    event["reason"] = 7

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_unexpected_payload_property_fails() -> None:
    event = _valid_event()
    event["extra"] = "not part of the producer contract"

    with pytest.raises(ValidationError):
        _validator().validate(event)
