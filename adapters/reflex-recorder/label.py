"""label.py — Outcome labeling for Reflexarc runs (PART B, bead nervous-bus-fhr1q).

Assigns outcome ∈ {landed, reverted, abandoned, clean, thrashed, corrected}
to closed runs in runs.db.

PRECEDENCE (explicit > inferred):
1. EXPLICIT — bead_id → bd state + bus.bead.closed
2. EXPLICIT — git_branch → gh pr view (merged/closed-unmerged)
3. EXPLICIT — git log revert detection on branch
4. INFERRED — behavior shape over run_events (primary for key-less runs):
   - thrashed: edit→build-fail→redo n-gram loops
   - abandoned: no resolving action at end
   - clean: resolved with a positive resolving signal (structured commit or edit-and-clean-end)

`corrected` is WEAK without conversation transcripts (Tier-3).  We implement a
conservative best-effort proxy; see _infer_corrected() docstring.

Labels are PROVISIONAL — a PR merging hours later flips the label.  Each
transition appends to label_history (outcome, labeled_at, label_version, source)
and bumps the top-level fields.

PRECEDENCE GUARD: an inferred label (source='behavior_inference') NEVER
overwrites an explicit label (any source other than 'behavior_inference').
The `label_source` column tracks the precedence tier.

PRIVACY: reads bus + bd + git + gh only.  Does NOT read conversation transcripts.

Usage:
    python label.py [--db-path PATH] [--run-id ID] [--dry-run] [--verbose]
    python label.py --report          # print labeled-runs table then exit
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

LABEL_VERSION_CURRENT = 2   # bumped: B1-B5 hardening

# ── Outcome precedence tiers ──────────────────────────────────────────────────
# Higher tier = higher precedence; inferred (tier=1) never overwrites explicit (tier≥2).
_SOURCE_TIER: dict[str, int] = {
    "behavior_inference": 1,
    "pr_closed_unmerged": 2,
    "git_revert": 2,
    # git-ancestry attribution (git_outcome.py) — explicit, between PR and bead.
    # High-confidence git evidence (merge recorded, patch present, pickaxe-confirmed)
    # ranks with PR-merge; the empty-branch and pickaxe-discard signals are
    # explicit ground truth too.
    "git_merged_into_main": 3,
    "git_squash_equiv": 3,
    "git_partial_squash": 2,
    # fast-forward landed, confirmed by the run's own commit evidence
    "git_ff_landed": 3,
    "git_pickaxe_landed": 3,
    "git_revert_on_main": 3,
    "git_empty_branch": 2,
    "git_discarded_verified": 2,
    # explicit git evidence; must outrank behavior_inference
    "git_branch_gone": 2,
    "bead_close": 3,
    "bus_bead_closed": 3,
    "pr_merge": 3,
    # A worker's own `worker_done --outcome`, tier 3 alongside bead/PR/
    # git-merged: it is the worker's own explicit terminal report, not a
    # behavioral guess.
    "orca_worker_done": 3,
}


def _source_tier(source: str) -> int:
    return _SOURCE_TIER.get(source, 1)


# ── Thresholds (conservative — see rationale in _infer_from_behavior) ────────
# Documented here so an adversarial auditor can evaluate:
THRASH_EDIT_FAIL_LOOP_MIN = 3   # minimum edit→bash-fail→edit cycles to call thrash
THRASH_REREAD_RATE_MIN = 0.35   # re-Read rate > 35% of all tool calls → thrash signal
THRASH_BASH_FAIL_RATE_MIN = 0.30  # bash failure rate > 30% of bash calls → thrash
ABANDON_MIN_EVENTS = 5           # don't call abandon on tiny runs (<5 events)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse the RFC3339-UTC strings runs.db stores; None on anything else."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _run_cmd(cmd: list[str], timeout: int = 8) -> Optional[str]:
    """Run a subprocess; return stdout on success, None on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _bd_bead_state(bead_id: str) -> Optional[str]:
    """Query bd for bead state. Returns e.g. 'CLOSED', 'OPEN', 'IN_PROGRESS', None."""
    out = _run_cmd(["bd", "show", bead_id])
    if not out:
        return None
    # bd show output contains e.g. "CLOSED" or "OPEN" on the first line
    # Pattern: [● P1 · CLOSED] or [● P1 · OPEN] etc.
    m = re.search(r"\·\s+([A-Z_]+)\s*\]", out)
    if m:
        return m.group(1).upper()
    return None


