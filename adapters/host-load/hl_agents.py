"""Map load onto agent sessions: which Claude / Codex / Orca agent is this process the child (or leak) of.

Resolution order for a process, strongest first:
  ancestor   nearest claude/codex ancestor in the live process tree, named by ITS worktree
  cwd        the process's own cwd (or a path in argv) under ~/data2/worktrees/<project>/<worktree>
  environ    ORCA_WORKTREE_ID / ORCA_PANE_KEY / CODEX_COMPANION_SESSION_ID of a FLAGGED process
             (an orphan's ancestry is gone but it inherited its parent's environment)
The key is ``<project>/<worktree>`` whenever a worktree is known, so a live agent and the orphans it leaked
share one name; interactive sessions outside a worktree are ``<kind>@<project>#<pid>``.

Secrets: /proc/<pid>/environ also carries ORCA_AGENT_HOOK_TOKEN and ORCA_AGENT_LAUNCH_TOKEN. Only the three
allow-listed names below are ever parsed out, only for agent roots and the pids of findings, and the raw
environ bytes never leave read_env().
"""
import os
import re
from collections import defaultdict

WT_RE = re.compile(r"^/home/eric/data2/worktrees/([^/\s]+)/([^/\s]+)")
PROJ_RE = re.compile(r"^/home/eric/projects/([^/\s]+)")
AGENT_COMMS = {"claude": "claude", "codex": "codex"}
ENV_ALLOW = ("ORCA_WORKTREE_ID", "ORCA_PANE_KEY", "CODEX_COMPANION_SESSION_ID")


