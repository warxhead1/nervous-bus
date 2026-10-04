"""One sample window -> attributed result; bounded sqlite history of those results."""
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import hl_detect
import hl_interval
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
           cfg=None, exists=os.path.exists, prev=None, now=time.time):
    """Two scans `interval` apart. `cores` is the mean over that window ("instant"); `cores_int`
    is the mean over the whole gap since the `prev` snapshot (None without one). RSS/swap are the 2nd scan."""
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
    majflt_s = {pid: max(0, p.majflt - before[pid].majflt) / wall for pid, p in after.items()
                if pid in before and before[pid].key == p.key}
    io_delta = {pid: p.io_bytes - before[pid].io_bytes for pid, p in after.items()
                if pid in before and before[pid].key == p.key and p.io_bytes >= 0
                and before[pid].io_bytes >= 0}
    ts, up = now(), uptime(proc_root)
    boot, busy = hl_interval.boot_id(proc_root), hl_interval.busy_ticks(proc_root)
    integ, integ_info = hl_interval.integrate(prev, after, ts, up, boot, busy)
    projects = defaultdict(lambda: {"cores": 0.0, "cores_int": None, "rss": 0, "anon": 0, "swap": 0,
                                    "nproc": 0, "majflt_s": 0.0, "majflt_int": None, "top": []})
    for p in after.values():
        row = projects[p.project]
        row["cores"] += cores.get(p.pid, 0.0)
        row["rss"] += p.rss_bytes
        row["anon"] += p.anon_bytes if p.anon_bytes >= 0 else p.rss_bytes
        row["swap"] += p.swap_bytes
        row["nproc"] += 1
        row["majflt_s"] += majflt_s.get(p.pid, 0.0)
        if integ is not None:
            row["cores_int"] = (row["cores_int"] or 0.0) + integ.get(p.pid, {}).get("cores", 0.0)
            row["majflt_int"] = (row["majflt_int"] or 0.0) + integ.get(p.pid, {}).get("majflt_s", 0.0)
        row["top"].append((cores.get(p.pid, 0.0), p.pid, p.comm))
    for row in projects.values():
        row["top"] = [{"pid": pid, "comm": comm, "cores": round(c, 3)}
                      for c, pid, comm in sorted(row["top"], reverse=True)[:3]]
        for k in ("cores", "cores_int", "majflt_s", "majflt_int"):
            if row[k] is not None:
                row[k] = round(row[k], 4)
    psi = hl_procs.read_psi(psi_root)
    findings = hl_detect.detect(after, cores, io_delta, up, mem1, mem0, wall, psi,
                                {k: v["cores"] for k, v in projects.items()}, cfg, exists)
    return {"ts": ts, "wall_s": round(wall, 3), "psi": psi, "mem": mem1,
            "projects": dict(projects), "findings": findings, "nprocs": len(after),
            "unknown_share": _unknown_share(projects), "interval": integ_info,
            "_procs": after, "_snapshot": hl_interval.snapshot(after, ts, up, boot, busy)}


def _unknown_share(projects):
    total = sum(r["cores"] for r in projects.values())
    return round(projects["unknown"]["cores"] / total, 3) if total and "unknown" in projects else 0.0


