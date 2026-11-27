"""Golden sample and constraint tests for bus.hearth.decision.{requested,responded}.v1.json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = REPO_ROOT / "schemas"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{name}.v1.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


REQUESTED_GOLDEN = {
    "choice": {
        "decision_id": "dec-example-001",
        "correlation_id": "dec-example-001",
        "project": "demo-app",
        "session_id": "sess-001",
        "kind": "choice",
        "relay_mode": "session_text",
        "title": "Pick a cache eviction policy",
        "say": "Pick a cache eviction policy",
        "context": "Both fit the latency budget.",
        "options": [
            {"id": "lru", "label": "LRU", "description": "Evict least recently used", "say": "L R U"},
            {"id": "lfu", "label": "LFU", "description": "Evict least frequently used", "say": "L F U"},
        ],
        "parts": [],
        "recommended": "lru",
        "default_after": "2026-11-27T00:00:00Z",
        "audience": "owner",
        "status": "pending",
        "metadata": {"door": "two-way"},
        "created_at": "2026-11-26T18:00:00Z",
    },
    "errand_empty_shape": {
        "decision_id": "ny-sess-002",
        "correlation_id": "ny-sess-002",
        "project": "demo-app",
        "session_id": "sess-002",
        "kind": "errand",
        "relay_mode": "session_text",
        "title": "Merge PR #42 after checks pass",
        "say": "Merge PR 42 after checks pass",
        "context": "- Merge PR #42 after checks pass",
        "options": [],
        "parts": [],
        "audience": "owner",
        "status": "pending",
        "metadata": {"source": "needs-you", "name": "demo-branch"},
        "created_at": "2026-11-26T18:05:00Z",
    },
    "errand_with_parts": {
        "decision_id": "ny-sess-003",
        "correlation_id": "ny-sess-003",
        "project": "demo-app",
        "session_id": "sess-003",
        "kind": "errand",
        "relay_mode": "session_text",
        "title": "Two follow-up items",
        "say": "Two follow-up items",
        "context": "- Review release notes\n- Tag v1.2.0",
        "parts": [
            {"id": "p1", "label": "Review release notes"},
            {"id": "p2", "label": "Tag v1.2.0"},
        ],
        "audience": "owner",
        "status": "pending",
        "metadata": {"source": "needs-you"},
        "created_at": "2026-11-26T18:06:00Z",
    },
    "question_multiselect": {
        "decision_id": "askq-toolu-001",
        "correlation_id": "askq-toolu-001",
        "project": "demo-app",
        "session_id": "sess-004",
        "kind": "question",
        "relay_mode": "orca_ask_driver",
        "title": "Which targets should we build?",
        "say": "Which targets should we build?",
        "parts": [
            {
                "id": "p1",
                "label": "Select build targets",
                "multi_select": True,
                "options": [
                    {"id": "linux", "label": "Linux"},
                    {"id": "macos", "label": "macOS"},
                    {"id": "windows", "label": "Windows"},
                ],
            }
        ],
        "status": "pending",
        "metadata": {"orca_terminal": "term_example_1"},
        "created_at": "2026-11-26T18:10:00Z",
    },
    "permission": {
        "decision_id": "perm-corr-001",
        "correlation_id": "perm-corr-001",
        "project": "demo-app",
        "session_id": "sess-005",
        "kind": "permission",
        "relay_mode": "permission_keystroke",
        "title": "Allow running integration tests?",
        "say": "Allow running integration tests?",
        "options": [
            {"id": "allow", "label": "Yes"},
            {"id": "allow_session", "label": "Yes for session"},
            {"id": "deny", "label": "No"},
        ],
        "expires_at": "2026-11-26T18:15:00Z",
        "status": "pending",
        "metadata": {"tool_name": "Bash"},
        "created_at": "2026-11-26T18:10:00Z",
    },
}

RESPONDED_GOLDEN = {
    "choice_answered": {
        "decision_id": "dec-example-001",
        "project": "demo-app",
        "session_id": "sess-001",
        "kind": "choice",
        "status": "answered",
        "option_id": "lru",
        "via": "kiln",
        "answered_by": "owner",
        "responded_at": "2026-11-26T18:01:00Z",
    },
    "errand_done": {
        "decision_id": "ny-sess-002",
        "project": "demo-app",
        "session_id": "sess-002",
        "kind": "errand",
        "status": "answered",
        "done": True,
        "via": "terminal",
        "answered_by": "owner",
        "responded_at": "2026-11-26T18:07:00Z",
    },
    "question_multiselect_answered": {
        "decision_id": "askq-toolu-001",
        "project": "demo-app",
        "session_id": "sess-004",
        "kind": "question",
        "status": "answered",
        "answers": {"p1": ["linux", "macos"]},
        "via": "hearth-web",
        "answered_by": "owner",
        "responded_at": "2026-11-26T18:11:00Z",
    },
    "choice_defaulted": {
        "decision_id": "dec-example-002",
        "project": "demo-app",
        "session_id": "sess-001",
        "kind": "choice",
        "status": "defaulted",
        "option_id": "lru",
        "via": None,
        "answered_by": "default",
        "responded_at": "2026-11-27T00:00:00Z",
    },
    "errand_withdrawn": {
        "decision_id": "ny-sess-003",
        "project": "demo-app",
        "session_id": "sess-003",
        "kind": "errand",
        "status": "withdrawn",
        "via": "chat",
        "answered_by": "agent",
        "responded_at": "2026-11-26T18:12:00Z",
    },
    "permission_expired": {
        "decision_id": "perm-corr-001",
        "project": "demo-app",
        "session_id": "sess-005",
        "kind": "permission",
        "status": "expired",
        "via": None,
        "answered_by": None,
        "responded_at": "2026-11-26T18:15:00Z",
    },
}


@pytest.mark.parametrize("sample_name", sorted(REQUESTED_GOLDEN))
def test_requested_golden_samples_valid(sample_name: str) -> None:
    _validator("bus.hearth.decision.requested").validate(REQUESTED_GOLDEN[sample_name])


@pytest.mark.parametrize("sample_name", sorted(RESPONDED_GOLDEN))
def test_responded_golden_samples_valid(sample_name: str) -> None:
    _validator("bus.hearth.decision.responded").validate(RESPONDED_GOLDEN[sample_name])


def test_requested_rejects_invalid_kind_relay_and_say_length() -> None:
    v = _validator("bus.hearth.decision.requested")
    base = REQUESTED_GOLDEN["choice"]
    with pytest.raises(ValidationError):
        v.validate({**base, "kind": "unknown"})
    with pytest.raises(ValidationError):
        v.validate({**base, "relay_mode": "carrier_pigeon"})
    with pytest.raises(ValidationError):
        v.validate({**base, "say": "x" * 141})
    with pytest.raises(ValidationError):
        v.validate({**base, "parts": [{"id": "p1", "label": "P"}]})


def test_responded_rejects_invalid_status_and_missing_required() -> None:
    v = _validator("bus.hearth.decision.responded")
    base = RESPONDED_GOLDEN["errand_done"]
    with pytest.raises(ValidationError):
        v.validate({**base, "status": "pending"})
    with pytest.raises(ValidationError):
        v.validate({"decision_id": "ny-1", "status": "answered"})


def test_live_hearth_today_shapes_still_validate() -> None:
    """Today's live Hearth row_json and responded_event (no kind/relay_mode/metadata,
    both options and empty parts present, legacy via) must never dead-letter."""
    req_v = _validator("bus.hearth.decision.requested")
    live_requested = {
        "decision_id": "dec-live-001",
        "correlation_id": "dec-live-001",
        "project": "demo-app",
        "title": "Legacy choice decision",
        "context": "",
        "options": [
            {"id": "a", "label": "Option A", "description": ""},
            {"id": "b", "label": "Option B", "description": ""},
        ],
        "recommended": "a",
        "default_after": None,
        "links": [],
        "audience": "all",
        "status": "pending",
        "option_id": None,
        "parts": [],
        "answers": None,
        "comments": [],
        "text": None,
        "answered_by": None,
        "created_at": "2026-11-26T18:00:00Z",
        "responded_at": None,
        "session_id": None,
        "artifacts": [],
        "viewer_url": "https://example.test/decisions/dec-live-001",
    }
    req_v.validate(live_requested)

    resp_v = _validator("bus.hearth.decision.responded")
    for legacy_via in (None, "web", "phone", "cli"):
        live_responded = {
            "decision_id": "dec-live-001",
            "correlation_id": "dec-live-001",
            "status": "answered",
            "option_id": "a",
            "answers": None,
            "text": None,
            "answered_by": "phone",
            "responded_at": "2026-11-26T18:05:00Z",
        }
        if legacy_via is not None:
            live_responded["via"] = legacy_via
        resp_v.validate(live_responded)
