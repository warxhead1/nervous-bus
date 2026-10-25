#!/usr/bin/env python3
"""Trim (and optionally redact) a Claude Code transcript into a test fixture.

    python3 make_fixture.py SRC.jsonl OUT.jsonl [--redact] [--since ISO] [--until ISO]

TRIM keeps only what transcript_steps reads: `assistant` tool_use blocks
(name, id, input) and `user` tool_result blocks (tool_use_id, is_error), each
with its record timestamp. Thinking, prose, attachments and api-request records
are dropped. A tool result keeps only its verdict lines (pass / fail markers and
the line after a panic), capped, so test output does not ride along. A Write
body keeps only the lines holding a number.

REDACT makes a fixture safe for this PUBLIC repo when the source project is
private: every identifier segment (split on `_`) longer than two characters is
replaced by a stable placeholder (`w17`, `W17` for upper case) unless it is a
structural word the detectors key on (shell and test-runner verbs, python and
rust keywords, pass/fail markers, bound words like MAX / MIN / budget, file
extensions). Numbers, punctuation, timestamps and flags (`--ignored`) are kept,
so the same placeholder stands for the same name everywhere: a harness keeps its
identity across reruns and a test file's stem still links its edit to its panic.
"""
from __future__ import annotations

import json
import keyword
import re
import sys
from pathlib import Path

VERDICT_RE = re.compile(
    r"test result:|CHECK (?:PASS|FAILED|FAST OK)|panicked at|\bFAILED\b|\bpassed\b|\bfailed\b"
    r"|^ok\s|^not ok|# fail|ℹ (?:pass|fail)|AssertionError|assertion failed|\bFAIL\b|^PASS\b"
    r"|Summary \[|over 2x budget|error\[|^error:", re.I | re.M)
MAX_RESULT_LINES = 12

KEEP = {
    # shell, runners, taxonomy verbs
    "sed", "cargo", "test", "tests", "nextest", "run", "release", "build", "check", "bash",
    "zsh", "python", "python3", "node", "npx", "tsx", "git", "commit", "push", "add", "echo",
    "cat", "head", "tail", "grep", "command", "cut", "awk", "for", "done", "then", "else",
    "while", "until", "timeout", "time", "tee", "mkdir", "chmod", "example", "examples",
    "bin", "ignored", "include", "nocapture", "threads", "workspace", "fast", "make", "ninja",
    "cmake", "tsc", "webpack", "vite", "gradle", "mvn", "package", "pytest", "jest", "vitest",
    "npm", "pnpm", "ctest", "find", "stat", "sleep", "env", "export", "printf", "sort",
    "uniq", "paste", "true", "false", "EOF", "PYEOF",
    # python / rust structure the edit parsers need
    "open", "read", "write", "replace", "print", "index", "count", "const", "let", "pub",
    "mut", "f64", "f32", "usize", "u32", "u64", "i32", "i64", "assert", "assert_eq",
    "assert_ne", "expect", "toBe", "toEqual",
    # verdict markers
    "result", "FAILED", "failed", "passed", "panicked", "thread", "error", "CHECK", "PASS",
    "FAST", "FAIL", "fail", "pass", "Summary", "not", "AssertionError", "assertion", "over",
    "WARN", "Error", "rerun",
    # bound words (gate_bound_loosened direction)
    "max", "maximum", "limit", "budget", "budgets", "ceil", "ceiling", "upper", "tol",
    "tolerance", "eps", "epsilon", "thresh", "threshold", "allow", "allowed", "timeout",
    "cap", "most", "min", "minimum", "floor", "lower", "least", "ratchet",
    # file extensions / gate path words
    "rs", "py", "ts", "js", "mjs", "sh", "toml", "json", "md", "txt", "spec", "gate",
    "conftest", "scripts",
}
KEEP_LOWER = {k.lower() for k in KEEP} | set(keyword.kwlist) | set(getattr(keyword, "softkwlist", []))

_IDENT = re.compile(r"(?<![A-Za-z0-9_\\])[A-Za-z_][A-Za-z0-9_]*")


class Redactor:
    def __init__(self):
        self.map: dict[str, str] = {}

    def _seg(self, seg: str) -> str:
        if len(seg) <= 2 or seg.lower() in KEEP_LOWER or not re.search(r"[A-Za-z]", seg):
            return seg
        key = seg.lower()
        if key not in self.map:
            self.map[key] = f"w{len(self.map) + 1}"
        ph = self.map[key]
        return ph.upper() if seg.isupper() else ph

    def word(self, m: re.Match) -> str:
        tok = m.group(0)
        start = m.start()
        s = m.string
        if re.search(r"(?:^|[\s'\"])--?$", s[max(0, start - 3):start]):  # a flag: --ignored, -p
            return tok
        if tok.lower() in KEEP_LOWER:
            return tok
        return "_".join(self._seg(p) for p in tok.split("_"))

    def text(self, s: str) -> str:
        return _IDENT.sub(self.word, s)

    def value(self, v):
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        return v


def _verdict_lines(text: str) -> str:
    lines = text.splitlines()
    keep: list[str] = []
    for i, ln in enumerate(lines):
        if VERDICT_RE.search(ln):
            keep.append(ln[:400])
            if "panicked at" in ln and i + 1 < len(lines):
                keep.append(lines[i + 1][:400])
        if len(keep) >= MAX_RESULT_LINES:
            break
    return "\n".join(keep)


def _trim_input(name: str, inp: dict) -> dict:
    if name == "Write" and isinstance(inp.get("content"), str):
        body = "\n".join(l for l in inp["content"].splitlines() if re.search(r"\d", l))
        return {"file_path": inp.get("file_path", ""), "content": body}
    if name in ("Read", "Grep", "Glob"):
        return {k: inp[k] for k in ("file_path", "path", "pattern") if k in inp}
    if name in ("Agent", "Task"):
        return {k: inp[k] for k in ("description", "subagent_type", "model") if k in inp}
    return inp


def trim(src: Path, redact: bool, since: str = "", until: str = "") -> list[dict]:
    red = Redactor() if redact else None
    out: list[dict] = []
    for line in open(src, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("assistant", "user"):
            continue
        ts = rec.get("timestamp") or ""
        if (since and ts < since) or (until and ts > until):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        blocks = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = _trim_input(str(b.get("name")), b.get("input") or {})
                blocks.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                               "input": red.value(inp) if red else inp})
            elif b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                text = _verdict_lines(c if isinstance(c, str) else "")
                blocks.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id"),
                               "is_error": bool(b.get("is_error")),
                               "content": red.text(text) if red else text})
        if blocks:
            out.append({"type": rec["type"], "timestamp": ts,
                        "message": {"role": rec["type"], "content": blocks}})
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    opts = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True)
            for a in argv if a.startswith("--")}
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    recs = trim(Path(args[0]), bool(opts.get("--redact")),
                str(opts.get("--since") or ""), str(opts.get("--until") or ""))
    with open(args[1], "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(recs)} records -> {args[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
