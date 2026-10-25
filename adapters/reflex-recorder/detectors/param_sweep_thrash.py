"""detectors/param_sweep_thrash.py — serial parameter sweeps with no gated progress.

SIGNAL
======
Within one run, the agent re-runs the SAME harness again and again where the
only thing that changed since the last run of it is a number: a constant
edited in place (`sed -i 's/^const D_MAX: f64 = 0.5;/... = 1.0;/'`, an Edit
whose old/new differ only in numeric literals, an inline python `.replace`),
or the harness's own numeric arguments / `VAR=value` env settings
(`stats.sh oval rookie 3 8` -> `O_DELAY=6 stats.sh oval rookie 3 8`). Each such
re-run is a PARAM RERUN. Progress is a passing test or gate run (a test
command that is not limited to `--ignored` probes, or a gate script's pass
marker). The detector fires when one stretch without progress holds at least
MIN_PARAM_RERUNS param reruns spread over at least MIN_SPAN_MIN minutes.

A second trigger reads the same habit at the call level: SERIAL CALLS, i.e.
foreground Bash calls over SERIAL_CALL_MIN_S that run a harness more than once
in sequence (`for sd in 1 2 3 4; do S0=$sd ./explore ...; done`, or
`sweep.sh 22 0.8; sweep.sh 17 0.8`). The run fires when it holds at least
MIN_SERIAL_CALLS of them totalling MIN_SERIAL_MIN minutes. The model sits idle
for the whole call, and the points are independent, so they could run in
parallel or as one detached sweep.

That is hand-tuning by trial: each point costs a harness run (minutes, for a
simulation), nothing is checkpointed as a measured result, and nothing proves
the tuned behaviour is better than where it started. The response is to stop
the lane, checkpoint what it learned, and turn the sweep into ONE scripted,
gated measurement (a sweep script that writes a table, then a test that pins
the chosen value's acceptance criterion).

Thresholds were derived 2026-10-24 from podracing lanes (receipts in the PR).
The stopped sonnet podbot lane is the positive. The negatives are a playability
lane that also swept but gated and committed every ~20 min, a yaw-settle lane,
and a rocks lane. The serial-call thresholds were set from all 29 lanes of that
session.

Reads transcripts (transcript_steps): the bus summary keeps only command verbs.
Remediation rung: AUTOMATE candidate (a lane-side counter that interrupts after
N param reruns without a passing gate); this detector itself is INFORM.
"""
from __future__ import annotations

import re
import shlex
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from transcript_steps import (
    EditTracker, Step, StepCache, is_foreground_bash, is_numeric_only, is_poll_wait,
    mask_numbers, run_refs, strip_heredocs,
)

MIN_PARAM_RERUNS = 15
MIN_SPAN_MIN = 45.0
# Second trigger: serial harness calls (a seed / param loop inside ONE foreground call).
SERIAL_CALL_MIN_S = 60.0
MIN_SERIAL_CALLS = 10
MIN_SERIAL_MIN = 20.0

# ── progress (test / gate passes) ────────────────────────────────────────────

TEST_CMD_RE = re.compile(
    r"\b(cargo\s+(?:\+\S+\s+)?(?:nextest\s+run|test)|pytest|go\s+test|npm\s+(?:run\s+)?test|"
    r"pnpm\s+(?:run\s+)?test|yarn\s+test|vitest|jest|node\s+--test|ctest|swift\s+test|"
    r"(?:\S*/)?check(?:_\w+)?\.sh|make\s+(?:test|check))\b")
PROBE_ONLY_RE = re.compile(r"--ignored\b|--include-ignored\b")
PASS_RE = re.compile(
    r"test result: ok\.|CHECK PASS|CHECK FAST OK|\b\d+ passed\b|^ok\s|# fail 0|ℹ fail 0|"
    r"Summary \[.*\] \d+ tests? run: \d+ passed|^PASS\b|\ball tests passed\b", re.M | re.I)
