"""detectors/guard_denial.py — a run that loses tool calls to harness and hook denials.

SIGNAL
======
Within one run: tool calls whose result is an error AND whose text is a denial
issued before the tool ran. A denied call never reaches PostToolUse, so the bus
summary (`run_events`) cannot see it; the transcript carries the verdict. The
detector reads the transcript tier (transcript_steps), like foreground_wait.

Only `is_error` results are classified, and an ordinary non-zero exit
(`Exit code N`) is never a denial, whatever its output quotes. The families:

  worktree_guard      Claude Code's built-in refusal for an agent running in an
                      isolated worktree ("This agent is isolated in the
                      worktree ... Refusing to run it"). The sub-reason is the
                      clause after "but this command", normalized to one of
                      too_complex, names_git, wrapper (nice/ionice/setsid/
                      nohup), runtime_value, cd_runtime, git_dir_runtime,
                      heredoc_git, other.
  report_write_block  "Subagents should return findings as text".
  sleep_block         "Blocked: sleep N followed by ...".
  hook_block          "PreToolUse:<Tool> hook error ... BLOCKED"; the sub-reason
                      is the basename of the hook script.
  tool_unavailable    "No such tool available: <Tool>"; the sub-reason is the tool.
  permission_denied   a permission rule, the auto-mode classifier, a built-in
                      safety check, or the user declined the call.

Per run it records the total calls, the denials by family and by sub-reason,
and repeats: denials beyond the first in each (family, sub-reason). A repeat is
the agent hitting the same refusal again instead of changing its call shape.
`verbatim_repeats` counts denied calls whose tool and input are identical to an
earlier denied call in the run, the strongest sign that nothing was adapted.

The run fires when it was denied at least MIN_DENIALS times and those denials
are at least MIN_RATE of its calls. The count floor sits above the number of
denials seen in lanes whose briefs were merely imperfect; the rate floor keeps
a long host session with a few stray denials quiet. The derivation and its
margins are in the PR that added this detector.

Almost every denial here is visible in the dispatch brief before the agent
starts (a nice/setsid wrapper, a `file:` operand outside the worktree, a request
for a report file, a `sleep N` wait). The fix is in the brief, not in the agent.

Reads transcripts (transcript_steps). Remediation rung: AUTOMATE. The automate
half is a PreToolUse brief-lint on Agent/Task dispatches (claude-hook-fast#48);
this detector measures what that lint prevents.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from transcript_steps import Step, StepCache, run_refs

MIN_DENIALS = 3
MIN_RATE = 0.01

WORKTREE_GUARD = "worktree_guard"
REPORT_WRITE_BLOCK = "report_write_block"
SLEEP_BLOCK = "sleep_block"
HOOK_BLOCK = "hook_block"
TOOL_UNAVAILABLE = "tool_unavailable"
PERMISSION_DENIED = "permission_denied"

FAMILIES = (WORKTREE_GUARD, REPORT_WRITE_BLOCK, SLEEP_BLOCK, HOOK_BLOCK,
            TOOL_UNAVAILABLE, PERMISSION_DENIED)

WG_SUB_REASONS = ("too_complex", "names_git", "wrapper", "runtime_value", "cd_runtime",
                  "git_dir_runtime", "heredoc_git", "other")

_EXIT_RE = re.compile(r"Exit code -?\d+\b")
_WRAP_RE = re.compile(r"^<tool_use_error>\s*|\s*</tool_use_error>$")

_WG_RE = re.compile(r"This agent is isolated in the worktree\b")
_REPORT_RE = re.compile(r"Subagents should return findings as text\b")
_SLEEP_RE = re.compile(r"Blocked: sleep \d")
_HOOK_RE = re.compile(r"PreToolUse:(\w+) hook error\b")
_TOOL_RE = re.compile(r"(?:Error: )?No such tool available: ([\w:.-]+?)(?:[.\s]|$)")

_PERMISSION_KINDS = (
    (re.compile(r"The user doesn't want to proceed\b|User rejected\b|The user (?:has )?rejected\b"),
     "user_declined"),
    (re.compile(r"Permission for this action was denied by the Claude Code auto mode classifier"),
     "auto_classifier"),
    (re.compile(r"Permission for this command was denied by a built-in Claude Code safety check"),
     "safety_check"),
    (re.compile(r"Permission to use \w+.*?\bhas been denied\b", re.S), "deny_rule"),
    (re.compile(r"Claude requested permissions to use \w+"), "not_granted"),
)

# (pattern, sub-reason) in precedence order; first match wins. Wrapper first so
# a wrapper inside a "too complex" construct is still the wrapper; runtime_value
# before too_complex so "a value computed at runtime ... inside a construct too
# complex" names the cause.
_CLAUSE_RE = re.compile(r"but this command\s+(.*?)(?:\.\s+Refusing to run it|$)", re.S)
_CLAUSE_RULES = (
    (re.compile(r"^(?:runs|hands)\s+(?:nice|ionice|setsid|nohup)\b"), "wrapper"),
    (re.compile(r"^feeds\s+python\b"), "heredoc_git"),
    (re.compile(r"^names git\b"), "names_git"),
    (re.compile(r"^changes directory\b"), "cd_runtime"),
    (re.compile(r"^points git at\b"), "git_dir_runtime"),
    (re.compile(r"computed at runtime|assembled (?:by the command|at runtime)"), "runtime_value"),
    (re.compile(r"too complex to verify"), "too_complex"),
)


@dataclass
class Denial:
    family: str
    sub_reason: str
    step: Step

    @property
    def key(self) -> str:
        return f"{self.family}/{self.sub_reason}"


@dataclass
class RunDenials:
    total_calls: int = 0
    denials: list[Denial] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.denials)

    @property
    def rate(self) -> float:
        return self.count / self.total_calls if self.total_calls else 0.0

    @property
    def by_family(self) -> dict[str, int]:
        return dict(Counter(d.family for d in self.denials))

    @property
    def by_sub_reason(self) -> dict[str, int]:
        return dict(Counter(d.key for d in self.denials))

    @property
    def repeats(self) -> int:
        return sum(n - 1 for n in self.by_sub_reason.values())

    @property
    def verbatim_repeats(self) -> int:
        seen: set[str] = set()
        n = 0
        for d in self.denials:
            sig = d.step.tool + "\0" + json.dumps(d.step.input, sort_keys=True, default=str)
            if sig in seen:
                n += 1
            seen.add(sig)
        return n


def worktree_sub_reason(text: str) -> str:
    """Normalize the clause after "but this command" to one of WG_SUB_REASONS."""
    m = _CLAUSE_RE.search(text)
    if not m:
        return "other"
    clause = " ".join(m.group(1).split())
    for rx, name in _CLAUSE_RULES:
        if rx.search(clause):
            return name
    return "other"


def hook_basename(text: str) -> str:
    """Basename of the hook script named in `hook error: [<script> ...]`."""
    m = re.search(r"hook error:\s*\[([^\]]*)\]", text)
    if not m:
        return "unknown_hook"
    parts = m.group(1).split()
    path = next((p for p in parts if "/" in p), parts[0] if parts else "")
    return os.path.basename(path) or "unknown_hook"


def classify(step: Step) -> Optional[Denial]:
    """The denial a step's result represents, or None."""
    if not step.is_error:
        return None
    text = _WRAP_RE.sub("", step.output.strip())
    if not text or _EXIT_RE.match(text):
        return None
    if _WG_RE.match(text):
        return Denial(WORKTREE_GUARD, worktree_sub_reason(text), step)
    if _REPORT_RE.match(text):
        return Denial(REPORT_WRITE_BLOCK, "report_write", step)
    if _SLEEP_RE.match(text):
        return Denial(SLEEP_BLOCK, "sleep", step)
    if _HOOK_RE.match(text):
        return Denial(HOOK_BLOCK, hook_basename(text), step)
    m = _TOOL_RE.match(text)
    if m:
        return Denial(TOOL_UNAVAILABLE, m.group(1), step)
    for rx, kind in _PERMISSION_KINDS:
        if rx.match(text):
            return Denial(PERMISSION_DENIED, kind, step)
    return None


