"""The baseline captured before the grill ships: hearth's answered rows and asks per landed run.

Hearth rows come from `hearth-ask list --status answered`. Runs come from the read-only
`reflex sql` verb. Attention events come from the bus journal (agent.attention.v1), joined
to runs by session id. Runs with no outcome label are excluded."""
import datetime
import gzip
import json
import os
import re
import shlex
import statistics
import subprocess

from crucible_ledger.store import LedgerError

SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOOL_TIMEOUT_S = 120


def _tool(env_name, default):
    return shlex.split(os.environ.get(env_name) or default)


def _run(cmd, what):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)
    except FileNotFoundError:
        raise LedgerError(f"{what} not found: {cmd[0]}", 127)
    except subprocess.TimeoutExpired:
        raise LedgerError(f"{what} timed out", 124)
    if p.returncode:
        raise LedgerError(f"{what} failed ({p.returncode}): {p.stderr.strip()[:200]}", 5)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise LedgerError(f"{what} printed no JSON", 5)


def hearth_rows(project=None, since=None):
    cmd = _tool("CRUCIBLE_HEARTH_ASK", "hearth-ask") + ["list", "--status", "answered"]
    if project:
        cmd += ["--project", project]
    rows = _run(cmd, "hearth-ask").get("decisions", [])
    return [r for r in rows if not since or str(r.get("created_at", ""))[:10] >= since]


def _seconds(r):
    try:
        a = datetime.datetime.fromisoformat(r["created_at"])
        b = datetime.datetime.fromisoformat(r["responded_at"])
        return (b - a).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def hearth_summary(rows):
    free = [r for r in rows if str(r.get("text") or "").strip()]
    owner = [s for s in (_seconds(r) for r in rows if r.get("answered_by") != "default") if s is not None]
    return {"answered": len(rows), "free_text": len(free),
            "free_text_share": round(len(free) / len(rows), 4) if rows else None,
            "defaulted": sum(1 for r in rows if r.get("answered_by") == "default"),
            "median_seconds_to_answer": round(statistics.median(owner), 3) if owner else None}


def event_files(paths):
    if paths:
        return list(paths)
    base = os.path.join(os.path.expanduser("~"), ".cache", "nervous-bus", "debug.jsonl")
    return [p for p in [base, base + ".1.gz", base + ".2.gz", base + ".3.gz"] if os.path.isfile(p)]


def attention_asks(paths, since=None):
    """{session_id: set of distinct ask strings} over every agent.attention.v1 event."""
    asks, events = {}, 0
    for path in event_files(paths):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "agent.attention.v1" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    d = ev.get("data") or {}
                    if ev.get("type") != "agent.attention.v1" or not d.get("session_id"):
                        continue
                    if since and str(ev.get("time", ""))[:10] < since:
                        continue
                    events += 1
                    s = asks.setdefault(d["session_id"], set())
                    s.update(i for i in d.get("items") or [] if isinstance(i, str) and i.strip())
        except OSError as e:
            raise LedgerError(f"cannot read {path}: {e}", 5)
    return asks, events


def _where(project, since):
    if project and not SAFE.match(project):
        raise LedgerError("bad --project", 2)
    if since and not DATE.match(since):
        raise LedgerError("--since must be YYYY-MM-DD", 2)
    return ("" if not project else f" AND project = '{project}'") + ("" if not since else f" AND started >= '{since}'")


def reflex_runs(project=None, since=None):
    """({outcome: {session_id: runs}}, count of runs excluded for having no outcome label)."""
    cmd = _tool("CRUCIBLE_REFLEX", "reflex") + ["sql", "--json"]
    w = _where(project, since)
    rows = _run(cmd + ["SELECT session_id, outcome, count(*) AS n FROM runs WHERE labeled_at IS NOT NULL "
                       "AND outcome IS NOT NULL AND session_id IS NOT NULL" + w + " GROUP BY session_id, outcome"], "reflex")
    skipped = _run(cmd + ["SELECT count(*) AS n FROM runs WHERE (labeled_at IS NULL OR outcome IS NULL)" + w], "reflex")
    by = {}
    for r in rows:
        if r.get("outcome") is not None:
            by.setdefault(r["outcome"], {})[r["session_id"]] = int(r["n"])
    return by, int(skipped[0]["n"]) if skipped else 0


def asks_per_run(by_outcome, asks):
    """A session's distinct asks count once per outcome it has runs in; runs with no events count zero asks."""
    out = {}
    for outcome, sessions in sorted(by_outcome.items()):
        runs = sum(sessions.values())
        n = sum(len(asks.get(s, ())) for s in sessions)
        out[outcome] = {"runs": runs, "sessions": len(sessions), "asks": n,
                        "sessions_with_asks": sum(1 for s in sessions if asks.get(s)),
                        "asks_per_run": round(n / runs, 4) if runs else None}
    return out


def baseline(project=None, hearth_project=None, since=None, events=None):
    rows = hearth_rows(hearth_project or project, since)
    by_outcome, skipped = reflex_runs(project, since)
    asks, n_events = attention_asks(events, since)
    return {
        "hearth": hearth_summary(rows),
        "runs": asks_per_run(by_outcome, asks),
        "asks_per_landed_run": asks_per_run(by_outcome, asks).get("landed", {}).get("asks_per_run"),
        "excluded_unlabeled_runs": skipped,
        "attention": {"sessions": len(asks), "events": n_events},
        "project": project, "since": since,
    }
