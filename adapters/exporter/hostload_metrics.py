"""Host-load gauges for the nervous-bus exporter.

Reads the NEWEST sample the host-load adapter recorded (sqlite, read-only, once per scrape) rather than
sampling /proc itself: the timer already pays for the sample, and a scrape evaluated here can never leave a
stale label behind when a project goes quiet. All gauges are prefixed ``nbus_host_load_``.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get("NERVOUS_HOST_LOAD_DB", str(Path.home() / ".cache/nervous-bus/host-load/history.sqlite3")))
STALE_AFTER_S = 300
TOP_PROJECTS = 20
TOP_AGENTS = 15


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _rows(db, sql, *args):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def collect(db_path=DB, now=None):
    """-> [(name, help, [(labels, value)])]; every family is a gauge."""
    now = time.time() if now is None else now
    fams = []

    def fam(name, help_text, samples):
        fams.append((f"nbus_host_load_{name}", help_text, samples))

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        fam("up", "1 when a host-load sample newer than 5 minutes exists.", [({}, 0)])
        return fams
    try:
        row = _rows(db, "SELECT MAX(ts) FROM project")
        ts = row[0][0] if row and row[0][0] else None
        if ts is None:
            fam("up", "1 when a host-load sample newer than 5 minutes exists.", [({}, 0)])
            return fams
        age = now - ts
        fam("up", "1 when a host-load sample newer than 5 minutes exists.", [({}, 1 if age < STALE_AFTER_S else 0)])
        fam("sample_age_seconds", "Age of the newest recorded host-load sample.", [({}, round(age, 1))])
        if age >= STALE_AFTER_S:
            return fams          # stale numbers would read as current load; expose only the age
        proj = _rows(db, "SELECT project,cores,cores_int,rss,anon,swap,nproc,majflt_s,majflt_int FROM project WHERE ts=?",
                     ts)
        proj.sort(key=lambda r: -(r[2] if r[2] is not None else (r[1] or 0)))
        keep = [r for r in proj if r[0] != "~other"][:TOP_PROJECTS]
        rest = [r for r in proj if r not in keep]
        if rest:
            keep.append(("~other",) + tuple(sum((r[i] or 0) for r in rest) for i in range(1, 9)))
        total = sum((r[2] if r[2] is not None else r[1]) or 0 for r in proj) or 1.0
        L = lambda r: {"project": r[0]}  # noqa: E731
        cores = lambda r: (r[2] if r[2] is not None else r[1]) or 0.0  # noqa: E731
        fam("project_cpu_cores", "CPU cores used by project; mean over the interval since the previous sample "
            "when known, else the 3s window.", [(L(r), cores(r)) for r in keep])
        fam("project_cpu_instant_cores", "CPU cores used by project over the short sample window.",
            [(L(r), r[1] or 0.0) for r in keep])
        fam("project_cpu_share_ratio", "Project share of all attributed CPU (0..1).",
            [(L(r), cores(r) / total) for r in keep])
        fam("project_rss_bytes", "Resident set size summed per project (shared pages counted per process).",
            [(L(r), r[3] or 0) for r in keep])
        fam("project_anon_bytes", "Anonymous resident memory per project (excludes shared/file pages).",
            [(L(r), r[4] or 0) for r in keep])
        fam("project_swap_bytes", "Swap currently held by the project's processes.", [(L(r), r[5] or 0) for r in keep])
        fam("project_processes", "Processes attributed to the project.", [(L(r), r[6] or 0) for r in keep])
        fam("project_major_faults_per_second", "Major page faults per second paid by the project.",
            [(L(r), (r[8] if r[8] is not None else r[7]) or 0.0) for r in keep])

        counts = {}
        for kind, sev, project in _rows(db, "SELECT kind,severity,project FROM finding WHERE ts=?", ts):
            counts[(kind, sev, project)] = counts.get((kind, sev, project), 0) + 1
        fam("findings", "Findings in the newest sample, by detector, severity and project.",
            [({"kind": k, "severity": s, "project": p}, n) for (k, s, p), n in sorted(counts.items())])

        agents = _rows(db, "SELECT agent,COALESCE(cores_int,cores),cores,rss,nproc,orphans FROM agent WHERE ts=? "
                           "ORDER BY 2 DESC LIMIT ?", ts, TOP_AGENTS)
        fam("agent_cpu_cores", "CPU cores used per agent session (claude/codex/orca worktree).",
            [({"agent": a[0]}, a[1] or 0.0) for a in agents])
        fam("agent_rss_bytes", "Resident memory per agent session.", [({"agent": a[0]}, a[3] or 0) for a in agents])
        fam("agent_orphan_processes", "Processes of the agent reparented to systemd (leak candidates).",
            [({"agent": a[0]}, a[5] or 0) for a in agents])

        psi = _rows(db, "SELECT cpu_some10,cpu_some60,mem_some10,mem_full10,io_some10,io_full10,swap_used,mem_available "
                        "FROM psi WHERE ts=?", ts)
        if psi:
            p = psi[0]
            fam("psi_some_avg10_percent", "PSI 'some' avg10 stall percent.",
                [({"resource": "cpu"}, p[0] or 0), ({"resource": "memory"}, p[2] or 0), ({"resource": "io"}, p[4] or 0)])
            fam("psi_cpu_some_avg60_percent", "PSI cpu 'some' avg60 stall percent.", [({}, p[1] or 0)])
            fam("psi_full_avg10_percent", "PSI 'full' avg10 stall percent.",
                [({"resource": "memory"}, p[3] or 0), ({"resource": "io"}, p[5] or 0)])
            fam("swap_used_bytes", "Swap in use host-wide.", [({}, p[6] or 0)])
            fam("memory_available_bytes", "MemAvailable host-wide.", [({}, p[7] or 0)])
        iv = _rows(db, "SELECT coverage,unattributed_cores,unknown_cores,unknown_nproc,swap_in_s,swap_out_s,"
                       "system_busy_cores FROM interval_stat WHERE ts=?", ts)
        if iv:
            c = iv[0]
            for name, help_text, val in (
                    ("cpu_coverage_ratio", "Fraction of system busy CPU the per-process scan explained.", c[0]),
                    ("unattributed_cpu_cores", "Busy CPU from processes that exited unseen between samples.", c[1]),
                    ("unknown_project_cpu_cores", "CPU in the residual 'unknown' project bucket.", c[2]),
                    ("unknown_project_processes", "Processes in the residual 'unknown' project bucket.", c[3]),
                    ("swap_in_pages_per_second", "Host swap-in rate (pages/s).", c[4]),
                    ("swap_out_pages_per_second", "Host swap-out rate (pages/s).", c[5]),
                    ("system_busy_cores", "Host non-idle CPU in cores over the sample interval.", c[6])):
                if val is not None:
                    fam(name, help_text, [({}, val)])
    finally:
        db.close()
    return fams


def render_text(fams) -> str:
    out = []
    for name, help_text, samples in fams:
        out += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            out.append(f"{name}{'{' + lab + '}' if lab else ''} {float(value)}")
    return "\n".join(out) + "\n"


class HostLoadCollector:
    """prometheus_client custom collector: evaluated at scrape time."""

    def __init__(self, db_path=DB):
        self.db_path = db_path

    def collect(self):
        from prometheus_client.core import GaugeMetricFamily
        for name, help_text, samples in collect(self.db_path):
            names = sorted({k for labels, _ in samples for k in labels})
            g = GaugeMetricFamily(name, help_text, labels=names)
            for labels, value in samples:
                g.add_metric([labels.get(k, "") for k in names], float(value))
            yield g


def attach(registry, db_path=DB):
    """Hook the host-load gauges into the exporter's MetricRegistry (either backend)."""
    registry.add_collector(HostLoadCollector(db_path), lambda: render_text(collect(db_path)))
