"""transcript_steps.py — tool-call steps recovered from Claude Code transcripts.

The bus (`run_events`) carries a sanitized summary of every tool call: the
command's VERBS (``{"command": "sed cat head"}``), byte counts, and an error
flag. That is enough for histograms but not for any detector that has to read
WHAT changed (a constant 0.35 -> 0.45), WHAT ran (``stats.sh oval rookie 3 8``),
WHAT the output said (``test result: FAILED``), or HOW LONG each call took.

The transcript has all of it. This module turns one transcript into an ordered
list of :class:`Step` (tool_use paired with its tool_result: start, end, input,
bounded output, error flag) and resolves a run-store row to its transcript.
Consumers: ``detectors/param_sweep_thrash``, ``detectors/gate_bound_loosened``,
and ``query.py time``.

Resolution (run -> transcript)
==============================
Every activity event carries ``transcript_path`` (the HOST session's file) and,
for a delegated agent, ``subagent_id``. Claude Code writes the agent's own
transcript at ``<dirname(transcript_path)>/<session_id>/subagents/agent-<id>.jsonl``.
Live files under ``~/.claude/projects`` are tried first, then the durable mirror
``~/.cache/nervous-bus/reflex/transcripts`` (same relative layout; it survives
worktree reaping). Steps are clipped to the run's ``[started, ended]`` window,
because idle-split runs share one transcript.

Stdlib only. Never raises on a corrupt line: a bad record is skipped.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

HOME = Path.home()
LIVE_ROOT = HOME / ".claude" / "projects"
MIRROR_ROOT = HOME / ".cache" / "nervous-bus" / "reflex" / "transcripts"

# Output kept per step: head + tail, so a long log keeps both its first error and
# its final summary line.
OUTPUT_HEAD = 6000
OUTPUT_TAIL = 6000

# Slack (seconds) when clipping steps to a run window: the run's `started` is
# the first PostToolUse event, which lands after the tool_use timestamp.
WINDOW_SLACK_S = 120.0


# ── time helpers ─────────────────────────────────────────────────────────────

def parse_ts(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Python <3.11 fromisoformat rejects >6 fractional digits (the bus stamps ns).
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", v)
    if m and m.group(2) and len(m.group(2)) > 7:
        v = m.group(1) + m.group(2)[:7] + m.group(3)
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


# ── steps ────────────────────────────────────────────────────────────────────

@dataclass
class Step:
    """One tool call: the model's tool_use paired with its tool_result."""

    id: str
    tool: str
    input: dict
    t0: float
    t1: Optional[float] = None
    output: str = ""
    is_error: bool = False
    line: int = 0                 # transcript line of the tool_use (for quoting)
    result_line: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, (self.t1 if self.t1 is not None else self.t0) - self.t0)

    @property
    def command(self) -> str:
        if self.tool == "Bash":
            c = self.input.get("command")
            return c if isinstance(c, str) else ""
        return ""

    @property
    def file_path(self) -> str:
        p = self.input.get("file_path") or self.input.get("notebook_path") or ""
        return p if isinstance(p, str) else ""


def _bound(text: str) -> str:
    if len(text) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return text
    return text[:OUTPUT_HEAD] + "\n[...]\n" + text[-OUTPUT_TAIL:]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for x in content:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                parts.append(x["text"])
        return "\n".join(parts)
    return ""


def parse_transcript_lines(lines: Iterable[str]) -> list[Step]:
    """Steps from transcript lines, in tool_use order.

    Only ``assistant`` / ``user`` records are read: Claude Code also writes
    ``attachment`` and ``api-request*`` records (the bulk of a file) that carry
    duplicate content and no timestamp.
    """
    steps: dict[str, Step] = {}
    order: list[str] = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("assistant", "user"):
            continue
        t = parse_ts(rec.get("timestamp"))
        if t is None:
            continue
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                bid = block.get("id")
                if not isinstance(bid, str) or bid in steps:
                    continue
                inp = block.get("input")
                steps[bid] = Step(id=bid, tool=str(block.get("name") or ""),
                                  input=inp if isinstance(inp, dict) else {},
                                  t0=t, line=n)
                order.append(bid)
            elif kind == "tool_result":
                s = steps.get(block.get("tool_use_id"))
                if s is None or s.t1 is not None:
                    continue
                s.t1 = t
                s.output = _bound(_result_text(block.get("content")))
                s.is_error = bool(block.get("is_error"))
                s.result_line = n
    return [steps[i] for i in order]