FAILED_RE = re.compile(
    r"test result: FAILED|CHECK FAILED|\b[1-9]\d* failed\b|panicked at|^FAIL\b|# fail [1-9]|ℹ fail [1-9]",
    re.M)


def is_progress(step: Step) -> bool:
    """A passing test/gate run. Probe-only test invocations do not count, and
    neither does a command that merely names a test runner (`cat check.sh`)."""
    if step.tool != "Bash" or step.is_error:
        return False
    ran_test = False
    for seg in shell_segments(_strip_heredocs(step.command)):
        words = _program_words(seg)
        if not words:
            continue
        line = " ".join(words)
        if TEST_CMD_RE.match(line) or (Path(words[0]).name in _SHELLS and len(words) > 1
                                       and TEST_CMD_RE.match(" ".join(words[1:]))) \
                or TEST_CMD_RE.match(Path(words[0]).name):
            if PROBE_ONLY_RE.search(line):
                return False
            ran_test = True
    if not ran_test:
        return False
    out = step.output or ""
    return bool(PASS_RE.search(out)) and not FAILED_RE.search(out)


# ── harness invocations ──────────────────────────────────────────────────────

_KEYWORDS = {"do", "done", "then", "else", "elif", "fi", "{", "}", "!", "while", "until"}
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"time", "timeout", "nice", "env", "nohup", "setsid", "stdbuf", "command", "exec"}


