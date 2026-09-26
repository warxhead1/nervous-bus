"""orca_outcome.py — Orca `worker_done --outcome` as a tier-3 label source.

Orca workers self-report their outcome by shelling out to
`orca orchestration send --type worker_done --outcome succeeded|failed|blocked`
before stopping. label.py has no other reader for that signal.

Orca's own orchestration store (`~/.config/orca/orchestration.db`) has a
`messages` row with `type='worker_done'` carrying the outcome, but none of
`messages`/`dispatch_contexts`/`worker_dispatches`/`external_worker_runs`
carries a codex rollout id — only pane/dispatch UUIDs, which don't match the
`runs.session_id` join key. This module instead reads the codex rollout files
directly: each worker's own `orca orchestration send ... --type worker_done`
call lands as a `CommandExecution` in that worker's own rollout transcript,
and the rollout's filename already contains the join key —
`rollout-<timestamp>-<session_id>.jsonl`, and `runs.session_id` is exactly
that id (mirrored verbatim under every configured account under
`~/.config/orca/codex-accounts/<acct>/home/sessions/`; dedupe by session id,
not path).

Outcome mapping (tier 3, alongside bead_close/pr_merge/git_merged_into_main):
    succeeded -> "clean"     (the coordinator, not the worker, lands/merges —
                              per the Orca contract a worker never commits to
                              main itself, so "clean" not "landed")
    failed    -> "abandoned" (closest existing outcome; the schema's outcome
                              enum is frozen to
                              landed/abandoned/reverted/thrashed/corrected/clean/null
                              — there is no "failed")
    blocked   -> None        (ambiguous — often a REJECT-worked-as-designed
                              audit verdict, not a run failure; leave
                              unlabeled rather than guess)
"""
from __future__ import annotations

import glob
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Every place a codex rollout can live. `~/.codex/sessions` is the primary
# runtime; `~/.config/orca/codex-accounts/<acct>/home/sessions` mirrors the
# exact same files per configured account — duplicates, deduped below by
# session id, not by path.
def _rollout_roots() -> list[str]:
    roots = [str(Path.home() / ".codex" / "sessions")]
    roots.extend(sorted(glob.glob(
        str(Path.home() / ".config" / "orca" / "codex-accounts" / "*" / "home" / "sessions")
    )))
    return roots


_ROLLOUT_NAME_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

# `orca orchestration send --from term_... --dispatch-capability dcap_... \
#  --type worker_done ... --outcome succeeded`. Only the flags this module
# needs; the rest of the command line (subject/body quoting) is irrelevant.
_WORKER_DONE_RE = re.compile(r"--type\s+worker_done\b.*?--outcome\s+([A-Za-z_]+)")

# Tier-3 outcome mapping — see module docstring for rationale on each arm.
_OUTCOME_MAP: dict[str, Optional[str]] = {
    "succeeded": "clean",
    "failed": "abandoned",
    "blocked": None,
}

# Process-local cache: thousands of rollout files on disk, so a backfill/
# reverify pass over many thousands of runs must build the index ONCE, not
# once per run. Keyed by the since_days bound so a differently-bounded call
# (e.g. tests) doesn't reuse a stale wider-or-narrower scan.
_INDEX_CACHE: dict[Optional[int], dict[str, str]] = {}


def _session_id_from_path(path: str) -> Optional[str]:
    m = _ROLLOUT_NAME_RE.search(Path(path).name)
    return m.group(1) if m else None


def _rollout_day(path: Path) -> Optional[datetime]:
    """Best-effort YYYY/MM/DD parsed from the rollout's own path segments."""
    parts = path.parts
    if len(parts) < 4:
        return None
    y, mo, d = parts[-4], parts[-3], parts[-2]
    try:
        return datetime(int(y), int(mo), int(d), tzinfo=timezone.utc)
    except ValueError:
        return None