def parse_transcript(path: Path | str, since: Optional[float] = None,
                     until: Optional[float] = None) -> list[Step]:
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        steps = parse_transcript_lines(fh)
    if since is not None:
        steps = [s for s in steps if s.t0 >= since - WINDOW_SLACK_S]
    if until is not None:
        steps = [s for s in steps if s.t0 <= until + WINDOW_SLACK_S]
    return steps


# ── run -> transcript resolution ─────────────────────────────────────────────

@dataclass
class RunRef:
    run_id: str
    project: str
    started: Optional[float]
    ended: Optional[float]
    transcript: Optional[Path]
    agent_label: str = ""          # subagent id or "host"


def _relative_to_known_root(p: Path) -> Optional[Path]:
    for root in (LIVE_ROOT, MIRROR_ROOT):
        try:
            return p.relative_to(root)
        except ValueError:
            continue
    return None


def resolve_transcript(transcript_path: str, session_id: str = "",
                       subagent_id: str = "",
                       roots: Optional[list[Path]] = None) -> Optional[Path]:
    """Locate the transcript for a host session or one of its subagents."""
    if not transcript_path:
        return None
    host = Path(transcript_path)
    rel = _relative_to_known_root(host)
    roots = roots if roots is not None else [LIVE_ROOT, MIRROR_ROOT]
    candidates: list[Path] = []
    if subagent_id:
        sess = session_id or host.stem
        sub_rel = Path(f"{sess}/subagents/agent-{subagent_id}.jsonl")
        if rel is not None:
            candidates += [r / rel.parent / sub_rel for r in roots]
        candidates.append(host.parent / sub_rel)
    else:
        if rel is not None:
            candidates += [r / rel for r in roots]
        candidates.append(host)
    for c in candidates:
        if c.is_file():
            return c
    return None


def run_refs(conn: sqlite3.Connection, project: Optional[str] = None,
             since_ts: Optional[str] = None, run_ids: Optional[list[str]] = None,
             roots: Optional[list[Path]] = None,
             min_events: int = 0) -> list[RunRef]:
    """RunRefs for runs in the store, with their transcript resolved (or None)."""
    where, params = ["1=1"], []
    if project:
        where.append("project = ?")
        params.append(project)
    if since_ts:
        where.append("started >= ?")
        params.append(since_ts)
    if min_events:
        where.append("event_count >= ?")
        params.append(min_events)
    if run_ids:
        where.append(f"run_id IN ({','.join('?' * len(run_ids))})")
        params += list(run_ids)
    rows = conn.execute(
        f"SELECT run_id, project, started, ended FROM runs WHERE {' AND '.join(where)} "
        "ORDER BY started", params).fetchall()
    refs: list[RunRef] = []
    for run_id, proj, started, ended in rows:
        raw = conn.execute(
            "SELECT raw_json FROM run_events WHERE run_id = ? ORDER BY seq LIMIT 1",
            (run_id,)).fetchone()
        data: dict = {}
        if raw:
            try:
                env = json.loads(raw[0])
                data = env.get("data", env) if isinstance(env, dict) else {}
            except (json.JSONDecodeError, ValueError):
                data = {}
        sub = str(data.get("subagent_id") or "")
        path = resolve_transcript(str(data.get("transcript_path") or ""),
                                  str(data.get("session_id") or ""), sub, roots)
        refs.append(RunRef(run_id=run_id, project=proj, started=parse_ts(started),
                           ended=parse_ts(ended), transcript=path,
                           agent_label=sub or "host"))
    return refs


class StepCache:
    """Parse each transcript once per pass; clip per run window."""

    def __init__(self):
        self._cache: dict[Path, list[Step]] = {}

    def steps_for(self, ref: RunRef) -> list[Step]:
        if ref.transcript is None:
            return []
        if ref.transcript not in self._cache:
            self._cache[ref.transcript] = parse_transcript(ref.transcript)
        lo = (ref.started or 0.0) - WINDOW_SLACK_S
        hi = (ref.ended or float("inf")) + WINDOW_SLACK_S
        return [s for s in self._cache[ref.transcript] if lo <= s.t0 <= hi]


