"""Inefficiency detectors over one attributed sample window.

Each finding: kind, severity (info|warn|crit), project, summary, count, cpu_cores,
rss_bytes, pids (capped) and an evidence dict. Detectors only describe; nothing here acts.
"""
import os
import re
from collections import defaultdict

from hl_procs import scrub
from hl_services import excused

DEFAULTS = {
    "orphan_min_cores": 0.02,      # per process, cores
    "loop_min_cores": 0.8,
    "loop_min_age_s": 600,
    "loop_max_io_bytes": 65536,    # rchar+wchar growth over the window
    "swap_pages_per_s": 200,
    "kswapd_cores": 0.05,
    "mem_psi_some10": 10.0,
    "cpu_psi_some60": 50.0,
    "stale_rss_bytes": 256 * 1024 * 1024,
    "max_pids": 8,
}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ash", "ksh"}
SPIN = re.compile(r"while\s+(?::|true)\s*;|for\s*\(\(\s*;\s*;\s*\)\)|yes\s*>\s*/dev/null")
WORKTREES = re.compile(r"^(/home/eric/data2/worktrees/[^/]+/[^/]+)")
GO_VERBS = {"build", "test", "vet", "run", "install", "generate"}


def _pids(ps, cfg):
    return [p.pid for p in sorted(ps, key=lambda p: -p.rss_bytes)[:cfg["max_pids"]]]


def _finding(kind, severity, project, summary, ps, cores, evidence, cfg):
    tally = defaultdict(int)
    for p in ps:
        if getattr(p, "agent", ""):
            tally[p.agent] += 1
    if tally:
        evidence = {**evidence, "agents": [{"agent": a, "count": n} for a, n in
                                           sorted(tally.items(), key=lambda kv: -kv[1])[:5]]}
    return {"kind": kind, "severity": severity, "project": project, "summary": summary,
            "count": len(ps), "cpu_cores": round(sum(cores.get(p.pid, 0.0) for p in ps), 3),
            "rss_bytes": sum(p.rss_bytes for p in ps), "pids": _pids(ps, cfg), "evidence": evidence}


def cmd_text(p):
    return scrub(" ".join(p.cmdline) or "[" + p.comm + "]", 100)


def user_systemd_pids(procs):
    return {p.pid for p in procs.values() if p.comm == "systemd" and p.pid != 1}


def orphans(procs, cores, cfg, probe=None, notes=None):
    """probe(candidates) -> {pid: [service-evidence reasons]}; notes["excused"] receives what it cleared."""
    parents = user_systemd_pids(procs)
    groups = defaultdict(list)
    cands = []
    for p in procs.values():
        if p.uid == 0 or p.pid == 1 or not (p.ppid == 1 or p.ppid in parents):
            continue
        if cores.get(p.pid, 0.0) < cfg["orphan_min_cores"]:
            continue
        if p.comm not in SHELLS and p.cgroup.endswith(".service"):
            continue  # a service main process under its own unit is not an orphan
        if p.comm not in SHELLS and not WORKTREES.match(p.cwd):
            continue
        cands.append(p)
    ev = probe(cands) if (probe and cands) else {}
    for p in cands:
        spin = bool(SPIN.search(" ".join(p.cmdline)))
        if excused(ev.get(p.pid), spin):
            if notes is not None:
                notes.setdefault("excused", []).append(
                    {"pid": p.pid, "comm": p.comm, "project": p.project, "reasons": ev[p.pid],
                     "cores": round(cores.get(p.pid, 0.0), 3), "cmd": cmd_text(p)[:60]})
            continue
        groups[(p.project, p.comm, cmd_text(p)[:80])].append(p)
    out, every = [], set()
    for (project, comm, cmd), ps in groups.items():
        total = sum(cores.get(p.pid, 0.0) for p in ps)
        every.update(p.pid for p in ps)
        spin = bool(SPIN.search(" ".join(ps[0].cmdline)))
        out.append(_finding(
            "orphan_cpu", "crit" if total >= 1.0 else "warn", project,
            f"{len(ps)} orphan {comm} reparented to systemd burning {total:.2f} cores"
            + (" (shell spin loop)" if spin else ""), ps, cores,
            {"cmd": cmd, "cwd": ps[0].cwd, "cgroup": sorted({p.cgroup for p in ps}),
             "ppid": sorted({p.ppid for p in ps}), "spin_loop": spin,
             "oldest_start_ticks": min(p.start_ticks for p in ps)}, cfg))
    return out, every


