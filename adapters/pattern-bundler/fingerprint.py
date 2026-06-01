"""Shared message fingerprinting — used by both Window and bundler."""
from __future__ import annotations
import hashlib
import re

_FP_SUBS: list = [
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*'), '{ts}'),
    (re.compile(r'0x[0-9a-fA-F]{4,}'), '{hex}'),
    (re.compile(r'\b[0-9A-Z]{26}\b'), '{ulid}'),
    (re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE), '{uuid}'),
    (re.compile(r'\b(?:pid|PID)[=: ]\d+'), 'pid={N}'),
    (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), '{ip}'),
    (re.compile(r'\d+'), '{N}'),
    (re.compile(r'\s+'), ' '),
]

def fingerprint(message: str) -> str:
    """Normalize message to structural fingerprint, stripping variable parts."""
    msg = message[:500]
    for pattern, replacement in _FP_SUBS:
        msg = pattern.sub(replacement, msg)
    return msg.strip()[:150].lower()

def fp_hash(fp: str) -> str:
    return hashlib.sha256(fp.encode()).hexdigest()[:16]
