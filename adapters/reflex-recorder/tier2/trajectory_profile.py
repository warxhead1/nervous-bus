"""trajectory_profile.py — inductive heuristic extraction from subagent runs.

Where the Tier-1 detectors are DEDUCTIVE (each encodes one known anti-pattern),
this module is INDUCTIVE: it reconstructs the full tool-call trajectory of a run
from task-start to completion and computes a battery of mechanical heuristics that
characterise *how* an agent worked, not just *whether* it tripped a known rule.

The output is meant to be sampled and read — by a human or by a fan-out of LLM
agents — to discover NEW patterns worth promoting into Tier-1 detectors.

Heuristics computed per run
===========================
  phases                : coarse task-arc segmentation (orient/explore/edit/
                          run-verify/wait) by tool-class transitions
  tool_bigrams          : most common adjacent (tool_a -> tool_b) transitions
  stalls                : tool calls whose *following* gap exceeds STALL_S, with
                          the command that triggered the wait (GPU steps, builds)
  poll_loops            : runs of `sleep N && cat/tail/status` polling — a busy
                          wait that a signal/await would eliminate
  nav_churn             : grep<->Read interleaving ratio — high == agent does not
                          know where the code lives (navigation cost)
  repeated_cmd_clusters : near-identical Bash commands run >=N times (normalised
                          by stripping literals) — a missing-tooling smell
  edit_verify_cycles    : Edit/Write -> build/test -> (revert?) loops
  redo_reads            : same file Read >=N times (re-orientation cost)

All heuristics are pure functions of the run_events stream; no transcript or git
access required, so this is cheap and replay-stable.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapter_api import CommandTaxonomy, taxonomy_for  # noqa: E402

DEFAULT_DB = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

# ── tunables ─────────────────────────────────────────────────────────────────
STALL_S = 45.0          # a post-call gap longer than this is a "stall"
POLL_RE = re.compile(r"\bsleep\s+\d", re.I)
POLL_PEEK_RE = re.compile(r"\b(cat|tail|head|status|ls|stat|grep)\b", re.I)
REPEAT_MIN = 3          # a normalised command seen >= this many times is a cluster
REDO_READ_MIN = 3       # same file read >= this many times is re-orientation churn

# tool → coarse class, used for phase segmentation
_TOOL_CLASS = {
    "Read": "explore", "Grep": "explore", "Glob": "explore", "LS": "explore",
    "NotebookRead": "explore",
    "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Task": "dispatch",
    "Bash": "act",  # refined below by command content
}


# ── event model ──────────────────────────────────────────────────────────────

@dataclass
class Ev:
    seq: int
    tool: str
    event: str
    cmd: str           # best-effort human key (command / file / pattern)
    ts: Optional[datetime]
    gap_after: float = 0.0   # seconds until next event


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.split(".")[0].rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _key(summary: str) -> str:
    """Pull a human-readable key from a tool_summary JSON blob."""
    try:
        sj = json.loads(summary)
    except (json.JSONDecodeError, TypeError):
        return str(summary)[:200]
    for k in ("command", "file_path", "pattern", "description", "prompt", "url"):
        if sj.get(k):
            return str(sj[k]).replace("\n", " ")
    return ""


def load_trajectory(conn: sqlite3.Connection, run_id: str) -> list[Ev]:
    rows = conn.execute(
        "SELECT raw_json FROM run_events WHERE run_id=? ORDER BY seq", (run_id,)
    ).fetchall()
    evs: list[Ev] = []
    for i, (rj,) in enumerate(rows):
        e = json.loads(rj)
        d = e.get("data", {})
        evs.append(Ev(
            seq=i,
            tool=d.get("tool_name", "") or "",
            event=d.get("event", "") or "",
            cmd=_key(d.get("tool_summary", "")),
            ts=_parse_ts(d.get("ts") or d.get("time") or ""),
        ))
    for a, b in zip(evs, evs[1:]):
        if a.ts and b.ts:
            a.gap_after = max(0.0, (b.ts - a.ts).total_seconds())
    return evs


# ── command normalisation (for repeat clustering) ────────────────────────────

_NORM_SUBS = [
    (re.compile(r"/[\w./@~-]+"), "<path>"),       # absolute-ish paths
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"),   # hashes / ids
    (re.compile(r"\b\d+\b"), "<n>"),               # bare numbers
    (re.compile(r'"[^"]*"'), '"<s>"'),            # double-quoted literals
    (re.compile(r"'[^']*'"), "'<s>'"),            # single-quoted literals
    (re.compile(r"\s+"), " "),
]


def normalise_cmd(cmd: str) -> str:
    """Collapse literals so structurally-identical commands cluster together."""
    s = cmd.strip()
    # take the leading pipeline stage — the verb matters, not the tail
    s = s.split("|")[0].split("&&")[0].strip()
    for rx, rep in _NORM_SUBS:
        s = rx.sub(rep, s)
    return s.strip()[:120]


def classify(ev: Ev, taxonomy: Optional[CommandTaxonomy] = None) -> str:
    """Classify an event into an activity class.

    Bash commands are classified by the active project's CommandTaxonomy (which
    knows that project's build/run-verify verbs); everything else by tool name.
    Falls back to the generic taxonomy when no adapter governs the project.
    """
    if ev.tool == "Bash":
        tax = taxonomy or CommandTaxonomy()
        return tax.classify(ev.cmd)
    return _TOOL_CLASS.get(ev.tool, "act")


# ── heuristics ───────────────────────────────────────────────────────────────

@dataclass
class Profile:
    run_id: str
    n_events: int
    span_s: float
    active_s: float                       # span minus stall time
    phases: list[tuple[str, int]]         # (class, count) run-length-ish summary
    tool_hist: dict[str, int]
    bigrams: list[tuple[str, int]]
    stalls: list[dict]
    poll_loops: list[dict]
    nav_churn: float
    repeated_cmd_clusters: list[dict]
    redo_reads: list[dict]
    edit_verify_cycles: int
    flags: list[str] = field(default_factory=list)


def profile_run(conn: sqlite3.Connection, run_id: str,
                taxonomy: Optional[CommandTaxonomy] = None) -> Profile:
    evs = load_trajectory(conn, run_id)
    n = len(evs)

    # Resolve the command taxonomy from the run's project adapter (generic default
    # if no private adapter governs it).
    if taxonomy is None:
        row = conn.execute("SELECT project FROM runs WHERE run_id=?", (run_id,)).fetchone()
        taxonomy = taxonomy_for(row[0]) if row and row[0] else CommandTaxonomy()

    classes = [classify(e, taxonomy) for e in evs]

    # span / active
    tss = [e.ts for e in evs if e.ts]
    span = (tss[-1] - tss[0]).total_seconds() if len(tss) >= 2 else 0.0
    stall_time = sum(e.gap_after for e in evs if e.gap_after > STALL_S)
    active = max(0.0, span - stall_time)

    # phase run-length compression
    phases: list[tuple[str, int]] = []
    for c in classes:
        if phases and phases[-1][0] == c:
            phases[-1] = (c, phases[-1][1] + 1)
        else:
            phases.append((c, 1))

    # bigrams over tool classes
    bg = Counter(zip(classes, classes[1:]))
    bigrams = [(f"{a}->{b}", n_) for (a, b), n_ in bg.most_common(8)]

    # stalls (gap after a call > STALL_S)
    stalls = [
        {"seq": e.seq, "gap_s": round(e.gap_after), "class": classify(e, taxonomy),
         "cmd": e.cmd[:100]}
        for e in evs if e.gap_after > STALL_S
    ]

    # poll loops: sleep+peek runs
    poll_loops = []
    for e in evs:
        if e.tool == "Bash" and POLL_RE.search(e.cmd) and POLL_PEEK_RE.search(e.cmd):
            poll_loops.append({"seq": e.seq, "cmd": e.cmd[:100]})

    # nav churn: fraction of explore<->explore transitions that flip grep<->read
    flips = 0
    explore_pairs = 0
    for a, b in zip(evs, evs[1:]):
        ca, cb = classify(a, taxonomy), classify(b, taxonomy)
        if ca == "explore" and cb == "explore":
            explore_pairs += 1
            ta = "grep" if (a.tool in ("Grep", "Glob") or (a.tool == "Bash")) else "read"
            tb = "grep" if (b.tool in ("Grep", "Glob") or (b.tool == "Bash")) else "read"
            if ta != tb:
                flips += 1
    nav_churn = (flips / explore_pairs) if explore_pairs else 0.0

    # repeated command clusters
    norm = Counter()
    norm_examples: dict[str, str] = {}
    for e in evs:
        if e.tool == "Bash" and e.cmd:
            nk = normalise_cmd(e.cmd)
            norm[nk] += 1
            norm_examples.setdefault(nk, e.cmd[:100])
    repeated = [
        {"pattern": k, "count": c, "example": norm_examples[k]}
        for k, c in norm.most_common() if c >= REPEAT_MIN
    ]

    # redo reads: same file read >= N times
    read_files = Counter(
        e.cmd for e in evs if e.tool == "Read" and e.cmd
    )
    redo_reads = [
        {"file": _short_path(f), "count": c}
        for f, c in read_files.most_common() if c >= REDO_READ_MIN
    ]

    # edit->verify cycles: Edit/Write followed within 4 events by build/run-verify
    edit_verify = 0
    for i, e in enumerate(evs):
        if classify(e, taxonomy) == "edit":
            window = classes[i + 1:i + 5]
            if any(w in ("build", "run-verify") for w in window):
                edit_verify += 1

    th: dict[str, int] = Counter(e.tool for e in evs if e.tool)

    prof = Profile(
        run_id=run_id, n_events=n, span_s=round(span), active_s=round(active),
        phases=_compress_phases(phases), tool_hist=dict(th), bigrams=bigrams,
        stalls=stalls, poll_loops=poll_loops, nav_churn=round(nav_churn, 2),
        repeated_cmd_clusters=repeated, redo_reads=redo_reads,
        edit_verify_cycles=edit_verify,
    )
    prof.flags = _derive_flags(prof)
    return prof


def _compress_phases(phases: list[tuple[str, int]], top: int = 12) -> list[tuple[str, int]]:
    """Keep the phase sequence but cap length for readability."""
    if len(phases) <= top:
        return phases
    head = phases[: top - 1]
    tail_count = sum(c for _, c in phases[top - 1:])
    return head + [("…", tail_count)]


def _short_path(p: str) -> str:
    parts = p.split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else p


def _derive_flags(p: Profile) -> list[str]:
    flags = []
    if p.poll_loops:
        flags.append(f"busy_wait_polling x{len(p.poll_loops)}")
    if p.nav_churn >= 0.6 and p.n_events >= 20:
        flags.append(f"high_nav_churn={p.nav_churn}")
    big_repeat = [r for r in p.repeated_cmd_clusters if r["count"] >= 5]
    if big_repeat:
        flags.append(f"missing_tooling_smell x{len(big_repeat)}")
    if p.redo_reads:
        flags.append(f"redo_reads x{len(p.redo_reads)}")
    if p.span_s and p.active_s and (p.active_s / p.span_s) < 0.5:
        flags.append("wait_dominated")
    if p.edit_verify_cycles == 0 and p.n_events >= 30:
        flags.append("no_edit_verify_cycle")  # all explore, no landed change?
    return flags


# ── CLI / aggregate ──────────────────────────────────────────────────────────

def subagent_run_ids(conn: sqlite3.Connection, project: Optional[str] = None,
                     min_events: int = 10) -> list[str]:
    q = ("SELECT run_id FROM runs WHERE (run_key_kind='worktree' "
         "OR worktree_slug IS NOT NULL) AND event_count >= ?")
    args: list[Any] = [min_events]
    if project:
        q += " AND project = ?"
        args.append(project)
    q += " ORDER BY event_count DESC"
    return [r[0] for r in conn.execute(q, args).fetchall()]


def _print_profile(p: Profile) -> None:
    print(f"\n━━ {p.run_id}  ({p.n_events} ev, span={p.span_s}s active={p.active_s}s)")
    print("  phases: " + " ".join(f"{c}:{n}" for c, n in p.phases))
    if p.flags:
        print("  FLAGS:  " + ", ".join(p.flags))
    if p.stalls:
        tot = sum(s["gap_s"] for s in p.stalls)
        print(f"  stalls: {len(p.stalls)} totalling {tot}s; "
              f"worst: {max(p.stalls, key=lambda s: s['gap_s'])['cmd']}")
    for r in p.repeated_cmd_clusters[:4]:
        print(f"  repeat x{r['count']}: {r['example']}")
    for r in p.redo_reads[:4]:
        print(f"  reread x{r['count']}: {r['file']}")


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inductive trajectory profiler")
    ap.add_argument("run_id", nargs="?", help="profile a single run; omit to scan")
    ap.add_argument("--project", default=None)
    ap.add_argument("--min-events", type=int, default=10)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", action="store_true", help="emit JSON")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db)

    if a.run_id:
        p = profile_run(conn, a.run_id)
        if a.json:
            print(json.dumps(p.__dict__, indent=2, default=str))
        else:
            _print_profile(p)
        return 0

    ids = subagent_run_ids(conn, a.project, a.min_events)
    profs = [profile_run(conn, rid) for rid in ids]
    if a.json:
        print(json.dumps([p.__dict__ for p in profs], indent=2, default=str))
        return 0

    # aggregate report
    flag_counts = Counter()
    for p in profs:
        for f in p.flags:
            flag_counts[f.split("=")[0].split(" x")[0]] += 1
    print(f"Profiled {len(profs)} subagent runs"
          + (f" in {a.project}" if a.project else ""))
    print("\n=== flag prevalence (runs tripping each heuristic) ===")
    for f, c in flag_counts.most_common():
        print(f"  {c:3}/{len(profs)}  {f}")
    for p in profs:
        _print_profile(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
