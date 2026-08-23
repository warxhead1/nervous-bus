"""Contract tests for the planned kb.session.indexed.v2 migration payload."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "kb.session.indexed.v2.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=FormatChecker())


def _scoped_sync_batch() -> dict:
    return {
        "event_kind": "batch_indexed",
        "source": "kb.sync",
        "scope": "scoped",
        "project": "hearth",
        "added": 3,
        "already_known": 4,
        "trigger_session_id": "01JSCOPEDSYNC",
    }


def _unscoped_sync_batch() -> dict:
    return {
        "event_kind": "batch_indexed",
        "source": "kb.sync",
        "scope": "unscoped",
        "added": 3,
        "already_known": 4,
    }


def _bead_complete_batch() -> dict:
    return {
        "event_kind": "batch_indexed",
        "source": "watch.bead_complete",
        "scope": "scoped",
        "project": "nervous-bus",
        "added": 1,
        "entry_ids": ["9e0c4be4-96f8-47d4-a9d9-997b24931a52"],
    }


def _verified_session() -> dict:
    return {
        "event_kind": "verified_session_indexed",
        "source": "kb.ingest-sessions",
        "project": "tengine",
        "entry_id": "codex_svdag_wt645_512mb",
        "session_id": "codex_svdag_wt645_512mb",
        "silo": "svdag_wt645_512mb",
        "passed": True,
        "fps_avg": 60.0,
        "anomaly_count": 0,
        "session_dir": "/home/eric/.tengine/sessions/codex_svdag_wt645_512mb",
    }


def _session_link() -> dict:
    return {
        "event_kind": "session_linked",
        "source": "watch.session.started",
        "project": "hearth",
        "linked_session_id": "01JHEARTHSESSION",
    }


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize(
    "event_factory",
    [_scoped_sync_batch, _unscoped_sync_batch, _bead_complete_batch, _verified_session, _session_link],
    ids=["sync-scoped", "sync-unscoped", "watch-bead-complete", "ingest-passing-session", "watch-session-started"],
)
def test_every_measured_producer_class_validates_in_its_migrated_form(event_factory) -> None:
    _validator().validate(event_factory())


def test_unscoped_sync_omits_project_instead_of_emitting_null() -> None:
    event = _unscoped_sync_batch()
    _validator().validate(event)

    event["project"] = None
    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_scoped_sync_requires_a_project() -> None:
    event = _scoped_sync_batch()
    del event["project"]

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_unscoped_sync_rejects_project_even_when_its_value_is_a_string() -> None:
    event = _unscoped_sync_batch()
    event["project"] = "hearth"

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_batch_cannot_masquerade_as_a_verified_session() -> None:
    event = _scoped_sync_batch()
    event.update(
        {
            "event_kind": "verified_session_indexed",
            "entry_id": "session-1",
            "session_id": "session-1",
            "silo": "svdag",
            "passed": True,
            "fps_avg": 60.0,
            "anomaly_count": 0,
            "session_dir": "/tmp/session-1",
        }
    )

    with pytest.raises(ValidationError):
        _validator().validate(event)


@pytest.mark.parametrize(
    ("event_factory", "required_field"),
    [
        (_scoped_sync_batch, "added"),
        (_unscoped_sync_batch, "already_known"),
        (_bead_complete_batch, "entry_ids"),
        (_verified_session, "passed"),
        (_session_link, "linked_session_id"),
    ],
)
def test_each_variant_rejects_a_missing_required_field(event_factory, required_field: str) -> None:
    event = event_factory()
    del event[required_field]

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_unknown_discriminator_is_rejected() -> None:
    event = _verified_session()
    event["event_kind"] = "batch_or_session"

    with pytest.raises(ValidationError):
        _validator().validate(event)


@pytest.mark.parametrize(
    "event_factory",
    [_scoped_sync_batch, _unscoped_sync_batch, _bead_complete_batch, _verified_session, _session_link],
)
def test_each_variant_rejects_extra_fields(event_factory) -> None:
    event = event_factory()
    event["unexpected"] = "schema drift"

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_verified_session_requires_the_passing_producer_invariant() -> None:
    event = _verified_session()
    event["passed"] = False

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_session_link_cannot_be_mistaken_for_verification_evidence() -> None:
    event = _session_link()
    event["silo"] = "svdag"

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_bead_complete_entry_ids_must_be_uuids() -> None:
    event = _bead_complete_batch()
    event["entry_ids"] = ["not-a-uuid"]

    with pytest.raises(ValidationError):
        _validator().validate(event)


def test_validation_does_not_mutate_the_producer_fixture() -> None:
    event = _verified_session()
    before = copy.deepcopy(event)
    _validator().validate(event)
    assert event == before