def stale_cwd(procs, cores, cfg, exists=os.path.exists):
    groups = defaultdict(list)
    for p in procs.values():
        if not p.cwd:
            continue
        deleted = p.cwd.endswith(" (deleted)")
        m = WORKTREES.match(p.cwd.replace(" (deleted)", ""))
        if deleted or (m and not exists(m.group(1))):
            groups[(p.project, m.group(1) if m else p.cwd)].append(p)
    out = []
    for (project, where), ps in groups.items():
        cpu = sum(cores.get(p.pid, 0.0) for p in ps)
        heavy = cpu >= cfg["orphan_min_cores"] or sum(p.rss_bytes for p in ps) >= cfg["stale_rss_bytes"]
        out.append(_finding(
            "stale_cwd", "warn" if heavy else "info", project,
            f"{len(ps)} process(es) whose cwd {where} no longer exists", ps, cores,
            {"cwd": where, "comms": sorted({p.comm for p in ps})[:6]}, cfg))
    return out


def pure_loops(procs, cores, io_delta, uptime_s, cfg, skip=frozenset()):
    from hl_procs import CLK_TCK
    out = []
    for p in procs.values():
        c = cores.get(p.pid, 0.0)
        if p.pid in skip or c < cfg["loop_min_cores"] or not p.cmdline:
            continue
        age = uptime_s - p.start_ticks / CLK_TCK
        if age < cfg["loop_min_age_s"]:
            continue
        io = io_delta.get(p.pid)
        if io is None or io > cfg["loop_max_io_bytes"]:
            continue  # unreadable io is not evidence of a pure loop
        life = p.cpu_ticks / CLK_TCK / age
        if life < cfg["loop_min_cores"]:
            continue
        out.append(_finding(
            "cpu_loop", "warn", p.project,
            f"{p.comm} pid {p.pid} at {c:.2f} cores for {age/60:.0f} min with ~no io",
            [p], cores, {"cmd": cmd_text(p), "age_s": round(age), "io_bytes_in_window": io,
                         "lifetime_cores": round(life, 2), "basis": p.basis}, cfg))
    return out


def build_sig(p):
    a = p.cmdline
    c = p.comm
    joined = " ".join(a)
    if c == "go" and len(a) > 1:
        verb = next((x for x in a[1:] if not x.startswith("-")), "")
        return ("go", verb) if verb in GO_VERBS else None
    if c == "cargo" and len(a) > 1:
        return ("cargo", next((x for x in a[1:] if not x.startswith(("-", "+"))), ""))
    if "GradleWrapperMain" in joined or c == "gradlew":
        return ("gradle", " ".join(x for x in a if not x.startswith("-") and "/" not in x and "java" not in x)[:60])
    if c in ("make", "ninja", "gmake"):
        return (c, "")
    if "UnrealBuildTool" in joined:
        return ("ubt", next((x for x in a if not x.startswith("-") and "/" not in x and "." not in x), ""))
    if "pytest" in joined and c.startswith(("python", "pytest")):
        return ("pytest", "")
    if c in ("npm", "pnpm", "yarn") and len(a) > 2 and a[1] == "run":
        return (c, a[2])
    return None


def duplicate_builds(procs, cores, cfg):
    groups = defaultdict(list)
    for p in procs.values():
        sig = build_sig(p)
        if not sig:
            continue
        parent = procs.get(p.ppid)
        if parent and build_sig(parent) and build_sig(parent)[0] == sig[0]:
            continue  # child of the same tool: part of one build
        groups[(sig, p.project)].append(p)
    out = []
    for ((tool, verb), project), ps in groups.items():
        if len(ps) < 2:
            continue
        same = len({(p.cwd, tuple(p.cmdline[1:])) for p in ps}) == 1
        label = f"{tool} {verb}".strip()
        out.append(_finding(
            "duplicate_build", "warn" if same else "info", project,
            (f"{len(ps)} identical concurrent `{label}` in the same cwd" if same
             else f"{len(ps)} concurrent `{label}` under {project} (cwds differ; not proven same target)"),
            ps, cores, {"tool": tool, "verb": verb, "same_target": same,
                        "members": [{"pid": p.pid, "cwd": p.cwd, "cmd": cmd_text(p)}
                                    for p in ps[:cfg["max_pids"]]]}, cfg))
    return out


