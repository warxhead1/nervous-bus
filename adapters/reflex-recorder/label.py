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
from datetime import datetime, timezone
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
    "bead_close": 3,
    "bus_bead_closed": 3,
    "pr_merge": 3,
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


def _gh_pr_state(branch: str, worktree_path: Optional[str]) -> Optional[dict]:
    """Query gh pr for the most recent PR on branch.

    B4 fix: use `gh -C <dir> pr view <branch>` so gh resolves the remote from
    the correct repo.  Degrades safely to None on no-PR or network error.

    Returns dict with {state, mergedAt} or None if no PR found.
    """
    # Determine the working directory for gh: prefer the worktree path, fall
    # back to ".".  We don't gate on Path.exists() here — the worktree may have
    # been cleaned up but the parent project git repo is still valid for gh.
    # If gh fails (no remote, network error), _run_cmd returns None and we
    # degrade safely to None without mislabeling.
    work_dir = worktree_path if worktree_path else "."

    out = _run_cmd(
        ["gh", "-C", work_dir, "pr", "view", branch, "--json", "state,mergedAt,closedAt"],
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


# ── Inferred labeling from behavior shape ─────────────────────────────────────

# Tools classified as resolving actions (indicate productive completion):
_RESOLVING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "Bash"})

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
                cmd = summary_obj.get("command", "")
            except (json.JSONDecodeError, TypeError):
                cmd = str(summary)
        else:
            cmd = ""

        # Match `git commit`, `git -C <path> commit`, `git push`
        if re.search(r"\bgit\b.*\bcommit\b", cmd):
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
    """Check if the final ~20% of events contain Edit/Write calls."""
    if not events:
        return False
    tail_start = max(0, len(events) - max(3, len(events) // 5))
    tail = events[tail_start:]
    return any(ev.get("tool_name") in ("Edit", "Write") for ev in tail)


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
      - event_count >= ABANDON_MIN_EVENTS (5)
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

    n_events = len(events)
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
        and n_events >= ABANDON_MIN_EVENTS
        and not has_commit
        and not has_edit_tail
    ):
        # M3: require that the run showed some failure/error state or was
        # substantial enough (many events without resolving = stronger abandon signal)
        has_failure_signal = bash_fails > 0 or bash_fail_rate > 0
        is_substantial = n_events >= ABANDON_MIN_EVENTS * 2
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

def compute_label(
    run: dict,
    run_events: list[dict],
    verbose: bool = False,
) -> Optional[tuple[str, str]]:
    """Compute the best available outcome + source for a run.

    Returns (outcome, source) or None if we cannot determine anything.

    Precedence: bead_id explicit > pr explicit > git_revert > behavior_inferred.
    Returns None when behavior inference returns None (insufficient signal).
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

    # EXPLICIT: PR / git_revert
    if git_branch and git_branch not in ("HEAD", "main", "master"):
        result = label_from_pr(git_branch, worktree)
        if result:
            if verbose:
                print(f"  [label] branch={git_branch} → {result}")
            return result

    # INFERRED: behavior shape (may return None)
    parsed = _parse_events(run_events)
    outcome, source = _infer_from_behavior(run, parsed, verbose=verbose)
    if outcome is None:
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


def backfill(
    db_path: Path,
    run_id_filter: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """Run the labeling pass over all (or one) run(s) in runs.db.

    Also enriches git_branch/bead_id for pre-PART-A runs that lack these fields.
    Returns a list of result dicts for reporting.

    B5 fix: compute_label returning None means 'insufficient signal'; we do NOT
    coerce None → 'clean'.  Such runs are left with outcome=None (unlabeled).
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")

    if run_id_filter:
        cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id_filter,))
    else:
        cur = conn.execute("SELECT * FROM runs ORDER BY started ASC")

    cols = [d[0] for d in cur.description]
    runs = [dict(zip(cols, row)) for row in cur.fetchall()]

    results = []
    for run in runs:
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
        label_result = compute_label(run, run_events, verbose=verbose)
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
                if existing_outcome is not None and _source_tier(existing_source) == 1:
                    # Existing label was inferred and new inference says null → clear it
                    if not dry_run:
                        conn.execute(
                            "UPDATE runs SET outcome=NULL, labeled_at=NULL WHERE run_id=?",
                            (run_id,),
                        )
                    changed = True
                if signals and not dry_run:
                    existing_features.update(signals)
                    conn.execute(
                        "UPDATE runs SET features=? WHERE run_id=?",
                        (json.dumps(existing_features), run_id),
                    )

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
        })

    conn.close()
    return results


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
    args = parser.parse_args()

    results = backfill(
        db_path=args.db_path,
        run_id_filter=args.run_id,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    print_report(results)

    changed = sum(1 for r in results if r.get("changed"))
    mode = "[DRY-RUN] " if args.dry_run else ""
    print(f"\n{mode}Labeled {len(results)} runs; {changed} changed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
