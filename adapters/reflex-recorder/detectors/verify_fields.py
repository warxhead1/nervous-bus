"""detectors/verify_fields.py -- read the verify fields claude-hook-fast emits.

Since 2026-10-04 the bus Bash tool_summary is verb-only ("cargo", not
"cargo test"), and tool_response_summary carried only bytes/is_error, so the
keyword floor could never match a verify and no outcome was visible.
claude-hook-fast#54 adds, all optional:

  tool_summary           verify_class  test | build | typecheck | lint
                         verify_cmd    canonical phrase, e.g. "cargo test"
  tool_response_summary  verify_outcome  pass | fail | unknown
                         exit_code, and is_error=true on a failed call
                         (nbus-activity wired to PostToolUseFailure)

Events older than #54 carry none of these; callers keep their legacy paths.
"""
from __future__ import annotations

import json

# lint is not a verification: the keyword floor never counted it either.
VERIFY_CLASSES = frozenset({"test", "build", "typecheck"})


def _parse(summary) -> dict:
    if isinstance(summary, dict):
        return summary
    try:
        v = json.loads(summary or "")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def verify_text(tool_summary) -> str:
    """Command text for a keyword predicate: the verbs plus verify_cmd, so a
    floor keyword such as "cargo test" or "tsc " matches again."""
    ts = _parse(tool_summary)
    cmd = ts.get("command", "") or ""
    phrase = ts.get("verify_cmd", "") or ""
    return f"{cmd} {phrase} " if phrase else cmd


def is_verify_class(tool_summary) -> bool:
    return _parse(tool_summary).get("verify_class") in VERIFY_CLASSES


def outcome(tool_response_summary) -> str | None:
    """"fail", "pass" or "unknown" from the hook's fields; None when the event
    predates them (fall back to output scanning)."""
    rs = _parse(tool_response_summary)
    vo = rs.get("verify_outcome")
    if vo == "fail" or rs.get("is_error") is True:
        return "fail"
    if vo in ("pass", "unknown"):
        return vo
    return None
