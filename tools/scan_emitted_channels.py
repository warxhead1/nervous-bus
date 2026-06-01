#!/usr/bin/env python3
"""scan_emitted_channels.py — static scanner for nervous-bus emit sites.

Walks one or more producer source trees and extracts the channel/type strings
of every call site that publishes to nervous-bus. Output is a JSON array of
records suitable for piping into `check_schema_coverage.py`.

Recognised patterns:

  Python:
    - nbus.publish("<channel>", ...)
    - nervous.publish("<channel>", ...)
    - obs._publish("<channel>", ...)
    - emit("<channel>", ...)                         # deerflow.bus.emit helper
    - CHANNEL_*  = "<channel>"                       # module-level constant

  Go:
    - <pub>.Publish(<ctx>, "<stream>", ...)
    - <Const>Stream = "<stream>"                     # const string literal
    - exec.Command("nervous", "publish", "<channel>", ...)
    - streamBase + "." + stream                      # runtime prefix: emit
                                                       <base>.<const> for every
                                                       Stream/Channel/Topic const
                                                       in the same file. The
                                                       hearth-loom publisher
                                                       prepends a base segment.

  Rust:
    - publish!("<channel>", ...)
    - pub const CHANNEL_<name>: &str = "<channel>";

  Shell:
    - nervous publish <channel> ...                  # bare invocation
    - zellij pipe -p nervous-bus -n <channel> --     # legacy direct pipe

False positives are tolerated; the coverage checker has an allowlist. False
negatives are the real risk — be greedy.

Output records:
    {
      "file":      "<relpath>",
      "line":      N,
      "channel":   "<wire-effective>",
      "producer":  "<name>",
      "emit_type": "const" | "call"
    }

`emit_type` distinguishes constants/declarations (`const X = "..."`) from
direct call sites (`publish("...")`). Useful for distinguishing definition
from emit in audit reports.

Usage:
    scan_emitted_channels.py [<producer>=]<path> [...]
        ↳ writes JSON list to stdout
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Heuristic: nervous-bus channels are lowercase, dot-separated, and contain
# at least one dot. We use this to filter obvious false positives like
# "OK" or single-word identifiers caught by a too-greedy regex.
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9._-]*\.[a-z][a-z0-9._-]*$")

# Directories we never walk into (build artifacts, deps, VCS).
SKIP_DIRS = {
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".deer-flow",          # deer-flow runtime cache; 10+ GB of throwaway state
    ".langgraph_api",
    "logs",                # runtime logs, never sources
    "pr-build",
}

# Skip generated / vendored sub-trees by relative path prefix.
SKIP_PREFIXES = (
    "vendor/",
    "third_party/",
    "node_modules/",
    # Don't double-count agents' worktrees — they mirror the same source tree.
    ".claude/worktrees/",
    ".git/",
)


def looks_like_channel(s: str) -> bool:
    """True if the string is plausibly a nervous-bus channel name."""
    if not s:
        return False
    if not CHANNEL_RE.match(s):
        return False
    # Reject obvious non-channel strings.
    if s.startswith(("http", "/", ".", "_")):
        return False
    return True


# ─── Python AST scanner ──────────────────────────────────────────────────────

_PY_CALL_TARGETS = {
    # attribute_chain → True if the call's channel arg is its FIRST positional
    "nbus.publish",
    "nervous.publish",
    "obs._publish",
    "self._publish",
    "bus.emit",
    "bus.publish",
    "_bus.emit",
    "_bus.publish",
}

# Bare-name callees (no attribute) we treat as emit helpers.
_PY_BARE_CALL_TARGETS = {"emit", "publish", "_emit", "_publish", "_bus_emit"}


def _attr_chain(node: ast.AST) -> str | None:
    """Return dotted name for ast.Attribute chains; None if not pure attrs."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def scan_python(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, channel, emit_type) tuples for a .py file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        # Module-level constants: CHANNEL_FOO = "some.channel"
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and (
                    tgt.id.startswith("CHANNEL_")
                    or tgt.id.endswith("_CHANNEL")
                    or tgt.id.endswith("_STREAM")
                    or tgt.id.endswith("_TOPIC")
                ):
                    val = node.value
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        if looks_like_channel(val.value):
                            found.append((node.lineno, val.value, "const"))
            continue

        # Call sites: <foo>.publish("ch", ...), emit("ch", ...), ...
        if isinstance(node, ast.Call):
            target_name: str | None = None
            if isinstance(node.func, ast.Attribute):
                chain = _attr_chain(node.func)
                if chain and chain in _PY_CALL_TARGETS:
                    target_name = chain
                # Also accept the last two segments: helper.publish("ch", ...).
                elif chain:
                    tail = ".".join(chain.split(".")[-2:])
                    if tail in _PY_CALL_TARGETS:
                        target_name = tail
            elif isinstance(node.func, ast.Name):
                if node.func.id in _PY_BARE_CALL_TARGETS:
                    target_name = node.func.id

            if not target_name or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if looks_like_channel(first.value):
                    found.append((node.lineno, first.value, "call"))

    return found


