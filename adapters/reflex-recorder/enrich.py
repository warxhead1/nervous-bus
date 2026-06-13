"""enrich.py — Capture-time enrichment helpers for reflex-recorder.

PART A: git_branch + bead_id derivation at run close.
PART C: token feature-folding from bus.agent.activity.v1 events.

Called by the Segmenter at run close time (for A) and during event fold (for C).
Does NOT block the hot path — all subprocess calls use short timeouts and fail
gracefully to None/0.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional


# ── PART A — git_branch + bead_id ────────────────────────────────────────────

# Bead ID pattern: <project>-<5-char-base36> or <project>-<5char>.<n>
# e.g. nervous-bus-fhr1q, loom-yluar, deer-flow-0y8, deer-flow-0y8.1
_BEAD_ID_RE = re.compile(
    r"""(?x)
    # prefix: project slug
    (?:
        # branch-style prefixes: loom/, deer-flow/, deer-flow-prefix on branch name
        (?:loom|deer-flow|deer)\s*/\s*  |  # "loom/" or "deer-flow/" then bead
        # direct bead on the branch (no prefix)
    )?
    # the bead ID itself
    (
        # standard: <project>-<5alphanum> optionally .<n>
        (?:[a-z][a-z0-9]*-)+   # project segment(s)
        [a-z0-9]{4,6}          # short ID
        (?:\.\d+)?             # optional .1 .2 sub-issue suffix
    )
    $
    """,
    re.IGNORECASE,
)

# Known branch-prefix → no bead (these are refname prefixes that indicate
# a structural branch, not a work branch tied to a specific bead):
_NON_BEAD_BRANCH_PREFIXES = (
    "main",
    "master",
    "HEAD",
    "worktree-",   # e.g. worktree-agent-a3f... (internal worktree branches)
    "feat/",
    "fix/",
    "chore/",
    "task/",
    "ci-",
    "salvage/",
    "exec/",
)


def derive_git_branch(worktree_path: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """Derive the git branch from a worktree absolute path or cwd fallback.

    Strategy:
    1. If worktree_path is set (absolute path of the worktree), run
       `git -C <path> rev-parse --abbrev-ref HEAD`.
    2. If worktree_path is None (session run), fall back to project cwd.
    3. If git fails or path doesn't exist, return None.

    Short timeout (3s) so a stalled git never blocks the close path.
    """
    paths_to_try: list[str] = []
    if worktree_path:
        paths_to_try.append(worktree_path)
    if cwd:
        paths_to_try.append(cwd)

    for path in paths_to_try:
        branch = _git_branch_at(path)
        if branch is not None:
            return branch
    return None


def _git_branch_at(path: str) -> Optional[str]:
    """Run git rev-parse in path; return branch string or None."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch != "HEAD":
                return branch
    except Exception:
        pass
    return None


def derive_bead_id(branch: Optional[str]) -> Optional[str]:
    """Best-effort bead_id extraction from a branch name.

    Recognises:
    - loom/<bead-id>           → bead-id  (hearth-loom PR branches)
    - deer-flow/<bead-id>      → bead-id  (deer-flow work branches)
    - <bead-id> directly       → bead-id  (direct bead branches)
    - worktree-<agent-...>     → None     (internal worktree branch)
    - feat/... fix/... etc.    → None     (structural branches)
    - main / master / HEAD     → None

    Returns None when not derivable; callers should treat None as "unknown",
    not "no bead".
    """
    if not branch:
        return None

    # Strip non-bead structural branch prefixes early
    for prefix in _NON_BEAD_BRANCH_PREFIXES:
        if branch.startswith(prefix):
            return None

    # loom/<bead-id> pattern
    if branch.startswith("loom/"):
        candidate = branch[len("loom/"):]
        return candidate if _looks_like_bead_id(candidate) else None

    # deer-flow/<bead-id> pattern
    if branch.startswith("deer-flow/"):
        candidate = branch[len("deer-flow/"):]
        return candidate if _looks_like_bead_id(candidate) else None

    # deer/<bead-id> pattern
    if branch.startswith("deer/"):
        candidate = branch[len("deer/"):]
        return candidate if _looks_like_bead_id(candidate) else None

    # Direct bead_id on the branch (no prefix) — e.g. "nervous-bus-fhr1q"
    if _looks_like_bead_id(branch):
        return branch

    return None


