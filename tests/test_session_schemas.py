"""Schema tests for the agent.session.{heartbeat,linked,snapshot}.v1 schemas
(nervous-bus-ql7g, Phase 1.1 of the orchestrator session design).

Each schema gets:
* minimal-valid event passes Draft202012Validator
* fully-populated event passes
* missing-required-field event fails

For agent.session.linked.v1 additionally:
* spawned_by: null (human-initiated) passes
* spawned_by: "<session_id>" (sibling-helped spawn) passes

Schema-layer enforcement only — chain-terminates-at-null is a publisher-layer
concern (the chain walker lives in the SDK, not in the JSON schema).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schemas"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


@pytest.fixture(scope="module")
def heartbeat_validator() -> Draft202012Validator:
    schema = _load("agent.session.heartbeat.v1.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def linked_validator() -> Draft202012Validator:
    schema = _load("agent.session.linked.v1.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def snapshot_validator() -> Draft202012Validator:
    schema = _load("agent.session.snapshot.v1.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# ---------------------------------------------------------------- heartbeat


def test_heartbeat_minimal_valid(heartbeat_validator: Draft202012Validator) -> None:
    event = {
        "session_id": "01HXYZABCDEFG",
        "ts": "2026-05-16T12:00:00Z",
        "healthy": True,
        "agent_type": "claude-code",
    }
    heartbeat_validator.validate(event)


def test_heartbeat_fully_populated(heartbeat_validator: Draft202012Validator) -> None:
    event = {
        "pane_id_qualified": "zellij:nervous-bus:5",
        "session_id": "01HXYZABCDEFG",
        "ts": "2026-05-16T12:00:00Z",
        "healthy": True,
        "agent_type": "claude-code",
        "focus_bead": "nervous-bus-ql7g",
        "current_phase": "implementing",
        "subagents_in_flight": 2,
        "recent_tool_calls": ["Read", "Edit", "Bash", "Write", "Bash"],
        "context_percent": 42.5,
        "parent_main_session_id": "01HMAINABC",
    }
    heartbeat_validator.validate(event)


def test_heartbeat_missing_required_fails(heartbeat_validator: Draft202012Validator) -> None:
    event = {
        "session_id": "01HXYZABCDEFG",
        "ts": "2026-05-16T12:00:00Z",
        # missing healthy + agent_type
    }
    with pytest.raises(ValidationError):
        heartbeat_validator.validate(event)


# ---------------------------------------------------------------- linked


def test_linked_minimal_valid(linked_validator: Draft202012Validator) -> None:
    event = {
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "peer",
        "mode": "linked-peer",
        "linked_at": "2026-05-16T12:00:00Z",
    }
    linked_validator.validate(event)


def test_linked_fully_populated(linked_validator: Draft202012Validator) -> None:
    event = {
        "pane_id_qualified": "zellij:tengine:7",
        "session_id": "01HSIBLINGZ",
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "council_member",
        "mode": "council-member",
        "linked_at": "2026-05-16T12:00:00Z",
        "spawned_by": "01HMAINABC",
        "epic_id": "planet-X-launch",
        "project_pair": "nervous-bus<->tengine",
    }
    linked_validator.validate(event)


def test_linked_missing_required_fails(linked_validator: Draft202012Validator) -> None:
    event = {
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "peer",
        # missing mode + linked_at
    }
    with pytest.raises(ValidationError):
        linked_validator.validate(event)


def test_linked_spawned_by_null_passes(linked_validator: Draft202012Validator) -> None:
    """Human-initiated spawn: spawned_by is null (chain terminator)."""
    event = {
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "peer",
        "mode": "linked-peer",
        "linked_at": "2026-05-16T12:00:00Z",
        "spawned_by": None,
    }
    linked_validator.validate(event)


def test_linked_spawned_by_session_id_passes(linked_validator: Draft202012Validator) -> None:
    """Sibling-helped spawn: spawned_by is another session_id (must terminate at null upstream)."""
    event = {
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "subordinate",
        "mode": "linked-subordinate",
        "linked_at": "2026-05-16T12:00:00Z",
        "spawned_by": "01HMAINABC",
    }
    linked_validator.validate(event)


def test_linked_role_enum_rejects_unknown(linked_validator: Draft202012Validator) -> None:
    event = {
        "main_session_id": "01HMAINABC",
        "sibling_session_id": "01HSIBLINGZ",
        "role": "boss",  # not in enum
        "mode": "linked-peer",
        "linked_at": "2026-05-16T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        linked_validator.validate(event)


# ---------------------------------------------------------------- snapshot


def test_snapshot_minimal_valid(snapshot_validator: Draft202012Validator) -> None:
    event = {
        "session_id": "01HXYZABCDEFG",
        "ts": "2026-05-16T12:00:00Z",
    }
    snapshot_validator.validate(event)


def test_snapshot_fully_populated(snapshot_validator: Draft202012Validator) -> None:
    event = {
        "pane_id_qualified": "zellij:nervous-bus:5",
        "session_id": "01HXYZABCDEFG",
        "ts": "2026-05-16T12:00:00Z",
        "agent_type": "claude-code",
        "project": "nervous-bus",
        "model": "claude-opus-4-7",
        "transcript_path": "/home/user/.claude/projects/foo/transcript.jsonl",
        "focus_bead": "nervous-bus-ql7g",
        "current_phase": "implementing",
        "collaboration_mode": "linked-peer",
        "parent_main_session_id": "01HMAINABC",
        "linked_siblings": ["01HSIBLING1", "01HSIBLING2"],
        "epic_id": "planet-X-launch",
        "subagents_in_flight": 1,
        "recent_tool_calls": ["Read", "Edit", "Bash"],
        "context_percent": 33.0,
        "healthy": True,
        "started_at": "2026-05-16T10:00:00Z",
    }
    snapshot_validator.validate(event)


def test_snapshot_missing_required_fails(snapshot_validator: Draft202012Validator) -> None:
    event = {
        "session_id": "01HXYZABCDEFG",
        # missing ts
    }
    with pytest.raises(ValidationError):
        snapshot_validator.validate(event)
