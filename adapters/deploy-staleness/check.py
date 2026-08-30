#!/usr/bin/env python3
"""deploy-staleness — watch the deploys, not just the code.

Motivating incident (measured, 2026-08-30, one sweep): we fix code in repos,
but the long-running artifacts EMBODYING that code keep running stale builds,
and nothing surfaced it.

  1. kb-watch.service ran the Aug 24 `kb` binary for ~6h after
     ~/.local/bin/kb was replaced with a critical fix (restarted 16:47 today
     -- this run should show it FRESH).
  2. The running orca AppImage (pid started Sat 2026-08-29 13:44 from
     dist/orca-linux.AppImage) predates both today's dist rebuild (13:15) and
     every fix merged today. The fuse mount pins the OLD build's contents
     even after the underlying file is overwritten -- file-newer-than-process
     is the tell, not file-existence.
  3. Generally: any systemd --user service whose ExecStart source changed
     after ExecMainStartTimestamp is serving code nobody reviewing "merged +
     tests green" would know is not what's actually running.

This class defeats every "merged + tests green" verification, because both
of those checks are about the REPO, and this incident lives entirely in the
gap between the repo and the process.

Two independent surfaces, one run, one report:

1. **systemd --user units** -- for each monitored unit (auto-discovered:
   Type=simple/exec/notify whose resolved ExecStart code target lives under
   a configured project/local-bin root, plus roster.json include/exclude
   overrides; Type=oneshot is ALWAYS skipped -- a oneshot re-execs fresh
   every run by construction, so "staleness" doesn't apply to it), compare
   `ExecMainStartTimestamp` against the freshness of the code it's running:
     - script-backed (target resolves inside a git repo): max(file mtime,
       last commit touching that path) via `git log -1 --format=%ct -- path`.
     - binary-backed (no git repo, e.g. ~/.local/bin/kb): file mtime alone,
       UNLESS `/proc/<pid>/exe` resolves to "... (deleted)" -- a symlink to a
       replaced-then-removed inode -- which is STALE regardless of any
       timestamp math, since the running process is definitionally executing
       bytes that no longer exist on the filesystem under that path.
2. **AppImage-style unmanaged processes** (roster.json `appimage_matchers`,
   matched by a substring against `/proc/<pid>/cmdline`, never `pgrep` --
   pgrep-via-subprocess would match its own wrapper) -- compare the
   process's start time (`ps -o lstart= -p <pid>`) against the mtime of the
   launch file named in argv[0] of that same process's `/proc/<pid>/cmdline`.
   File-newer-than-process-start is stale: this is the fuse-mount-pins-old-
   build case, and it is NOT a systemd unit so it never surfaces in pass 1.

Both passes land in one ~/.cache/nervous-bus/deploy-staleness/report.md +
summary.json. One `bus.deploy.stale.v1` event is published per NEW
transition (never-before-seen stale, or a stale target recovering to fresh),
deduped via a state file so a stale target sitting there for days doesn't
spam the bus every 15-minute timer tick.

Usage:
    python3 check.py                 # real run: evaluate, publish transitions, write report
    python3 check.py --dry-run       # compute + report, never call `nervous publish`
    python3 check.py --roster fixture.json --state-file /tmp/s.json  # tests (inject stub fns)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CACHE_DIR = Path(os.environ.get("NERVOUS_DEPLOY_STALENESS_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "deploy-staleness")))
STATE_FILE = CACHE_DIR / "state.json"
REPORT_FILE = CACHE_DIR / "report.md"
SUMMARY_FILE = CACHE_DIR / "summary.json"
ROSTER_FILE = Path(__file__).resolve().parent / "roster.json"

NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN",
    str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"),
)

# Type=oneshot units re-exec fresh on every activation by construction --
# "staleness" (running-process-predates-its-code) is not a concept that
# applies to them. Always skipped, roster or no roster.
LONG_RUNNING_TYPES = {"simple", "exec", "notify", "forking", "dbus"}

# Re-alert on a target that has stayed stale every poll since the last
# alert, at most this often -- matches the staleness/ci-watch cooldown
# convention so a target stuck stale for a week doesn't spam the bus every
# 15-minute timer tick.
PERSISTENT_STALE_REMINDER_S = 24 * 3600.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# systemd wrappers -- isolated so tests can stub without a real systemd user
# session (fixtures feed parse_exec_start()/evaluate_unit() directly).
# --------------------------------------------------------------------------

def systemctl_list_units() -> List[str]:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        sys.stderr.write(f"[deploy-staleness] systemctl list-units failed: {e}\n")
        return []
    units = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split()[0]
        if name.endswith(".service"):
            units.append(name)
    return units


def systemctl_show(unit: str) -> Dict[str, str]:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "Type", "-p", "ExecStart",
             "-p", "ExecMainStartTimestamp", "-p", "ExecMainPID", unit],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        sys.stderr.write(f"[deploy-staleness] systemctl show {unit} failed: {e}\n")
        return {}
    props: Dict[str, str] = {}
    # ExecStart's value itself can embed "; " and newlines are only used to
    # separate top-level Key=Value properties, so a line-based split on
    # "^([A-Za-z]+)=" is safe: property names never appear mid-value here.
    current_key = None
    for line in out.stdout.splitlines():
        if "=" in line and not line.startswith(" ") and _looks_like_prop_key(line):
            key, _, val = line.partition("=")
            props[key] = val
            current_key = key
        elif current_key:
            props[current_key] += "\n" + line
    return props


def _looks_like_prop_key(line: str) -> bool:
    key = line.split("=", 1)[0]
    return bool(key) and all(c.isalnum() or c == "_" for c in key) and key[0].isalpha()


def parse_systemd_timestamp(raw: Optional[str]) -> Optional[float]:
    """'Sun 2026-08-30 16:47:05 EDT' / '' / 'n/a' -> epoch seconds (or None).

    Shells to `date -d` rather than hand-rolling TZ-abbreviation parsing --
    glibc's zone table is the thing that actually knows what "EDT" means on
    THIS box, and hand-rolling it risks a silent multi-hour skew that this
    adapter's whole job is to catch, not commit.
    """
    if not raw or raw.strip() in ("", "n/a"):
        return None
    try:
        out = subprocess.run(["date", "-d", raw, "+%s"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def parse_exec_start(raw: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """Parse systemctl show's ExecStart= value.

    Format: '{ path=/x/y ; argv[]=/x/y --flag ; ignore_errors=no ; ... }'
    Returns (path, argv_tokens). Never raises on malformed input.
    """
    if not raw:
        return None, []
    path = None
    argv: List[str] = []
    p_idx = raw.find("path=")
    if p_idx != -1:
        rest = raw[p_idx + len("path="):]
        path = rest.split(" ;", 1)[0].strip()
    a_idx = raw.find("argv[]=")
    if a_idx != -1:
        rest = raw[a_idx + len("argv[]="):]
        argv_str = rest.split(" ; ignore_errors", 1)[0].strip()
        argv = argv_str.split(" ") if argv_str else []
    return path, argv


def resolve_target_file(path: Optional[str], argv: List[str]) -> Optional[str]:
    """The actual source file whose freshness matters.

    For a binary-backed unit (ExecStart path IS the code, e.g. ~/.local/bin/kb
    invoked as `kb watch --live`), that's `path` itself. For an
    interpreter-backed unit (python3 /path/to/script.py ...), it's the first
    argv token after argv[0] that looks like an absolute file path -- the
    interpreter binary's own mtime is irrelevant to whether THIS script is
    stale.
    """
    if path is None:
        return None
    for tok in argv[1:]:
        if tok.startswith("/"):
            return tok
    return path


def find_git_repo_root(path: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(path).parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def git_commit_time(repo_root: str, path: str) -> Optional[float]:
    try:
        rel = os.path.relpath(path, repo_root)
        out = subprocess.run(
            ["git", "-C", repo_root, "log", "-1", "--format=%ct", "--", rel],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def file_mtime(path: str) -> Optional[float]:
    try:
        return os.stat(os.path.realpath(path)).st_mtime
    except OSError:
        return None


def proc_exe_deleted(pid: Optional[int]) -> bool:
    """True iff /proc/<pid>/exe resolves to a "(deleted)" link -- the
    process is executing an inode that has since been unlinked (replaced).
    False (not True) if the pid is gone or unreadable -- absence of evidence
    is not evidence of deletion here.
    """
    if not pid:
        return False
    try:
        link = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return link.endswith(" (deleted)")


def code_freshness(target: str, *, git_root_fn=find_git_repo_root, git_time_fn=git_commit_time,
                    mtime_fn=file_mtime) -> Tuple[Optional[float], str]:
    """Returns (freshest_epoch, reason) for a script/binary target path."""
    mtime = mtime_fn(target)
    repo_root = git_root_fn(target)
    if repo_root:
        commit_ts = git_time_fn(repo_root, target)
        if commit_ts is not None and (mtime is None or commit_ts > mtime):
            return commit_ts, "git_commit"
    return mtime, "mtime"


def evaluate_unit(unit: str, props: Dict[str, str], *,
                   git_root_fn=find_git_repo_root, git_time_fn=git_commit_time,
                   mtime_fn=file_mtime, proc_deleted_fn=proc_exe_deleted,
                   ts_parse_fn=parse_systemd_timestamp, now: Optional[float] = None) -> Optional[dict]:
    """Returns a result dict, or None if this unit should be skipped
    (oneshot, or ExecStart/target unresolvable)."""
    now = now if now is not None else time.time()
    unit_type = (props.get("Type") or "simple").strip()
    if unit_type == "oneshot":
        return None

    path, argv = parse_exec_start(props.get("ExecStart"))
    target = resolve_target_file(path, argv)
    if not target:
        return {
            "target": unit, "kind": "unit", "verdict": "unknown",
            "reason": "no_exec_start", "running_since": None, "code_newest_at": None,
            "remedy_hint": f"systemctl --user restart {unit}",
        }

    pid_raw = (props.get("ExecMainPID") or "0").strip()
    try:
        pid = int(pid_raw)
    except ValueError:
        pid = 0
    pid = pid or None

    start_ts = ts_parse_fn(props.get("ExecMainStartTimestamp"))

    if proc_deleted_fn(pid):
        return {
            "target": unit, "kind": "unit", "pid": pid, "verdict": "stale",
            "reason": "deleted_exe", "running_since": iso(start_ts),
            "code_newest_at": iso(now), "code_target": target,
            "remedy_hint": f"systemctl --user restart {unit}",
        }

    code_ts, reason = code_freshness(target, git_root_fn=git_root_fn, git_time_fn=git_time_fn, mtime_fn=mtime_fn)
    if code_ts is None or start_ts is None:
        return {
            "target": unit, "kind": "unit", "pid": pid, "verdict": "unknown",
            "reason": "insufficient_data", "running_since": iso(start_ts),
            "code_newest_at": iso(code_ts), "code_target": target,
            "remedy_hint": f"systemctl --user restart {unit}",
        }

    verdict = "stale" if code_ts > start_ts else "fresh"
    return {
        "target": unit, "kind": "unit", "pid": pid, "verdict": verdict,
        "reason": reason, "running_since": iso(start_ts), "code_newest_at": iso(code_ts),
        "code_target": target, "remedy_hint": f"systemctl --user restart {unit}",
    }


# --------------------------------------------------------------------------
# Roster + auto-discovery
# --------------------------------------------------------------------------

def load_roster(path: Path = ROSTER_FILE) -> dict:
    with path.open() as f:
        return json.load(f)


def discover_units(roster: dict, *, list_fn=systemctl_list_units, show_fn=systemctl_show) -> List[str]:
    project_roots = [str(Path(p)) for p in roster.get("project_roots", [])]
    local_bin_roots = [str(Path(p)) for p in roster.get("local_bin_roots", [])]
    exclude = set(roster.get("exclude_units", []))
    include = set(roster.get("include_units", []))

    discovered = set()
    for unit in list_fn():
        if unit in exclude:
            continue
        props = show_fn(unit)
        if (props.get("Type") or "simple").strip() == "oneshot":
            continue
        path, argv = parse_exec_start(props.get("ExecStart"))
        target = resolve_target_file(path, argv)
        if not target:
            continue
        if any(target.startswith(root + "/") or target == root for root in project_roots + local_bin_roots):
            discovered.add(unit)

    return sorted((discovered | include) - exclude)


# --------------------------------------------------------------------------
# AppImage / unmanaged process matching
# --------------------------------------------------------------------------

def list_proc_pids() -> List[int]:
    out = []
    try:
        for name in os.listdir("/proc"):
            if name.isdigit():
                out.append(int(name))
    except OSError:
        pass
    return out


def read_cmdline(pid: int) -> Optional[List[str]]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split(b"\x00")
    return [p.decode("utf-8", errors="replace") for p in parts if p != b""]


def ps_lstart(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return val or None


def find_matching_pids(matcher: dict, *, pid_list_fn=list_proc_pids, cmdline_fn=read_cmdline) -> List[Tuple[int, List[str]]]:
    pattern = matcher.get("cmdline_contains", "")
    if not pattern:
        return []
    out = []
    for pid in pid_list_fn():
        cmdline = cmdline_fn(pid)
        if not cmdline:
            continue
        joined = " ".join(cmdline)
        if pattern in joined:
            out.append((pid, cmdline))
    return out


def evaluate_appimage(matcher: dict, *, pid_list_fn=list_proc_pids, cmdline_fn=read_cmdline,
                       lstart_fn=ps_lstart, ts_parse_fn=parse_systemd_timestamp,
                       mtime_fn=file_mtime) -> List[dict]:
    name = matcher.get("name", "appimage")
    results = []
    for pid, cmdline in find_matching_pids(matcher, pid_list_fn=pid_list_fn, cmdline_fn=cmdline_fn):
        launch_path = cmdline[0] if cmdline else None
        lstart_raw = lstart_fn(pid)
        start_ts = ts_parse_fn(lstart_raw) if lstart_raw else None
        code_ts = mtime_fn(launch_path) if launch_path else None

        if code_ts is None or start_ts is None:
            results.append({
                "target": f"appimage:{name}", "kind": "process", "pid": pid,
                "verdict": "unknown", "reason": "insufficient_data",
                "running_since": iso(start_ts), "code_newest_at": iso(code_ts),
                "code_target": launch_path,
                "remedy_hint": f"relaunch {name} (pid {pid}, {launch_path})",
            })
            continue

        verdict = "stale" if code_ts > start_ts else "fresh"
        results.append({
            "target": f"appimage:{name}", "kind": "process", "pid": pid,
            "verdict": verdict, "reason": "file_newer_than_process" if verdict == "stale" else "mtime",
            "running_since": iso(start_ts), "code_newest_at": iso(code_ts),
            "code_target": launch_path,
            "remedy_hint": f"relaunch {name} (kill pid {pid}, restart from {launch_path})",
        })
    return results


# --------------------------------------------------------------------------
# Publish + dedupe
# --------------------------------------------------------------------------

def dedupe_key(result: dict) -> str:
    if result["kind"] == "unit":
        return f"unit:{result['target']}"
    return f"process:{result['target']}:{result.get('pid')}"


def publish_transition(result: dict, *, dry_run: bool) -> bool:
    payload = {
        "target": result["target"],
        "kind": result["kind"],
        "pid": result.get("pid"),
        "verdict": result["verdict"],
        "reason": result["reason"],
        "running_since": result.get("running_since"),
        "code_newest_at": result["code_newest_at"],
        "remedy_hint": result["remedy_hint"],
    }
    if dry_run:
        sys.stderr.write(f"[deploy-staleness] (dry-run) would publish bus.deploy.stale.v1: {payload}\n")
        return True
    try:
        subprocess.run(
            [NERVOUS_BIN, "publish", "bus.deploy.stale.v1", json.dumps(payload)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"[deploy-staleness] publish failed for {result['target']}: {e}\n")
        return False


def load_state(path: Path = STATE_FILE) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data: Dict[str, dict], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.rename(path)


def process_results(results: List[dict], state: Dict[str, dict], *, dry_run: bool, now: Optional[float] = None) -> None:
    """Publishes bus.deploy.stale.v1 for new transitions / persistent-stale
    reminders, and updates `state` in place. Mutates `results` to add a
    `transitioned` flag for the report.
    """
    now = now if now is not None else time.time()
    for r in results:
        if r["verdict"] not in ("stale", "fresh"):
            r["transitioned"] = False
            continue

        key = dedupe_key(r)
        prior = state.get(key, {})
        prev_verdict = prior.get("verdict")
        is_transition = prev_verdict is not None and prev_verdict != r["verdict"]
        first_seen_stale = prev_verdict is None and r["verdict"] == "stale"

        should_publish = False
        if is_transition or first_seen_stale:
            should_publish = True
        elif r["verdict"] == "stale":
            last_notified = parse_iso(prior.get("last_notified_at"))
            if last_notified is None or (now - last_notified) >= PERSISTENT_STALE_REMINDER_S:
                should_publish = True

        r["transitioned"] = is_transition or first_seen_stale

        if should_publish:
            published = publish_transition(r, dry_run=dry_run)
            if published and not dry_run:
                state.setdefault(key, {})["last_notified_at"] = iso(now)

        entry = state.setdefault(key, {})
        entry["verdict"] = r["verdict"]
        entry["reason"] = r["reason"]
        entry["running_since"] = r.get("running_since")
        entry["code_newest_at"] = r.get("code_newest_at")
        entry["updated_at"] = iso(now)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def render_report(results: List[dict], *, generated_at: Optional[float] = None) -> str:
    generated_at = generated_at if generated_at is not None else time.time()
    stale = [r for r in results if r["verdict"] == "stale"]
    fresh = [r for r in results if r["verdict"] == "fresh"]
    unknown = [r for r in results if r["verdict"] == "unknown"]

    lines = [
        "# nervous-bus deploy-staleness report",
        "",
        f"Generated: {iso(generated_at)}",
        f"Targets observed: {len(results)}  (stale={len(stale)} fresh={len(fresh)} unknown={len(unknown)})",
        "",
    ]

    def row(r: dict) -> str:
        return (
            f"| {r['target']} | {r.get('pid') or ''} | {r.get('running_since') or 'n/a'} | "
            f"{r.get('code_newest_at') or 'n/a'} | {r['verdict']} | {r.get('reason', '')} | {r['remedy_hint']} |"
        )

    lines.append("## STALE — running code older than what's on disk")
    lines.append("")
    lines.append("| unit/process | pid | running since | code newest | verdict | reason | remedy |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in sorted(stale, key=lambda r: r["target"]):
        lines.append(row(r))
    lines.append("")

    lines.append("## FRESH")
    lines.append("")
    lines.append("| unit/process | pid | running since | code newest | verdict | reason | remedy |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in sorted(fresh, key=lambda r: r["target"]):
        lines.append(row(r))
    lines.append("")

    if unknown:
        lines.append("## Unknown (insufficient data — not alerted on)")
        lines.append("")
        lines.append("| unit/process | pid | reason |")
        lines.append("|---|---|---|")
        for r in sorted(unknown, key=lambda r: r["target"]):
            lines.append(f"| {r['target']} | {r.get('pid') or ''} | {r.get('reason', '')} |")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(*, roster_path: Path = ROSTER_FILE, state_path: Path = STATE_FILE, report_path: Path = REPORT_FILE,
        summary_path: Path = SUMMARY_FILE, dry_run: bool = False,
        list_fn=systemctl_list_units, show_fn=systemctl_show,
        pid_list_fn=list_proc_pids, cmdline_fn=read_cmdline, lstart_fn=ps_lstart,
        now: Optional[float] = None) -> List[dict]:
    now = now if now is not None else time.time()
    roster = load_roster(roster_path)
    state = load_state(state_path)

    results: List[dict] = []

    for unit in discover_units(roster, list_fn=list_fn, show_fn=show_fn):
        props = show_fn(unit)
        r = evaluate_unit(unit, props, now=now)
        if r is not None:
            results.append(r)

    for matcher in roster.get("appimage_matchers", []):
        results.extend(evaluate_appimage(
            matcher, pid_list_fn=pid_list_fn, cmdline_fn=cmdline_fn, lstart_fn=lstart_fn,
        ))

    process_results(results, state, dry_run=dry_run, now=now)

    save_state(state, state_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, generated_at=now))
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({
        "generated_at": iso(now),
        "targets": results,
    }, indent=2, sort_keys=True))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="deploy-staleness — detect running processes serving stale code")
    parser.add_argument("--roster", type=Path, default=ROSTER_FILE)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    parser.add_argument("--summary-file", type=Path, default=SUMMARY_FILE)
    parser.add_argument("--dry-run", action="store_true", help="compute + report, never publish")
    args = parser.parse_args()

    results = run(
        roster_path=args.roster, state_path=args.state_file, report_path=args.report_file,
        summary_path=args.summary_file, dry_run=args.dry_run,
    )

    stale = [r for r in results if r["verdict"] == "stale"]
    sys.stderr.write(
        f"[deploy-staleness] evaluated {len(results)} targets: stale={len(stale)} report={args.report_file}\n"
    )
    for r in stale:
        sys.stderr.write(
            f"[deploy-staleness] STALE {r['target']} (pid={r.get('pid')}): "
            f"running_since={r.get('running_since')} code_newest={r.get('code_newest_at')} reason={r['reason']}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
