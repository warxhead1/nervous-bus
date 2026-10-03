"""Schema tests for resource refs on bus.agent.activity.v1 and bus.work.assignment.v1.

The activity additions are optional and additive: an event that predates them
must still validate, and an event carrying them must be constrained.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def activity() -> Draft202012Validator:
    return _validator("bus.agent.activity.v1.json")


@pytest.fixture(scope="module")
def assignment() -> Draft202012Validator:
    return _validator("bus.work.assignment.v1.json")


BASE_ACTIVITY = {
    "ts": "2026-10-03T12:00:00Z",
    "event": "tool_call",
    "agent_kind": "host_claude_code",
    "session_id": "s1",
    "project": "app",
}

BASE_ASSIGNMENT = {
    "ts": "2026-10-03T12:00:00Z",
    "assignment_id": "toolu_1",
    "worker": {"kind": "claude-subagent", "id": "toolu_1"},
    "title": "do the thing",
    "assigned_by": "s1",
    "scope": None,
}


def test_activity_without_new_fields_still_valid(activity):
    activity.validate(BASE_ACTIVITY)


def test_activity_with_resources_progress_and_assignment_id(activity):
    activity.validate(
        {
            **BASE_ACTIVITY,
            "resources": [
                {"ref": "file:/srv/app/src/main.go", "intent": "write"},
                {"ref": "repo:app/src/main.go", "intent": "write", "sha256": "a" * 64},
                {"ref": "table:shop.public.orders", "intent": "query"},
            ],
            "assignment_id": "toolu_1",
            "progress": {"phase": "testing", "done": 3, "total": 9, "unit": "modules"},
        }
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"resources": [{"ref": "file:/x", "intent": "delete"}]},  # unknown intent
        {"resources": [{"ref": "no-kind-prefix", "intent": "read"}]},
        {"resources": [{"intent": "read"}]},  # missing ref
        {"resources": [{"ref": "file:/x", "intent": "read", "sha256": "short"}]},
        {"resources": "file:/x"},  # not an array
        {"progress": {"done": -1}},
    ],
)
def test_activity_rejects_malformed_additions(activity, bad):
    with pytest.raises(Exception):
        activity.validate({**BASE_ACTIVITY, **bad})


def test_assignment_minimal_and_null_scope_valid(assignment):
    assignment.validate(BASE_ASSIGNMENT)


def test_assignment_full_valid(assignment):
    assignment.validate(
        {
            **BASE_ASSIGNMENT,
            "event": "assigned",
            "refs": ["issue:app#42", "bead:bd-ab12"],
            "scope": {"write": ["repo:app/src/**"], "read_hint": ["repo:app/docs/**"]},
            "expected": {"duration_s": 600, "deliverable": "PR"},
        }
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": {}},  # empty object is neither null nor a declared scope
        {"scope": {"write": []}},
        {"scope": {"write": ["a"], "exec": ["b"]}},
        {"scope": "write=a"},
        {"worker": {"kind": "claude-subagent"}},
        {"assignment_id": ""},
        {"refs": ["not a ref"]},
    ],
)
def test_assignment_rejects_malformed(assignment, bad):
    with pytest.raises(Exception):
        assignment.validate({**BASE_ASSIGNMENT, **bad})


@pytest.mark.parametrize("field", ["ts", "assignment_id", "worker", "title", "assigned_by"])
def test_assignment_required_fields(assignment, field):
    doc = dict(BASE_ASSIGNMENT)
    del doc[field]
    with pytest.raises(Exception):
        assignment.validate(doc)


def test_channels_md_documents_resource_refs():
    text = (SCHEMA_DIR / "CHANNELS.md").read_text()
    assert "## Resource refs" in text
    assert "`bus.work.assignment.v1`" in text
