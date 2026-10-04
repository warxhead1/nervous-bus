"""One sample window -> attributed result; bounded sqlite history of those results."""
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import hl_detect
import hl_procs

DEFAULT_DB = Path(os.environ.get("NERVOUS_HOST_LOAD_DB",
                                 str(Path.home() / ".cache/nervous-bus/host-load/history.sqlite3")))
MIN_CORES, MIN_RSS = 0.01, 64 * 1024 * 1024
OTHER = "~other"


def uptime(proc_root):
    try:
        return float((Path(proc_root) / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def sample(proc_root="/proc", psi_root=None, interval=3.0, docker=None, sleep=time.sleep,
           cfg=None, exists=os.path.exists):
    """Two scans `interval` apart. CPU is the mean over that window; RSS/swap are the 2nd scan."""
    psi_root = psi_root or os.path.join(proc_root, "pressure")
    before = hl_procs.scan(proc_root)
    mem0 = hl_procs.read_mem(proc_root)
    t0 = time.monotonic()
    sleep(interval)
    after = hl_procs.scan(proc_root)
    wall = max(time.monotonic() - t0, 1e-6) if sleep is time.sleep else interval
    mem1 = hl_procs.read_mem(proc_root)
    docker = hl_procs.docker_projects() if docker is None else docker
    hl_procs.attribute(after, docker)
    cores = hl_procs.cpu_deltas(before, after, wall)
    io_delta = {pid: p.io_bytes - before[pid].io_bytes for pid, p in after.items()
                if pid in before and before[pid].key == p.key and p.io_bytes >= 0
                and before[pid].io_bytes >= 0}
    projects = defaultdict(lambda: {"cores": 0.0, "rss": 0, "swap": 0, "nproc": 0, "top": []})
    for p in after.values():
        row = projects[p.project]
        row["cores"] += cores.get(p.pid, 0.0)
        row["rss"] += p.rss_bytes
        row["swap"] += p.swap_bytes
        row["nproc"] += 1
        row["top"].append((cores.get(p.pid, 0.0), p.pid, p.comm))
    for row in projects.values():
        row["top"] = [{"pid": pid, "comm": comm, "cores": round(c, 3)}
                      for c, pid, comm in sorted(row["top"], reverse=True)[:3]]
        row["cores"] = round(row["cores"], 4)
    psi = hl_procs.read_psi(psi_root)
    findings = hl_detect.detect(after, cores, io_delta, uptime(proc_root), mem1, mem0, wall, psi,
                                {k: v["cores"] for k, v in projects.items()}, cfg, exists)
    return {"ts": time.time(), "wall_s": round(wall, 3), "psi": psi, "mem": mem1,
            "projects": dict(projects), "findings": findings, "nprocs": len(after),
            "unknown_share": _unknown_share(projects)}


def _unknown_share(projects):
    total = sum(r["cores"] for r in projects.values())
    return round(projects["unknown"]["cores"] / total, 3) if total and "unknown" in projects else 0.0


class History:
    def __init__(self, path=DEFAULT_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS psi (ts REAL PRIMARY KEY, cpu_some10 REAL, cpu_some60 REAL,
          mem_some10 REAL, mem_full10 REAL, io_some10 REAL, io_full10 REAL,
          swap_used INTEGER, mem_available INTEGER);
        CREATE TABLE IF NOT EXISTS project (ts REAL NOT NULL, project TEXT NOT NULL, cores REAL,
          rss INTEGER, swap INTEGER, nproc INTEGER, PRIMARY KEY(ts, project));
        CREATE TABLE IF NOT EXISTS finding (ts REAL NOT NULL, kind TEXT, severity TEXT, project TEXT,
          summary TEXT, cpu_cores REAL, rss_bytes INTEGER, evidence TEXT);
        CREATE INDEX IF NOT EXISTS finding_ts ON finding(ts);""")

    def record(self, s, retention_days=7):
        p, m, ts = s["psi"], s["mem"], s["ts"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO psi VALUES (?,?,?,?,?,?,?,?,?)", (
                ts, p.get("cpu_some_avg10"), p.get("cpu_some_avg60"), p.get("memory_some_avg10"),
                p.get("memory_full_avg10"), p.get("io_some_avg10"), p.get("io_full_avg10"),
                m.get("swap_total", 0) - m.get("swap_free", 0), m.get("mem_available")))
            fold = {"cores": 0.0, "rss": 0, "swap": 0, "nproc": 0}
            for name, r in s["projects"].items():
                if r["cores"] >= MIN_CORES or r["rss"] >= MIN_RSS or name == "unknown":
                    self.db.execute("INSERT OR REPLACE INTO project VALUES (?,?,?,?,?,?)",
                                    (ts, name, r["cores"], r["rss"], r["swap"], r["nproc"]))
                else:
                    for k in fold:
                        fold[k] += r[k]
            if fold["nproc"]:
                self.db.execute("INSERT OR REPLACE INTO project VALUES (?,?,?,?,?,?)",
                                (ts, OTHER, fold["cores"], fold["rss"], fold["swap"], fold["nproc"]))
            for f in s["findings"]:
                self.db.execute("INSERT INTO finding VALUES (?,?,?,?,?,?,?,?)", (
                    ts, f["kind"], f["severity"], f["project"], f["summary"], f["cpu_cores"],
                    f["rss_bytes"], json.dumps(f["evidence"], default=str)))
            cut = time.time() - retention_days * 86400
            for t in ("psi", "project", "finding"):
                self.db.execute(f"DELETE FROM {t} WHERE ts < ?", (cut,))

    def psi_series(self, since):
        cur = self.db.execute("SELECT ts,cpu_some10,cpu_some60,mem_some10,mem_full10,io_some10,io_full10 "
                              "FROM psi WHERE ts>=? ORDER BY ts", (since,))
        keys = ["ts", "cpu_some10", "cpu_some60", "mem_some10", "mem_full10", "io_some10", "io_full10"]
        return [dict(zip(keys, r)) for r in cur]

    def project_series(self, since):
        """{project: [(ts, cores, rss)]} plus the sorted list of timestamps."""
        out = defaultdict(list)
        for ts, name, cores, rss in self.db.execute(
                "SELECT ts,project,cores,rss FROM project WHERE ts>=? ORDER BY ts", (since,)):
            out[name].append((ts, cores or 0.0, rss or 0))
        return dict(out)

    def recent_findings(self, since, limit=30):
        cur = self.db.execute("SELECT ts,kind,severity,project,summary,cpu_cores FROM finding "
                              "WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))
        return [dict(zip(("ts", "kind", "severity", "project", "summary", "cpu_cores"), r)) for r in cur]