def shell_segments(cmd: str) -> list[str]:
    """Split a shell command into simple commands, respecting quotes.

    Splits at top level on newline, `;`, `&&`, `||`, `|`, `&`, parentheses,
    backticks and `$(`; a sed script's `;` inside quotes stays put."""
    segs, buf, i, q = [], [], 0, None
    while i < len(cmd):
        ch = cmd[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        elif ch in "\n;|&()`" or cmd.startswith("$(", i):
            segs.append("".join(buf))
            buf = []
            if cmd.startswith("$(", i):
                i += 1
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def _program_words(seg: str) -> list[str]:
    """Words of a simple command with leading keywords, env and wrappers removed."""
    try:
        words = shlex.split(seg, comments=False, posix=True)
    except ValueError:
        words = seg.split()
    while words and (words[0] in _KEYWORDS or words[0] in _WRAPPERS
                     or _ENV_RE.match(words[0])):
        w = words.pop(0)
        if w == "timeout" and words and re.fullmatch(r"[\d.]+[smh]?", words[0]):
            words.pop(0)
    if words and words[0] == "for":
        return []
    return words
_SHELLS = {"bash", "sh", "zsh"}
_INTERP = {"node", "python", "python3", "tsx", "deno", "bun", "ts-node", "npx"}
_NOT_HARNESS = {
    # shell builtins / file and text tools / vcs / package managers / build-only
    "cd", "echo", "printf", "cat", "head", "tail", "grep", "rg", "sed", "awk", "cut", "sort",
    "uniq", "wc", "tr", "ls", "find", "mkdir", "rm", "cp", "mv", "chmod", "touch", "tee",
    "git", "gh", "set", "export", "for", "while", "if", "test", "[", "[[", "true", "false",
    "sleep", "wait", "kill", "ps", "pgrep", "ss", "curl", "jq", "diff", "cmp", "paste",
    "column", "xargs", "date", "read", "local", "source", ".", "pnpm", "npm", "yarn", "pip",
    "uptime", "nproc", "which", "stat", "du", "df", "bd", "needs-you", "make", "cmake",
    "ninja", "go", "rustc", "wasm-bindgen", "tar", "unzip", "gzip", "basename", "dirname",
    "realpath", "readlink", "env", "seq", "bc", "expr", "let", "else", "elif", "esac", "case",
    "in", "{", "}", "!", "command", "builtin", "type", "hash", "trap", "exit", "return",
}


@dataclass
class HarnessRun:
    key: str                   # e.g. "stats.sh", "explore", "cargo-test:yaw_settle"
    sig: str                   # invocation with numbers / env values masked
    raw: str                   # invocation as written (args + env)


def _expand_vars(cmd: str) -> str:
    """Substitute simple `NAME=value` assignments made earlier in the command."""
    env: dict[str, str] = {}
    for m in re.finditer(r"(?:^|[;&\n]\s*)([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]+)", cmd):
        env[m.group(1)] = m.group(2).strip("'\"")

    def sub(m):
        return env.get(m.group(1) or m.group(2), m.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, cmd) if env else cmd


_strip_heredocs = strip_heredocs


def harness_runs(cmd: str) -> list[HarnessRun]:
    """Harness invocations in one shell command (programs, scripts, examples)."""
    cmd = _expand_vars(_strip_heredocs(cmd))
    runs: list[HarnessRun] = []
    for seg in shell_segments(cmd):
        try:
            raw_words = shlex.split(seg, comments=False, posix=True)
        except ValueError:
            raw_words = seg.split()
        env_parts = [w for w in raw_words[: len(raw_words) - len(_program_words(seg))]
                     if _ENV_RE.match(w)]
        words = _program_words(seg)
        if not words:
            continue
        prog = words[0]
        args = words[1:]
        key = None
        base = Path(prog).name
        if base in _SHELLS and args and not args[0].startswith("-"):
            prog, args = args[0], args[1:]
            base = Path(prog).name
        if base in _INTERP:
            script = next((a for a in args if not a.startswith("-")), None)
            if base == "npx" and script:
                args = args[args.index(script) + 1:]
                script = next((a for a in args if not a.startswith("-")), None)
            if script and re.search(r"\.(m?[jt]s|py)$", script) and "--test" not in args:
                key = Path(script).name
                args = args[args.index(script) + 1:]
        elif base == "cargo" and args:
            if args[0] == "run" and ("--example" in args or "--bin" in args):
                flag = "--example" if "--example" in args else "--bin"
                i = args.index(flag)
                if i + 1 < len(args):
                    key = args[i + 1]
                    args = args[i + 2:]
            elif args[0] == "test" and PROBE_ONLY_RE.search(" ".join(args)):
                # an ignored probe test used as a measurement harness
                valued = {"-p", "--package", "--test", "--features", "--bin", "--example",
                          "-j", "--jobs", "--target", "--profile", "--test-threads"}
                pos, tname, skip = [], "", False
                for i, a in enumerate(args[1:]):
                    if skip:
                        skip = False
                        continue
                    if a in valued:
                        skip = True
                        if a == "--test" and i + 2 < len(args):
                            tname = args[i + 2]
                        continue
                    if not a.startswith("-"):
                        pos.append(a)
                key = f"cargo-test:{tname}:{pos[0] if pos else ''}"
        elif "/" in prog or prog.endswith(".sh"):
            if base not in _NOT_HARNESS and not TEST_CMD_RE.search(prog):
                key = base
        if key is None or key in _NOT_HARNESS:
            continue
        raw = " ".join(env_parts + args)
        sig = mask_numbers(" ".join(re.sub(r"=.*", "=#", e) for e in env_parts) + " " + " ".join(args))
        runs.append(HarnessRun(key=key, sig=sig.strip(), raw=raw.strip()))
    return runs


# ── the analysis ─────────────────────────────────────────────────────────────

@dataclass
class ParamRerun:
    step: Step
    key: str
    why: str                   # "numeric edit: ..." | "args: a -> b"


@dataclass
class Stretch:
    reruns: list[ParamRerun] = field(default_factory=list)
    harness_seconds: float = 0.0
    start: Optional[float] = None     # last progress (or run start) before it
    end: Optional[float] = None       # progress that closed it (None = run end)

    @property
    def span_min(self) -> float:
        if not self.reruns:
            return 0.0
        return (self.reruns[-1].step.t0 - self.reruns[0].step.t0) / 60.0


def analyze(steps: list[Step]) -> list[Stretch]:
    """Stretches between progress events, each with its param reruns (pure)."""
    tracker = EditTracker()
    stretches: list[Stretch] = [Stretch(start=steps[0].t0 if steps else None)]
    # per harness key: last run's raw invocation, and edits since it
    last_raw: dict[str, str] = {}
    last_sig: dict[str, str] = {}
    pending: dict[str, list] = {}
    for step in steps:
        edits = tracker.edits(step)
        hruns = harness_runs(step.command) if step.tool == "Bash" else []
        # an edit in the same command precedes its harness run (sed -i ... && run)
        for k in pending:
            pending[k].extend(edits)
        for hr in hruns:
            if hr.key in last_raw:
                ed = pending.get(hr.key, [])
                code_edits = [e for e in ed if e.via != "append" or e.old is not None]
                structural = [e for e in ed if not is_numeric_only(e.old, e.new)]
                numeric = [e for e in ed if is_numeric_only(e.old, e.new)]
                why = None
                if numeric and not structural:
                    e = numeric[0]
                    why = f"numeric edit {Path(e.path).name or '?'}: {_short(e.old)} -> {_short(e.new)}"
                elif not code_edits and hr.raw != last_raw[hr.key] and hr.sig == last_sig[hr.key]:
                    why = f"args: {_short(last_raw[hr.key])} -> {_short(hr.raw)}"
                if why:
                    stretches[-1].reruns.append(ParamRerun(step, hr.key, why))
                    stretches[-1].harness_seconds += step.duration
            last_raw[hr.key] = hr.raw
            last_sig[hr.key] = hr.sig
            pending[hr.key] = []
        if is_progress(step):
            stretches[-1].end = step.t0
            stretches.append(Stretch(start=step.t0))
    return stretches


def worst(stretches: list[Stretch]) -> Optional[Stretch]:
    s = [x for x in stretches if x.reruns]
    return max(s, key=lambda x: (len(x.reruns), x.span_min)) if s else None


def fires(st: Optional[Stretch], n: int = MIN_PARAM_RERUNS, t: float = MIN_SPAN_MIN) -> bool:
    return st is not None and len(st.reruns) >= n and st.span_min >= t


# ── serial harness calls (one foreground call running the harness N times) ───

_LOOP_RE = re.compile(r"\b(?:for|while)\b[^\n]*?(?:;|\n)\s*do\b")


def serial_calls(steps: list[Step], min_s: float = SERIAL_CALL_MIN_S) -> list[Step]:
    """Foreground Bash calls longer than `min_s` that run a harness more than once,
    one after another: a `for`/`while` loop around it, or a `a; b; c` chain. A
    sleep-polling wait is not a serial call (that is foreground_wait's signal)."""
    out = []
    for s in steps:
        if not is_foreground_bash(s) or s.duration <= min_s or is_poll_wait(s.command):
            continue
        runs = harness_runs(s.command)
        if len(runs) >= 2 or (runs and _LOOP_RE.search(strip_heredocs(s.command))):
            out.append(s)
    return out


def serial_fires(calls: list[Step], n: int = MIN_SERIAL_CALLS,
                 t: float = MIN_SERIAL_MIN) -> bool:
    return len(calls) >= n and sum(s.duration for s in calls) >= t * 60


def _short(s: Optional[str], n: int = 70) -> str:
    one = " ".join((s or "").split())
    return one if len(one) <= n else one[: n - 3] + "..."


def _iso(t: Optional[float]) -> str:
    if t is None:
        return "run end"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(st: Stretch) -> list[str]:
    keys: dict[str, int] = {}
    for r in st.reruns:
        keys[r.key] = keys.get(r.key, 0) + 1
    ev = [
        f"{len(st.reruns)} param reruns over {st.span_min:.0f} min, "
        f"{st.harness_seconds / 60:.0f} min inside them, no passing test/gate "
        f"from {_iso(st.start)} to {_iso(st.end)}",
        "harnesses: " + ", ".join(f"{k} x{v}" for k, v in sorted(keys.items(), key=lambda kv: -kv[1])),
    ]
    for r in st.reruns[:3] + (st.reruns[-2:] if len(st.reruns) > 5 else []):
        ev.append(f"  {_iso(r.step.t0)} {r.key}: {r.why}")
    return ev


def describe_serial(calls: list[Step]) -> list[str]:
    ev = [f"{len(calls)} foreground calls over {SERIAL_CALL_MIN_S:.0f} s ran a harness "
          f"serially (loop or chain): {sum(s.duration for s in calls) / 60:.0f} min with "
          f"the model idle"]
    for s in sorted(calls, key=lambda s: -s.duration)[:2]:
        ev.append(f"  {_iso(s.t0)} {s.duration / 60:.1f} min: {_short(s.command, 120)}")
    return ev


class ParamSweepThrashDetector(BaseDetector):
    """Flag runs that hand-sweep parameters through a harness without gating."""

    DETECTOR_NAME = "param_sweep_thrash"
    transcript_roots: Optional[list[Path]] = None

    def detect(self, conn: sqlite3.Connection,
               since_ts: Optional[str] = None) -> list[PatternCandidate]:
        cache = StepCache()
        out: list[PatternCandidate] = []
        # a stretch needs >= MIN_PARAM_RERUNS Bash calls: skip runs too small to hold one
        for ref in run_refs(conn, since_ts=since_ts, roots=self.transcript_roots,
                            min_events=MIN_PARAM_RERUNS):
            steps = cache.steps_for(ref)
            if not steps:
                continue
            st = worst(analyze(steps))
            ser = serial_calls(steps)
            by_stretch, by_serial = fires(st), serial_fires(ser)
            if not (by_stretch or by_serial):
                continue
            keys: dict[str, int] = {}
            for k in ([r.key for r in st.reruns] if by_stretch else
                      [h.key for s in ser for h in harness_runs(s.command)]):
                keys[k] = keys.get(k, 0) + 1
            top_key = max(keys, key=keys.get) if keys else "harness"
            ev = [f"run_id={ref.run_id}", f"agent={ref.agent_label}"]
            if st is not None and st.reruns:
                ev += describe(st)
            if ser:
                ev += describe_serial(ser)
            out.append(PatternCandidate(
                project=ref.project,
                pattern_name="param_sweep_thrash",
                signature=f"{ref.project}:{self.DETECTOR_NAME}:{top_key}",
                detector=self.DETECTOR_NAME,
                occurrences=max(len(st.reruns) if st else 0, len(ser)),
                evidence=ev,
                run_ids=[ref.run_id],
                proposed_remediation=(
                    "Stop the lane and checkpoint what it measured. Replace the hand sweep "
                    "with one scripted sweep that runs its points in parallel (or detached) "
                    "and writes a table, then pin the chosen value with a test of its "
                    "acceptance criterion."),
                extra={
                    "remediation_rung": "inform",
                    "triggers": [t for t, on in (("param_reruns", by_stretch),
                                                 ("serial_calls", by_serial)) if on],
                    "param_reruns": len(st.reruns) if st else 0,
                    "span_min": round(st.span_min, 1) if st else 0.0,
                    "harness_min": round(st.harness_seconds / 60, 1) if st else 0.0,
                    "serial_calls": len(ser),
                    "serial_min": round(sum(s.duration for s in ser) / 60, 1),
                    "thresholds": {"min_param_reruns": MIN_PARAM_RERUNS,
                                   "min_span_min": MIN_SPAN_MIN,
                                   "serial_call_min_s": SERIAL_CALL_MIN_S,
                                   "min_serial_calls": MIN_SERIAL_CALLS,
                                   "min_serial_min": MIN_SERIAL_MIN},
                },
            ))
        return out