# ─── Go regex scanner ────────────────────────────────────────────────────────

# Match: pub.Publish(ctx, "<channel>", ...) — also handles whitespace/newlines
# between the comma and the string.
_GO_PUBLISH_CALL_RE = re.compile(
    r"\.\s*Publish(?:LoomCoord|BeadLifecycle)?\s*\(\s*[A-Za-z_][\w.]*\s*,\s*\"([^\"]+)\"",
)

# Match: const Foo = "bar.baz.v1"  (single-line)
# Also: FooStream = "bar.baz.v1"   (in const blocks)
_GO_CONST_STREAM_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*(?:Stream|Channel|Topic))\b\s*=\s*\"([^\"]+)\"",
)

# Match: exec.Command("nervous", "publish", "<channel>", ...)
_GO_NERVOUS_EXEC_RE = re.compile(
    r"exec\.Command(?:Context)?\s*\(\s*(?:[^,]+,\s*)?\"nervous\"\s*,\s*\"publish\"\s*,\s*\"([^\"]+)\"",
)

# Match: ChFoo = "tachyonos.bar.baz.v1"  — tachyonac-engine channels.go convention
# Also catches: ChFoo Channel = "..." (any const name prefixed with Ch)
_GO_CH_CONST_RE = re.compile(
    r"\bCh[A-Z][A-Za-z0-9_]*\s*=\s*\"([^\"]+)\"",
)

# Match: .Publish("<channel>", ...) — single-arg (no ctx), tachyonac-engine nbus client
# Separate from the ctx-first pattern above.
_GO_PUBLISH_NO_CTX_RE = re.compile(
    r"\.\s*Publish\s*\(\s*\"([^\"]+)\"",
)

# Match: streamBase + "." + stream  — runtime concatenation pattern.
# When this appears in a file, the publisher prepends a base segment (default
# "bus") to every stream name. We use a fuzzier regex so variations like
# `p.streamBase + "." + stream` or `key := streamBase + "." + name` all hit.
#
# IMPORTANT: this only TELLS us the file is using the prefix pattern. We then
# emit prefixed channels for every <Suffix>Stream/Channel/Topic const we found
# in the same file. The base segment defaults to "bus" per
# hearth-loom/internal/bus/publisher.go:28.
_GO_STREAM_BASE_PREFIX_RE = re.compile(
    r"\bstreamBase\b\s*\+\s*\"\.\"\s*\+\s*[A-Za-z_][\w]*",
)

# Match: streamBase = "<value>"  — the default base segment.
_GO_STREAM_BASE_ASSIGN_RE = re.compile(
    r"\bstreamBase\s*=\s*\"([^\"]+)\"",
)


def scan_go(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []

    # First, collect (lineno, const_value) for *Stream/*Channel/*Topic consts.
    # We'll use these to synthesise prefixed channels if the file also has the
    # streamBase+"."+stream concat pattern.
    const_sites: list[tuple[int, str]] = []

    # Iterate line by line so we get a meaningful lineno, then also run the
    # multi-line-friendly regexes against the full body.
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _GO_CONST_STREAM_RE.finditer(line):
            ch = m.group(2)
            if looks_like_channel(ch):
                found.append((i, ch, "const"))
                const_sites.append((i, ch))
        for m in _GO_CH_CONST_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "const"))
        for m in _GO_NERVOUS_EXEC_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "call"))

    # Multi-line Publish(...) call — ctx-first (hearth-loom style).
    for m in _GO_PUBLISH_CALL_RE.finditer(src):
        ch = m.group(1)
        if not looks_like_channel(ch):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        found.append((lineno, ch, "call"))

    # Single-arg Publish("<channel>", ...) — tachyonac-engine nbus client style.
    for m in _GO_PUBLISH_NO_CTX_RE.finditer(src):
        ch = m.group(1)
        if not looks_like_channel(ch):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        found.append((lineno, ch, "call"))

    # ── streamBase prefix synthesis ────────────────────────────────────────
    # If the file contains a runtime concatenation `streamBase + "." + stream`,
    # the publisher's effective wire-channel is `<streamBase>.<const-value>`.
    # We emit synthetic prefixed channels for each const we collected.
    # Default base is "bus" (hearth-loom/internal/bus/publisher.go:28).
    if _GO_STREAM_BASE_PREFIX_RE.search(src):
        base = "bus"
        m = _GO_STREAM_BASE_ASSIGN_RE.search(src)
        if m and looks_like_channel(m.group(1) + ".x"):
            # Use a literal-looking base if present and valid.
            base = m.group(1)
        for lineno, ch in const_sites:
            prefixed = f"{base}.{ch}"
            if looks_like_channel(prefixed):
                found.append((lineno, prefixed, "const"))

    return found


# ─── Rust regex scanner ──────────────────────────────────────────────────────