# ── edits ────────────────────────────────────────────────────────────────────

@dataclass
class EditFrag:
    """One textual change: `old` became `new` in `path` (path may be unknown)."""

    path: str
    old: Optional[str]            # None = created / appended (structural)
    new: str
    via: str                      # Edit | MultiEdit | Write | sed | python | append
    step: Optional[Step] = None


# A numeric literal: not part of an identifier (`T63_MAX`, `w12`) or a longer token
# (`0xf4e6`, `60s`). The optional suffix covers Rust literals (`1.0f64`, `5_usize`).
NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?"
    r"(?:_?[fiu](?:8|16|32|64|size))?(?![A-Za-z0-9_])")
_NUM_RE = NUM_RE


def mask_numbers(s: str) -> str:
    return _NUM_RE.sub("#", s)


def numbers_in(s: str) -> list[str]:
    return [m.group(0) for m in _NUM_RE.finditer(s)]


def is_numeric_only(old: Optional[str], new: str) -> bool:
    """True when old -> new changes only numeric literals."""
    if old is None or old == new:
        return False
    return mask_numbers(old) == mask_numbers(new)


# sed -i [-E] [-e] 'script' file...   (script may hold several `;`-joined s cmds)
_SED_RE = re.compile(
    r"""\bsed\s+(?:-[A-Za-z]*i[A-Za-z]*(?:\.\w+)?\s+)(?:-[A-Za-z]+\s+)*(?:-e\s+)?(?P<q>['"])(?P<script>.*?)(?P=q)\s+(?P<files>[^;&|\n]*)""",
    re.S)


def _split_sed_script(script: str) -> list[tuple[str, str]]:
    """s/a/b/ commands inside a sed script (any delimiter), as (a, b)."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(script):
        m = re.compile(r"(?:^|[;\n]\s*)(?:\d+|/[^/]*/)?s(.)").search(script, i)
        if not m:
            break
        d = m.group(1)
        j = m.end()
        parts: list[str] = []
        buf = []
        while j < len(script) and len(parts) < 2:
            ch = script[j]
            if ch == "\\" and j + 1 < len(script):
                buf.append(script[j:j + 2])
                j += 2
                continue
            if ch == d:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            j += 1
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
        i = j
    return out


def _sed_unescape(s: str) -> str:
    s = re.sub(r"\\([/|#.\[\]*^$&+?(){}])", r"\1", s)
    return s.lstrip("^").rstrip("$")


def _sed_edits(cmd: str) -> list[EditFrag]:
    out: list[EditFrag] = []
    for m in _SED_RE.finditer(cmd):
        files = m.group("files").split()
        path = files[0] if files else ""
        for a, b in _split_sed_script(m.group("script")):
            out.append(EditFrag(path=path, old=_sed_unescape(a), new=_sed_unescape(b), via="sed"))
    return out


_PY_HEREDOC_RE = re.compile(
    r"""python3?\b[^\n<]*?<<-?\s*['"]?(?P<tag>\w+)['"]?[^\n]*\n(?P<body>.*?)\n(?P=tag)\b""", re.S)
_PY_C_RE = re.compile(r"""python3?\b[^\n]*?\s-c\s+(?P<q>['"])(?P<body>.*?)(?<!\\)(?P=q)""", re.S)
_SRC_EXT = re.compile(r"\.(rs|py|ts|tsx|js|mjs|go|c|h|cc|cpp|hpp|toml|json|ya?ml|sh|md|txt|wgsl|glsl|budgets|min_tests)$")


def _python_edits(cmd: str) -> list[EditFrag]:
    """str.replace(old, new) pairs inside an inline python edit script."""
    out: list[EditFrag] = []
    bodies = [m.group("body") for m in _PY_HEREDOC_RE.finditer(cmd)]
    bodies += [m.group("body").replace('\\"', '"') for m in _PY_C_RE.finditer(cmd)]
    for body in bodies:
        try:
            tree = ast.parse(body)
        except (SyntaxError, ValueError):
            continue
        consts = [n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        path = next((c for c in consts if _SRC_EXT.search(c) and "\n" not in c), "")
        # helper functions that wrap .replace (e.g. `def rep(a, b): s = s.replace(a, b)`)
        helpers = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                   and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                           and c.func.attr == "replace" for c in ast.walk(f))}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            f = node.func
            is_rep = (isinstance(f, ast.Attribute) and f.attr == "replace") or \
                     (isinstance(f, ast.Name) and f.id in helpers)
            a, b = node.args[0], node.args[1]
            if is_rep and isinstance(a, ast.Constant) and isinstance(b, ast.Constant) \
                    and isinstance(a.value, str) and isinstance(b.value, str):
                out.append(EditFrag(path=path, old=a.value, new=b.value, via="python"))
    return out


