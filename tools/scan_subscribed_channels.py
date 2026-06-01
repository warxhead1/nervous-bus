#!/usr/bin/env python3
"""scan_subscribed_channels.py — static scanner for nervous-bus subscribe sites.

The subscribe-side analogue of `scan_emitted_channels.py`. Walks one or more
consumer source trees and extracts the channel/type strings that each repo
SUBSCRIBES TO / CONSUMES. Output is a JSON array of records suitable for piping
into `check_subscription_coverage.py`.

Recognised patterns:

  Python (.py) — via ast:
    - Module/class-level constant assignments whose target name matches one of
      the subscribe-constant conventions (`_*_GLOB`, `_*_GLOBS`, `CHANNEL`,
      `_CHANNEL`, `_CHANNEL_*`, `_DISPATCH_CHANNEL`, `_SUBSCRIPTION_GLOB`,
      `_BUS_*_GLOB`, `_COMMAND_CHANNEL`, `_ERROR_CHANNEL`) whose RHS is a string
      constant — or a list/tuple literal of strings — that `looks_like_channel`.
    - Call sites `*.subscribe(channel_glob=<arg>)`: a string constant is
      recorded directly; a `Name` referencing a collected constant resolves to
      that constant's value(s).

  Rust (.rs) — via regex:
    - `t.starts_with("<ch>")`  → prefix subscription
    - `t == "<ch>"` / `"<ch>" == t` → exact subscription

  Go (.go) — via regex:
    - `NewNbusConsumer(..., []string{ "a", "b" })` → each string a subscription
    - `case "<ch>":` inside a `switch channel` → exact
    - `const <Name>Stream = "nbus:<ch>"` → strip `nbus:` → exact

  TypeScript (.ts) — via regex with constant resolution:
    - `export const CHANNELS = { KEY: 'value', ... } as const` builds a
      key→literal dict.
    - `onNbusEvent(CHANNELS.KEY, ...)` resolves KEY via the dict.
    - `onNbusEvent('literal', ...)` records the literal directly.

  Annotation escape hatch (all languages):
    - A line comment `nbus-sub: <channel>` (`# nbus-sub: ...` or
      `// nbus-sub: ...`) records `<channel>` explicitly. This lets dynamic /
      computed subscribe sites be declared.

A channel ending in `.*` is a prefix subscription (the trailing `.*` is
stripped); everything else is exact. False positives are tolerated; the
coverage checker has an allowlist and baseline. False negatives are the real
risk — be greedy.

Output records:
    {
      "file":       "<relpath>",
      "line":       N,
      "channel":    "<name-or-prefix>",
      "consumer":   "<label>",
      "match_type": "exact" | "prefix"
    }

`prefix` means a glob/starts_with subscription (the channel is stored WITHOUT a
trailing `*` or `.`, e.g. `bus.bead` for `bus.bead.*`); `exact` means a full
channel name.

Usage:
    scan_subscribed_channels.py [<consumer>=]<path> [...]
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

# Relaxed form for PREFIX candidates: a prefix may be a single segment
# (e.g. `autobench`, `tengine`, `tachyonos`) after stripping the trailing
# `.*` / `*` / `.`. We must NOT require an internal dot here, or required
# inventory entries like `autobench.*` → `autobench` get silently dropped.
PREFIX_RE = re.compile(r"^[a-z][a-z0-9._-]*$")

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
    """True if the string is plausibly a nervous-bus channel name (exact form)."""
    if not s:
        return False
    if not CHANNEL_RE.match(s):
        return False
    # Reject obvious non-channel strings.
    if s.startswith(("http", "/", ".", "_")):
        return False
    return True


def looks_like_prefix(s: str) -> bool:
    """True if the string is plausibly a channel prefix (single segment OK)."""
    if not s:
        return False
    if not PREFIX_RE.match(s):
        return False
    if s.startswith(("http", "/", ".", "_")):
        return False
    return True


def classify(raw: str) -> tuple[str, str] | None:
    """Normalise a raw channel/glob string → (channel, match_type) or None.

    A trailing `.*` (or bare `*`) → prefix; the suffix and any trailing `.`
    are stripped. Otherwise exact. Returns None if the string doesn't look
    like a channel under the appropriate (exact vs prefix) validator.
    """
    s = raw.strip()
    if not s:
        return None
    if s.endswith(".*") or s.endswith("*"):
        # Prefix subscription: strip the glob suffix + trailing dot.
        base = s[:-2] if s.endswith(".*") else s[:-1]
        base = base.rstrip(".")
        if looks_like_prefix(base):
            return (base, "prefix")
        return None
    if looks_like_channel(s):
        return (s, "exact")
    return None


# ─── Annotation escape hatch ─────────────────────────────────────────────────

# `# nbus-sub: <channel>` or `// nbus-sub: <channel>`
_ANNOTATION_RE = re.compile(r"nbus-sub:\s*([A-Za-z0-9_.\-*]+)")


def scan_annotations(src: str) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _ANNOTATION_RE.finditer(line):
            res = classify(m.group(1))
            if res is not None:
                found.append((i, res[0], res[1]))
    return found


# ─── Python AST scanner ──────────────────────────────────────────────────────

# Constant-name conventions for subscribe-side channel constants.
_PY_GLOB_SUFFIXES = ("_GLOB", "_GLOBS")
# Exact-match names (bare). endswith() would drag in emit-only constants
# (e.g. _STARTED_CHANNEL, _COMPLETED_CHANNEL); match these exactly.
_PY_EXACT_NAMES = {"CHANNEL", "_CHANNEL", "_DISPATCH_CHANNEL", "_COMMAND_CHANNEL", "_ERROR_CHANNEL"}
# Prefix-name conventions (a constant whose *name* starts with these).
_PY_PREFIX_NAMES = ("_CHANNEL_", "_SUBSCRIPTION_GLOB", "_BUS_")


def _is_subscribe_const_name(name: str) -> bool:
    if name in _PY_EXACT_NAMES:
        return True
    if name.endswith(_PY_GLOB_SUFFIXES):
        return True
    if name == "_SUBSCRIPTION_GLOB":
        return True
    if name.startswith(_PY_PREFIX_NAMES):
        return True
    return False


def _const_string_values(val: ast.AST) -> list[str]:
    """Extract string values from a constant RHS: str | list/tuple of str."""
    out: list[str] = []
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        out.append(val.value)
    elif isinstance(val, (ast.List, ast.Tuple)):
        for elt in val.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
    return out


def scan_python(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, channel, match_type) tuples for a .py file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        # Still honor annotations even if the file doesn't parse.
        return scan_annotations(src)

    found: list[tuple[int, str, str]] = []

    # First pass: collect subscribe-side constants by name → list of raw values.
    const_values: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and _is_subscribe_const_name(tgt.id):
                    vals = _const_string_values(node.value)
                    if vals:
                        const_values.setdefault(tgt.id, []).extend(vals)
                        for v in vals:
                            res = classify(v)
                            if res is not None:
                                found.append((node.lineno, res[0], res[1]))

    # Second pass: *.subscribe(channel_glob=<arg>) call sites.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Only care about a `.subscribe(...)` attribute call.
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "subscribe"):
            continue
        # Find the channel_glob keyword (or first positional, fallback).
        arg: ast.AST | None = None
        for kw in node.keywords:
            if kw.arg == "channel_glob":
                arg = kw.value
                break
        if arg is None and node.args:
            arg = node.args[0]
        if arg is None:
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            res = classify(arg.value)
            if res is not None:
                found.append((node.lineno, res[0], res[1]))
        elif isinstance(arg, ast.Name) and arg.id in const_values:
            for v in const_values[arg.id]:
                res = classify(v)
                if res is not None:
                    found.append((node.lineno, res[0], res[1]))
        elif isinstance(arg, ast.Attribute):
            # self.CHANNEL / cls.CHANNEL — resolve by attribute name.
            if arg.attr in const_values:
                for v in const_values[arg.attr]:
                    res = classify(v)
                    if res is not None:
                        found.append((node.lineno, res[0], res[1]))

    found.extend(scan_annotations(src))
    return found


# ─── Rust regex scanner ──────────────────────────────────────────────────────

# `<x>.starts_with("<ch>")` → prefix (the value may carry a trailing `.`).
_RS_STARTS_WITH_RE = re.compile(r"\.starts_with\(\s*\"([^\"]+)\"\s*\)")
# `<x> == "<ch>"` or `"<ch>" == <x>` → exact.
_RS_EQ_RE = re.compile(r"==\s*\"([^\"]+)\"|\"([^\"]+)\"\s*==")


def scan_rust(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _RS_STARTS_WITH_RE.finditer(line):
            # starts_with may include a trailing `.`; classify() strips it.
            res = classify(m.group(1) + "*")
            if res is not None:
                found.append((i, res[0], "prefix"))
        for m in _RS_EQ_RE.finditer(line):
            ch = m.group(1) or m.group(2)
            res = classify(ch)
            if res is not None:
                # An `==` compare is always an exact subscription, even if the
                # literal happens to end in `.*` (it won't, in practice).
                found.append((i, res[0], "exact"))
    found.extend(scan_annotations(src))
    return found


# ─── Go regex scanner ────────────────────────────────────────────────────────

# NewNbusConsumer(..., []string{ "a", "b" })  — capture the brace body.
_GO_NEWCONSUMER_RE = re.compile(
    r"NewNbusConsumer\s*\([^)]*\[\]string\s*\{([^}]*)\}",
    re.DOTALL,
)
# A quoted string inside a []string{...} body.
_GO_QUOTED_RE = re.compile(r"\"([^\"]+)\"")
# `case "<ch>":` (inside a switch channel).
_GO_CASE_RE = re.compile(r"\bcase\s+\"([^\"]+)\"\s*:")
# `const <Name>Stream = "nbus:<ch>"` (also bare assignment in const block).
_GO_NBUS_STREAM_RE = re.compile(r"\b\w*Stream\s*=\s*\"nbus:([^\"]+)\"")


def scan_go(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []

    # Multi-line NewNbusConsumer([]string{...}) lists.
    for m in _GO_NEWCONSUMER_RE.finditer(src):
        body = m.group(1)
        lineno = src.count("\n", 0, m.start()) + 1
        for sm in _GO_QUOTED_RE.finditer(body):
            res = classify(sm.group(1))
            if res is not None:
                found.append((lineno, res[0], res[1]))

    # Per-line: case "<ch>": and nbus:-prefixed Stream consts.
    for i, line in enumerate(src.splitlines(), start=1):
        for m in _GO_CASE_RE.finditer(line):
            res = classify(m.group(1))
            if res is not None:
                found.append((i, res[0], res[1]))
        for m in _GO_NBUS_STREAM_RE.finditer(line):
            res = classify(m.group(1))
            if res is not None:
                found.append((i, res[0], res[1]))

    found.extend(scan_annotations(src))
    return found


# ─── TypeScript regex scanner ────────────────────────────────────────────────

# `export const CHANNELS = { ... } as const`
_TS_CHANNELS_BLOCK_RE = re.compile(
    r"export\s+const\s+CHANNELS\s*=\s*\{(.*?)\}\s*as\s+const",
    re.DOTALL,
)
# A `KEY: 'value'` entry inside the CHANNELS map.
_TS_CHANNELS_ENTRY_RE = re.compile(r"([A-Z_][A-Z0-9_]*)\s*:\s*['\"]([^'\"]+)['\"]")
# `onNbusEvent(CHANNELS.KEY` — resolved via the map.
_TS_ONNBUS_CONST_RE = re.compile(r"onNbusEvent\(\s*CHANNELS\.([A-Z_][A-Z0-9_]*)")
# `onNbusEvent('literal'` — direct string.
_TS_ONNBUS_LITERAL_RE = re.compile(r"onNbusEvent\(\s*['\"]([^'\"]+)['\"]")


def _ts_channels_map(src: str) -> dict[str, str]:
    out: dict[str, str] = {}
    block = _TS_CHANNELS_BLOCK_RE.search(src)
    if not block:
        return out
    for m in _TS_CHANNELS_ENTRY_RE.finditer(block.group(1)):
        out[m.group(1)] = m.group(2)
    return out


def scan_typescript(path: Path, channels_map: dict[str, str] | None = None) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[int, str, str]] = []
    cmap = channels_map if channels_map is not None else _ts_channels_map(src)

    for i, line in enumerate(src.splitlines(), start=1):
        for m in _TS_ONNBUS_CONST_RE.finditer(line):
            key = m.group(1)
            if key in cmap:
                res = classify(cmap[key])
                if res is not None:
                    found.append((i, res[0], res[1]))
        for m in _TS_ONNBUS_LITERAL_RE.finditer(line):
            res = classify(m.group(1))
            if res is not None:
                found.append((i, res[0], res[1]))

    found.extend(scan_annotations(src))
    return found


# ─── Walker ──────────────────────────────────────────────────────────────────

EXT_DISPATCH = {
    ".py": scan_python,
    ".rs": scan_rust,
    ".go": scan_go,
}


def _nested_repo_prefixes(root: Path) -> tuple[str, ...]:
    """Relative-path prefixes for nested git repos / worktrees under `root`.

    Any subdirectory (other than `root` itself) that contains a `.git` entry —
    file (a worktree pointer) or directory (a real repo) — is a separate
    checkout boundary. We must not descend into it, or its source tree is
    double-counted under both the parent walk and its own path. This is more
    robust than hardcoding directory names.

    Returns posix relative-path prefixes (each ending in `/`).
    """
    prefixes: list[str] = []
    for dotgit in root.rglob(".git"):
        # A `.git` directly under `root` belongs to `root` itself — not nested.
        parent = dotgit.parent
        if parent == root:
            continue
        rel = parent.relative_to(root).as_posix()
        # Skip `.git` entries living inside an already-skipped tree (compare the
        # PARENT's path parts — `.git` itself is in SKIP_DIRS, so don't count it).
        if any(part in SKIP_DIRS for part in parent.relative_to(root).parts):
            continue
        prefixes.append(rel + "/")
    return tuple(sorted(prefixes))


def _collect_ts_channels_map(root: Path, nested: tuple[str, ...] = ()) -> dict[str, str]:
    """Pre-scan the whole tree for the CHANNELS map (lives in schemas.ts)."""
    cmap: dict[str, str] = {}
    for path in root.rglob("*.ts"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(p) for p in SKIP_PREFIXES):
            continue
        if any(rel.startswith(p) for p in nested):
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cmap.update(_ts_channels_map(src))
    return cmap


def walk_tree(root: Path, consumer: str) -> list[dict]:
    out: list[dict] = []
    root = root.resolve()
    if not root.exists():
        return out

    def _emit(rel: str, results: list[tuple[int, str, str]]) -> None:
        for lineno, ch, mtype in results:
            out.append(
                {
                    "file": rel,
                    "line": lineno,
                    "channel": ch,
                    "consumer": consumer,
                    "match_type": mtype,
                }
            )

    # Single-file mode (useful for tests).
    if root.is_file():
        if root.suffix == ".ts":
            _emit(root.name, scan_typescript(root))
        else:
            fn = EXT_DISPATCH.get(root.suffix)
            if fn is not None:
                _emit(root.name, fn(root))
        return out

    # Identify nested git repos / worktrees so we don't double-count them.
    nested = _nested_repo_prefixes(root)

    # Pre-scan the CHANNELS map once for the whole tree (TS).
    ts_map = _collect_ts_channels_map(root, nested=nested)

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(p) for p in SKIP_PREFIXES):
            continue
        # Don't descend into nested git repos / worktrees (separate checkouts).
        if any(rel.startswith(p) for p in nested):
            continue
        # Skip test fixtures so we don't pollute coverage with synthetic channels.
        if "/testdata/" in f"/{rel}":
            continue
        if path.suffix == ".ts":
            _emit(rel, scan_typescript(path, channels_map=ts_map))
            continue
        fn = EXT_DISPATCH.get(path.suffix)
        if fn is None:
            continue
        _emit(rel, fn(path))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "paths",
        nargs="+",
        help="One or more source roots to scan. Each may be prefixed with "
        "`<consumer>=` to label its records (e.g. hearth=/path/to/repo).",
    )
    ap.add_argument(
        "--consumer",
        default=None,
        help="Default consumer label for un-labelled paths.",
    )
    args = ap.parse_args(argv)

    records: list[dict] = []
    for raw in args.paths:
        if "=" in raw:
            consumer, _, p = raw.partition("=")
        else:
            consumer = args.consumer or Path(raw).name
            p = raw
        records.extend(walk_tree(Path(p), consumer))

    # Stable sort: consumer, file, line, channel.
    records.sort(key=lambda r: (r["consumer"], r["file"], r["line"], r["channel"]))
    # Dedupe identical records (a channel can be matched by multiple regexes).
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in records:
        k = (r["consumer"], r["file"], r["line"], r["channel"], r["match_type"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)

    json.dump(deduped, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