_RS_PUBLISH_MACRO_RE = re.compile(r"\bpublish!\s*\(\s*\"([^\"]+)\"")
_RS_CONST_CHANNEL_RE = re.compile(
    r"\bpub\s+const\s+(?:CHANNEL_|STREAM_|TOPIC_)[A-Z0-9_]+\s*:\s*&?\s*'?\s*?\s*str\s*=\s*\"([^\"]+)\"",
)
# kb-style: publish_to_bus("kb.entry.created.v1", ...)
# Uses re.DOTALL so it matches across multi-line calls (channel on next line).
_RS_PUBLISH_TO_BUS_RE = re.compile(r"\bpublish_to_bus\s*\(\s*\"([^\"]+)\"", re.DOTALL)


def scan_rust(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _RS_PUBLISH_MACRO_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "call"))
        for m in _RS_CONST_CHANNEL_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "const"))
    # Full-source pass for multi-line publish_to_bus("...", ...) calls.
    for m in _RS_PUBLISH_TO_BUS_RE.finditer(src):
        ch = m.group(1)
        if not looks_like_channel(ch):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        found.append((lineno, ch, "call"))
    return found


# ─── Shell regex scanner ─────────────────────────────────────────────────────

# Matches: nervous publish <channel> ...
# Greedy on the channel token until whitespace or shell metacharacter.
_SH_NERVOUS_PUBLISH_RE = re.compile(
    r"(?:^|[\s;&|`\$\(])nervous\s+publish\s+([A-Za-z0-9_.\-]+)",
)

# Matches: zellij pipe -p nervous-bus -n <channel> --
# The `-n <channel>` flag is the named pipe / channel name.
_SH_ZELLIJ_PIPE_RE = re.compile(
    r"zellij\s+pipe\s+(?:[^\n]*?-p\s+nervous-bus)?[^\n]*?-n\s+([A-Za-z0-9_.\-]+)",
)


def scan_shell(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []
    for i, line in enumerate(src.splitlines(), start=1):
        # Skip comments — common case: `# nervous publish foo ...` in docs.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in _SH_NERVOUS_PUBLISH_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "call"))
        for m in _SH_ZELLIJ_PIPE_RE.finditer(line):
            ch = m.group(1)
            if looks_like_channel(ch):
                found.append((i, ch, "call"))
    return found


# ─── Walker ──────────────────────────────────────────────────────────────────

EXT_DISPATCH = {
    ".py": scan_python,
    ".go": scan_go,
    ".rs": scan_rust,
    ".sh": scan_shell,
    ".bash": scan_shell,
}


def walk_tree(root: Path, producer: str) -> list[dict]:
    out: list[dict] = []
    root = root.resolve()
    if not root.exists():
        return out
    # If `root` is a single file, scan it directly (useful for tests).
    if root.is_file():
        fn = EXT_DISPATCH.get(root.suffix)
        if fn is not None:
            for lineno, ch, etype in fn(root):
                out.append(
                    {
                        "file": root.name,
                        "line": lineno,
                        "channel": ch,
                        "producer": producer,
                        "emit_type": etype,
                    }
                )
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip noisy / generated trees.
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(p) for p in SKIP_PREFIXES):
            continue
        # Skip test fixtures so we don't pollute coverage with synthetic channels.
        if "/testdata/" in f"/{rel}":
            continue
        fn = EXT_DISPATCH.get(path.suffix)
        if fn is None:
            # Handle shell scripts without extension (e.g. ./nervous, ./bin/foo).
            if path.suffix == "" and path.is_file():
                try:
                    with path.open("rb") as fh:
                        head = fh.readline(256)
                    if head.startswith(b"#!") and (b"bash" in head or b"/sh" in head):
                        fn = scan_shell
                except OSError:
                    pass
            if fn is None:
                continue
        for lineno, ch, etype in fn(path):
            out.append(
                {
                    "file": rel,
                    "line": lineno,
                    "channel": ch,
                    "producer": producer,
                    "emit_type": etype,
                }
            )
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "paths",
        nargs="+",
        help="One or more source roots to scan. Each may be prefixed with "
        "`<producer>=` to label its records (e.g. hearth-loom=/path/to/repo).",
    )
    ap.add_argument(
        "--producer",
        default=None,
        help="Default producer label for un-labelled paths.",
    )
    args = ap.parse_args(argv)

    records: list[dict] = []
    for raw in args.paths:
        if "=" in raw:
            producer, _, p = raw.partition("=")
        else:
            producer = args.producer or Path(raw).name
            p = raw
        records.extend(walk_tree(Path(p), producer))

    # Stable sort: producer, file, line, channel
    records.sort(key=lambda r: (r["producer"], r["file"], r["line"], r["channel"]))
    # Dedupe identical records (a channel can be matched by multiple regexes).
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in records:
        k = (r["producer"], r["file"], r["line"], r["channel"], r.get("emit_type", ""))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)

    json.dump(deduped, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
