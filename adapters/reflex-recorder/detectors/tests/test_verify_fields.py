"""Verify fields from claude-hook-fast#54 restore the detectors' verify signal.

Since 2026-10-04 the bus Bash summary is verb-only ("cargo"), so the keyword
floor never matched a verify and no outcome was visible. These tests build
events in the post-#54 shape and check the substrate reads verify_class,
verify_cmd and verify_outcome, while legacy full-command events keep working.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from detectors import verify_fields
from detectors.dispatch_lineage import (
    _last_test_status_in_segment,
    event_is_verify,
    last_test_signal_before,
    ran_build_or_test,
)
from detectors.edit_build_fail_revert import _find_thrash_cycles


def _bash(seq, verbs, cls="", phrase="", outcome="", is_error=False, extra_rs=None, raw_cmd=None):
    ts = {"command": raw_cmd if raw_cmd is not None else verbs, "command_bytes": 40}
    if cls:
        ts["verify_class"] = cls
        ts["verify_cmd"] = phrase
    rs = {"bytes": 120, "is_error": is_error}
    if outcome:
        rs["verify_outcome"] = outcome
    rs.update(extra_rs or {})
    return {"seq": seq, "tool_name": "Bash", "cwd": "/w", "project": "p",
            "tool_summary": json.dumps(ts), "tool_response_summary": json.dumps(rs)}


def _edit(seq, path="/w/src/a.rs"):
    return {"seq": seq, "tool_name": "Edit", "cwd": "/w", "project": "p",
            "tool_summary": json.dumps({"file_path": path, "old_string_bytes": 3, "new_string_bytes": 4}),
            "tool_response_summary": json.dumps({"bytes": 50, "is_error": False})}


def test_verb_only_verify_is_seen_through_verify_class():
    ev = _bash(1, "cd cargo", "test", "cargo test", "pass")
    assert event_is_verify(ev)
    assert ran_build_or_test([ev])


def test_verify_cmd_matches_keyword_floor_without_class():
    # A predicate that only sees text: verify_cmd puts "cargo test" back in it.
    ev = _bash(1, "cargo", "", "")
    ev["tool_summary"] = json.dumps({"command": "cargo", "verify_cmd": "cargo test"})
    assert event_is_verify(ev)


def test_class_outside_the_floor_counts():
    # vitest is not in BUILD_KEYWORDS; the hook's class still marks it.
    assert event_is_verify(_bash(1, "npx", "test", "vitest", "pass"))


def test_lint_is_not_verification():
    assert not event_is_verify(_bash(1, "go", "lint", "go vet", "pass"))


def test_last_test_signal_reads_hook_outcome():
    events = [_bash(1, "cargo", "test", "cargo test", "fail"), _edit(2)]
    assert last_test_signal_before(events, 5).status == "failed"
    events = [_bash(1, "cargo", "test", "cargo test", "pass")]
    assert last_test_signal_before(events, 5).status == "passed"


def test_failed_call_event_reads_as_failed():
    # PostToolUseFailure shape: is_error with an exit code.
    ev = _bash(1, "cd cargo tail", "test", "cargo test", "fail", is_error=True, extra_rs={"exit_code": 101})
    assert last_test_signal_before([ev], 5).status == "failed"
    assert _last_test_status_in_segment([ev]) == "failed"


def test_segment_status_skips_background_launch():
    events = [_bash(1, "cargo", "test", "cargo test", "pass"),
              _bash(2, "cargo", "test", "cargo test", "unknown", extra_rs={"background": True})]
    assert _last_test_status_in_segment(events) == "passed"


def test_legacy_full_command_event_unchanged():
    ev = {"seq": 1, "tool_name": "Bash", "cwd": "/w", "project": "p",
          "tool_summary": json.dumps({"command": "cargo test -p core"}),
          "tool_response_summary": json.dumps({"stdout": "test result: FAILED. 1 failed", "stderr": ""})}
    assert last_test_signal_before([ev], 5).status == "failed"
    assert _last_test_status_in_segment([ev]) == "failed"
    assert verify_fields.outcome(ev["tool_response_summary"]) is None


def test_thrash_cycle_found_from_hook_outcome():
    fail = [_edit(1), _bash(2, "cargo", "test", "cargo test", "fail"),
            _bash(3, "git", raw_cmd="git checkout -- src/a.rs")]
    cycles = _find_thrash_cycles(fail)
    assert len(cycles) == 1 and cycles[0]["build_cmd"] == "cargo test"
    ok = [_edit(1), _bash(2, "cargo", "test", "cargo test", "pass"),
          _bash(3, "git", raw_cmd="git checkout -- src/a.rs")]
    assert _find_thrash_cycles(ok) == []


def test_non_verify_failure_is_not_a_build_failure():
    # A failed grep after a build is not the build failing.
    events = [_edit(1), _bash(2, "cargo", "test", "cargo test", "pass"),
              _bash(3, "grep", is_error=True, extra_rs={"exit_code": 1}),
              _bash(4, "git", raw_cmd="git checkout -- src/a.rs")]
    assert _find_thrash_cycles(events) == []