def _looks_like_bead_id(s: str) -> bool:
    """Heuristic: does this string look like a bead ID?

    Bead IDs observed: nervous-bus-oukii, loom-yluar, deer-flow-0y8,
    nervous-bus-fhr1q, nervous-bus-fhr1q.1

    Pattern: at least two hyphen-separated segments, last segment is 3-6
    lowercase alphanumeric chars (optional .N suffix), at least one middle
    project segment.
    """
    # Must not contain slashes (those are branch prefixes, already stripped)
    if "/" in s:
        return False
    parts = s.split(".")
    base = parts[0]
    segments = base.split("-")
    if len(segments) < 2:
        return False
    last = segments[-1]
    # Last segment: 3-6 chars, lowercase alphanumeric
    if not re.match(r"^[a-z0-9]{3,6}$", last):
        return False
    # At least one project segment before the short ID
    project_parts = segments[:-1]
    if not all(re.match(r"^[a-z][a-z0-9]*$", p) for p in project_parts):
        return False
    return True


# ── PART C — token feature-folding ───────────────────────────────────────────

def fold_token_features(features: dict, activity: dict) -> None:
    """Fold token and tool-error fields from one activity event into features dict.

    Mutates `features` in place.  Call once per event during run.fold().

    Fields consumed (all optional per emitted-signals.md):
      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
      model, tool_is_error, tool_error_type

    Features written:
      tokens_input_total, tokens_output_total,
      tokens_cache_read, tokens_cache_write,
      cache_hit_rate (computed at finalize),
      models_seen {model: count},
      primary_model (computed at finalize),
      tool_errors, tool_error_<type>, tool_error_rate (computed at finalize),
      tool_calls (denominator for error rate)
    """
    # Token totals
    if "input_tokens" in activity:
        features["tokens_input_total"] = (
            features.get("tokens_input_total", 0) + activity["input_tokens"]
        )
    if "output_tokens" in activity:
        features["tokens_output_total"] = (
            features.get("tokens_output_total", 0) + activity["output_tokens"]
        )
    if "cache_read_tokens" in activity:
        features["tokens_cache_read"] = (
            features.get("tokens_cache_read", 0) + activity["cache_read_tokens"]
        )
    if "cache_write_tokens" in activity:
        features["tokens_cache_write"] = (
            features.get("tokens_cache_write", 0) + activity["cache_write_tokens"]
        )

    # Model tracking
    if model := activity.get("model"):
        models_seen = features.get("models_seen", {})
        models_seen[model] = models_seen.get(model, 0) + 1
        features["models_seen"] = models_seen

    # Tool call counter (denominator for error rate)
    if activity.get("event") == "tool_call":
        features["tool_calls"] = features.get("tool_calls", 0) + 1

    # Tool errors
    if activity.get("tool_is_error"):
        features["tool_errors"] = features.get("tool_errors", 0) + 1
        err_type = activity.get("tool_error_type") or "error"
        err_key = f"tool_error_{err_type}"
        features[err_key] = features.get(err_key, 0) + 1


def finalize_token_features(features: dict) -> None:
    """Compute derived aggregate fields after all events have been folded.

    Call once at run close (after fold_token_features for all events).
    Mutates `features` in place.
    """
    # cache_hit_rate: reads / writes (proxy for prompt-reuse efficiency)
    write = features.get("tokens_cache_write", 0)
    read = features.get("tokens_cache_read", 0)
    if write > 0:
        features["cache_hit_rate"] = round(read / write, 4)

    # primary_model: most-used model in the run
    models_seen = features.get("models_seen", {})
    if models_seen:
        features["primary_model"] = max(models_seen, key=models_seen.get)

    # tool_error_rate: errors / total tool calls
    tool_calls = features.get("tool_calls", 0)
    tool_errors = features.get("tool_errors", 0)
    if tool_calls > 0:
        features["tool_error_rate"] = round(tool_errors / tool_calls, 4)