def read_env(pid, proc_root="/proc"):
    """{allow-listed name: value} for pid; {} when unreadable. Nothing else is retained."""
    try:
        with open(os.path.join(proc_root, str(pid), "environ"), "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        name, _, value = item.partition(b"=")
        if name.decode("ascii", "ignore") in ENV_ALLOW:
            out[name.decode()] = value.decode("utf-8", "replace")[:80]
    return out


def worktree_of(path):
    m = WT_RE.match((path or "").replace(" (deleted)", ""))
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _worktree_for(p):
    wt = worktree_of(p.cwd)
    if wt:
        return wt, "cwd"
    for arg in p.cmdline:
        wt = worktree_of(arg) or worktree_of(arg.split("=", 1)[-1])
        if wt:
            return wt, "cmdline"
    return None, ""


def assign(procs, env_reader=read_env):
    """Set p.agent / p.agent_basis on every process; return {agent_key: info} for agents that own any process.

    env_reader is called for agent roots only (see enrich() for flagged pids).
    """
    roots = {p.pid: AGENT_COMMS[p.comm] for p in procs.values() if p.comm in AGENT_COMMS}
    info = {}

    def ensure(key, **kw):
        row = info.setdefault(key, {"agent": key, "kinds": set(), "roots": [], "worktree": None,
                                    "orca_worktree_id": None, "orca_pane": None, "session_id": None})
        for k, v in kw.items():
            if k == "kind" and v:
                row["kinds"].add(v)
            elif k == "root" and v:
                row["roots"].append(v)
            elif v and not row.get(k):
                row[k] = v
        return row

    root_key = {}
    for pid, kind in roots.items():
        p = procs[pid]
        wt, _ = _worktree_for(p)
        pm = PROJ_RE.match(p.cwd or "")
        key = wt or f"{kind}@{pm.group(1) if pm else (os.path.basename(p.cwd) or 'unknown')}#{pid}"
        root_key[pid] = key
        env = env_reader(pid)
        ensure(key, kind=kind, root=pid, worktree=wt, orca_worktree_id=env.get("ORCA_WORKTREE_ID"),
               orca_pane=env.get("ORCA_PANE_KEY"), session_id=env.get("CODEX_COMPANION_SESSION_ID"))

    for p in procs.values():
        p.agent, p.agent_basis = "", ""
        cur, hops = p, 0
        while cur and hops < 24:
            if cur.pid in root_key:
                p.agent, p.agent_basis = root_key[cur.pid], "ancestor"
                break
            cur = procs.get(cur.ppid) if cur.ppid else None
            hops += 1
        if not p.agent:
            wt, basis = _worktree_for(p)
            if wt:
                p.agent, p.agent_basis = wt, basis
        if p.agent:
            ensure(p.agent, worktree=p.agent if "/" in p.agent and "@" not in p.agent else None)

    return info


def enrich(procs, info, pids, env_reader=read_env):
    """Add environment identity for flagged pids: names an orphan whose cwd and ancestry gave nothing."""
    for pid in pids:
        p = procs.get(pid)
        if not p or p.agent_basis == "ancestor":
            continue
        env = env_reader(pid)
        if not env:
            continue
        if not p.agent and env.get("ORCA_WORKTREE_ID"):
            p.agent, p.agent_basis = "orca:" + env["ORCA_WORKTREE_ID"][:36], "environ"
        if p.agent:
            row = info.setdefault(p.agent, {"agent": p.agent, "kinds": set(), "roots": [], "worktree": None,
                                            "orca_worktree_id": None, "orca_pane": None, "session_id": None})
            for key, name in (("orca_worktree_id", "ORCA_WORKTREE_ID"), ("orca_pane", "ORCA_PANE_KEY"),
                              ("session_id", "CODEX_COMPANION_SESSION_ID")):
                row[key] = row.get(key) or env.get(name)
    return info


def summarize(procs, info, cores, cores_int=None, top=3):
    """Per-agent load rows, busiest first. cores/cores_int are {pid: cores}."""
    rows = defaultdict(lambda: {"cores": 0.0, "cores_int": None, "rss": 0, "nproc": 0, "orphans": 0,
                                "top": [], "basis": set(), "projects": set()})
    for p in procs.values():
        if not p.agent:
            continue
        r = rows[p.agent]
        r["cores"] += cores.get(p.pid, 0.0)
        if cores_int is not None:
            r["cores_int"] = (r["cores_int"] or 0.0) + cores_int.get(p.pid, 0.0)
        r["rss"] += p.rss_bytes
        r["nproc"] += 1
        parent = procs.get(p.ppid)
        if p.agent_basis != "ancestor" and (p.ppid == 1 or (parent is not None and parent.comm == "systemd")):
            r["orphans"] += 1
        r["top"].append((cores.get(p.pid, 0.0), p.pid, p.comm))
        r["basis"].add(p.agent_basis)
        r["projects"].add(p.project)
    out = []
    for key, r in rows.items():
        meta = info.get(key, {})
        out.append({"agent": key, "kinds": sorted(meta.get("kinds", ())), "roots": meta.get("roots", []),
                    "worktree": meta.get("worktree"), "orca_worktree_id": meta.get("orca_worktree_id"),
                    "orca_pane": meta.get("orca_pane"), "session_id": meta.get("session_id"),
                    "cores": round(r["cores"], 3),
                    "cores_int": round(r["cores_int"], 3) if r["cores_int"] is not None else None,
                    "rss": r["rss"], "nproc": r["nproc"], "orphans": r["orphans"],
                    "basis": sorted(b for b in r["basis"] if b), "projects": sorted(r["projects"])[:4],
                    "top": [{"pid": pid, "comm": comm, "cores": round(c, 3)}
                            for c, pid, comm in sorted(r["top"], reverse=True)[:top]]})
    return sorted(out, key=lambda a: (-(a["cores_int"] if a["cores_int"] is not None else a["cores"]), -a["rss"]))


def retally(finding, procs):
    """Re-derive finding["agents"] after enrich() named more processes. Counts are exact only when every
    sampled pid maps to one agent (then the finding's full count applies); otherwise they cover sampled pids."""
    tally = defaultdict(int)
    for pid in finding.get("pids", []):
        p = procs.get(pid)
        if p and p.agent:
            tally[p.agent] += 1
    if len(tally) == 1 and sum(tally.values()) == len(finding.get("pids", [])):
        tally = {next(iter(tally)): finding.get("count", sum(tally.values()))}
    return [{"agent": a, "count": n} for a, n in sorted(tally.items(), key=lambda kv: -kv[1])[:5]]