def summarize(steps: list[Step]) -> RunDenials:
    out = RunDenials(total_calls=len(steps))
    for s in steps:
        d = classify(s)
        if d is not None:
            out.denials.append(d)
    return out


def fires(run: RunDenials, min_denials: int = MIN_DENIALS, min_rate: float = MIN_RATE) -> bool:
    return run.count >= min_denials and run.rate >= min_rate


_WG_FIX = {
    "wrapper": "drop nice/ionice/setsid/nohup; the host governor already sets priority",
    "heredoc_git": "put the python in a script file under the scratch dir and run "
                   "`python3 -I /abs/script.py` instead of a heredoc or -c",
    "names_git": "run git as its own plain command from the worktree root, with no "
                 "pipes, subshells or command substitution around it",
    "cd_runtime": "run from the worktree root; do not cd to a computed directory",
    "git_dir_runtime": "run git without -C or --git-dir pointing at a computed path",
    "runtime_value": "pass literal operands; if a value must be computed, compute it inside "
                     "a script file run as `python3 -I /abs/script.py`",
    "too_complex": "split the command into plain separate calls, or put the logic in a "
                   "script file run as `python3 -I /abs/script.py`",
    "other": "keep each call a plain command; put paths outside the worktree inside a "
             "script file run as `python3 -I /abs/script.py`",
}