def _causes(paging):
    if not paging:
        return {}
    iv = paging.get("interval") or paging["window"]
    cg = paging["cgroups"].get("interval") or paging["cgroups"]["window"]
    return {"over": "interval" if paging.get("interval") else "window",
            "faulting_projects": iv["projects"], "faulting_processes": iv["processes"],
            "thrashing_units": cg.get("by_refault", []), "growing_units": cg.get("by_growth", []),
            "throttled_units": cg.get("throttled", []),
            "swap_in_pages_s": paging["swap_in_pages_s"], "swap_out_pages_s": paging["swap_out_pages_s"]}


def swap_pressure(procs, cores, mem, mem_prev, wall_s, psi, cfg, paging=None):
    pages = 0
    if mem_prev and wall_s > 0:
        pages = ((mem.get("pswpin", 0) - mem_prev.get("pswpin", 0))
                 + (mem.get("pswpout", 0) - mem_prev.get("pswpout", 0))) / wall_s
    kswapd = [p for p in procs.values() if p.comm.startswith("kswapd")]
    k_cores = sum(cores.get(p.pid, 0.0) for p in kswapd)
    some10 = psi.get("memory_some_avg10", 0.0)
    if not (pages >= cfg["swap_pages_per_s"] or k_cores >= cfg["kswapd_cores"]
            or some10 >= cfg["mem_psi_some10"]):
        return []
    by_swap, by_rss = defaultdict(int), defaultdict(int)
    for p in procs.values():
        by_swap[p.project] += p.swap_bytes
        by_rss[p.project] += p.rss_bytes
    top = lambda d: [{"project": k, "bytes": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:5] if v]
    return [{"kind": "memory_pressure", "severity": "crit" if some10 >= 2 * cfg["mem_psi_some10"] else "warn",
             "project": (top(by_swap) or top(by_rss) or [{"project": "unknown"}])[0]["project"],
             "summary": f"memory pressure: swap {pages:.0f} pages/s, kswapd {k_cores:.2f} cores, psi some10 {some10:.1f}",
             "count": 0, "cpu_cores": round(k_cores, 3), "rss_bytes": 0, "pids": [],
             "evidence": {"swap_pages_per_s": round(pages, 1), "kswapd_cores": round(k_cores, 3),
                          "mem_psi_some10": some10, "top_swap_held": top(by_swap), "top_rss": top(by_rss),
                          "causes": _causes(paging),
                          "basis": "top_swap_held/top_rss say who HOLDS memory; causes say who is paging "
                                   "(major faults, cgroup refaults) and who is growing"}}]


def cpu_pressure(project_cores, psi, cfg):
    some60 = psi.get("cpu_some_avg60", 0.0)
    if some60 < cfg["cpu_psi_some60"]:
        return []
    top = sorted(project_cores.items(), key=lambda kv: -kv[1])[:5]
    return [{"kind": "cpu_pressure", "severity": "crit", "project": top[0][0] if top else "unknown",
             "summary": f"cpu psi some avg60 {some60:.0f}%; top: "
                        + ", ".join(f"{k} {v:.1f}c" for k, v in top),
             "count": 0, "cpu_cores": round(sum(v for _, v in top), 2), "rss_bytes": 0, "pids": [],
             "evidence": {"cpu_some_avg60": some60, "top": [{"project": k, "cores": round(v, 2)} for k, v in top]}}]


def detect(procs, cores, io_delta, uptime_s, mem, mem_prev, wall_s, psi, project_cores,
           cfg=None, exists=os.path.exists, probe=None, notes=None, paging=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    orph, orphan_pids = orphans(procs, cores, cfg, probe, notes)
    found = orph + stale_cwd(procs, cores, cfg, exists) + pure_loops(procs, cores, io_delta, uptime_s, cfg, orphan_pids)
    found += duplicate_builds(procs, cores, cfg) + swap_pressure(procs, cores, mem, mem_prev, wall_s, psi, cfg, paging)
    found += cpu_pressure(project_cores, psi, cfg)
    rank = {"crit": 0, "warn": 1, "info": 2}
    return sorted(found, key=lambda f: (rank[f["severity"]], -f["cpu_cores"]))