def public(s):
    """The JSON-safe view of a sample (drops private _-prefixed live objects)."""
    return {k: v for k, v in s.items() if not k.startswith("_")}


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
        CREATE INDEX IF NOT EXISTS finding_ts ON finding(ts);
        CREATE TABLE IF NOT EXISTS snapshot_meta (id INTEGER PRIMARY KEY CHECK (id=1), ts REAL,
          uptime REAL, boot TEXT, busy INTEGER);
        CREATE TABLE IF NOT EXISTS proc_snapshot (pid INTEGER, start_ticks INTEGER, cpu_ticks INTEGER,
          majflt INTEGER, read_bytes INTEGER, project TEXT, agent TEXT, child_ticks INTEGER DEFAULT 0, ppid INTEGER DEFAULT 0,
          PRIMARY KEY(pid, start_ticks));
        CREATE TABLE IF NOT EXISTS interval_stat (ts REAL PRIMARY KEY, interval_s REAL, observed_cores REAL,
          system_busy_cores REAL, coverage REAL, unattributed_cores REAL, unknown_cores REAL,
          unknown_nproc INTEGER, swap_in_s REAL, swap_out_s REAL);""")
        for col, typ in (("child_ticks", "INTEGER DEFAULT 0"), ("ppid", "INTEGER DEFAULT 0")):
            have = {r[1] for r in self.db.execute("PRAGMA table_info(proc_snapshot)")}
            if col not in have:
                self.db.execute(f"ALTER TABLE proc_snapshot ADD COLUMN {col} {typ}")
        for col, typ in (("cores_int", "REAL"), ("anon", "INTEGER"), ("majflt_s", "REAL"), ("majflt_int", "REAL")):
            have = {r[1] for r in self.db.execute("PRAGMA table_info(project)")}
            if col not in have:
                self.db.execute(f"ALTER TABLE project ADD COLUMN {col} {typ}")

    def record(self, s, retention_days=7):
        p, m, ts = s["psi"], s["mem"], s["ts"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO psi VALUES (?,?,?,?,?,?,?,?,?)", (
                ts, p.get("cpu_some_avg10"), p.get("cpu_some_avg60"), p.get("memory_some_avg10"),
                p.get("memory_full_avg10"), p.get("io_some_avg10"), p.get("io_full_avg10"),
                m.get("swap_total", 0) - m.get("swap_free", 0), m.get("mem_available")))
            cols = "(ts,project,cores,rss,swap,nproc,cores_int,anon,majflt_s,majflt_int)"
            ins = f"INSERT OR REPLACE INTO project {cols} VALUES (?,?,?,?,?,?,?,?,?,?)"
            fold = {"cores": 0.0, "rss": 0, "swap": 0, "nproc": 0, "cores_int": 0.0, "anon": 0,
                    "majflt_s": 0.0, "majflt_int": 0.0}
            for name, r in s["projects"].items():
                if r["cores"] >= MIN_CORES or r["rss"] >= MIN_RSS or name in ("unknown", "kernel"):
                    self.db.execute(ins, (ts, name, r["cores"], r["rss"], r["swap"], r["nproc"],
                                          r.get("cores_int"), r.get("anon"), r.get("majflt_s"),
                                          r.get("majflt_int")))
                else:
                    for k in fold:
                        fold[k] += r.get(k) or 0
            if fold["nproc"]:
                self.db.execute(ins, (ts, OTHER, fold["cores"], fold["rss"], fold["swap"], fold["nproc"],
                                      fold["cores_int"], fold["anon"], fold["majflt_s"], fold["majflt_int"]))
            for f in s["findings"]:
                self.db.execute("INSERT INTO finding VALUES (?,?,?,?,?,?,?,?)", (
                    ts, f["kind"], f["severity"], f["project"], f["summary"], f["cpu_cores"],
                    f["rss_bytes"], json.dumps(f["evidence"], default=str)))
            iv = s.get("interval") or {}
            unk = s["projects"].get("unknown", {})
            self.db.execute("INSERT OR REPLACE INTO interval_stat VALUES (?,?,?,?,?,?,?,?,?,?)", (
                ts, iv.get("interval_s"), iv.get("observed_cores"), iv.get("system_busy_cores"),
                iv.get("coverage"), iv.get("unattributed_cores"), unk.get("cores"), unk.get("nproc"),
                (s.get("paging") or {}).get("swap_in_pages_s"), (s.get("paging") or {}).get("swap_out_pages_s")))
            if s.get("_snapshot"):
                self.save_snapshot(s["_snapshot"], s.get("_procs") or {})
            cut = time.time() - retention_days * 86400
            for t in ("psi", "project", "finding", "interval_stat"):
                self.db.execute(f"DELETE FROM {t} WHERE ts < ?", (cut,))

    def save_snapshot(self, snap, procs):
        """Keep only the latest per-process counters; they exist to be diffed by the next run."""
        self.db.execute("DELETE FROM proc_snapshot")
        self.db.execute("INSERT OR REPLACE INTO snapshot_meta VALUES (1,?,?,?,?)",
                        (snap["ts"], snap["uptime"], snap["boot"], snap["busy"]))
        self.db.executemany(
            "INSERT OR REPLACE INTO proc_snapshot (pid,start_ticks,cpu_ticks,majflt,read_bytes,child_ticks,ppid,project,agent)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(pid, start, t, mf, rb, ct, pp, getattr(procs.get(pid), "project", None),
              getattr(procs.get(pid), "agent", None))
             for (pid, start), (t, mf, rb, ct, pp) in snap["rows"].items()])

    def load_snapshot(self):
        meta = self.db.execute("SELECT ts,uptime,boot,busy FROM snapshot_meta WHERE id=1").fetchone()
        if not meta:
            return None
        rows = {(pid, start): (t, mf, rb, ct or 0, pp or 0)
                for pid, start, t, mf, rb, ct, pp in self.db.execute(
                    "SELECT pid,start_ticks,cpu_ticks,majflt,read_bytes,child_ticks,ppid FROM proc_snapshot")}
        return {"ts": meta[0], "uptime": meta[1], "boot": meta[2], "busy": meta[3], "rows": rows}

    def psi_series(self, since):
        cur = self.db.execute("SELECT ts,cpu_some10,cpu_some60,mem_some10,mem_full10,io_some10,io_full10 "
                              "FROM psi WHERE ts>=? ORDER BY ts", (since,))
        keys = ["ts", "cpu_some10", "cpu_some60", "mem_some10", "mem_full10", "io_some10", "io_full10"]
        return [dict(zip(keys, r)) for r in cur]

    def project_series(self, since, prefer_interval=True):
        """{project: [(ts, cores, rss)]}. cores is the interval mean when recorded, else the 3s window."""
        out = defaultdict(list)
        for ts, name, cores, ci, rss in self.db.execute(
                "SELECT ts,project,cores,cores_int,rss FROM project WHERE ts>=? ORDER BY ts", (since,)):
            use = ci if (prefer_interval and ci is not None) else cores
            out[name].append((ts, use or 0.0, rss or 0))
        return dict(out)

    def latest(self):
        """Newest recorded sample as {"ts", "projects": {name: row}} or None."""
        row = self.db.execute("SELECT MAX(ts) FROM project").fetchone()
        if not row or row[0] is None:
            return None
        ts = row[0]
        keys = ("cores", "cores_int", "rss", "anon", "swap", "nproc", "majflt_s", "majflt_int")
        projects = {name: dict(zip(keys, vals)) for name, *vals in self.db.execute(
            "SELECT project,cores,cores_int,rss,anon,swap,nproc,majflt_s,majflt_int FROM project WHERE ts=?", (ts,))}
        findings = [dict(zip(("kind", "severity", "project", "summary", "cpu_cores"), r)) for r in self.db.execute(
            "SELECT kind,severity,project,summary,cpu_cores FROM finding WHERE ts=?", (ts,))]
        psi = self.psi_series(ts)[-1:] or [{}]
        return {"ts": ts, "projects": projects, "findings": findings, "psi": psi[0]}

    def recent_findings(self, since, limit=30):
        cur = self.db.execute("SELECT ts,kind,severity,project,summary,cpu_cores FROM finding "
                              "WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))
        return [dict(zip(("ts", "kind", "severity", "project", "summary", "cpu_cores"), r)) for r in cur]
