"""Unpublished Hearth artifact schema contract and identity fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "schemas" / "hearth.artifact.promoted.v1.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())

@pytest.fixture(scope="module")
def schema() -> dict:
    return _load_schema()

@pytest.fixture(scope="module")
def validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FormatChecker())

def _valid_event() -> dict:
    """Complete producer metadata fixture."""
    return {
        "schema_version": 1,
        "artifact_id": "9e0c4be4-96f8-47d4-a9d9-997b24931a52",
        "revision": 7,
        "artifact_uri": "hearth-artifact://9e0c4be4-96f8-47d4-a9d9-997b24931a52/revisions/7",
        "project": "hearth",
        "slug": "design-notes-2026-q3",
        "title": "Hearth Q3 design notes",
        "summary": "Notes from the 2026 Q3 Hearth design sync.",
        "kind": "markdown",
        "sha256": "a" * 64,
        "bytes": 4096,
        "provenance": {
            "repository": "git@github.com:hearth/hearth.git",
            "commit": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
            "branch": "main",
            "path": "docs/notes/2026-q3.md",
            "session_id": "01H9EXAMPLE",
            "bead_id": "hearth-7w2k.3",
            "run_id": "run-42",
            "task_id": "task-42",
            "dispatch_id": "dispatch-42",
            "agent": "claude-code",
            "dirty": False,
        },
        "kb_refs": [
            "kb://hearth/architectural-decisions",
            "kb://hearth/design-notes/q3",
        ],
        "created_at": "2026-09-09T12:00:00Z",
    }


def test_schema_is_valid_draft_2020_12(schema: dict) -> None:
    Draft202012Validator.check_schema(schema)

def test_canonical_event_validates(validator: Draft202012Validator) -> None:
    validator.validate(_valid_event())

def test_minimal_provenance_with_only_dirty_flag_validates(
    validator: Draft202012Validator,
) -> None:
    """Every provenance property is optional; only the documented ones exist."""
    event = _valid_event()
    event["provenance"] = {"dirty": True}
    validator.validate(event)

def test_empty_provenance_object_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["provenance"] = {}
    validator.validate(event)

def test_empty_kb_refs_array_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kb_refs"] = []
    validator.validate(event)

def test_each_closed_kind_validates(validator: Draft202012Validator) -> None:
    for kind in ("html", "markdown", "json", "text"):
        event = _valid_event()
        event["kind"] = kind
        validator.validate(event)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "artifact_id",
        "revision",
        "artifact_uri",
        "project",
        "slug",
        "title",
        "summary",
        "kind",
        "sha256",
        "bytes",
        "provenance",
        "kb_refs",
        "created_at",
    ],
)
def test_required_field_omission_rejected(
    validator: Draft202012Validator, field: str
) -> None:
    event = _valid_event()
    del event[field]
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # Not a UUID.
        ("artifact_id", "not-a-uuid"),
        # Uppercase UUID — UUIDs in this contract are lowercase.
        ("artifact_id", "9E0C4BE4-96F8-47D4-A9D9-997B24931A52"),
        # Revision must be >= 1.
        ("revision", 0),
        ("revision", -3),
        # artifact_uri does not match the canonical lowercase-UUID + positive-int pattern.
        (
            "artifact_uri",
            "hearth-artifact://9E0C4BE4-96F8-47D4-A9D9-997B24931A52/revisions/7",
        ),
        (
            "artifact_uri",
            "https://hearth.example/9e0c4be4-96f8-47d4-a9d9-997b24931a52/r/7",
        ),
        (
            "artifact_uri",
            "hearth-artifact://9e0c4be4-96f8-47d4-a9d9-997b24931a52/revisions/0",
        ),
        ("artifact_uri", "hearth-artifact://9e0c4be4-96f8-47d4-a9d9-997b24931a52"),
        # project: must start with [a-z0-9], contain only [a-z0-9._-], length 1..100.
        ("project", "Hearth"),
        ("project", "hearth team"),
        ("project", ""),
        ("project", "a" * 101),
        # slug: same shape as project.
        ("slug", "Hearth-Notes"),
        ("slug", ""),
        ("slug", "a" * 101),
        # title is identity metadata: empty string not accepted.
        ("title", ""),
        ("title", "a" * 201),
        # bytes: bounded 1..2097152.
        ("bytes", 0),
        ("bytes", 2097153),
        # sha256: lowercase 64-hex.
        ("sha256", "A" * 64),
        ("sha256", "z" * 64),
        ("sha256", "ab" * 31),  # 62 chars — wrong length
        # created_at: not a date-time.
        ("created_at", "not-a-timestamp"),
        # schema_version: must be the const 1.
        ("schema_version", 2),
        ("schema_version", "1"),
    ],
)
def test_identity_format_length_guards(
    validator: Draft202012Validator, field: str, value: Any
) -> None:
    event = _valid_event()
    event[field] = value
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize("kind", ["pdf", "binary", "xml", "image/png", "MARKDOWN", "", "md"])
def test_arbitrary_kind_rejected(validator: Draft202012Validator, kind: str) -> None:
    event = _valid_event()
    event["kind"] = kind
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize(
    "extra",
    [
        "fetched_html",
        "raw_content",
        "content",
        "body",
        "html",
        "payload",
        "auth_token",
        "bearer",
        "authorization",
    ],
)
def test_extra_property_on_data_rejected(
    validator: Draft202012Validator, extra: str
) -> None:
    """The data object denies extra properties — credential fields cannot leak in
    via additional payload keys, even ones a careless producer might add."""
    event = _valid_event()
    event[extra] = "leak"
    with pytest.raises(ValidationError):
        validator.validate(event)

def test_extra_property_on_provenance_rejected(
    validator: Draft202012Validator,
) -> None:
    event = _valid_event()
    event["provenance"]["signed_attestation"] = "in-toto:something"
    event["provenance"]["bearer_token"] = "sk_live_xxx"
    event["provenance"]["fetched_url"] = "https://example.com/private"
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize(
    "field",
    [
        "repository",
        "commit",
        "branch",
        "path",
        "session_id",
        "bead_id",
        "run_id",
        "task_id",
        "dispatch_id",
        "agent",
    ],
)
def test_provenance_string_fields_have_2048_byte_cap(
    validator: Draft202012Validator, field: str
) -> None:
    event = _valid_event()
    event["provenance"][field] = "a" * 2049
    with pytest.raises(ValidationError):
        validator.validate(event)

def test_provenance_dirty_must_be_boolean(
    validator: Draft202012Validator,
) -> None:
    event = _valid_event()
    event["provenance"]["dirty"] = "true"
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize(
    "kb_ref",
    [
        # traversal segments
        "kb://hearth/../etc/passwd",
        "kb://hearth/foo/../../bar",
        # leading slash inside the path
        "kb://hearth//foo",
        # uppercase or invalid characters
        "kb://Hearth/foo",
        "kb://hearth/Foo",
        # empty segments
        "kb://hearth/",
        "kb:///",
        # query / fragment
        "kb://hearth/foo?token=secret",
        "kb://hearth/foo#frag",
        # percent-encoded slash (deceptive traversal)
        "kb://hearth/foo%2F..%2Fbar",
        # non-kb scheme
        "https://hearth.example/article",
        "file:///etc/passwd",
        "javascript:alert(1)",
        # too long
        "kb://hearth/" + ("a" * 600),
    ],
)
def test_traversal_or_unsafe_kb_ref_rejected(
    validator: Draft202012Validator, kb_ref: str
) -> None:
    event = _valid_event()
    event["kb_refs"] = [kb_ref]
    with pytest.raises(ValidationError):
        validator.validate(event)

def test_kb_refs_must_be_unique(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kb_refs"] = ["kb://hearth/x", "kb://hearth/x"]
    with pytest.raises(ValidationError):
        validator.validate(event)

def test_kb_refs_capped_at_32(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kb_refs"] = [f"kb://hearth/entry-{i}" for i in range(33)]
    with pytest.raises(ValidationError):
        validator.validate(event)

def test_kb_refs_at_32_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kb_refs"] = [f"kb://hearth/entry-{i}" for i in range(32)]
    validator.validate(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auth_token", "sk_live_supersecret"),
        ("bearer_token", "Bearer abc.def.ghi"),
        ("authorization", "Bearer abc.def.ghi"),
        ("cookie", "session=abc123"),
        ("api_key", "key-1234"),
        ("fetched_content", "<html>nope</html>"),
        ("raw_html", "<html>nope</html>"),
        ("presigned_url", "https://hearth.example/get?sig=xxx"),
    ],
)
def test_credential_or_content_field_rejected_no_matter_the_name(
    validator: Draft202012Validator, field: str, value: str
) -> None:
    """Even if a producer names a credential-bearing field differently, additional
    top-level properties are forbidden by ``additionalProperties: false``. The
    event is sealed: only the documented fields can appear in the data object."""
    event = _valid_event()
    event[field] = value
    with pytest.raises(ValidationError):
        validator.validate(event)


def _uri_invariants_hold(event: dict) -> tuple[bool, str]:
    """Return (ok, message) for the documented cross-field identity invariant.

    The schema CANNOT enforce these (per docs/HEARTH-ARTIFACTS.md §1.1); this
    helper mirrors the producer/consumer check that real publishers/consumers
    are required to perform. We expose it as a free function so the same
    invariant can be asserted by tests, sample validators, and any ad-hoc
    reviewer.
    """
    import re

    URI_RE = re.compile(
        r"^hearth-artifact://([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{12})/revisions/([1-9][0-9]*)$"
    )
    uri = event["artifact_uri"]
    m = URI_RE.match(uri)
    if m is None:
        return False, f"artifact_uri {uri!r} does not match the canonical shape"
    uri_uuid, uri_rev = m.group(1), int(m.group(2))
    if uri_uuid != event["artifact_id"]:
        return False, (
            f"artifact_uri UUID {uri_uuid!r} does not match artifact_id "
            f"{event['artifact_id']!r}"
        )
    if uri_rev != event["revision"]:
        return False, (
            f"artifact_uri revision {uri_rev} does not match revision "
            f"{event['revision']}"
        )
    return True, ""

def test_schema_does_not_enforce_cross_field_uri_identity(
    validator: Draft202012Validator,
) -> None:
    """Demonstrate that the schema, by itself, accepts an event whose ``artifact_uri``
    UUID disagrees with ``artifact_id``. The schema only enforces URI shape; the
    producer/consumer invariant in ``_uri_invariants_hold`` catches the mismatch.
    This is the explicit "MUST be documented as producer/consumer validation"
    guarantee in the design contract.
    """
    event = _valid_event()
    # Different *valid* UUID inside the URI.
    event["artifact_uri"] = (
        "hearth-artifact://00000000-0000-0000-0000-000000000000/revisions/7"
    )
    # Schema is satisfied — the URI is well-formed.
    validator.validate(event)
    # The documented invariant catches it.
    ok, msg = _uri_invariants_hold(event)
    assert not ok, (
        f"cross-field identity invariant should fail for mismatched URI; got {msg!r}"
    )
    assert "does not match artifact_id" in msg

def test_schema_does_not_enforce_cross_field_uri_revision(
    validator: Draft202012Validator,
) -> None:
    """Same as the UUID case, for the revision segment. The URI's trailing int must
    equal the ``revision`` field; the schema cannot enforce this; the producer/
    consumer invariant must."""
    event = _valid_event()
    event["artifact_uri"] = (
        "hearth-artifact://9e0c4be4-96f8-47d4-a9d9-997b24931a52/revisions/99"
    )
    validator.validate(event)
    ok, msg = _uri_invariants_hold(event)
    assert not ok
    assert "does not match revision" in msg

def test_uri_invariants_hold_for_canonical_event() -> None:
    event = _valid_event()
    ok, msg = _uri_invariants_hold(event)
    assert ok, msg


def test_renaming_required_field_to_extra_key_still_rejected(
    validator: Draft202012Validator,
) -> None:
    """Producer cannot smuggle in a duplicate by renaming a required field with
    the right shape but wrong meaning: e.g. ``summary2`` with credential-shaped
    text. The schema denies every property not explicitly enumerated, regardless
    of the value's shape."""
    event = _valid_event()
    event["summary_secret"] = "sk_live_supersecret"
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize("absent_field", ["repository", "commit", "branch", "path"])
def test_provenance_string_field_absence_is_valid(
    validator: Draft202012Validator, absent_field: str
) -> None:
    event = _valid_event()
    del event["provenance"][absent_field]
    validator.validate(event)


