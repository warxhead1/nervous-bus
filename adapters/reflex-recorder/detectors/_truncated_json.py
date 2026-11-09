"""detectors/_truncated_json.py -- read one string field out of JSON text that a
capture bound cut mid-string.

tool_summary / tool_response_summary are bounded, so a long value (an Agent
`prompt`) is cut with no closing quote and json.loads rejects the whole object.
`string_field` pulls a single top-level-looking string value straight from the
text instead.

The cut value is returned only when the caller opts in (`partial=True`): a
truncated prompt is still a usable prefix, but a truncated id or enum
("agentId" cut to a prefix, "isolation" cut to "work") is a wrong value, not a
shorter one, so those come back "" unless the closing quote is present.
"""
from __future__ import annotations

import json
import re

_FIELD_CACHE: dict[str, "re.Pattern[str]"] = {}

# An unfinished \uXXXX at the very end, and a high-surrogate escape whose low half
# was cut off. `(?<!\\)(?:\\\\)*` keeps an escaped backslash ("\\\\u00") from
# being read as the start of a \u escape.
_PARTIAL_UNICODE = re.compile(r"(?<!\\)((?:\\\\)*)\\u[0-9a-fA-F]{0,3}\Z")
_LONE_HIGH_SURROGATE = re.compile(r"(?<!\\)((?:\\\\)*)\\u[dD][89abAB][0-9a-fA-F]{2}\Z")


def _pattern(key: str) -> "re.Pattern[str]":
    """Group 1 = the string body (escape sequences kept verbatim), group 2 = the
    closing quote if the text has one. A lone trailing backslash is never part of
    group 1: `\\.` needs a character after it."""
    pat = _FIELD_CACHE.get(key)
    if pat is None:
        pat = re.compile(
            r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)("?)', re.DOTALL
        )
        _FIELD_CACHE[key] = pat
    return pat


def _decode(body: str) -> str:
    try:
        text = json.loads('"' + body + '"', strict=False)
    except ValueError:
        text = body.replace("\\n", "\n").replace('\\"', '"')
    # A lone surrogate (bad source data) would raise on any later utf-8 write.
    return text.encode("utf-8", "replace").decode("utf-8")


def string_field(raw: str, key: str, *, partial: bool = False) -> str:
    """Decoded string value of `key` in `raw`, or "" if absent.

    A value with no closing quote (cut by the bound) is returned as the decoded
    prefix when `partial` is True and as "" otherwise.
    """
    m = _pattern(key).search(raw or "")
    if not m:
        return ""
    body, closed = m.group(1), m.group(2)
    if not closed:
        if not partial:
            return ""
        body = _PARTIAL_UNICODE.sub(r"\1", body)
        body = _LONE_HIGH_SURROGATE.sub(r"\1", body)
    return _decode(body)