def iter_rollout_paths(since_days: Optional[int] = None,
                        roots: Optional[list[str]] = None) -> list[str]:
    """List rollout jsonl paths across all known roots, deduped by session id.

    since_days bounds the scan to rollouts whose YYYY/MM/DD path segment falls
    within the last N+1 days (a +1-day pad for TZ/rollover slop) — without it,
    a growing session history means an ever-slower first-call scan on every
    fresh process. None means unbounded (used by the one-time full-history
    reverify pass and by --false-abandon-rate-style audits).
    """
    seen: dict[str, str] = {}
    cutoff = None
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(since_days) + 1)

    for root in (roots if roots is not None else _rollout_roots()):
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for jsonl_path in root_path.glob("*/*/*/*.jsonl"):
            sid = _session_id_from_path(str(jsonl_path))
            if not sid or sid in seen:
                continue
            if cutoff is not None:
                day = _rollout_day(jsonl_path)
                if day is not None and day < cutoff:
                    continue
            seen[sid] = str(jsonl_path)
    return list(seen.values())


def _extract_worker_done_outcome(path: str) -> Optional[str]:
    """Return the LAST worker_done outcome this rollout reported, or None.

    A rollout may call `orca orchestration send --type worker_done` more than
    once (a retried/corrected report); the later call is authoritative. Cheap
    substring pre-filter before any json.loads — most lines in a rollout are
    plain model tokens, not CommandExecution records.
    """
    last_outcome: Optional[str] = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "worker_done" not in line or "orchestration send" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                # CommandExecution (event_msg.item) carries `command` as a
                # list of argv strings; the response_item custom_tool_call
                # carries the same text inside `input` (a JS source string
                # embedding the shell command). Either shape is fine — we
                # only regex for the --type/--outcome flags.
                item = payload.get("item")
                cmd = item.get("command") if isinstance(item, dict) else None
                if cmd is None:
                    cmd = payload.get("input")
                if isinstance(cmd, list):
                    cmd_str = " ".join(str(c) for c in cmd)
                elif isinstance(cmd, str):
                    cmd_str = cmd
                else:
                    continue
                m = _WORKER_DONE_RE.search(cmd_str)
                if m:
                    last_outcome = m.group(1).strip().lower()
    except OSError:
        return None
    return last_outcome


def build_worker_done_index(since_days: Optional[int] = None,
                             *, force_refresh: bool = False,
                             roots: Optional[list[str]] = None) -> dict[str, str]:
    """session_id -> raw worker_done outcome string ('succeeded'/'failed'/'blocked').

    Cached per-process per since_days bound (see _INDEX_CACHE). force_refresh
    and roots are test-only hooks (roots lets a test point this at a tmp_path
    fixture instead of the real ~/.codex/sessions tree).
    """
    if roots is None and not force_refresh and since_days in _INDEX_CACHE:
        return _INDEX_CACHE[since_days]

    index: dict[str, str] = {}
    for path in iter_rollout_paths(since_days=since_days, roots=roots):
        sid = _session_id_from_path(path)
        if not sid:
            continue
        outcome = _extract_worker_done_outcome(path)
        if outcome:
            index[sid] = outcome

    if roots is None:
        _INDEX_CACHE[since_days] = index
    return index


def label_from_orca_worker_done(
    run: dict, *, since_days: Optional[int] = None, roots: Optional[list[str]] = None,
) -> Optional[tuple[str, str]]:
    """Tier-3 label source: Orca `worker_done --outcome` self-report.

    Joins on run['session_id'] == the codex rollout's own id (see module
    docstring for the verification). Returns (outcome, 'orca_worker_done') or
    None when there's no rollout match, the session never sent worker_done, or
    the outcome maps to None (blocked — ambiguous, see module docstring).
    """
    session_id = run.get("session_id")
    if not session_id:
        return None
    index = build_worker_done_index(since_days=since_days, roots=roots)
    raw = index.get(session_id)
    if raw is None:
        return None
    mapped = _OUTCOME_MAP.get(raw)
    if mapped is None:
        return None
    return mapped, "orca_worker_done"