def test_kind_html_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kind"] = "html"
    validator.validate(event)

def test_kind_json_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kind"] = "json"
    validator.validate(event)

def test_kind_text_validates(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["kind"] = "text"
    validator.validate(event)


@pytest.mark.parametrize("length", [0, 4000])
def test_summary_boundary_lengths_validate(
    validator: Draft202012Validator, length: int
) -> None:
    event = _valid_event()
    event["summary"] = "x" * length
    validator.validate(event)

def test_summary_at_4001_rejected(validator: Draft202012Validator) -> None:
    event = _valid_event()
    event["summary"] = "x" * 4001
    with pytest.raises(ValidationError):
        validator.validate(event)


@pytest.mark.parametrize("size", [1, 2097152])
def test_bytes_boundary_validates(
    validator: Draft202012Validator, size: int
) -> None:
    event = _valid_event()
    event["bytes"] = size
    validator.validate(event)


def test_kb_refs_with_safe_prefix_segments_validates(
    validator: Draft202012Validator,
) -> None:
    """A realistic multi-segment kb URI is allowed: project + nested slug."""
    event = _valid_event()
    event["kb_refs"] = [
        "kb://hearth/architectural-decisions",
        "kb://hearth/notes/2026-q3",
        "kb://hearth/specs/auth/identity",
        "kb://nervous-bus/specs/hearth-artifacts",
    ]
    validator.validate(event)

@pytest.mark.parametrize("reference", ["kb://hearth", "kb://./x", "kb://hearth/.", "kb://../x"])
def test_kb_ref_requires_safe_project_and_path(validator, reference):
    event = _valid_event()
    event["kb_refs"] = [reference]
    with pytest.raises(ValidationError):
        validator.validate(event)

@pytest.mark.parametrize("reference", ["kb://hearth/_draft", "kb://hearth/" + "a" * 200, "kb://hearth/.entry"])
def test_kb_ref_matches_producer_grammar(validator, reference):
    event = _valid_event()
    event["kb_refs"] = [reference]
    validator.validate(event)

def test_blank_title_rejected(validator):
    event = _valid_event()
    event["title"] = " \t\n"
    with pytest.raises(ValidationError):
        validator.validate(event)
