"""Harness-neutral reply scoring: the core behind `reply-score` (Claude Code Stop hook) and `prose-guard score`.

Code blocks and inline code are dropped, and `{{short|long}}` stretches count as their long form.
"""
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from steering_hits import log_hit

FENCED = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$|^[ \t]*(```|~~~).*\Z", re.S | re.M)
STRETCH = re.compile(r"\{\{(.*?)\|(.*?)\}\}", re.S)
INLINE = re.compile(r"`[^`\n]*`")
TAIL_BYTES = 2_000_000


def load_checker():
    path = Path(__file__).resolve().parents[1] / "skills" / "ste80-humanizer" / "scripts" / "ste_check.py"
    spec = importlib.util.spec_from_file_location("ste_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ste_check"] = mod           # its dataclass looks itself up here
    spec.loader.exec_module(mod)
    return mod


def transcript_reply(path):
    """Text of the last assistant message in a Claude Code transcript (JSONL), or ""."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        if row.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text = content if isinstance(content, str) else "\n".join(
            b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    return ""


def prose_of(text):
    text = STRETCH.sub(lambda m: m.group(2), text)
    text = FENCED.sub("", text)
    return INLINE.sub("code", text)


def score_reply(text, ev=None, surface="reply", log=True):
    """Score one reply. Returns {words, avg_sentence, max_sentence, findings, rules}, or None for an empty reply.

    Logs a reply-score record, including a clean pass. Never raises on a logging failure.
    """
    text = prose_of(text)
    if not text.strip():
        return None
    mod = load_checker()
    findings = mod.check(text, False)
    summary = mod.summary(text)
    rules = Counter(f.rule for f in findings)
    stats = {"words": sum(len(p.split()) for _, p in mod.paragraphs(mod.strip_markdown(text))),
             "avg_sentence": summary["avg_words_per_sentence"], "max_sentence": summary["max_words_per_sentence"]}
    if log:
        log_hit("reply-score", surface, ev or {}, "none", len(findings), rules, "", stats)
    return {**stats, "findings": len(findings), "rules": dict(rules)}