_APPEND_RE = re.compile(r"""\bcat\s+>>?\s*(?P<path>[^\s<;&|]+)\s*<<""")


def bash_edits(cmd: str) -> list[EditFrag]:
    """Source edits made from a shell command (sed -i, python replace, cat >)."""
    out = _sed_edits(cmd) + _python_edits(cmd)
    for m in _APPEND_RE.finditer(cmd):
        out.append(EditFrag(path=m.group("path"), old=None, new="", via="append"))
    return out


def _line_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Changed (old_block, new_block) hunks between two file bodies."""
    a, b = old.splitlines(), new.splitlines()
    hunks: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        hunks.append(("\n".join(a[i1:i2]), "\n".join(b[j1:j2])))
    return hunks


class EditTracker:
    """Turns steps into EditFrags; keeps last-known file bodies to diff Writes."""

    def __init__(self):
        self.bodies: dict[str, str] = {}

    def edits(self, step: Step) -> list[EditFrag]:
        out: list[EditFrag] = []
        inp = step.input
        if step.tool == "Edit":
            old, new = inp.get("old_string"), inp.get("new_string")
            if isinstance(old, str) and isinstance(new, str):
                out.append(EditFrag(step.file_path, old, new, "Edit"))
                body = self.bodies.get(step.file_path)
                if body is not None and old in body:
                    self.bodies[step.file_path] = body.replace(old, new, 1)
        elif step.tool == "MultiEdit":
            for e in inp.get("edits") or []:
                if isinstance(e, dict) and isinstance(e.get("old_string"), str):
                    out.append(EditFrag(step.file_path, e["old_string"],
                                        str(e.get("new_string") or ""), "MultiEdit"))
        elif step.tool == "Write":
            content = inp.get("content")
            if isinstance(content, str):
                prev = self.bodies.get(step.file_path)
                if prev is None:
                    out.append(EditFrag(step.file_path, None, content, "Write"))
                else:
                    for o, n in _line_diff(prev, content):
                        out.append(EditFrag(step.file_path, o, n, "Write"))
                self.bodies[step.file_path] = content
        elif step.tool == "Bash":
            out = bash_edits(step.command)
        for e in out:
            e.step = step
        return out


# ── shell shape helpers (shared by detectors and run_cost) ───────────────────

def strip_heredocs(cmd: str) -> str:
    """The command with heredoc bodies removed (their text is data, not shell)."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", ln)
        if m:
            tag = m.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
        i += 1
    return "\n".join(out)


# A loop whose body sleeps (`until grep -q DONE log; do sleep 10; done`,
# `for i in $(seq 1 55); do ...; sleep 10; done`), or a standalone long sleep.
_POLL_LOOP_RE = re.compile(r"\b(?:until|while|for)\b[\s\S]*?\bdo\b[\s\S]*?\bsleep\s+\d")
_SLEEP_RE = re.compile(
    r"(?:^|[;&|(]|\bthen\b|\bdo\b)\s*sleep\s+(\d+(?:\.\d+)?)([smh]?)(?![\w.])", re.M)
STANDALONE_SLEEP_MIN_S = 30.0


def is_poll_wait(cmd: str) -> bool:
    """True when a shell command is a hand-rolled wait: a sleep-polling loop, or
    a standalone `sleep` of at least STANDALONE_SLEEP_MIN_S seconds."""
    body = strip_heredocs(cmd)
    if _POLL_LOOP_RE.search(body):
        return True
    for m in _SLEEP_RE.finditer(body):
        secs = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
        if secs >= STANDALONE_SLEEP_MIN_S:
            return True
    return False


def is_foreground_bash(step: Step) -> bool:
    return step.tool == "Bash" and not step.input.get("run_in_background")