def _bd_structured_resolution(bead_id: str) -> Optional[str]:
    """Extract the structured resolution/status field from bd show output.

    M2 fix: key off bd's structured status/resolution field, not free-text
    substring matching that false-positives on 'reverted the bad approach,
    shipped the fix'.

    Returns the raw resolution string (e.g. 'wontfix', 'abandoned', 'reverted',
    'done', 'merged') if present, None otherwise.
    """
    out = _run_cmd(["bd", "show", bead_id])
    if not out:
        return None
    # Look for structured "Resolution:" or "Status:" line with a single token value
    # Example: "Resolution: wontfix" or "Resolution: done"
    m = re.search(r"^(?:Resolution|Status):\s*(\S+)", out, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip().lower()
    # Fallback: "Close reason:" but only extract single-token structured values.
    # Avoid matching free-text descriptions.
    m2 = re.search(r"^Close reason:\s*(\S+)\s*$", out, re.IGNORECASE | re.MULTILINE)
    if m2:
        return m2.group(1).strip().lower()
    return None


def _run_cmd_cwd(cmd: list[str], cwd: str, timeout: int = 8) -> Optional[str]:
    """Like _run_cmd, but runs the subprocess with the given working directory."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _gh_pr_state(branch: str, worktree_path: Optional[str]) -> Optional[dict]:
    """Query gh pr for the most recent PR on branch.

    `gh` has no `-C` flag; run it with cwd=work_dir so it resolves that
    repo's remote. Degrades safely to None on no-PR or network error.

    Returns dict with {state, mergedAt} or None if no PR found.
    """
    # Determine the working directory for gh: prefer the worktree path, fall
    # back to ".".  We don't gate on Path.exists() here — the worktree may have
    # been cleaned up but the parent project git repo is still valid for gh.
    # If gh fails (no remote, network error), _run_cmd_cwd returns None and we
    # degrade safely to None without mislabeling.
    work_dir = worktree_path if worktree_path else "."

    out = _run_cmd_cwd(
        ["gh", "pr", "view", branch, "--json", "state,mergedAt,closedAt"],
        cwd=work_dir,
        timeout=10,
    )
    if out:
        try:
            return json.loads(out)
        except Exception:
            pass
    return None


def _git_has_revert(branch: str, worktree_path: Optional[str]) -> bool:
    """Check if there's a revert commit on this branch.

    Uses git log on the branch (or in the worktree directory).
    Conservative: only flags explicit "Revert" commit messages.
    """
    work_dir = worktree_path or "."
    if not Path(work_dir).exists():
        return False
    out = _run_cmd(
        ["git", "-C", work_dir, "log", "--oneline", "--max-count=50", branch],
        timeout=5,
    )
    if not out:
        return False
    lines = out.lower().splitlines()
    return any(line.lstrip().startswith("revert ") for line in lines)


# ── Bus signals: check bead.closed on Redis ───────────────────────────────────

def _redis_bead_closed_outcome(bead_id: str) -> Optional[str]:
    """Check nbus:bus.bead.closed stream for this bead_id.

    M1 fix: bus.bead.closed has no disposition field — use the bus event only
    to TRIGGER a bd state lookup, never to assign an outcome directly.

    Returns the bd-derived outcome (via label_from_bead) or None.
    """
    try:
        import redis as redis_lib
        r = redis_lib.Redis(host="localhost", port=6379, db=0,
                            socket_timeout=2, decode_responses=True)
        entries = r.xrevrange("nbus:bus.bead.closed", count=200)
        for _entry_id, fields in entries:
            raw = fields.get("_raw", "{}")
            try:
                obj = json.loads(raw)
                data = obj.get("data", {})
                if data.get("bead_id") == bead_id:
                    # M1: bus event confirmed the bead was closed — now do the
                    # authoritative bd lookup instead of reading a non-existent
                    # disposition field.
                    state = _bd_bead_state(bead_id)
                    if state == "CLOSED":
                        resolution = _bd_structured_resolution(bead_id) or ""
                        if resolution in ("wontfix", "abandoned"):
                            return "abandoned"
                        if resolution == "reverted":
                            return "reverted"
                        return "landed"
                    # Bus says closed but bd disagrees — return None, let other
                    # explicit paths try.
                    return None
            except Exception:
                continue
    except Exception:
        pass
    return None


# ── Explicit labeling from external ground truth ──────────────────────────────

def label_from_bead(bead_id: str, worktree_path: Optional[str]) -> Optional[tuple[str, str]]:
    """Try to derive outcome from bead state.

    Returns (outcome, source) or None.
    Sources: 'bead_close', 'bus_bead_closed'
    """
    # Check bus first (lower latency than bd for fresh closes)
    bus_outcome = _redis_bead_closed_outcome(bead_id)
    if bus_outcome:
        return bus_outcome, "bus_bead_closed"

    # Check bd state
    state = _bd_bead_state(bead_id)
    if not state:
        return None

    if state in ("CLOSED",):
        # M2 fix: use structured resolution field, not free-text
        resolution = _bd_structured_resolution(bead_id) or ""
        if resolution in ("wontfix", "abandoned"):
            return "abandoned", "bead_close"
        if resolution == "reverted":
            return "reverted", "bead_close"
        # Closed with a non-negative resolution → landed
        return "landed", "bead_close"

    if state in ("OPEN", "IN_PROGRESS", "READY"):
        # Not yet closed — no explicit terminal label
        return None

    return None


def label_from_pr(branch: str, worktree_path: Optional[str]) -> Optional[tuple[str, str]]:
    """Try to derive outcome from GitHub PR state.

    Returns (outcome, source) or None.
    """
    pr = _gh_pr_state(branch, worktree_path)
    if not pr:
        # No PR yet — try git revert as fallback
        if _git_has_revert(branch, worktree_path):
            return "reverted", "git_revert"
        return None

    state = (pr.get("state") or "").upper()
    merged_at = pr.get("mergedAt")

    if state == "MERGED" or merged_at:
        return "landed", "pr_merge"

    if state == "CLOSED":
        # Closed without merge
        if _git_has_revert(branch, worktree_path):
            return "reverted", "git_revert"
        return "abandoned", "pr_closed_unmerged"

    # OPEN — not yet resolved
    if _git_has_revert(branch, worktree_path):
        return "reverted", "git_revert"

    return None


# ── Explicit labeling from local git ancestry (no PR required) ────────────────

#: Trunk refs: never a dispatch branch, never git-ancestry-labeled here.
_TRUNK_BRANCHES = frozenset({"main", "master", "HEAD"})

#: Any non-trunk branch: Orca/codex lanes use arbitrary names (warxhead1/<lane>,
#: codex/*), not only the worktree-agent-/wf- shapes.
_DISPATCH_BRANCH_RE = re.compile(r"^(?!(?:main|master|HEAD)$).+$")


def _project_repo_root(run: dict) -> Optional[str]:
    """Resolve the MAIN repo path for a run (where `main` lives), not the worktree.

    Prefers `git rev-parse --git-common-dir` from inside the run's worktree
    (any layout); falls back to stripping a `.claude/worktrees/` suffix (the
    worktree may be gone post-landing), then to ~/projects/<project>.
    """
    wt = run.get("worktree")
    if wt and Path(wt).exists():
        out = _run_cmd(["git", "-C", wt, "rev-parse", "--git-common-dir"])
        if out:
            common_dir = Path(out)
            if not common_dir.is_absolute():
                common_dir = Path(wt) / common_dir
            repo_root = common_dir.parent
            if (repo_root / ".git").exists():
                return str(repo_root)
    if wt:
        m = re.match(r"^(.*?)/\.claude/worktrees/", wt)
        if m and Path(m.group(1), ".git").exists():
            return m.group(1)
    project = run.get("project")
    if project:
        cand = Path.home() / "projects" / project
        if (cand / ".git").exists():
            return str(cand)
    return None


def label_from_git_merge(
    git_branch: str, run: dict, *, verify_discards: bool = True,
    require_dispatch_shape: bool = True, run_has_commit: Optional[bool] = None,
) -> Optional[tuple[str, str]]:
    """Derive outcome from local git ancestry for a worktree-dispatched branch.

    Fills label.py's blind spot for projects that squash-merge agent worktrees
    locally with NO GitHub PR (e.g. tengine): `gh pr view` finds nothing, but
    git ancestry + main's squash trail recover landed/empty/discarded.

    By default (require_dispatch_shape=True), only non-trunk branch names
    (_DISPATCH_BRANCH_RE: anything except main/master/HEAD) are considered —
    this is the FRESH-labeling path (compute_label).

    require_dispatch_shape=False drops the gate entirely: nervous-bus-33's
    reverify path re-checks labels that are ALREADY behavior_inference (never
    a stronger source), against ANY resolvable branch. Measured on the live
    DB (nervous-bus-33): 66/80 of a random abandoned+branch sample used
    Orca-shaped names, so gating reverify on the old regex would have made
    the vast majority of candidates permanently unreachable.

    Returns a terminal (outcome, source) only for HIGH-confidence verdicts;
    pending and weak/ambiguous discards return None (never poison the store
    on patch-id alone). When verify_discards, a medium-confidence discard is
    pickaxe-checked (follows the code through rebase/squash/reword) before
    being trusted.

    run_has_commit: the run's own commit evidence, passed to
    classify_branch_outcome to tell fast-forward-landed from empty branches.
    """
    if not git_branch:
        return None
    if require_dispatch_shape and not _DISPATCH_BRANCH_RE.match(git_branch):
        return None
    repo = _project_repo_root(run)
    if not repo:
        return None
    try:
        import git_outcome as go
    except ImportError:
        return None

    live = go.live_worktree_branches(repo)
    bo = go.classify_branch_outcome(
        repo, git_branch, worktree_live=(git_branch in live), run_has_commit=run_has_commit,
    )

    if bo.outcome is None:          # pending / in-flight — no terminal label
        return None
    if bo.source == "git_discarded_unmerged":
        if not verify_discards:
            return None             # medium confidence; don't write on patch-id alone
        landed = go.verify_discarded_landed(repo, git_branch)
        if landed is True:
            return "landed", "git_pickaxe_landed"
        if landed is False:
            return "abandoned", "git_discarded_verified"
        return None                 # ambiguous (partial / unprobeable) — abstain
    return bo.outcome, bo.source


# ── Explicit labeling from Orca worker_done self-report ────────────────────────

def label_from_orca_worker_done(
    run: dict, since_days: Optional[int] = None,
) -> Optional[tuple[str, str]]:
    """Tier-3 label source: Orca `worker_done --outcome` self-report.

    Thin wrapper so label.py has one call site; see orca_outcome.py for the
    join mechanics (run['session_id'] == the codex rollout's own id) and the
    outcome mapping. Degrades to None (never raises) if orca_outcome can't be
    imported, matching label_from_git_merge's tolerance of a missing sibling
    module.

    since_days bounds the one-time per-process rollout index build to rollouts
    dated within that many days (None = every rollout ever written).
    """
    try:
        import orca_outcome
    except ImportError:
        return None
    return orca_outcome.label_from_orca_worker_done(run, since_days=since_days)


#: A rollout is dated by its session's START day, and a run segment can begin
#: days after its session did, so the rollout scan reaches this many days
#: further back than the oldest run being labeled.
ORCA_ROLLOUT_PAD_DAYS = 14


def _orca_scan_days(runs: list[dict], now: Optional[datetime] = None) -> Optional[int]:
    """Rollout-scan bound (days) that covers every run in `runs`, or None
    (unbounded) when it cannot be derived."""
    starts = []
    for r in runs:
        ts = _parse_ts(r.get("started"))
        if ts is not None:
            starts.append(ts)
    if not starts:
        return None
    now = now or datetime.now(timezone.utc)
    age_days = (now - min(starts)).total_seconds() / 86400.0
    return max(int(age_days) + 1, 1) + ORCA_ROLLOUT_PAD_DAYS


# ── Inferred labeling from behavior shape ─────────────────────────────────────

# Tools classified as resolving actions (indicate productive completion):
# apply_patch is codex-cli's only file-edit tool.
_RESOLVING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "Bash", "apply_patch"})

# Tools classified as exploration (re-Read = rereading already-seen files):
_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})


def _parse_events(run_events: list[dict]) -> list[dict]:
    """Parse raw_json from run_events rows into activity data dicts."""
    parsed = []
    for row in run_events:
        try:
            obj = json.loads(row["raw_json"])
            data = obj.get("data", obj)
            parsed.append(data)
        except Exception:
            pass
    return parsed


def _bash_is_failure(ev: dict) -> bool:
    """Determine if a Bash event represents a failure.

    B2 fix: prefer tool_is_error when present; fall back to parsing
    tool_response_summary for structured stderr/exitCode fields.
    The old heuristic `"error" in resp[:200]` is removed — it over-fires on
    error messages in stdout that are informational.
    """
    # Primary: tool_is_error field (claude-hook-fast c219a98+)
    if ev.get("tool_is_error"):
        return True

    # Fallback: structured tool_response_summary
    resp = ev.get("tool_response_summary", "")
    if not resp:
        return False

    try:
        resp_obj = json.loads(resp)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(resp_obj, dict):
        return False

    # Non-zero exit code
    if resp_obj.get("exitCode", 0) not in (0, None, ""):
        exit_code = resp_obj.get("exitCode")
        if isinstance(exit_code, int) and exit_code != 0:
            return True
    # Legacy camelCase variant
    if resp_obj.get("exit_code", 0) not in (0, None, ""):
        exit_code = resp_obj.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return True

    # Explicit stderr with non-empty content AND interrupted=False
    # (interrupted=True means the agent killed it intentionally — not a failure)
    stderr = resp_obj.get("stderr", "")
    if stderr and resp_obj.get("interrupted") is False:
        # Only count non-trivial stderr (warnings from build tools are normal)
        # Require the stderr to look like a real error, not just a warning line
        stderr_lower = stderr.lower()
        if any(kw in stderr_lower for kw in ("error:", "fatal:", "failed", "panicked")):
            return True

    return False


def _count_bash_failures(events: list[dict]) -> tuple[int, int]:
    """Return (bash_total, bash_failures) from event list."""
    total = 0
    fails = 0
    for ev in events:
        if ev.get("tool_name") == "Bash":
            total += 1
            if _bash_is_failure(ev):
                fails += 1
    return total, fails


def _count_edit_fail_loops(events: list[dict]) -> int:
    """Count Edit→Bash-fail→Edit n-gram loops (thrash signal).

    Conservative: we require the Bash between edits to be a failure per
    _bash_is_failure().  Only counts the loop if all three conditions are met.
    """
    loops = 0
    n = len(events)
    for i in range(1, n - 1):
        prev = events[i - 1]
        curr = events[i]
        nxt = events[i + 1]
        is_edit_before = prev.get("tool_name") in ("Edit", "Write")
        is_bash = curr.get("tool_name") == "Bash"
        is_edit_after = nxt.get("tool_name") in ("Edit", "Write")
        if not (is_edit_before and is_bash and is_edit_after):
            continue
        if _bash_is_failure(curr):
            loops += 1
    return loops


def _has_resolving_commit(events: list[dict]) -> bool:
    """Check if any Bash event contains a resolving commit.

    B3 fix: match the STRUCTURED gitOperation.commit field in
    tool_response_summary (shape: {"gitOperation":{"commit":{"kind":"committed",...}}})
    as the primary signal.  Also match `git -C <path> commit` in tool_summary
    as a secondary signal.  Simple `git commit` substring matching is kept as
    tertiary.  bd close also counts.
    """
    for ev in events:
        if ev.get("tool_name") != "Bash":
            continue

        # PRIMARY: structured gitOperation in tool_response_summary
        resp = ev.get("tool_response_summary", "")
        if resp:
            try:
                resp_obj = json.loads(resp)
                git_op = resp_obj.get("gitOperation", {})
                if git_op.get("commit") or git_op.get("push"):
                    return True
            except (json.JSONDecodeError, TypeError):
                pass

        # SECONDARY: command string patterns (tool_summary or tool_response_summary)
        summary = ev.get("tool_summary", "")
        if summary:
            try:
                summary_obj = json.loads(summary)
                cmd = summary_obj.get("command", "") if isinstance(summary_obj, dict) else str(summary)
            except (json.JSONDecodeError, TypeError):
                cmd = str(summary)
        else:
            cmd = ""

        # Match `git commit`, `git -C <path> commit`, `git push`.
        # Anchored to the subcommand: `--no-commit-id` is not a commit.
        if re.search(r"\bgit(\s+-C\s+\S+)?\s+commit\b", cmd):
            return True
        if re.search(r"\bgit\b.*\bpush\b", cmd):
            return True
        # gh pr create
        if "gh pr create" in cmd:
            return True
        # bd close / bd update (bead lifecycle = resolving action)
        if re.search(r"\bbd\s+close\b|\bbd\s+update\b", cmd):
            return True

    return False


def _has_resolving_edit(events: list[dict]) -> bool:
    """Check if the final ~20% of events contain Edit/Write/apply_patch calls."""
    if not events:
        return False
    tail_start = max(0, len(events) - max(3, len(events) // 5))
    tail = events[tail_start:]
    return any(ev.get("tool_name") in ("Edit", "Write", "apply_patch") for ev in tail)


def _infer_corrected(events: list[dict]) -> Optional[tuple[str, str]]:
    """Best-effort proxy for human correction.

    IMPORTANT CAVEAT: 'corrected' in its true sense (human mid-run course
    correction) lives in the conversation transcript, which is Tier-3 data
    that we deliberately do NOT read here.  This function is intentionally a
    stub — we do NOT set outcome=corrected from this path.

    Returns None always.
    """
    return None


def _infer_from_behavior(
    run: dict,
    events: list[dict],
    verbose: bool = False,
) -> tuple[Optional[str], str]:
    """Infer outcome from behavior shape alone.

    Returns (outcome, source) where outcome may be None (insufficient signal)
    and source='behavior_inference'.

    B5 fix: returns None instead of defaulting to 'clean' for opaque/micro
    runs with no positive resolving signal.  A null outcome means 'not yet
    labeled'; consumers MUST check labeled_at before using.

    THRESHOLDS (documented for auditor review):

    THRASHED:
      - edit→bash-fail→edit loops >= THRASH_EDIT_FAIL_LOOP_MIN (3)
        Rationale: 3+ tight compile-fail loops is a strong signal.
      - OR bash failure rate >= THRASH_BASH_FAIL_RATE_MIN (30%) with >=10 calls
      - Combined with reread rate >= THRASH_REREAD_RATE_MIN (35%)

    ABANDONED:
      - B1 fix: close_reason MUST be 'idle_timeout' (not 'recorder_shutdown').
        recorder_shutdown is operational, not semantic.
      - tool_call event count (raw event_count double-counts codex's
        paired tool_call+tool_return) >= ABANDON_MIN_EVENTS (5)
      - No resolving commit (structured gitOperation or command pattern)
      - No resolving edit in the last 20% of events
      - M3 fix: require a failure/error signal at the boundary OR absence of
        resolving action across multiple idle windows (stronger signal than
        mere absence-of-edit alone).

    CLEAN:
      - B5 fix: requires a POSITIVE resolving signal:
        a) has_resolving_commit (structured commit confirmed), OR
        b) has_resolving_edit AND close_reason is 'ended' (natural close)
      - NOT thrashed, NOT abandoned

    DEFAULT (null):
      - When none of the above signals are strong enough, return None.
      - Tiny runs, pure-read runs, opaque runs with no signal → null.
      - Consumers gate on labeled_at; null = not-yet-labeled.
    """
    if not events:
        return None, "behavior_inference"

    # Heartbeats carry the last tool_name; counting them manufactures activity.
    events = [ev for ev in events if ev.get("event") != "heartbeat"]
    if not events:
        return None, "behavior_inference"

    # Local tool_call count (codex emits tool_call+tool_return per call);
    # runs.event_count is left untouched for other consumers.
    n_events = len(events)
    n_tool_call_events = sum(1 for ev in events if ev.get("event") == "tool_call")
    tool_calls = [ev for ev in events if ev.get("tool_name")]
    n_tool_calls = len(tool_calls) or 1

    # Count tool types
    read_calls = sum(1 for ev in tool_calls if ev.get("tool_name") in _READ_TOOLS)
    reread_rate = read_calls / n_tool_calls

    bash_total, bash_fails = _count_bash_failures(tool_calls)
    bash_fail_rate = bash_fails / bash_total if bash_total >= 10 else 0.0

    edit_fail_loops = _count_edit_fail_loops(tool_calls)

    has_commit = _has_resolving_commit(tool_calls)
    has_edit_tail = _has_resolving_edit(tool_calls)

    close_reason = run.get("close_reason", "")
    # B1 fix: only idle_timeout (not recorder_shutdown) qualifies for abandon
    is_idle_timeout = close_reason == "idle_timeout"

    if verbose:
        print(
            f"  [infer] run={run['run_id'][:12]} events={n_events} "
            f"tool_call_events={n_tool_call_events} "
            f"edit_fail_loops={edit_fail_loops} reread_rate={reread_rate:.2f} "
            f"bash_fail_rate={bash_fail_rate:.2f} bash_total={bash_total} "
            f"has_commit={has_commit} has_edit_tail={has_edit_tail} "
            f"close_reason={close_reason}"
        )

    # THRASH check: needs BOTH the loop signal AND the reread signal
    loop_thrash = edit_fail_loops >= THRASH_EDIT_FAIL_LOOP_MIN
    bash_thrash = (bash_total >= 10) and (bash_fail_rate >= THRASH_BASH_FAIL_RATE_MIN)
    reread_signal = reread_rate >= THRASH_REREAD_RATE_MIN
    is_thrashed = (loop_thrash or bash_thrash) and reread_signal

    if is_thrashed:
        return "thrashed", "behavior_inference"

    # ABANDON check
    # B1: exclude recorder_shutdown; only idle_timeout qualifies.
    # M3: require stronger signal: either has bash failures OR event count is
    # substantial (>= 2 * ABANDON_MIN_EVENTS), not just absence-of-edit.
    if (
        is_idle_timeout
        and n_tool_call_events >= ABANDON_MIN_EVENTS
        and not has_commit
        and not has_edit_tail
    ):
        # M3: require that the run showed some failure/error state or was
        # substantial enough (many events without resolving = stronger abandon signal)
        has_failure_signal = bash_fails > 0 or bash_fail_rate > 0
        is_substantial = n_tool_call_events >= ABANDON_MIN_EVENTS * 2
        if has_failure_signal or is_substantial:
            return "abandoned", "behavior_inference"

    # CLEAN check: B5 fix — requires a POSITIVE resolving signal
    if has_commit:
        return "clean", "behavior_inference"
    if has_edit_tail and close_reason == "ended":
        return "clean", "behavior_inference"

    # DEFAULT: insufficient signal → null (not labeled)
    # This covers: micro runs, pure-read runs, opaque runs, 1-event idle_timeout runs.
    return None, "behavior_inference"


# ── Core labeling function ────────────────────────────────────────────────────

def _prefer_higher_tier(
    a: Optional[tuple[str, str]], b: Optional[tuple[str, str]],
) -> Optional[tuple[str, str]]:
    """Pick whichever of two (outcome, source) results has the higher source
    tier. On a tie, keep `a` — used to prefer a git/PR verdict (more specific:
    can say "landed") over an orca_worker_done verdict (only ever "clean")
    when both are tier 3: orca_worker_done must never downgrade
    a stronger git/pr landed label.
    """
    if a is None:
        return b
    if b is None:
        return a
    return b if _source_tier(b[1]) > _source_tier(a[1]) else a


def _session_has_landed_sibling(conn: sqlite3.Connection, run: dict) -> bool:
    """True if some OTHER run sharing this run's run_key committed, pushed,
    or landed.

    Keyed on run_key, NOT session_id/host_conversation_id: those two are
    shared by a parent host session and every subagent/workflow shard it
    dispatches (bus.agent.run.closed.v1's own schema note), so keying on
    either treats unrelated subagents as siblings of the lead and of each
    other, erasing genuine subagent abandonment. run_key is the
    idle-timeout-split identity (segment.py's continues_run_id chain: one
    agent/pane's run fragmented by the idle boundary) — only those segments
    are the SAME logical run. A sibling counts as "landed elsewhere" on any of:
      - outcome == 'landed' (any source — the strongest possible signal), or
      - outcome == 'clean' AND the label's source is an EXPLICIT tier
        (git/PR/bead/orca_worker_done — not just another behavior_inference
        'clean', which could itself be the same false-positive this fix
        guards against), or
      - features.has_resolving_commit is True (a structured commit/push was
        seen in that segment's own events, regardless of what it got labelled).
    Read-only: one indexed SELECT keyed on run_key.
    """
    run_key = run.get("run_key")
    run_id = run.get("run_id")
    if not run_key:
        return False

    query = "SELECT outcome, label_history, features FROM runs WHERE run_key = ?"
    params: list = [run_key]
    if run_id:
        query += " AND run_id != ?"
        params.append(run_id)

    cur = conn.execute(query, params)
    for sib_outcome, sib_history_json, sib_features_json in cur.fetchall():
        if sib_outcome == "landed":
            return True
        try:
            features = json.loads(sib_features_json or "{}")
        except (json.JSONDecodeError, TypeError):
            features = {}
        if features.get("has_resolving_commit"):
            return True
        if sib_outcome == "clean":
            try:
                history = json.loads(sib_history_json or "[]")
            except (json.JSONDecodeError, TypeError):
                history = []
            source = history[-1].get("source") if history else None
            if source in _EXPLICIT_SOURCES:
                return True
    return False


def compute_label(
    run: dict,
    run_events: list[dict],
    verbose: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    orca_since_days: Optional[int] = None,
) -> Optional[tuple[str, str]]:
    """Compute the best available outcome + source for a run.

    Returns (outcome, source) or None if we cannot determine anything.

    Precedence: bead_id explicit > pr explicit > git_revert / orca_worker_done
    (both tier 3, git preferred on tie) > behavior_inferred. Returns None when
    behavior inference returns None (insufficient signal).

    conn (optional): when given, an 'abandoned' behavior_inference verdict is
    checked against sibling runs sharing this run's run_key before being
    returned — see _session_has_landed_sibling. Callers with no DB connection
    at hand (unit tests, --run-id single-row paths that haven't opened one)
    simply skip that check; it can only ever turn a would-be 'abandoned' into
    None, never the reverse, so omitting conn is always safe, just less
    accurate.
    """
    bead_id = run.get("bead_id")
    git_branch = run.get("git_branch")
    worktree = run.get("worktree")

    # EXPLICIT: bead_id
    if bead_id:
        result = label_from_bead(bead_id, worktree)
        if result:
            if verbose:
                print(f"  [label] bead_id={bead_id} → {result}")
            return result

    # Parsed once: the git tier (run_has_commit) and behavior inference both use them.
    parsed = _parse_events(run_events)
    run_has_commit: Optional[bool] = None
    if parsed:
        non_heartbeat_calls = [
            ev for ev in parsed if ev.get("tool_name") and ev.get("event") != "heartbeat"
        ]
        run_has_commit = _has_resolving_commit(non_heartbeat_calls)

    # EXPLICIT: PR / git_revert / local git ancestry
    git_result: Optional[tuple[str, str]] = None
    if git_branch and git_branch not in ("HEAD", "main", "master"):
        git_result = label_from_pr(git_branch, worktree)
        if git_result is None:
            # EXPLICIT: local git ancestry (squash/merge with no PR — tengine pattern)
            git_result = label_from_git_merge(git_branch, run, run_has_commit=run_has_commit)
        if git_result and verbose:
            print(f"  [label] branch={git_branch} → {git_result}")

    # EXPLICIT: Orca worker_done self-report. Runs entirely in-process from
    # cached rollout text — no subprocess, so it's cheap to always check even
    # when git_result already exists (the tier compare below decides which
    # one wins; it never downgrades git_result).
    orca_result = label_from_orca_worker_done(run, since_days=orca_since_days)
    if orca_result and verbose:
        print(f"  [label] session_id={run.get('session_id')} orca_worker_done → {orca_result}")

    explicit_result = _prefer_higher_tier(git_result, orca_result)
    if explicit_result:
        return explicit_result

    # INFERRED: behavior shape (may return None)
    outcome, source = _infer_from_behavior(run, parsed, verbose=verbose)
    if outcome is None:
        return None

    # An 'abandoned' verdict from behavior_inference is a per-segment read;
    # roll it up to the run's other segments before it's terminal.
    if outcome == "abandoned" and conn is not None and _session_has_landed_sibling(conn, run):
        if verbose:
            print(f"  [label] run={run.get('run_id', '')[:12]} abandoned → None "
                  f"(sibling segment landed; session rollup)")
        return None

    return outcome, source


def compute_features_signals(run: dict, run_events: list[dict]) -> dict:
    """Compute behavioral signal features to store alongside the outcome label.

    These are stored in run.features regardless of whether they affect the label,
    so mining over labeled runs is not lossy even when explicit labels override
    inferred ones.
    """
    parsed = _parse_events(run_events)
    if not parsed:
        return {}

    tool_calls = [ev for ev in parsed if ev.get("tool_name")]
    n_tool_calls = len(tool_calls) or 1

    read_calls = sum(1 for ev in tool_calls if ev.get("tool_name") in _READ_TOOLS)
    bash_total, bash_fails = _count_bash_failures(tool_calls)
    edit_fail_loops = _count_edit_fail_loops(tool_calls)
    has_commit = _has_resolving_commit(tool_calls)

    signals: dict = {}
    if edit_fail_loops > 0:
        signals["thrash_edit_fail_loops"] = edit_fail_loops
    if bash_total > 0:
        signals["bash_fail_rate"] = round(bash_fails / bash_total, 4)
        signals["bash_calls"] = bash_total
        signals["bash_failures"] = bash_fails
    if read_calls > 0:
        signals["reread_rate"] = round(read_calls / n_tool_calls, 4)
        signals["read_calls"] = read_calls
    if has_commit:
        signals["has_resolving_commit"] = True

    return signals


# ── Store update ──────────────────────────────────────────────────────────────

def apply_label(
    conn: sqlite3.Connection,
    run_id: str,
    outcome: str,
    source: str,
    extra_features: Optional[dict] = None,
    dry_run: bool = False,
) -> bool:
    """Apply an outcome label to a run, appending to label_history.

    PRECEDENCE GUARD (Minor fix): an inferred label (source tier=1) NEVER
    overwrites an explicit label (source tier≥2).  The label_history records
    the attempted overwrite but the top-level outcome is NOT changed.

    Returns True if label was written (changed or new), False if unchanged or blocked.
    """
    now = _now_utc()

    cur = conn.execute(
        "SELECT outcome, label_version, label_history, features FROM runs WHERE run_id=?",
        (run_id,),
    )
    row = cur.fetchone()
    if not row:
        return False

    existing_outcome, label_version, label_history_json, features_json = row
    label_history = json.loads(label_history_json or "[]")
    features = json.loads(features_json or "{}")

    # Determine existing source tier from most recent history entry
    existing_source = None
    if label_history:
        existing_source = label_history[-1].get("source", "behavior_inference")
    existing_tier = _source_tier(existing_source or "behavior_inference")
    new_tier = _source_tier(source)

    # PRECEDENCE GUARD: inferred cannot overwrite explicit
    if existing_outcome is not None and new_tier < existing_tier:
        # Inferred trying to overwrite an explicit label — silently skip
        if extra_features:
            features.update(extra_features)
            if not dry_run:
                conn.execute(
                    "UPDATE runs SET features=? WHERE run_id=?",
                    (json.dumps(features), run_id),
                )
        return False

    # Check if label has changed
    if existing_outcome == outcome:
        # No transition — update features only if needed
        if extra_features:
            features.update(extra_features)
            if not dry_run:
                conn.execute(
                    "UPDATE runs SET features=? WHERE run_id=?",
                    (json.dumps(features), run_id),
                )
        return False

    # Label transition: bump version, append to history
    new_version = (label_version or 0) + 1
    history_entry = {
        "outcome": outcome,
        "labeled_at": now,
        "label_version": new_version,
        "source": source,
    }
    label_history.append(history_entry)

    if extra_features:
        features.update(extra_features)

    if not dry_run:
        conn.execute(
            """
            UPDATE runs
            SET outcome=?, labeled_at=?, label_version=?, label_history=?, features=?
            WHERE run_id=?
            """,
            (
                outcome,
                now,
                new_version,
                json.dumps(label_history),
                json.dumps(features),
                run_id,
            ),
        )
    return True


# ── Backfill loop ─────────────────────────────────────────────────────────────

def _backfill_enrich_run(conn: sqlite3.Connection, run: dict) -> dict:
    """At backfill time, attempt to enrich git_branch/bead_id for old runs
    that lack them (e.g. captured before PART A was live).

    Updates the DB row if new values are derived; mutates the run dict in place.
    Returns the (possibly enriched) run dict.
    """
    from enrich import derive_bead_id, derive_git_branch

    if run.get("git_branch"):
        return run  # already have it

    # Re-derive from worktree path / CWD stored in DB (worktree column = abs path)
    # CWD: reconstruct from first event's cwd field
    first_ev_cur = conn.execute(
        "SELECT raw_json FROM run_events WHERE run_id=? ORDER BY seq ASC LIMIT 1",
        (run["run_id"],),
    )
    first_row = first_ev_cur.fetchone()
    cwd = None
    if first_row:
        try:
            obj = json.loads(first_row[0])
            data = obj.get("data", obj)
            cwd = data.get("cwd")
        except Exception:
            pass

    git_branch = derive_git_branch(
        worktree_path=run.get("worktree"),
        cwd=cwd,
    )
    if git_branch:
        bead_id = derive_bead_id(git_branch)
        run["git_branch"] = git_branch
        run["bead_id"] = bead_id
        # Persist the enrichment so future runs of label.py don't re-derive
        conn.execute(
            "UPDATE runs SET git_branch=?, bead_id=? WHERE run_id=?",
            (git_branch, bead_id, run["run_id"]),
        )
    return run


#: A run the pass could not label is retried with exponential backoff and, after
#: this many failed attempts, parked as label_state='unattributable'.
LABEL_MAX_ATTEMPTS = 4

#: Days to wait after the first failed attempt; doubles with each further one
#: (1, 2, 4 ... days), so a run attempted nightly costs one lookup per
#: backoff window instead of one per night.
LABEL_BACKOFF_BASE_DAYS = 1.0

#: Progress line cadence (runs) for long passes; 0 disables.
PROGRESS_EVERY = 100

STATE_UNATTRIBUTABLE = "unattributable"

_ATTEMPT_COLUMNS = ("label_attempts", "last_attempt_at", "label_state")


def _has_attempt_columns(conn: sqlite3.Connection) -> bool:
    have = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    return all(c in have for c in _ATTEMPT_COLUMNS)


def _has_lookup_hooks(run: dict) -> bool:
    """True when an external source (bead, PR/git, Orca rollout) could ever
    produce a verdict for this run that a re-run would find.

    A run with none of those is decided by behavior inference alone, which is
    a pure function of its frozen run_events: a None there is final, so there
    is nothing to back off and retry.
    """
    branch = run.get("git_branch")
    return bool(
        run.get("bead_id")
        or (branch and branch not in _TRUNK_BRANCHES)
        or run.get("agent_kind") == "codex-cli"
    )


def select_label_queue(
    conn: sqlite3.Connection,
    *,
    since_days: Optional[int] = None,
    unlabeled_only: bool = False,
    now: Optional[datetime] = None,
    ignore_backoff: bool = False,
) -> list[dict]:
    """Runs the labeling pass should touch, newest first.

    unlabeled_only is the incremental queue: outcome IS NULL, not parked as
    unattributable, and either never attempted or past its backoff window
    (last_attempt_at + LABEL_BACKOFF_BASE_DAYS * 2**(attempts-1) days).
    ignore_backoff re-queues parked and not-yet-due runs too.

    Newest first so a pass cut off by its time budget has spent it on the
    runs a reader is most likely to look at, instead of re-walking the oldest
    never-labelable ones every night.
    """
    now = now or datetime.now(timezone.utc)
    clauses: list[str] = []
    params: dict = {}
    if since_days is not None:
        clauses.append("started >= :cutoff")
        params["cutoff"] = _fmt_ts(now - timedelta(days=int(since_days)))
    if unlabeled_only:
        clauses.append("outcome IS NULL")
        if not ignore_backoff and _has_attempt_columns(conn):
            clauses.append("COALESCE(label_state, '') != :parked")
            clauses.append(
                "(COALESCE(label_attempts, 0) = 0 OR last_attempt_at IS NULL OR "
                "julianday(last_attempt_at) + :base * (1 << MIN(label_attempts - 1, 20)) "
                "<= julianday(:now))"
            )
            params["parked"] = STATE_UNATTRIBUTABLE
            params["base"] = LABEL_BACKOFF_BASE_DAYS
            params["now"] = _fmt_ts(now)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur = conn.execute(f"SELECT * FROM runs {where} ORDER BY started DESC", params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def record_label_attempt(
    conn: sqlite3.Connection,
    run: dict,
    *,
    labeled: bool,
    now: datetime,
    max_attempts: int = LABEL_MAX_ATTEMPTS,
    dry_run: bool = False,
) -> str:
    """Book one labeling attempt against a run; returns its resulting state.

    labeled        -> attempt counted, any parked state cleared.
    not labeled    -> attempt counted; parked as unattributable once it has
                      used max_attempts, or at once when no external source
                      could ever change the verdict (_has_lookup_hooks).
    """
    attempts = int(run.get("label_attempts") or 0) + 1
    if labeled:
        state = None
    elif attempts >= max_attempts or not _has_lookup_hooks(run):
        state = STATE_UNATTRIBUTABLE
    else:
        state = None
    if not dry_run:
        conn.execute(
            "UPDATE runs SET label_attempts=?, last_attempt_at=?, label_state=? "
            "WHERE run_id=?",
            (attempts, _fmt_ts(now), state, run["run_id"]),
        )
    return state or ("labeled" if labeled else "pending")


def backfill(
    db_path: Path,
    run_id_filter: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
    since_days: Optional[int] = None,
    unlabeled_only: bool = False,
    *,
    ignore_backoff: bool = False,
    max_attempts: int = LABEL_MAX_ATTEMPTS,
    progress_every: int = PROGRESS_EVERY,
    budget_seconds: Optional[float] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Run the labeling pass over all (or one) run(s) in runs.db.

    Also enriches git_branch/bead_id for pre-PART-A runs that lack these fields.
    Returns a list of result dicts for reporting.

    B5 fix: compute_label returning None means 'insufficient signal'; we do NOT
    coerce None → 'clean'.  Such runs are left with outcome=None (unlabeled).

    since_days: bound the pass to runs started in the last N days.  Without
    this, a growing run history means an unbounded, ever-slower pass (this
    function always recomputes explicit sources, which shell out to gh/bd).
    unlabeled_only: the incremental queue (see select_label_queue). A
    landed/reverted/abandoned/clean/thrashed label is, in practice, terminal
    (a merged PR doesn't un-merge; a behavior-inferred label is a deterministic
    function of frozen run_events), so nightly passes only spend gh/bd calls on
    runs that are STILL unresolved AND due: each failed attempt is booked on
    the run (label_attempts / last_attempt_at), the next retry waits twice as
    long, and after max_attempts the run is parked as label_state=
    'unattributable' (outcome stays NULL). --run-id and ignore_backoff bypass
    that bookkeeping filter (an explicit single-run lookup always executes in
    full).
    progress_every: print a progress line to stdout every N runs (0 = off).
    budget_seconds: stop cleanly once this much wall time is spent; the rest
    of the queue is picked up by the next pass.
    """
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    if not dry_run:
        from store import ensure_label_columns
        ensure_label_columns(conn)
    book_attempts = _has_attempt_columns(conn)

    if run_id_filter:
        cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id_filter,))
        cols = [d[0] for d in cur.description]
        runs = [dict(zip(cols, row)) for row in cur.fetchall()]
    else:
        runs = select_label_queue(
            conn, since_days=since_days, unlabeled_only=unlabeled_only,
            now=now, ignore_backoff=ignore_backoff,
        )

    orca_days = _orca_scan_days(runs, now)
    t0 = time.monotonic()
    n_labeled = n_none = n_parked = 0

    results = []
    for idx, run in enumerate(runs, start=1):
        if budget_seconds is not None and time.monotonic() - t0 >= budget_seconds:
            print(f"[label] budget {budget_seconds:.0f}s spent after {idx - 1}/{len(runs)} "
                  f"runs; the rest stay queued for the next pass", flush=True)
            break
        run_id = run["run_id"]

        # PART A enrichment for pre-live runs
        if not dry_run:
            run = _backfill_enrich_run(conn, run)

        # Load events for this run
        ev_cur = conn.execute(
            "SELECT raw_json FROM run_events WHERE run_id=? ORDER BY seq ASC",
            (run_id,),
        )
        run_events = [{"raw_json": row[0]} for row in ev_cur.fetchall()]

        # Compute label
        # B5 fix: None = insufficient signal → leave outcome as null
        label_result = compute_label(run, run_events, verbose=verbose, conn=conn,
                                     orca_since_days=orca_days)
        if label_result is None:
            outcome, source = None, "behavior_inference"
        else:
            outcome, source = label_result

        # Compute behavioral signal features (stored in features regardless)
        signals = compute_features_signals(run, run_events)

        if verbose:
            print(
                f"run {run_id[:12]}.. project={run['project']} "
                f"branch={run.get('git_branch')} bead={run.get('bead_id')} "
                f"→ outcome={outcome} source={source}"
            )

        ends_unlabeled = False
        if outcome is not None:
            changed = apply_label(conn, run_id, outcome, source,
                                  extra_features=signals, dry_run=dry_run)
        else:
            # No label (insufficient signal).
            # B5 retroactive correction: if the existing label was set by
            # behavior_inference (tier=1) and the new inference is null, clear
            # the stale label so the DB reflects our honest signal level.
            # Do NOT clear explicit labels (tier≥2).
            changed = False
            cur2 = conn.execute(
                "SELECT outcome, label_history, features FROM runs WHERE run_id=?",
                (run_id,),
            )
            feat_row = cur2.fetchone()
            if feat_row:
                existing_outcome, existing_history_json, existing_features_json = feat_row
                existing_features = json.loads(existing_features_json or "{}")
                existing_history = json.loads(existing_history_json or "[]")
                # Determine if existing label is inferred-only
                existing_source = (
                    existing_history[-1].get("source", "behavior_inference")
                    if existing_history else "behavior_inference"
                )
                ends_unlabeled = existing_outcome is None
                if existing_outcome is not None and _source_tier(existing_source) == 1:
                    # Existing label was inferred and new inference says null → clear it
                    if not dry_run:
                        conn.execute(
                            "UPDATE runs SET outcome=NULL, labeled_at=NULL WHERE run_id=?",
                            (run_id,),
                        )
                    changed = True
                    ends_unlabeled = True
                if signals and not dry_run:
                    existing_features.update(signals)
                    conn.execute(
                        "UPDATE runs SET features=? WHERE run_id=?",
                        (json.dumps(existing_features), run_id),
                    )

        state = None
        if book_attempts and (outcome is not None or ends_unlabeled):
            state = record_label_attempt(
                conn, run, labeled=outcome is not None, now=now,
                max_attempts=max_attempts, dry_run=dry_run,
            )
        if outcome is not None:
            n_labeled += 1
        elif state == STATE_UNATTRIBUTABLE:
            n_parked += 1
        else:
            n_none += 1

        results.append({
            "run_id": run_id,
            "project": run["project"],
            "run_key_kind": run["run_key_kind"],
            "git_branch": run.get("git_branch"),
            "bead_id": run.get("bead_id"),
            "outcome": outcome,
            "source": source,
            "changed": changed,
            "event_count": run.get("event_count", 0),
            "close_reason": run.get("close_reason"),
            "label_state": state,
        })

        if progress_every and idx % progress_every == 0:
            print(f"[label] {idx}/{len(runs)} runs {time.monotonic() - t0:.0f}s "
                  f"labeled={n_labeled} pending={n_none} unattributable={n_parked}",
                  flush=True)

    conn.close()
    return results


# ── Reverification: re-check inferred abandoned/clean labels against git ──────
#
# nervous-bus-33: only bead-close/PR-merge/git-ancestry labels are EVER checked
# against ground truth after being written.  A behavior_inference verdict
# ('abandoned' or 'clean') is written once from the tail of run_events and then
# NEVER revisited — but the branch it names keeps living: it can be merged
# (locally, squash, or via a PR opened after the run closed) days or weeks
# later.  Measured: at least three branches labelled 'abandoned' were later
# merged into main.  This section re-runs the same EXPLICIT precedence tiers
# `compute_label` already applies to fresh runs (PR state, then git ancestry
# via git_outcome.py) against runs that currently carry only an INFERRED
# label, and — only on a genuine explicit verdict — appends a new
# label_history entry via apply_label (never rewrites/removes prior entries).
#
# Label source provenance: label_history[-1]['source'] is already the
# authoritative record of how the CURRENT outcome was set (apply_label appends
# it on every transition, including the very first label — see
# TestLabelHistoryTransitions in tests/test_label_hardened.py). No new column
# or schema migration is needed; _label_source() below is the read-side helper.
# A run with a non-null outcome but an EMPTY label_history predates this
# history mechanism (pre-dates the labeling system's own hardening) and its
# true provenance cannot be recovered from the DB; select_reverify_candidates
# conservatively treats that case as 'behavior_inference' so it is still
# eligible for git re-verification (an unprovable label is not the same
# guarantee as a provably-explicit one, so it must not be skipped).

REVERIFY_OUTCOMES = ("abandoned", "clean")

#: outcome sources that came from an explicit ground-truth check (git ancestry
#: or PR state), as opposed to behavioural inference over run_events. Mirrors
#: the non-'behavior_inference' half of _SOURCE_TIER.
_EXPLICIT_SOURCES = frozenset(k for k in _SOURCE_TIER if k != "behavior_inference")


def _label_source(run_row: dict) -> str:
    """Return the provenance of run_row's CURRENT outcome.

    Reads label_history[-1]['source'] (the field apply_label always writes on
    every transition). Empty/missing history (pre-dates the history
    mechanism) is conservatively reported as 'behavior_inference' — see the
    module-level note above for why that side is the safe default.
    """
    try:
        history = json.loads(run_row.get("label_history") or "[]")
    except (json.JSONDecodeError, TypeError):
        history = []
    if history:
        return history[-1].get("source") or "behavior_inference"
    return "behavior_inference"


#: A run reverify already looked at (confirmed, flipped, or no explicit signal
#: yet) is not looked at again for this many days. Without it a confirmed
#: verdict leaves no trace (apply_label writes nothing when the outcome is
#: unchanged), so every rerun repeats every gh/git lookup of the one before.
REVERIFY_COOLDOWN_DAYS = 7


def _reverify_stamp(run: dict) -> Optional[dict]:
    raw = run.get("features")
    try:
        feats = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        return None
    stamp = feats.get("reverify") if isinstance(feats, dict) else None
    return stamp if isinstance(stamp, dict) else None


def _reverify_is_fresh(run: dict, now: datetime, cooldown_days: float) -> bool:
    if cooldown_days <= 0:
        return False
    stamp = _reverify_stamp(run)
    at = _parse_ts(stamp.get("at")) if stamp else None
    return at is not None and now - at < timedelta(days=cooldown_days)


def _stamp_reverify(conn: sqlite3.Connection, run_id: str, status: str, now: datetime) -> None:
    row = conn.execute("SELECT features FROM runs WHERE run_id=?", (run_id,)).fetchone()
    try:
        feats = json.loads(row[0] or "{}") if row else {}
    except (json.JSONDecodeError, TypeError):
        feats = {}
    feats["reverify"] = {"at": _fmt_ts(now), "status": status}
    conn.execute("UPDATE runs SET features=? WHERE run_id=?", (json.dumps(feats), run_id))


def select_reverify_candidates(
    conn: sqlite3.Connection,
    since_days: Optional[int] = None,
    outcomes: tuple = REVERIFY_OUTCOMES,
    *,
    now: Optional[datetime] = None,
    cooldown_days: float = 0,
) -> list[dict]:
    """Select runs eligible for git re-verification, newest first.

    Eligible: outcome IN `outcomes` (default abandoned/clean), git_branch is
    set and is not a trunk ref (main/master/HEAD), the run's CURRENT label
    source is 'behavior_inference' (see _label_source), and — when
    `since_days` is given — `started` falls within that window. A positive
    `cooldown_days` additionally drops runs a previous reverify pass stamped
    (features.reverify.at) within that many days; the default 0 keeps the
    selection a pure read of label state, which the false-abandon audit needs
    (dropping confirmed runs would bias its sample).

    since_days bounds the pass the same way label.py's own --since-days does:
    without it, a growing run history means an unbounded, ever-slower scan
    (every candidate shells out to `gh pr view` + local git).
    """
    now = now or datetime.now(timezone.utc)
    clauses = ["outcome IS NOT NULL", "git_branch IS NOT NULL",
               "git_branch NOT IN ('HEAD', 'main', 'master')"]
    params: list = []

    placeholders = ",".join("?" * len(outcomes))
    clauses.append(f"outcome IN ({placeholders})")
    params.extend(outcomes)

    if since_days is not None:
        clauses.append("started >= ?")
        params.append(_fmt_ts(now - timedelta(days=int(since_days))))

    where = "WHERE " + " AND ".join(clauses)
    cur = conn.execute(f"SELECT * FROM runs {where} ORDER BY started DESC", params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return [r for r in rows
            if _label_source(r) not in _EXPLICIT_SOURCES
            and not _reverify_is_fresh(r, now, cooldown_days)]


def _run_has_commit_from_features(run: dict) -> Optional[bool]:
    """Best-effort run_has_commit signal from a run's PERSISTED features JSON.

    reverify_run has only the runs row. compute_features_signals writes
    has_resolving_commit only when True, so absence means unknown, not False.
    """
    raw = run.get("features")
    if not raw:
        return None
    try:
        feats = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(feats, dict) and feats.get("has_resolving_commit"):
        return True
    return None


def reverify_run(
    run: dict, *, verify_discards: bool = True, orca_since_days: Optional[int] = None,
) -> Optional[tuple[str, str]]:
    """Re-run ONLY the explicit precedence tiers (PR state, then git ancestry)
    for a run whose current label is inferred.

    Deliberately mirrors compute_label's explicit-tier ordering (PR before
    git-ancestry) but skips the bead_id tier: a bead close is already an
    explicit source and such runs are excluded by select_reverify_candidates.

    Returns (outcome, source) when an explicit ground-truth verdict exists
    (whether or not it matches the current label — the caller decides what to
    do with a confirmation vs. a flip). Returns None when no explicit signal
    is available yet (no PR, branch still pending / repo unresolvable, and no
    orca_worker_done match) — the run stays exactly as it is; reverify NEVER
    writes a weaker or absent verdict over an existing label.

    Also tries the orca_worker_done tier alongside the git tiers: since every
    candidate here is, by construction (select_reverify_candidates), currently
    only behavior_inference, any tier-3 result from either source is a strict
    upgrade — _prefer_higher_tier just picks git over orca_worker_done on a
    tie (git's "landed" is more specific than orca's "clean").
    """
    git_branch = run.get("git_branch")
    git_result: Optional[tuple[str, str]] = None

    if git_branch and git_branch not in ("HEAD", "main", "master"):
        worktree = run.get("worktree")
        git_result = label_from_pr(git_branch, worktree)

        if git_result is None:
            run_has_commit = _run_has_commit_from_features(run)
            # require_dispatch_shape=False: reverify only ever touches labels
            # that are already behavior_inference (never a stronger source,
            # and never a bead label — see select_reverify_candidates), so
            # dropping the branch-name shape gate here cannot demote or
            # misclassify an explicit label; it can only recover ground truth
            # for branches the fresh-labeling path can't see (Orca-style task
            # branches — see label_from_git_merge's docstring).
            git_result = label_from_git_merge(
                git_branch, run, verify_discards=verify_discards, require_dispatch_shape=False,
                run_has_commit=run_has_commit,
            )

    orca_result = label_from_orca_worker_done(run, since_days=orca_since_days)

    return _prefer_higher_tier(git_result, orca_result)


def reverify_labels(
    db_path: Path,
    since_days: Optional[int] = None,
    dry_run: bool = True,
    verbose: bool = False,
    outcomes: tuple = REVERIFY_OUTCOMES,
    verify_discards: bool = True,
    run_id_filter: Optional[str] = None,
    *,
    cooldown_days: float = REVERIFY_COOLDOWN_DAYS,
    progress_every: int = PROGRESS_EVERY,
    budget_seconds: Optional[float] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Re-verify inferred abandoned/clean labels against git ground truth.

    For every eligible run (select_reverify_candidates), re-runs the explicit
    tiers (reverify_run). When an explicit verdict is found, routes it through
    apply_label — which appends a NEW label_history entry on any outcome
    change (never rewrites/removes existing entries) and is a no-op when the
    outcome is unchanged (the run is still reported as 'confirmed' below, just
    with no new history entry, matching apply_label's existing no-transition
    behavior tested in TestLabelHistoryTransitions.test_same_label_does_not_append).

    Idempotent by construction: a run whose label already reflects the
    explicit tier makes no further change on a second pass, and apply_label's
    own precedence guard additionally prevents a lower-tier source from ever
    clobbering a higher tier back down.

    dry_run (default True, read-only): compute everything, write nothing.
    Only `dry_run=False` (CLI: --apply) persists any change.
    """
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")

    if run_id_filter:
        # An explicit single-run lookup always executes in full.
        candidates = [r for r in select_reverify_candidates(conn, since_days, outcomes, now=now)
                      if r["run_id"] == run_id_filter]
    else:
        candidates = select_reverify_candidates(
            conn, since_days, outcomes, now=now, cooldown_days=cooldown_days)

    orca_days = _orca_scan_days(candidates, now)
    t0 = time.monotonic()
    counts = {"flip": 0, "confirmed": 0, "no_explicit_signal": 0}

    results = []
    for idx, run in enumerate(candidates, start=1):
        if budget_seconds is not None and time.monotonic() - t0 >= budget_seconds:
            print(f"[reverify] budget {budget_seconds:.0f}s spent after {idx - 1}/"
                  f"{len(candidates)} candidates; a rerun skips the {idx - 1} already "
                  f"stamped", flush=True)
            break
        run_id = run["run_id"]
        old_outcome = run.get("outcome")

        reverified = reverify_run(run, verify_discards=verify_discards,
                                  orca_since_days=orca_days)
        if reverified is None:
            if not dry_run:
                _stamp_reverify(conn, run_id, "no_explicit_signal", now)
            counts["no_explicit_signal"] += 1
            results.append({
                "run_id": run_id, "project": run.get("project"),
                "git_branch": run.get("git_branch"),
                "old_outcome": old_outcome, "new_outcome": None, "new_source": None,
                "would_flip": False, "written": False, "status": "no_explicit_signal",
            })
        else:
            new_outcome, new_source = reverified
            would_flip = new_outcome != old_outcome
            status = "flip" if would_flip else "confirmed"
            changed = apply_label(
                conn, run_id, new_outcome, new_source, dry_run=dry_run,
                extra_features={"reverify": {"at": _fmt_ts(now), "status": status}},
            )
            written = bool(changed and not dry_run)
            counts[status] += 1

            if verbose:
                print(f"  [reverify] run={run_id[:12]} branch={run.get('git_branch')} "
                      f"{old_outcome} -> {new_outcome} (source={new_source}) "
                      f"would_flip={would_flip} written={written}")

            results.append({
                "run_id": run_id, "project": run.get("project"),
                "git_branch": run.get("git_branch"),
                "old_outcome": old_outcome, "new_outcome": new_outcome,
                "new_source": new_source,
                "would_flip": would_flip, "written": written,
                "status": status,
            })

        if progress_every and idx % progress_every == 0:
            print(f"[reverify] {idx}/{len(candidates)} {time.monotonic() - t0:.0f}s "
                  f"flip={counts['flip']} confirmed={counts['confirmed']} "
                  f"no_signal={counts['no_explicit_signal']}", flush=True)

    conn.close()
    return results


# ── Retroactive session rollup (#60) ──────────────────────────────────────────
#
# compute_label's fresh path turns an inferred 'abandoned' into "no label" when
# a sibling segment of the same run_key committed/landed (an idle-timeout
# split of one logical run). Labels written BEFORE that rule existed are
# frozen: reverify only re-checks explicit sources. rollup_labels re-applies
# the same rule (_session_has_landed_sibling) to those frozen abandons.

def rollup_labels(
    db_path: Path,
    since_days: Optional[int] = None,
    dry_run: bool = True,
    undo_path: Optional[Path] = None,
    progress_every: int = PROGRESS_EVERY,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Clear behavior_inference 'abandoned' labels whose run_key has a landed sibling.

    Candidates: outcome='abandoned', label source behavior_inference, non-empty
    run_key. A flip clears outcome/labeled_at (as the B5 retroactive clear
    does), keeps label_history intact, records features.rollup, and parks the
    run (label_state='unattributable') so the nightly queue does not recompute
    a verdict that is already known. Explicit labels are never touched.

    dry_run (default True): compute and report, write nothing. A real apply
    needs undo_path: the pre-image of every flipped row is appended there as
    JSON lines BEFORE the row is changed, so a bad batch can be restored.
    """
    if not dry_run and undo_path is None:
        raise ValueError("rollup_labels(dry_run=False) requires undo_path")
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    if not dry_run:
        from store import ensure_label_columns
        ensure_label_columns(conn)

    clauses = ["outcome = 'abandoned'", "run_key IS NOT NULL", "run_key != ''"]
    params: list = []
    if since_days is not None:
        clauses.append("started >= ?")
        params.append(_fmt_ts(now - timedelta(days=int(since_days))))
    cur = conn.execute(
        f"SELECT * FROM runs WHERE {' AND '.join(clauses)} ORDER BY started DESC", params)
    cols = [d[0] for d in cur.description]
    candidates = [r for r in (dict(zip(cols, row)) for row in cur.fetchall())
                  if _label_source(r) not in _EXPLICIT_SOURCES]

    undo = open(undo_path, "a", encoding="utf-8") if (undo_path and not dry_run) else None
    results = []
    t0 = time.monotonic()
    try:
        for idx, run in enumerate(candidates, start=1):
            flip = _session_has_landed_sibling(conn, run)
            written = False
            if flip and not dry_run:
                undo.write(json.dumps({
                    k: run.get(k) for k in
                    ("run_id", "outcome", "labeled_at", "label_version", "label_history",
                     "features", "label_attempts", "last_attempt_at", "label_state")
                }) + "\n")
                undo.flush()
                try:
                    feats = json.loads(run.get("features") or "{}")
                except (json.JSONDecodeError, TypeError):
                    feats = {}
                feats["rollup"] = {"at": _fmt_ts(now), "reason": "landed_sibling_on_run_key"}
                conn.execute(
                    "UPDATE runs SET outcome=NULL, labeled_at=NULL, features=?, "
                    "label_attempts=?, last_attempt_at=?, label_state=? WHERE run_id=?",
                    (json.dumps(feats), int(run.get("label_attempts") or 0) + 1,
                     _fmt_ts(now), STATE_UNATTRIBUTABLE, run["run_id"]),
                )
                written = True
            results.append({
                "run_id": run["run_id"], "project": run.get("project"),
                "agent_kind": run.get("agent_kind"), "run_key": run.get("run_key"),
                "git_branch": run.get("git_branch"), "started": run.get("started"),
                "event_count": run.get("event_count"),
                "would_flip": flip, "written": written,
                "status": "flip" if flip else "kept",
            })
            if progress_every and idx % progress_every == 0:
                print(f"[rollup] {idx}/{len(candidates)} {time.monotonic() - t0:.0f}s "
                      f"flip={sum(1 for r in results if r['would_flip'])}", flush=True)
    finally:
        if undo:
            undo.close()
        conn.close()
    return results


def print_rollup_report(results: list[dict], sample: int = 10) -> None:
    flips = [r for r in results if r["would_flip"]]
    by_proj: dict[str, int] = {}
    for r in flips:
        key = f"{r.get('project')}/{r.get('agent_kind')}"
        by_proj[key] = by_proj.get(key, 0) + 1
    print(f"Rollup candidates: {len(results)}  flip={len(flips)}  "
          f"kept={len(results) - len(flips)}  written={sum(1 for r in results if r['written'])}")
    for k, v in sorted(by_proj.items(), key=lambda kv: -kv[1]):
        print(f"  {k:48s} {v}")
    if flips and sample:
        print(f"\nFirst {min(sample, len(flips))} flips (hand-check run_key siblings):")
        for r in flips[:sample]:
            print(f"  {r['run_id']}  run_key={r['run_key']}  project={r['project']}  "
                  f"events={r['event_count']}  started={r['started']}")


def false_abandon_rate(
    conn: sqlite3.Connection,
    n: int = 60,
    seed: int = 42,
    verify_discards: bool = True,
) -> dict:
    """Measure the false-abandon rate over a reproducible seeded sample.

    Population: runs currently labelled 'abandoned' by behavior_inference with
    a non-trunk git_branch (select_reverify_candidates(outcomes=('abandoned',))
    against the connection's live data — READ-ONLY, no label is written here).

    Sample: `random.Random(seed).sample(population, min(n, len(population)))` —
    deterministic for a given (population identity, n, seed) so re-running
    this over an unchanged DB reproduces the same rate.

    false_abandon_rate = (# sampled runs whose explicit git tier recovers a
    non-abandoned outcome) / (# sampled runs), i.e. the denominator is the
    FULL sample (conservative: a run with no explicit signal yet counts
    against the rate as 'not proven false', not excluded) — reported alongside
    `no_signal` so the caller can see how many of those undetermined runs
    there were.

    Returns a dict: {n_population, n_sampled, seed, n_false, n_confirmed,
    n_no_signal, rate, examples: [{run_id, project, git_branch, new_outcome,
    new_source}, ...]} — examples are the flipped (false-abandon) runs only,
    capped at 20 for report size.
    """
    import random

    population = select_reverify_candidates(conn, since_days=None, outcomes=("abandoned",))
    rng = random.Random(seed)
    sample = population if len(population) <= n else rng.sample(population, n)

    n_false = n_confirmed = n_no_signal = 0
    examples: list[dict] = []
    for run in sample:
        reverified = reverify_run(run, verify_discards=verify_discards)
        if reverified is None:
            n_no_signal += 1
            continue
        new_outcome, new_source = reverified
        if new_outcome != "abandoned":
            n_false += 1
            if len(examples) < 20:
                examples.append({
                    "run_id": run["run_id"], "project": run.get("project"),
                    "git_branch": run.get("git_branch"),
                    "new_outcome": new_outcome, "new_source": new_source,
                })
        else:
            n_confirmed += 1

    n_sampled = len(sample)
    rate = round(n_false / n_sampled, 4) if n_sampled else None
    return {
        "n_population": len(population),
        "n_sampled": n_sampled,
        "seed": seed,
        "n_false": n_false,
        "n_confirmed": n_confirmed,
        "n_no_signal": n_no_signal,
        "rate": rate,
        "examples": examples,
    }


def print_reverify_report(results: list[dict]) -> None:
    """Print a reverify-pass table (mirrors print_report's shape)."""
    header = f"{'run_id':14s}  {'project':16s}  {'old':10s}  {'new':10s}  {'source':22s}  status"
    print(header)
    print("-" * len(header))
    for r in results:
        rid = r["run_id"][:12] + ".."
        print(f"{rid:14s}  {(r['project'] or '')[:16]:16s}  "
              f"{(r['old_outcome'] or ''):10s}  {(r['new_outcome'] or ''):10s}  "
              f"{(r['new_source'] or ''):22s}  {r['status']}")
    n_total = len(results)
    n_flip = sum(1 for r in results if r["status"] == "flip")
    n_confirmed = sum(1 for r in results if r["status"] == "confirmed")
    n_no_signal = n_total - n_flip - n_confirmed
    n_written = sum(1 for r in results if r["written"])
    print()
    print(f"Reverify candidates: {n_total}  flip={n_flip}  confirmed={n_confirmed}  "
          f"no_explicit_signal={n_no_signal}  written={n_written}")


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    """Print a labeled-runs table."""
    header = (
        f"{'run_id':14s}  {'project':20s}  {'kind':9s}  "
        f"{'close_reason':18s}  {'git_branch':30s}  "
        f"{'outcome':10s}  {'source':25s}  events"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        rid = r["run_id"][:12] + ".."
        branch = (r["git_branch"] or "")[:28]
        cr = (r.get("close_reason") or "")[:16]
        outcome_str = r["outcome"] or "null"
        print(
            f"{rid:14s}  {r['project']:20s}  {r['run_key_kind']:9s}  "
            f"{cr:18s}  {branch:30s}  "
            f"{outcome_str:10s}  {r['source']:25s}  {r['event_count']}"
        )

    # Summary stats
    n_total = len(results)
    n_explicit = sum(1 for r in results if r["source"] != "behavior_inference")
    n_inferred = n_total - n_explicit
    n_null = sum(1 for r in results if r["outcome"] is None)
    outcomes: dict[str, int] = {}
    for r in results:
        k = r["outcome"] or "null"
        outcomes[k] = outcomes.get(k, 0) + 1

    print()
    print(f"Total runs: {n_total}")
    print(f"  explicit labels: {n_explicit}")
    print(f"  inferred labels: {n_inferred}")
    print(f"  unlabeled (null): {n_null}")
    print(f"  outcome breakdown: {outcomes}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Reflexarc outcome labeler")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--run-id", default=None, help="Label a single run_id only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute labels but do not write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="Run backfill then print report table")
    parser.add_argument("--since-days", type=int, default=None,
                        help="Only consider runs started in the last N days "
                             "(bounds cost against a growing run history)")
    parser.add_argument("--unlabeled-only", action="store_true",
                        help="Incremental queue for nightly automation: only runs with no "
                             "outcome that were never attempted or are past their backoff "
                             "window (1, 2, 4 days), newest first; a run that fails "
                             "--max-attempts times is parked as label_state=unattributable")
    parser.add_argument("--ignore-backoff", action="store_true",
                        help="With --unlabeled-only: also retry runs that are not yet due "
                             "or are parked as unattributable")
    parser.add_argument("--max-attempts", type=int, default=LABEL_MAX_ATTEMPTS,
                        help="Failed attempts before a run is parked as unattributable "
                             f"(default {LABEL_MAX_ATTEMPTS})")
    parser.add_argument("--progress-every", type=int, default=PROGRESS_EVERY, metavar="N",
                        help="Print a progress line every N runs (0 = off)")
    parser.add_argument("--budget-seconds", type=float, default=None, metavar="S",
                        help="Stop cleanly after S seconds of work; the remaining queue is "
                             "picked up by the next pass")
    parser.add_argument("--reverify-cooldown-days", type=float, default=REVERIFY_COOLDOWN_DAYS,
                        metavar="N",
                        help="With --reverify-days: skip runs an earlier reverify pass "
                             f"already looked at within N days (default {REVERIFY_COOLDOWN_DAYS}; "
                             "0 re-checks everything)")
    parser.add_argument("--rollup-days", type=int, default=None, metavar="N",
                        help="Retroactively apply the run_key segment rollup to "
                             "behavior_inference 'abandoned' labels started in the last N days "
                             "(#60): clear the label when a sibling segment landed. Dry-run "
                             "unless --apply (which also needs --undo-file).")
    parser.add_argument("--undo-file", type=Path, default=None,
                        help="With --rollup-days --apply: JSONL file that receives each "
                             "flipped row's pre-image before it is changed")
    parser.add_argument("--reverify-days", type=int, default=None, metavar="N",
                        help="Re-verify inferred abandoned/clean labels with a non-trunk "
                             "git_branch, started in the last N days, against the explicit "
                             "git/PR tiers (nervous-bus-33). Read-only unless --apply is also "
                             "given. Mutually exclusive with a normal backfill pass.")
    parser.add_argument("--apply", action="store_true",
                        help="With --reverify-days or --rollup-days: actually write "
                             "(default is dry-run).")
    parser.add_argument("--false-abandon-rate", action="store_true",
                        help="Measure the false-abandon rate over a seeded sample of "
                             "behavior_inference 'abandoned' runs with a git branch. "
                             "ALWAYS read-only regardless of --apply. Use with --sample-n/--seed.")
    parser.add_argument("--sample-n", type=int, default=60,
                        help="Sample size for --false-abandon-rate (default 60)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for --false-abandon-rate (default 42, for reproducibility)")
    args = parser.parse_args()

    if args.false_abandon_rate:
        conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            report = false_abandon_rate(conn, n=args.sample_n, seed=args.seed)
        finally:
            conn.close()
        print(json.dumps(report, indent=2))
        return 0

    if args.rollup_days is not None:
        if args.apply and args.undo_file is None:
            parser.error("--rollup-days --apply requires --undo-file")
        results = rollup_labels(
            db_path=args.db_path,
            since_days=args.rollup_days,
            dry_run=not args.apply,
            undo_path=args.undo_file,
            progress_every=args.progress_every,
        )
        print_rollup_report(results)
        mode = "" if args.apply else "[DRY-RUN] "
        n_flip = sum(1 for r in results if r["would_flip"])
        print(f"\n{mode}Rollup checked {len(results)} abandoned labels; {n_flip} flip"
              f"{' (written)' if args.apply else ''}.")
        return 0

    if args.reverify_days is not None:
        results = reverify_labels(
            db_path=args.db_path,
            since_days=args.reverify_days,
            dry_run=not args.apply,
            verbose=args.verbose,
            run_id_filter=args.run_id,
            cooldown_days=args.reverify_cooldown_days,
            progress_every=args.progress_every,
            budget_seconds=args.budget_seconds,
        )
        print_reverify_report(results)
        mode = "" if args.apply else "[DRY-RUN] "
        n_flip = sum(1 for r in results if r["status"] == "flip")
        print(f"\n{mode}Reverified {len(results)} candidates; {n_flip} would flip"
              f"{' (written)' if args.apply else ''}.")
        return 0

    results = backfill(
        db_path=args.db_path,
        run_id_filter=args.run_id,
        dry_run=args.dry_run,
        verbose=args.verbose,
        since_days=args.since_days,
        unlabeled_only=args.unlabeled_only,
        ignore_backoff=args.ignore_backoff,
        max_attempts=args.max_attempts,
        progress_every=args.progress_every,
        budget_seconds=args.budget_seconds,
    )

    print_report(results)

    changed = sum(1 for r in results if r.get("changed"))
    parked = sum(1 for r in results if r.get("label_state") == STATE_UNATTRIBUTABLE)
    mode = "[DRY-RUN] " if args.dry_run else ""
    print(f"\n{mode}Labeled {len(results)} runs; {changed} changed; "
          f"{parked} parked as unattributable.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