def remediation(run: RunDenials) -> str:
    """The brief fix for every family and worktree sub-reason that fired."""
    fam = run.by_family
    subs = run.by_sub_reason
    parts: list[str] = []
    wg = [k.split("/", 1)[1] for k in subs if k.startswith(WORKTREE_GUARD + "/")]
    if wg:
        fixes = [f"{s}: {_WG_FIX[s]}" for s in WG_SUB_REASONS if s in wg]
        parts.append("Isolated-agent briefs must hand out only shapes the worktree guard can verify. "
                     + "; ".join(fixes) + ".")
    if REPORT_WRITE_BLOCK in fam:
        parts.append("Do not ask a subagent for a report file: tell it to return its findings as text.")
    if SLEEP_BLOCK in fam:
        parts.append("Do not tell agents to sleep-wait: use run_in_background and let the harness "
                     "wake the agent on exit.")
    if HOOK_BLOCK in fam:
        names = sorted({k.split("/", 1)[1] for k in subs if k.startswith(HOOK_BLOCK + "/")})
        parts.append("Hook(s) " + ", ".join(names) + " refused calls: read the hook's own message "
                     "for the sanctioned path and state it in the brief.")
    if TOOL_UNAVAILABLE in fam:
        names = sorted({k.split("/", 1)[1] for k in subs if k.startswith(TOOL_UNAVAILABLE + "/")})
        parts.append("This agent type lacks " + ", ".join(names) + ": name the missing tool and "
                     "its Bash equivalent in the brief, or dispatch a type that has it.")
    if PERMISSION_DENIED in fam:
        parts.append("Permission was refused: do not retry the same call; state the allowed "
                     "alternative in the brief.")
    parts.append("Automate: lint the dispatch brief for these shapes before it goes out "
                 "(claude-hook-fast brief-lint).")
    return " ".join(parts)


def _short(s: str, n: int = 110) -> str:
    one = " ".join(s.split())
    return one if len(one) <= n else one[: n - 3] + "..."


def describe(run: RunDenials, max_examples: int = 6) -> list[str]:
    fam = ", ".join(f"{k}={v}" for k, v in sorted(run.by_family.items(), key=lambda kv: -kv[1]))
    ev = [f"{run.count} of {run.total_calls} tool calls denied ({100 * run.rate:.1f}%), "
          f"{run.repeats} repeat(s), {run.verbatim_repeats} verbatim: {fam}"]
    first: dict[str, Denial] = {}
    for d in run.denials:
        first.setdefault(d.key, d)
    counts = run.by_sub_reason
    for key, d in sorted(first.items(), key=lambda kv: -counts[kv[0]])[:max_examples]:
        what = _short(d.step.command) if d.step.tool == "Bash" else (d.step.tool or "?")
        ev.append(f"  L{d.step.line} {key} x{counts[key]}: {what}")
    return ev


class GuardDenialDetector(BaseDetector):
    """Flag runs that lose several tool calls to harness and hook denials."""

    DETECTOR_NAME = "guard_denial"
    transcript_roots: Optional[list[Path]] = None

    def detect(self, conn: sqlite3.Connection,
               since_ts: Optional[str] = None) -> list[PatternCandidate]:
        cache = StepCache()
        out: list[PatternCandidate] = []
        for ref in run_refs(conn, since_ts=since_ts, roots=self.transcript_roots,
                            min_events=2):
            run = summarize(cache.steps_for(ref))
            if not fires(run):
                continue
            out.append(PatternCandidate(
                project=ref.project,
                pattern_name="guard_denial",
                signature=f"{ref.project}:{self.DETECTOR_NAME}",
                detector=self.DETECTOR_NAME,
                occurrences=run.count,
                evidence=[f"run_id={ref.run_id}", f"agent={ref.agent_label}"] + describe(run),
                run_ids=[ref.run_id],
                proposed_remediation=remediation(run),
                extra={
                    "remediation_rung": "automate",
                    "total_calls": run.total_calls,
                    "denials": run.count,
                    "denial_rate": round(run.rate, 4),
                    "by_family": run.by_family,
                    "by_sub_reason": run.by_sub_reason,
                    "repeats": run.repeats,
                    "verbatim_repeats": run.verbatim_repeats,
                    "thresholds": {"min_denials": MIN_DENIALS, "min_rate": MIN_RATE},
                },
            ))
        return out
