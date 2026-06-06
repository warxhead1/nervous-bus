#!/usr/bin/env python3
"""cc_sysmap — live system-map panel: containers, runners, GPU, host vitals.

Grouped by project. Importable by cc-bus-dashboard for tab-2, or run standalone.

Data sources (background thread, polled independently):
  docker ps / docker stats  → container list + cpu/mem per container
  systemctl --user show     → GitHub Actions runner states
  /proc/stat, /proc/meminfo → host CPU / RAM   (or psutil when available)
  nvidia-smi                → GPU util/VRAM/power/temp + per-process VRAM
  debug.jsonl tail          → bus ev/min sparkline per project

Usage:
  python3 cc_sysmap.py               # standalone live view
  python3 cc_sysmap.py --tick 3      # slower refresh
  python3 cc_sysmap.py --once        # one frame, exit
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import time

try:
    import redis as _redis_lib
    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, IO, List, Optional, Tuple

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── constants ────────────────────────────────────────────────────────────────
DEFAULT_LOG    = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"
POLL_DOCKER_S  = 15.0   # docker stats blocks ~1s; poll less often
POLL_GPU_S     = 10.0   # nvidia-smi is expensive; 10s is plenty for display
POLL_RUNNER_S  = 8.0
POLL_VITALS_S  = 5.0    # /proc reads are cheap; 5s is fine for display
POLL_ZS_S      = 10.0   # Redis zs:* key + git commit refresh interval
POLL_WT_S      = 10.0   # Worktree agent fs scan interval
POLL_SVCS_S    = 8.0    # systemd user service CPU/mem poll interval
BUS_WINDOW_S   = 1800   # 30-minute bus activity window
BUS_EVPS_S     = 60     # rolling window for events/sec on Bus panel
SPARK_W        = 24     # sparkline width (buckets); fits 80-col minimum
SPARK_BUCKET_S = BUS_WINDOW_S / SPARK_W   # seconds per bucket (75s)
SPARK_CHARS    = "▁▂▃▄▅▆▇█"

# Worktree agents — host-side dispatched subagent dirs
WORKTREES_DIR    = Path.home() / "projects" / "nervous-bus" / ".claude" / "worktrees"
# Producer sources we want to surface on the Bus panel (path-style URI roots).
BUS_PRODUCER_SOURCES: List[str] = [
    "/autobench", "/tengine", "/hearth", "/hearth-bridge",
    "/deer-flow", "/home-automation", "/nervous-bus", "/claude-code/main",
]
# Autobench: notional 5-hour MiniMax request cap (notional — see MEMORY.md).
AUTOBENCH_REQ_CAP_5H = 14250


# ── project manifest ─────────────────────────────────────────────────────────
@dataclass
class SubGroup:
    label: str
    match: object  # callable(container_name) -> bool

@dataclass
class ProjectDef:
    name: str
    emoji: str
    color: str
    border_color: str
    container_prefixes: List[str] = field(default_factory=list)
    container_exact: List[str]    = field(default_factory=list)
    runner_names: List[str]       = field(default_factory=list)
    bus_prefixes: List[str]       = field(default_factory=list)
    sub_groups: List[SubGroup]    = field(default_factory=list)
    git_path: Optional[str]       = None
    service_names: List[str]      = field(default_factory=list)  # systemd --user services


PROJECTS: List[ProjectDef] = [
    ProjectDef(
        name="deer-flow", emoji="🦌", color="blue", border_color="#1f6feb",
        container_prefixes=["deer-flow-"],
        bus_prefixes=["deer-flow."],
        git_path="~/projects/deer-flow",
        sub_groups=[
            SubGroup("Infra",    lambda n: n in {
                "deer-flow-gateway", "deer-flow-nginx", "deer-flow-langgraph",
                "deer-flow-graph-worker", "deer-flow-otel-collector"}),
            SubGroup("Langfuse", lambda n: "langfuse" in n),
            SubGroup("Sandboxes",lambda n: "sandbox" in n),
        ],
    ),
    ProjectDef(
        name="hearth-loom", emoji="🪡", color="purple", border_color="#8957e5",
        container_prefixes=["hearth-loom-agent-"],
        runner_names=["hearth-loom", "hearth-loom-2", "hearth-loom-3"],
        bus_prefixes=["loom."],
        git_path="~/projects/hearth-loom",
        sub_groups=[
            SubGroup("Agents", lambda n: n.startswith("hearth-loom-agent-")),
        ],
    ),
    ProjectDef(
        name="tengine", emoji="⚙️", color="yellow", border_color="#d29922",
        container_prefixes=[],
        runner_names=["tengine", "tengine-2", "tengine-3"],
        bus_prefixes=["tengine."],
        git_path="~/projects/tengine",
        service_names=[
            # TEngine distributed refactor microservice cluster (supervisor manages the rest)
            "refactor-supervisor", "refactor-gateway-proxy", "refactor-analysis",
            "refactor-gpu", "refactor-build-orchestrator", "refactor-dashboard",
            "refactor-job-orchestrator", "refactor-brain", "refactor-mcp", "refactor-neural",
            # Voice/CCM (Claude Code Mate) sidecars belong to TEngine
            "ccm-voice", "openwakeword",
        ],
    ),
    ProjectDef(
        name="hearth", emoji="🏠", color="green", border_color="#2ea043",
        container_prefixes=[],
        runner_names=["hearth", "hearth-2", "hearth-3"],
        bus_prefixes=["hearth."],
        git_path="~/projects/hearth",
        service_names=[
            "hearth-api",           # main home-automation hub (Rust) — was the overload culprit
            "hearth-builder",       # Go RPC sidecar for loom builds
            "hearth-linter",        # Go RPC sidecar for loom linting
            "hearth-tui",           # TUI intelligence daemon
            "hearth-csi-collector", # WiFi CSI frame collector for ML
            "hearth-stremio-proxy", # Stremio Dolby Vision proxy (Node.js)
        ],
    ),
    ProjectDef(
        name="tachyonos", emoji="📡", color="bright_magenta", border_color="#da2f95",
        container_prefixes=["tachyonos-"],
        container_exact=["tachyonac-pg"],
        bus_prefixes=["tachyonos."],
        git_path="~/projects/tachyonos",
    ),
    ProjectDef(
        name="nervous-bus", emoji="🧬", color="cyan", border_color="#39d353",
        container_prefixes=[],
        runner_names=["nervous-bus"],
        bus_prefixes=["bus.bead.", "bus.saga.", "bus.dead_letter", "agent."],
        git_path="~/projects/nervous-bus",
        service_names=[
            # Bus substrate adapters (log → bundle → classify → route pipeline)
            "log-normalizer",        # Docker/journald logs → nbus:logs Redis stream
            "pattern-bundler",       # nbus:logs → 50-event bundles
            "pattern-consumer",      # bundles → LLM → bus.pattern.signal.v1
            "signal-router",         # routes signals by confidence
            "redis-mirror",          # nbus:* stream fault-tolerance mirror
            "nervous-hearth-bridge", # Redis device state → hearth.device.state.v1
        ],
    ),
    ProjectDef(
        name="shared", emoji="🌉", color="white", border_color="#30363d",
        container_exact=["kokoro-tts", "comet", "comet-caddy", "deploy-postgres-1"],
        bus_prefixes=["home-automation."],
        sub_groups=[
            SubGroup("Services", lambda n: n in {
                "kokoro-tts", "comet", "comet-caddy", "deploy-postgres-1"}),
            SubGroup("CI", lambda n: bool(re.search(r"_golang|_node|_python|_rust|bookworm", n))),
        ],
    ),
]

# Redis zs:* keys written by project adapters; read by _poll_zs every 10s
ZS_KEYS_BY_PROJECT: Dict[str, List[str]] = {
    "hearth-loom": ["zs:hl-kanban", "zs:hl-loomies", "zs:hl-dispatcher", "zs:hl-ccm-by-agent"],
    "tengine":     ["zs:te-fps", "zs:te-svc", "zs:te-mcp", "zs:te-ccm"],
    "shared":      ["zs:ha-devices", "zs:ha-status", "zs:ha-subsys-active"],
}
_ALL_ZS_KEYS: List[str] = [k for keys in ZS_KEYS_BY_PROJECT.values() for k in keys]

# GPU process → project heuristic map (process name fragment → project emoji)
GPU_PROC_PROJECT: Dict[str, str] = {
    "shadergen":    "⚙️",
    "silo_tester":  "⚙️",
    "kokoro":       "🌉",
    "deer-flow":    "🦌",
    "hearth":       "🏠",
}


# ── data structs ─────────────────────────────────────────────────────────────
@dataclass
class ContainerInfo:
    name: str
    image: str
    uptime: str         # e.g. "18h", "3m"
    cpu_pct: float = 0.0
    mem_mb: float  = 0.0
    running: bool  = True

@dataclass
class RunnerInfo:
    name: str
    project: str
    active: bool = True
    busy: bool   = False   # future: gh API / lock-file detection

@dataclass
class GpuProcess:
    pid: int
    cmdline: str
    mem_mb: int
    project_emoji: str = "·"
    container_name: str = ""

@dataclass
class GpuInfo:
    name: str = "?"
    util_pct: int   = 0
    mem_used_mb: int = 0
    mem_total_mb: int = 0
    temp_c: int  = 0
    power_w: float  = 0.0
    power_limit_w: float = 0.0
    processes: List[GpuProcess] = field(default_factory=list)

@dataclass
class HostVitals:
    cpu_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_pct: float = 0.0
    net_rx_mbps: float = 0.0
    net_tx_mbps: float = 0.0


@dataclass
class WorktreeAgent:
    """A host-side dispatched subagent worktree under .claude/worktrees/agent-*/."""
    agent_id: str            # short id (last 12 chars after "agent-")
    branch:   str            # e.g. "worktree-agent-aa286ca..."
    status:   str            # "running" | "merged" | "abandoned"
    age_s:    float          # seconds since worktree mtime
    dirty:    bool = False   # has uncommitted changes


@dataclass
class AutobenchStatus:
    """Latest-seen autobench snapshot, derived from bus events."""
    session_id:        str = ""
    iter:              Optional[int] = None
    last_ahe_outcome:  str = "pending"   # hit | miss | refuted_live | pending
    last_ahe_age_s:    Optional[float] = None
    queue_pressure:    bool = False
    queue_dev_factor:  float = 0.0
    requests_5h:       int = 0
    last_event_age_s:  Optional[float] = None


@dataclass
class ServiceInfo:
    """Systemd --user service resource snapshot."""
    name: str           # bare name without .service suffix
    project: str        # ProjectDef.name that owns it
    state: str          # "active" | "inactive" | "failed" | "activating" | "unknown"
    cpu_pct: float = 0.0
    mem_mb:  float = 0.0
    tasks:   int   = 0
    pid:     int   = 0
    mem_high_mb: float = 0.0   # systemd MemoryHigh limit (0 = not set)
    mem_max_mb:  float = 0.0   # systemd MemoryMax limit (0 = not set)


@dataclass
class DeadLetterRecord:
    ts: float
    original_type: str
    failure_reason: str
    detail: str

@dataclass
class BusStatus:
    """Aggregate bus-channel snapshot for the BusPanel."""
    # channel-name → rolling deque of timestamps (last BUS_EVPS_S seconds)
    chan_hits: Dict[str, deque] = field(default_factory=dict)
    # channel-name → (pass, total) schema-validation counters (lifetime-of-process)
    chan_schema: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    # producer-source URI → last-seen wall-clock ts
    source_last_seen: Dict[str, float] = field(default_factory=dict)
    # recent dead_letter records (last 30)
    dead_letters: deque = field(default_factory=lambda: deque(maxlen=30))


# ── shared state ─────────────────────────────────────────────────────────────
class SysmapState:
    def __init__(self, log_path: Path = DEFAULT_LOG) -> None:
        self.lock          = threading.Lock()
        self.log_path      = log_path
        # loomie_projects: pane_id → project name (from loom.lifecycle bus events)
        self.loomie_projects: Dict[str, str] = {}
        self.containers:   List[ContainerInfo] = []
        self.runners:      List[RunnerInfo]    = []
        self.vitals        = HostVitals()
        self.gpu           = GpuInfo()
        self.started_at    = time.time()
        # Bus activity: project_name → deque of (timestamp,) for last BUS_WINDOW_S
        self.bus_hits: Dict[str, deque] = {p.name: deque() for p in PROJECTS}
        self._log_inode: Optional[int] = None
        self._log_pos:   int = 0
        self._log_fh: Optional[IO[str]] = None  # persistent handle; only used by poller thread
        self._prev_cpu_stat: Optional[Tuple[int, int]] = None  # (total, idle)
        self.services: List[ServiceInfo] = []
        # name → (CPUUsageNSec, sample_wall_time) for CPU% delta calculation
        self._prev_svc_cpu: Dict[str, Tuple[int, float]] = {}
        self.version: int = 0
        self.zs: Dict[str, str] = {}
        self.git_commits: Dict[str, Tuple[str, float]] = {}
        # New panels (kciq/opvt/6k8a) — populated by _tail_bus + _poll_worktrees
        self.bus_status   = BusStatus()
        self.autobench    = AutobenchStatus()
        # worker.v1 timestamps over the last 5h (rolling) for requests_5h
        self._autobench_worker_ts: deque = deque()
        self.worktree_agents: List[WorktreeAgent] = []
        self._redis = None
        if _HAS_REDIS:
            try:
                r = _redis_lib.Redis(
                    host="localhost", port=6379,
                    socket_connect_timeout=1, socket_timeout=1,
                )
                r.ping()
                self._redis = r
                raw = r.get("sysmap:docker:state")
                if raw:
                    self.containers = _deserialize_containers(json.loads(raw))
                raw = r.get("sysmap:gpu:state")
                if raw:
                    self.gpu = _deserialize_gpu(json.loads(raw))
            except Exception:
                self._redis = None


# ── helpers ───────────────────────────────────────────────────────────────────
def _run(*args, **kw) -> str:
    """Run subprocess, return stdout or '' on error."""
    try:
        r = subprocess.run(list(args), capture_output=True, text=True,
                           timeout=kw.get("timeout", 8))
        return r.stdout.strip()
    except Exception:
        return ""


def _parse_mem(s: str) -> float:
    """'12.5MiB' → MB (float). '0B' → 0."""
    s = s.strip()
    for suf, mult in [("GiB", 1024), ("MiB", 1), ("kB", 0.001), ("B", 1e-6)]:
        if s.endswith(suf):
            try:
                return float(s[: -len(suf)]) * mult
            except ValueError:
                return 0.0
    return 0.0


def _uptime_str(started_str: str) -> str:
    """Docker 'Up 3 hours' → '3h'. 'Up 5 minutes' → '5m'. etc."""
    m = re.search(r"Up\s+(.+)", started_str or "")
    if not m:
        return "?"
    s = m.group(1)
    for pat, fmt in [
        (r"(\d+)\s+day", lambda x: f"{x}d"),
        (r"(\d+)\s+hour", lambda x: f"{x}h"),
        (r"(\d+)\s+minute", lambda x: f"{x}m"),
        (r"(\d+)\s+second", lambda x: f"{x}s"),
        (r"About a minute", lambda x: "~1m"),
        (r"Less than a second", lambda x: "<1s"),
    ]:
        mx = re.search(pat, s)
        if mx:
            try:
                return fmt(mx.group(1))
            except IndexError:
                return fmt(None)
    return s[:6]


def _classify_container(name: str) -> Optional[ProjectDef]:
    for p in PROJECTS:
        if any(name.startswith(pfx) for pfx in p.container_prefixes):
            return p
        if name in p.container_exact:
            return p
    return None


def _classify_channel(channel: str) -> Optional[str]:
    for p in PROJECTS:
        if any(channel.startswith(pfx) for pfx in p.bus_prefixes):
            return p.name
    return None


def _gpu_proc_project(cmdline: str, cgroup: str) -> str:
    """Best-effort project emoji from cmdline + cgroup."""
    cl = cmdline.lower()
    for kw, emoji in GPU_PROC_PROJECT.items():
        if kw in cl:
            return emoji
    if "docker" in cgroup:
        return "🐳"
    return "·"


def _cgroup_container(pid: int) -> str:
    """Try to resolve docker container name from /proc/<pid>/cgroup."""
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text()
        m = re.search(r"docker-([a-f0-9]{12,})", cg)
        if m:
            cid = m.group(1)[:12]
            out = _run("docker", "inspect", "--format", "{{.Name}}", cid, timeout=2)
            return out.lstrip("/")
    except Exception:
        pass
    return ""


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_text().replace("\x00", " ").strip()[:80]
    except Exception:
        return ""


def _deserialize_containers(data: list) -> List[ContainerInfo]:
    out = []
    for d in data:
        try:
            out.append(ContainerInfo(**d))
        except Exception:
            pass
    return out


def _deserialize_gpu(data: dict) -> GpuInfo:
    try:
        data = dict(data)
        procs_raw = data.pop("processes", [])
        gpu = GpuInfo(**data)
        gpu.processes = [GpuProcess(**p) for p in procs_raw]
        return gpu
    except Exception:
        return GpuInfo()


# ── polling functions (called from background thread) ────────────────────────
def _poll_docker(state: SysmapState) -> None:
    if not shutil.which("docker"):
        return

    # docker ps for status/uptime
    ps_out = _run("docker", "ps", "-a",
                  "--format", '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}"}',
                  timeout=6)
    ps_map: Dict[str, dict] = {}
    for line in ps_out.splitlines():
        try:
            d = json.loads(line)
            ps_map[d["name"]] = d
        except json.JSONDecodeError:
            pass

    # docker stats for cpu/mem (blocks ~1s)
    stats_out = _run("docker", "stats", "--no-stream",
                     "--format", '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}',
                     timeout=12)
    stats_map: Dict[str, dict] = {}
    for line in stats_out.splitlines():
        try:
            d = json.loads(line)
            stats_map[d["name"]] = d
        except json.JSONDecodeError:
            pass

    containers: List[ContainerInfo] = []
    for name, info in ps_map.items():
        running = info["status"].startswith("Up")
        uptime  = _uptime_str(info["status"]) if running else "down"
        s = stats_map.get(name, {})
        try:
            cpu = float(s.get("cpu", "0%").rstrip("%"))
        except ValueError:
            cpu = 0.0
        mem = 0.0
        mem_str = s.get("mem", "0B / 0B")
        if " / " in mem_str:
            mem = _parse_mem(mem_str.split(" / ")[0])
        containers.append(ContainerInfo(
            name=name, image=info.get("image", "?"),
            uptime=uptime, cpu_pct=cpu, mem_mb=mem, running=running,
        ))

    with state.lock:
        state.containers = containers

    if state._redis is not None:
        try:
            state._redis.set(
                "sysmap:docker:state",
                json.dumps([dataclasses.asdict(c) for c in containers]),
                ex=30,
            )
        except Exception:
            pass


def _poll_runners(state: SysmapState) -> None:
    out = _run("systemctl", "--user", "list-units", "actions-runner-*",
               "--no-pager", "--output=json", timeout=5)
    runners: List[RunnerInfo] = []
    if out:
        try:
            units = json.loads(out)
        except json.JSONDecodeError:
            units = []
        for u in units:
            unit_name = u.get("unit", "")  # e.g. actions-runner-tengine-2.service
            active = u.get("active") == "active"
            # Extract runner name from service unit
            m = re.match(r"actions-runner-(.+)\.service$", unit_name)
            if not m:
                continue
            runner_name = m.group(1)  # e.g. "tengine-2"
            # Map runner name → project
            project = "?"
            for p in PROJECTS:
                if runner_name in p.runner_names:
                    project = p.name
                    break
            runners.append(RunnerInfo(name=runner_name, project=project, active=active))
    with state.lock:
        state.runners = runners


def _poll_gpu(state: SysmapState) -> None:
    if not shutil.which("nvidia-smi"):
        return

    def _fetch_stats() -> str:
        return _run(
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
            "temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
            timeout=4,
        )

    def _fetch_procs() -> str:
        return _run(
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader",
            timeout=4,
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        stats_fut = ex.submit(_fetch_stats)
        procs_fut = ex.submit(_fetch_procs)
        out      = stats_fut.result()
        proc_out = procs_fut.result()

    gpu = GpuInfo()
    if out:
        parts = [x.strip() for x in out.split(",")]
        if len(parts) >= 7:
            try:
                gpu.name          = parts[0]
                gpu.util_pct      = int(parts[1])
                gpu.mem_used_mb   = int(parts[2])
                gpu.mem_total_mb  = int(parts[3])
                gpu.temp_c        = int(parts[4])
                gpu.power_w       = float(parts[5])
                gpu.power_limit_w = float(parts[6])
            except (ValueError, IndexError):
                pass

    procs: List[GpuProcess] = []
    for line in proc_out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid     = int(parts[0])
            name    = parts[1]
            mem_str = parts[2]
            mem_mb  = int(re.search(r"\d+", mem_str).group())  # type: ignore[union-attr]
        except (ValueError, AttributeError, IndexError):
            continue
        cmdline    = _proc_cmdline(pid)
        container  = _cgroup_container(pid)
        proj_emoji = _gpu_proc_project(f"{name} {cmdline}", container)
        if container:
            proj_emoji = (
                _classify_container(container) or ProjectDef("?", "·", "dim", "dim")
            ).emoji
        procs.append(GpuProcess(
            pid=pid, cmdline=cmdline, mem_mb=mem_mb,
            project_emoji=proj_emoji, container_name=container or name[:20],
        ))

    gpu.processes = procs
    with state.lock:
        state.gpu = gpu

    if state._redis is not None:
        try:
            state._redis.set(
                "sysmap:gpu:state",
                json.dumps(dataclasses.asdict(gpu)),
                ex=15,
            )
        except Exception:
            pass


def _poll_vitals(state: SysmapState) -> None:
    # ── CPU via /proc/stat ────────────────────────────────────────────
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
        vals = list(map(int, line.split()[1:8]))
        total = sum(vals)
        idle  = vals[3] + vals[4]   # idle + iowait
        with state.lock:
            prev = state._prev_cpu_stat
            state._prev_cpu_stat = (total, idle)
        if prev:
            dt = total - prev[0]
            di = idle  - prev[1]
            cpu_pct = 100.0 * (1 - di / max(1, dt))
        else:
            cpu_pct = 0.0
    except Exception:
        cpu_pct = 0.0

    # ── RAM via /proc/meminfo ────────────────────────────────────────
    ram_used = ram_total = 0.0
    try:
        info: Dict[str, int] = {}
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            try:
                info[k.strip()] = int(v.split()[0])
            except (ValueError, IndexError):
                pass
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        used_kb  = total_kb - avail_kb
        ram_total = total_kb / 1_048_576   # GB
        ram_used  = used_kb  / 1_048_576
    except Exception:
        pass

    # ── network I/O via /proc/net/dev ────────────────────────────────
    net_rx_mbps = net_tx_mbps = 0.0
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()[2:]  # skip headers
        rx_bytes = tx_bytes = 0
        for ln in lines:
            iface, rest = ln.split(":", 1)
            if iface.strip() in ("lo",):
                continue
            cols = rest.split()
            rx_bytes += int(cols[0])
            tx_bytes += int(cols[8])
        with state.lock:
            prev_net = getattr(state, '_prev_net_stat', None)
            state._prev_net_stat = (rx_bytes, tx_bytes, time.time())
        if prev_net:
            dt = time.time() - prev_net[2]
            if dt > 0:
                net_rx_mbps = (rx_bytes - prev_net[0]) / dt / 1_048_576
                net_tx_mbps = (tx_bytes - prev_net[1]) / dt / 1_048_576
    except Exception:
        pass

    with state.lock:
        state.vitals = HostVitals(
            cpu_pct     = cpu_pct,
            ram_used_gb = ram_used,
            ram_total_gb= ram_total,
            ram_pct     = 100 * ram_used / max(0.001, ram_total),
            net_rx_mbps = net_rx_mbps,
            net_tx_mbps = net_tx_mbps,
        )


def _tail_bus(state: SysmapState) -> None:
    """Append new bus events to per-project hit deques."""
    # _log_fh is private to the poller thread — never read by the render thread.
    # The lock only guards inode/pos state; file I/O happens outside the lock.
    log = state.log_path
    if not log.exists():
        return
    try:
        stat  = log.stat()
        inode = stat.st_ino
        size  = stat.st_size
    except OSError:
        return

    with state.lock:
        if state._log_inode != inode:
            # rotation or first open — close stale handle, reset position
            if state._log_fh is not None:
                try:
                    state._log_fh.close()
                except Exception:
                    pass
                state._log_fh = None
            prev_inode = state._log_inode
            state._log_inode = inode
            # Skip to end on both first-open AND rotation. Reading history would
            # timestamp all events as 'now', flooding the rolling rate window.
            state._log_pos = size
            return
        pos = state._log_pos

    if size <= pos:
        return

    # Open handle once; reuse on subsequent calls
    if state._log_fh is None:
        try:
            state._log_fh = log.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return

    try:
        state._log_fh.seek(pos)
        new_lines = state._log_fh.readlines()
        new_pos   = state._log_fh.tell()
    except OSError:
        try:
            state._log_fh.close()
        except Exception:
            pass
        state._log_fh = None
        return

    now = time.time()
    hits: Dict[str, List[float]] = {p.name: [] for p in PROJECTS}
    # Per-channel + source bookkeeping for BusPanel (kciq).
    chan_hits_new: Dict[str, List[float]] = {}
    source_seen_new: Dict[str, float] = {}
    # Autobench (opvt) accumulators — only the *latest* event wins per field.
    ab_new: Dict[str, object] = {}
    ab_worker_ts: List[float] = []

    parsed_events: List[dict] = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_events.append(ev)
        channel = ev.get("type", "")
        project = _classify_channel(channel)
        if project:
            hits[project].append(now)
        if channel:
            chan_hits_new.setdefault(channel, []).append(now)
        src = ev.get("source", "")
        if src:
            # Normalise: only record the first path segment(s) we care about.
            # /agent-XYZ → "/agents" bucket so we don't blow up source_last_seen
            # with hundreds of ephemeral worktree sources.
            if src.startswith("/agent-"):
                src_key = "/agents"
            else:
                src_key = src
            source_seen_new[src_key] = now

        # Autobench parsing — keyed off source=/autobench
        if src == "/autobench":
            data = ev.get("data") or {}
            sid = data.get("session_id") or ""
            if sid:
                ab_new["session_id"] = sid
            # iter — only from iteration-level events
            if channel in ("autobench.iteration.v1",
                           "autobench.iteration.summary.v1"):
                if "iter" in data:
                    ab_new["iter"] = data.get("iter")
                if data.get("requests_used") is not None:
                    ab_new["requests_5h"] = int(data.get("requests_used") or 0)
            if channel == "autobench.improver.prediction.refuted_live.v1":
                ab_new["last_ahe_outcome"] = "refuted_live"
                ab_new["last_ahe_age_s"]   = 0.0
            elif channel == "autobench.improver.prediction.v1":
                # Optional verdict payload — if data.outcome present use it.
                outcome = (data.get("outcome") or "").lower()
                if outcome in ("hit", "miss", "pending"):
                    ab_new["last_ahe_outcome"] = outcome
                    ab_new["last_ahe_age_s"]   = 0.0
            if channel == "autobench.worker.queue_pressure.v1":
                df = float(data.get("deviation_factor") or 0.0)
                ab_new["queue_dev_factor"] = df
                ab_new["queue_pressure"]   = df > 0.25
            if channel == "autobench.worker.v1":
                ab_worker_ts.append(now)
            ab_new["last_event_age_s"] = 0.0

    cutoff = now - BUS_WINDOW_S
    cutoff_evps = now - BUS_EVPS_S
    cutoff_5h   = now - 5 * 3600
    loomie_proj_updates: Dict[str, str] = {}
    for ev in parsed_events:
        if ev.get("type") != "loom.lifecycle.v1":
            continue
        data = ev.get("data", {})
        pane = data.get("pane_id", "")
        proj = data.get("project", "")
        if pane and proj and proj not in ("unknown", ""):
            cname = pane.replace("docker:", "")
            loomie_proj_updates[cname] = proj

    with state.lock:
        state._log_pos = new_pos
        state.loomie_projects.update(loomie_proj_updates)
        for proj, ts_list in hits.items():
            dq = state.bus_hits[proj]
            dq.extend(ts_list)
            while dq and dq[0] < cutoff:
                dq.popleft()
        # BusStatus: per-channel rolling deques, source last-seen, dead_letters.
        bs = state.bus_status
        for chan, ts_list in chan_hits_new.items():
            dq = bs.chan_hits.setdefault(chan, deque())
            dq.extend(ts_list)
            while dq and dq[0] < cutoff:   # changed from cutoff_evps to cutoff
                dq.popleft()
        for src, ts in source_seen_new.items():
            bs.source_last_seen[src] = ts
        for ev in parsed_events:
            if ev.get("type") != "bus.dead_letter":
                continue
            data = ev.get("data") or {}
            bs.dead_letters.append(DeadLetterRecord(
                ts=now,
                original_type=data.get("original_type", "?"),
                failure_reason=data.get("failure_reason", "unknown"),
                detail=data.get("schema_violation_detail", ""),
            ))
        # AutobenchStatus: apply latest-wins updates.
        ab = state.autobench
        for k, v in ab_new.items():
            setattr(ab, k, v)
        if ab_worker_ts:
            state._autobench_worker_ts.extend(ab_worker_ts)
        while state._autobench_worker_ts and state._autobench_worker_ts[0] < cutoff_5h:
            state._autobench_worker_ts.popleft()
        ab.requests_5h = max(ab.requests_5h, len(state._autobench_worker_ts))


def _poll_zs(state: SysmapState) -> None:
    """Refresh Redis zs:* project status keys and stale git commits."""
    if state._redis is not None:
        try:
            values = state._redis.mget(_ALL_ZS_KEYS)
            zs_new: Dict[str, str] = {}
            for key, val in zip(_ALL_ZS_KEYS, values):
                if val is not None:
                    zs_new[key] = val.decode() if isinstance(val, bytes) else str(val)
            with state.lock:
                state.zs = zs_new
        except Exception:
            pass

    now = time.time()
    for proj in PROJECTS:
        if not proj.git_path:
            continue
        with state.lock:
            cached = state.git_commits.get(proj.name)
        if cached and (now - cached[1]) < 120:
            continue
        git_dir = Path(proj.git_path).expanduser()
        if not git_dir.exists():
            continue
        out = _run("git", "-C", str(git_dir), "log", "-1",
                   "--format=%ar · %s", timeout=3)
        with state.lock:
            state.git_commits[proj.name] = (out, now)


def _poll_worktrees(state: SysmapState,
                    worktrees_dir: Path = WORKTREES_DIR) -> None:
    """Scan .claude/worktrees/agent-*/ — derive status, age, dirty flag."""
    if not worktrees_dir.exists():
        with state.lock:
            state.worktree_agents = []
        return

    # Pre-compute merged-branches set once (cheap; bounded to ~30 entries).
    merged = set()
    out = _run("git", "-C", str(worktrees_dir.parent.parent),
               "branch", "-a", "--merged", "main", "--format=%(refname:short)",
               timeout=4)
    for line in out.splitlines():
        merged.add(line.strip())

    agents: List[WorktreeAgent] = []
    try:
        entries = sorted(worktrees_dir.glob("agent-*"))
    except OSError:
        entries = []
    now = time.time()
    for entry in entries:
        if not entry.is_dir():
            continue
        agent_id = entry.name[len("agent-"):][:12]
        branch = f"worktree-agent-{entry.name[len('agent-'):]}"
        # Probe branch — many possible branch-name conventions; check the dir.
        gitstat = _run("git", "-C", str(entry),
                       "status", "--short", "--porcelain", timeout=3)
        dirty = bool(gitstat.strip())
        branch_actual = _run("git", "-C", str(entry),
                             "rev-parse", "--abbrev-ref", "HEAD", timeout=3)
        if branch_actual:
            branch = branch_actual
        if branch in merged:
            status = "merged"
        else:
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                mtime = now
            age = now - mtime
            # Heuristic: idle > 6h without recent activity → "abandoned"
            if age > 6 * 3600 and not dirty:
                status = "abandoned"
            else:
                status = "running"
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = now
        agents.append(WorktreeAgent(
            agent_id=agent_id, branch=branch, status=status,
            age_s=now - mtime, dirty=dirty,
        ))

    with state.lock:
        state.worktree_agents = agents


def _utcnow() -> str:
    """Return current UTC time as RFC3339 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_mem_limit(raw: str) -> float:
    """Parse a systemd MemoryHigh/MemoryMax value (bytes or 'infinity') → MB."""
    if not raw or raw in ("infinity", "[not set]", "18446744073709551615"):
        return 0.0
    try:
        return int(raw) / 1_048_576
    except (ValueError, TypeError):
        return 0.0


def _poll_systemd_services(state: SysmapState) -> None:
    """Poll systemd --user services for CPU/mem/state across all tracked projects."""
    # Build the full service name → project mapping
    svc_to_proj: Dict[str, str] = {}
    for proj in PROJECTS:
        for svc in proj.service_names:
            svc_to_proj[svc] = proj.name

    if not svc_to_proj:
        return

    now = time.time()
    n_cpus = max(1, len(os.sched_getaffinity(0)))
    results: List[ServiceInfo] = []

    # Batch all services in one systemctl call for speed
    svc_units = [f"{n}.service" for n in svc_to_proj]
    raw = _run(
        "systemctl", "--user", "show",
        "--property=Id,ActiveState,MainPID,MemoryCurrent,TasksCurrent,CPUUsageNSec,MemoryHigh,MemoryMax",
        *svc_units,
        timeout=6,
    )

    # systemctl show with multiple units separates them with blank lines
    blocks = raw.split("\n\n") if raw else []
    for block in blocks:
        if not block.strip():
            continue
        data: Dict[str, str] = {}
        for line in block.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()

        svc_full = data.get("Id", "")
        svc_name = svc_full.removesuffix(".service")
        if svc_name not in svc_to_proj:
            continue

        state_str = data.get("ActiveState", "unknown")
        # MainPID / TasksCurrent are "[not set]" for services with the
        # corresponding accounting disabled — parse defensively or the
        # poller thread dies and the whole dashboard freezes at zero.
        try:
            pid = int(data.get("MainPID", "0") or "0")
        except (ValueError, TypeError):
            pid = 0
        try:
            tasks = int(data.get("TasksCurrent", "0") or "0")
        except (ValueError, TypeError):
            tasks = 0

        # MemoryCurrent: bytes, or "[not set]" / empty when service is inactive
        mem_raw = data.get("MemoryCurrent", "0")
        try:
            mem_mb = int(mem_raw) / 1_048_576
        except (ValueError, TypeError):
            mem_mb = 0.0

        mem_high_mb = _parse_mem_limit(data.get("MemoryHigh", ""))
        mem_max_mb  = _parse_mem_limit(data.get("MemoryMax", ""))

        # CPU%: delta of cumulative CPUUsageNSec over wall-clock interval
        cpu_ns_raw = data.get("CPUUsageNSec", "0")
        try:
            cpu_ns = int(cpu_ns_raw)
        except (ValueError, TypeError):
            cpu_ns = 0

        cpu_pct = 0.0
        with state.lock:
            prev = state._prev_svc_cpu.get(svc_name)
            state._prev_svc_cpu[svc_name] = (cpu_ns, now)
        if prev:
            ns_delta = cpu_ns - prev[0]
            t_delta  = now - prev[1]
            if t_delta > 0 and ns_delta >= 0:
                cpu_pct = 100.0 * (ns_delta / 1e9) / t_delta / n_cpus

        results.append(ServiceInfo(
            name=svc_name,
            project=svc_to_proj[svc_name],
            state=state_str,
            cpu_pct=cpu_pct,
            mem_mb=mem_mb,
            tasks=tasks,
            pid=pid,
            mem_high_mb=mem_high_mb,
            mem_max_mb=mem_max_mb,
        ))

    with state.lock:
        state.services = results


def _emit_system_heartbeat(state: SysmapState) -> None:
    """Publish bus.system.heartbeat.v1 so the mobile hub can detect DEGRADED."""
    try:
        with state.lock:
            svcs = list(state.services)
            v    = state.vitals
            gpu  = state.gpu

        failed = [s.name for s in svcs if s.state == "failed"]
        pressure = any(
            s.mem_high_mb > 0 and s.mem_mb / s.mem_high_mb > 0.85
            for s in svcs if s.state == "active"
        )
        payload = {
            "ts":               _utcnow(),
            "host":             subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip(),
            "services_up":      sum(1 for s in svcs if s.state == "active"),
            "services_total":   len(svcs),
            "services_failed":  failed,
            "memory_pressure":  pressure,
            "gpu_util_pct":     gpu.util_pct,
            "cpu_pct":          v.cpu_pct,
        }
        subprocess.run(
            ["nervous", "publish", "bus.system.heartbeat.v1", json.dumps(payload)],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


# ── background poller ────────────────────────────────────────────────────────
class _Poller(threading.Thread):
    def __init__(self, state: SysmapState) -> None:
        super().__init__(daemon=True, name="sysmap-poller")
        self.state = state
        self._next_docker    = 0.0
        self._next_gpu       = 0.0
        self._next_runner    = 0.0
        self._next_vitals    = 0.0
        self._next_zs        = 0.0
        self._next_wt        = 0.0
        self._next_svcs      = 0.0
        self._next_heartbeat = 0.0

    def run(self) -> None:
        while True:
            now = time.time()
            s = self.state
            if now >= self._next_docker:
                _poll_docker(s)
                self._next_docker = time.time() + POLL_DOCKER_S
            if now >= self._next_gpu:
                _poll_gpu(s)
                self._next_gpu = time.time() + POLL_GPU_S
            if now >= self._next_runner:
                _poll_runners(s)
                self._next_runner = time.time() + POLL_RUNNER_S
            if now >= self._next_vitals:
                _poll_vitals(s)
                self._next_vitals = time.time() + POLL_VITALS_S
            if now >= self._next_zs:
                _poll_zs(s)
                self._next_zs = time.time() + POLL_ZS_S
            if now >= self._next_wt:
                _poll_worktrees(s)
                self._next_wt = time.time() + POLL_WT_S
            if now >= self._next_svcs:
                _poll_systemd_services(s)
                self._next_svcs = time.time() + POLL_SVCS_S
            if now >= self._next_heartbeat:
                _emit_system_heartbeat(s)
                self._next_heartbeat = time.time() + 30.0
            _tail_bus(s)
            with s.lock:
                s.version += 1
            next_due = min(self._next_docker, self._next_gpu, self._next_runner,
                           self._next_vitals, self._next_zs, self._next_wt,
                           self._next_svcs, self._next_heartbeat)
            time.sleep(max(0.5, min(next_due - time.time(), 5.0)))


# ── rendering ────────────────────────────────────────────────────────────────
def _bar(pct: float, width: int = 10, color_fn=None) -> Text:
    filled = int(width * min(100, pct) / 100)
    out = Text()
    style = (color_fn(pct) if color_fn else
             ("bright_red" if pct > 85 else ("yellow" if pct > 60 else "green")))
    out.append("█" * filled, style=style)
    out.append("░" * (width - filled), style="dim")
    return out


def _sparkline(hits: deque, width: int = SPARK_W) -> Text:
    now = time.time()
    buckets = [0] * width
    for ts in hits:
        age = now - ts
        if age >= BUS_WINDOW_S:
            continue
        idx = int((BUS_WINDOW_S - age) / SPARK_BUCKET_S)
        idx = min(width - 1, max(0, idx))
        buckets[idx] += 1
    if not any(buckets):
        return Text("─" * width, style="dim")
    peak = max(buckets)
    out = Text()
    for v in buckets:
        if v == 0:
            out.append("▁", style="dim")
        else:
            lvl = min(7, max(0, int((v / peak) * 7)))
            style = "green" if lvl < 3 else ("yellow" if lvl < 6 else "bright_red")
            out.append(SPARK_CHARS[lvl], style=style)
    return out


def _cpu_color(pct: float) -> str:
    return "bright_red" if pct > 85 else ("yellow" if pct > 60 else "green")


def _mem_color(pct: float) -> str:
    return "bright_red" if pct > 90 else ("yellow" if pct > 70 else "blue")


def panel_vitals(state: SysmapState) -> Panel:
    with state.lock:
        v   = state.vitals
        gpu = state.gpu
        ctrs = state.containers
        runs = state.runners

    n_up   = sum(1 for c in ctrs if c.running)
    n_runs = sum(1 for r in runs if r.active)

    t = Table.grid(padding=(0, 2), expand=True)
    t.add_column(ratio=1)
    t.add_column(ratio=1)
    t.add_column(ratio=1)
    t.add_column(ratio=1)
    t.add_column(ratio=1)
    t.add_column(ratio=1)  # net I/O

    def metric(label: str, value: str, pct: float, color: str) -> Text:
        row = Text()
        row.append(f"{label}  ", style="dim")
        row.append(value, style=f"bold {color}")
        return row

    def pct_bar(pct: float, color: str, width: int = 8) -> Text:
        filled = int(width * pct / 100)
        t2 = Text()
        t2.append("█" * filled, style=color)
        t2.append("░" * (width - filled), style="dim")
        t2.append(f"  {pct:5.1f}%", style=color)
        return t2

    cpu_c   = _cpu_color(v.cpu_pct)
    ram_c   = _mem_color(v.ram_pct)
    vram_pct = 100 * gpu.mem_used_mb / max(1, gpu.mem_total_mb)
    vram_c  = _mem_color(vram_pct)
    pow_pct = 100 * gpu.power_w / max(1, gpu.power_limit_w)
    pow_c   = _cpu_color(pow_pct)
    gpu_c   = _cpu_color(gpu.util_pct)

    short_gpu = re.sub(r"NVIDIA GeForce |GeForce |RTX |GTX ", "", gpu.name) if gpu.name else "GPU"

    net_tx = v.net_tx_mbps
    net_rx = v.net_rx_mbps
    # Show KB/s when < 1 MB/s for readability
    if net_tx < 1.0 and net_rx < 1.0:
        net_label_val = f"↑{net_tx * 1024:.0f}  ↓{net_rx * 1024:.0f} KB/s"
    else:
        net_label_val = f"↑{net_tx:.1f}  ↓{net_rx:.1f} MB/s"

    row0 = [
        Text(f"CPU", style="dim"),
        Text(f"RAM  {v.ram_total_gb:.0f} GB", style="dim"),
        Text(f"GPU util  {short_gpu}", style="dim"),
        Text(f"VRAM  {gpu.mem_total_mb // 1024} GB  ·  {gpu.temp_c}°C", style="dim"),
        Text(f"GPU power  ·  {n_up} ctr  {n_runs} run", style="dim"),
        Text("net ↑↓", style="dim"),
    ]
    row1 = [
        pct_bar(v.cpu_pct, cpu_c),
        Text.assemble(
            Text(f"{v.ram_used_gb:.1f} / {v.ram_total_gb:.0f} GB", style=f"bold {ram_c}"),
        ),
        pct_bar(gpu.util_pct, gpu_c),
        Text.assemble(
            Text(f"{gpu.mem_used_mb / 1024:.1f} / {gpu.mem_total_mb // 1024} GB", style=f"bold {vram_c}"),
        ),
        Text.assemble(
            Text(f"{gpu.power_w:.0f} / {gpu.power_limit_w:.0f} W", style=f"bold {pow_c}"),
            Text(f"   ctr ", style="dim"),
            Text(str(n_up), style="bold green"),
            Text(f"  run ", style="dim"),
            Text(str(n_runs), style="bold cyan"),
        ),
        Text(net_label_val, style="cyan"),
    ]
    t.add_row(*row0)
    t.add_row(
        pct_bar(v.cpu_pct, cpu_c),
        pct_bar(v.ram_pct, ram_c),
        pct_bar(gpu.util_pct, gpu_c),
        pct_bar(vram_pct, vram_c),
        pct_bar(pow_pct, pow_c),
        Text(net_label_val, style="cyan"),
    )
    return Panel(t, title="[bold]host vitals[/]", border_style="dim", padding=(0, 1))


def _container_rows(containers: List[ContainerInfo], sub: SubGroup) -> List[ContainerInfo]:
    return [c for c in containers if sub.match(c.name)]


def _render_container_row(c: ContainerInfo, compact: bool = False) -> Tuple:
    dot = Text("●", style="green" if c.running else "red")
    name = Text(c.name.split("-")[-1] if compact else c.name, overflow="ellipsis")
    age  = Text(c.uptime, style="dim")
    cpu  = Text(f"{c.cpu_pct:4.1f}%",
                style="bright_red" if c.cpu_pct > 50 else ("yellow" if c.cpu_pct > 15 else "green"))
    mem  = Text(f"{c.mem_mb:>5.0f}MB" if c.mem_mb < 1024
                else f"{c.mem_mb / 1024:>4.1f}GB", style="dim")
    return dot, name, age, cpu, mem


def _read_drafts_for_project(state: "SysmapState", project: str) -> list[dict]:
    """Read up to 3 draft beads from nbus:draft-beads for the given project."""
    if state._redis is None:
        return []
    try:
        entries = state._redis.xrange("nbus:draft-beads", count=50)
        drafts = []
        for _eid, data in entries:
            hint = data.get("project_hint", "")
            if hint and hint != project:
                continue
            try:
                drafts.append({
                    "signal_id":   data.get("signal_id", "?"),
                    "signal_type": data.get("signal_type", "?"),
                    "confidence":  float(data.get("confidence", 0)),
                    "description": data.get("description", "")[:80],
                })
            except Exception:
                pass
        return drafts[:3]
    except Exception:
        return []


def panel_project(proj: ProjectDef, state: SysmapState) -> Panel:
    with state.lock:
        ctrs   = [c for c in state.containers if _classify_container(c.name) is proj]
        runs   = [r for r in state.runners    if r.project == proj.name]
        svcs   = [s for s in state.services   if s.project == proj.name]
        hits   = deque(state.bus_hits.get(proj.name, deque()))
        loomie_projects = dict(state.loomie_projects)  # {container_name: project}

    # Compute aggregate stats for header
    total_cpu = sum(c.cpu_pct for c in ctrs)
    total_mem = sum(c.mem_mb  for c in ctrs)
    now = time.time()
    recent_ev = sum(1 for ts in hits if now - ts < 60)

    header_stats = Text()
    if ctrs:
        header_stats.append(f"🐳 {len(ctrs)}", style="dim")
        header_stats.append(f"  cpu {total_cpu:.0f}%",
                            style="yellow" if total_cpu > 30 else "dim")
        mem_str = (f"{total_mem / 1024:.1f}GB" if total_mem >= 1024
                   else f"{total_mem:.0f}MB")
        header_stats.append(f"  mem {mem_str}", style="dim")
    if runs:
        busy = sum(1 for r in runs if r.busy)
        header_stats.append(f"  🏃 {len(runs)}", style=f"bold {proj.color}" if runs else "dim")
        if busy:
            header_stats.append(f"/{busy}run", style="bold green")
    header_stats.append(f"  {recent_ev}/m", style="dim")

    sections: List[object] = []

    # ── container sub-groups ────────────────────────────────────────
    if proj.sub_groups:
        ungrouped = list(ctrs)
        for sg in proj.sub_groups:
            grp = [c for c in ctrs if sg.match(c.name)]
            if not grp:
                continue
            for c in grp:
                if c in ungrouped:
                    ungrouped.remove(c)

            t = Table.grid(padding=(0, 1), expand=True)
            t.add_column(width=1)    # dot
            t.add_column(ratio=1)    # name
            t.add_column(width=4)    # age
            t.add_column(width=6)    # cpu
            t.add_column(width=7)    # mem

            label = Text()
            label.append(f"{sg.label} ", style="dim")
            label.append(f"{len(grp)}", style="bold dim")

            if len(grp) > 3:
                # Grouped summary row
                all_cpu = sum(c.cpu_pct for c in grp)
                all_mem = sum(c.mem_mb  for c in grp)
                dot = Text("●", style="green" if all(c.running for c in grp) else "yellow")
                grp_name = Text.assemble(
                    Text(f"×{len(grp)}  ", style=f"bold {proj.color}"),
                    Text(re.sub(r"-[a-f0-9]{8}$", "-*", grp[0].name), style=proj.color),
                )
                ages = sorted(set(c.uptime for c in grp))
                age  = Text(ages[0] if len(ages) == 1 else f"{ages[0]}–{ages[-1]}", style="dim")
                cpu  = Text(f"{all_cpu:4.0f}%",
                            style="bright_red" if all_cpu > 100 else ("yellow" if all_cpu > 30 else "green"))
                mem  = Text(f"{all_mem / 1024:.1f}GB" if all_mem > 1024 else f"{all_mem:.0f}MB", style="dim")
                t.add_row(dot, grp_name, age, cpu, mem)
                # Hearth-loom agents: show per-loomie project cross-reference
                if proj.name == "hearth-loom":
                    # Collect project breakdown: project → count
                    proj_counts: Dict[str, int] = {}
                    unknown = 0
                    for c in grp:
                        wp = loomie_projects.get(c.name, "")
                        if wp:
                            proj_counts[wp] = proj_counts.get(wp, 0) + 1
                        else:
                            unknown += 1
                    if proj_counts or unknown:
                        breakdown = Text("  working on: ", style="dim")
                        PROJ_COLORS = {
                            "hearth-loom": "purple", "tengine": "yellow",
                            "hearth": "green", "nervous-bus": "cyan",
                            "deer-flow": "blue",
                        }
                        for wp, cnt in sorted(proj_counts.items()):
                            c2 = PROJ_COLORS.get(wp, "white")
                            breakdown.append(f"{wp}", style=f"bold {c2}")
                            breakdown.append(f"×{cnt}  ", style=c2)
                        if unknown:
                            breakdown.append(f"?×{unknown}", style="dim")
                        t.add_row(Text(""), breakdown, Text(""), Text(""), Text(""))
                # Individual peek: top N containers by CPU
                _TOP = 4
                for _c in sorted(grp, key=lambda _x: _x.cpu_pct, reverse=True)[:_TOP]:
                    _cname = _c.name
                    for _pfx in proj.container_prefixes:
                        if _cname.startswith(_pfx):
                            _cname = _cname[len(_pfx):]
                            break
                    _wp = loomie_projects.get(_c.name, "")
                    _label = f"{_wp[:7]}/{_cname[:7]}" if _wp else _cname[:12]
                    _cpu_s = ("bright_red" if _c.cpu_pct > 50
                              else ("yellow" if _c.cpu_pct > 15 else "green"))
                    _mem_s = (f"{_c.mem_mb / 1024:.1f}GB" if _c.mem_mb >= 1024
                              else f"{_c.mem_mb:.0f}MB")
                    t.add_row(
                        Text("·", style="dim"),
                        Text(f"  {_label}", style="dim", overflow="ellipsis"),
                        Text(_c.uptime, style="dim"),
                        Text(f"{_c.cpu_pct:4.1f}%", style=_cpu_s),
                        Text(_mem_s, style="dim"),
                    )
                if len(grp) > _TOP:
                    t.add_row(Text(""),
                              Text(f"  +{len(grp) - _TOP} more", style="dim italic"),
                              Text(""), Text(""), Text(""))
            else:
                for c in grp:
                    t.add_row(*_render_container_row(c))

            sections.append(Panel(t, title=label, border_style="dim", padding=(0, 0)))

        if ungrouped:
            t = Table.grid(padding=(0, 1), expand=True)
            t.add_column(width=1)
            t.add_column(ratio=1)
            t.add_column(width=4)
            t.add_column(width=6)
            t.add_column(width=7)
            for c in ungrouped:
                t.add_row(*_render_container_row(c))
            sections.append(t)

    elif ctrs:
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(width=1)
        t.add_column(ratio=1)
        t.add_column(width=4)
        t.add_column(width=6)
        t.add_column(width=7)
        for c in ctrs:
            t.add_row(*_render_container_row(c))
        sections.append(t)
    elif not runs:
        sections.append(Text("(no containers)", style="dim italic"))

    # ── runners ─────────────────────────────────────────────────────
    if runs:
        rt = Table.grid(padding=(0, 1), expand=True)
        rt.add_column(width=5)   # state badge
        rt.add_column(ratio=1)   # name
        for r in runs:
            state_text = Text("RUN " if r.busy else "IDLE",
                              style="bold green" if r.busy else "bold dim")
            rt.add_row(state_text, Text(r.name, style=proj.color))
        sections.append(Panel(rt, title=Text("runners", style="dim"),
                              border_style="dim", padding=(0, 0)))

    # ── systemd services ────────────────────────────────────────────
    if svcs:
        st = Table.grid(padding=(0, 1), expand=True)
        st.add_column(width=1)    # state dot
        st.add_column(ratio=1)    # name
        st.add_column(width=6)    # cpu%
        st.add_column(width=7)    # mem
        st.add_column(width=3)    # task count (dim)

        active_svcs   = [s for s in svcs if s.state == "active"]
        inactive_svcs = [s for s in svcs if s.state != "active"]

        # Show active services sorted by CPU desc, then inactive ones dimmed
        for s in sorted(active_svcs, key=lambda x: x.cpu_pct, reverse=True):
            dot = Text("●", style="green")
            # Show yellow if >70% of MemoryHigh, red if >90% of MemoryHigh or near MemoryMax
            if s.mem_high_mb > 0 and s.mem_mb / s.mem_high_mb > 0.9:
                mem_style = "bright_red"
            elif s.mem_high_mb > 0 and s.mem_mb / s.mem_high_mb > 0.7:
                mem_style = "yellow"
            elif s.mem_mb > 800:
                mem_style = "yellow"
            else:
                mem_style = "dim"
            if s.mem_high_mb > 0 and s.mem_mb / s.mem_high_mb > 0.5:
                mem_str = f"{s.mem_mb:.0f}/{s.mem_high_mb:.0f}MB"
            else:
                mem_str = (f"{s.mem_mb / 1024:.1f}GB" if s.mem_mb >= 1024
                           else f"{s.mem_mb:.0f}MB")
            cpu_style = ("bright_red" if s.cpu_pct > 50
                         else ("yellow" if s.cpu_pct > 15 else "dim"))
            st.add_row(
                dot,
                Text(s.name, style=proj.color, overflow="ellipsis"),
                Text(f"{s.cpu_pct:5.1f}%", style=cpu_style),
                Text(mem_str, style=mem_style),
                Text(str(s.tasks) if s.tasks else "", style="dim"),
            )
        for s in inactive_svcs:
            dot = Text("○", style="red" if s.state == "failed" else "dim")
            st.add_row(
                dot,
                Text(s.name, style="dim", overflow="ellipsis"),
                Text("", style="dim"),
                Text("", style="dim"),
                Text(s.state[:4], style="red" if s.state == "failed" else "dim"),
            )

        # Summary header: aggregate CPU + mem for services
        total_svc_cpu = sum(s.cpu_pct for s in active_svcs)
        total_svc_mem = sum(s.mem_mb  for s in active_svcs)
        svc_mem_str = (f"{total_svc_mem / 1024:.1f}GB" if total_svc_mem >= 1024
                       else f"{total_svc_mem:.0f}MB")
        svc_title = Text.assemble(
            Text("services", style="dim"),
            Text(f"  {len(active_svcs)}", style="bold dim"),
            Text(f"  cpu {total_svc_cpu:.0f}%",
                 style="yellow" if total_svc_cpu > 30 else "dim"),
            Text(f"  mem {svc_mem_str}", style="dim"),
        )
        sections.append(Panel(st, title=svc_title, border_style="dim", padding=(0, 0)))

    # ── tengine-specific: note about on-demand processes ────────────
    if proj.name == "tengine" and not ctrs:
        sections.append(Text("shadergen · silo-tester\nlaunched on-demand", style="dim italic"))

    # ── idle enrichment: zs status + recent events + git commit ────────
    if not ctrs:
        with state.lock:
            zs       = dict(state.zs)
            git_line = state.git_commits.get(proj.name, ("", 0.0))[0]

        zs_keys = ZS_KEYS_BY_PROJECT.get(proj.name, [])
        if zs_keys:
            st = Table.grid(padding=(0, 1), expand=True)
            st.add_column(width=14, style="dim")
            st.add_column(ratio=1)
            for key in zs_keys:
                val = zs.get(key, "")
                if not val:
                    continue
                label   = re.sub(r"^(hl|te|ha|tx)-", "", key.split(":")[-1])
                healthy = val in ("live", "ok", "all_up", "ok/up")
                warn    = val.startswith("missing") or "error" in val.lower()
                color   = "green" if healthy else ("bright_red" if warn else "cyan")
                st.add_row(Text(label, style="dim"), Text(val, style=f"bold {color}"))
            if st.row_count:
                sections.append(st)

        recent_hits = sorted(ts for ts in hits if now - ts < BUS_WINDOW_S)
        if recent_hits:
            last5 = recent_hits[-5:]
            ev_text = Text()
            for ts in reversed(last5):
                age = int(now - ts)
                if age < 60:
                    age_str = f"{age}s ago"
                elif age < 3600:
                    age_str = f"{age // 60}m ago"
                else:
                    age_str = f"{age // 3600}h ago"
                ev_text.append(f"  ↑ {age_str}\n", style="dim")
            sections.append(ev_text)

        if git_line:
            sections.append(Text(f"  ↗ {git_line}", style="dim"))

    # ── bus sparkline ────────────────────────────────────────────────
    spark_row = Text()
    spark_row.append("bus  ", style="dim")
    spark_row.append(_sparkline(hits))
    spark_row.append(f"  {recent_ev}/m", style="dim")
    sections.append(spark_row)

    # Draft signal review (calibration mode)
    drafts = [d for d in _read_drafts_for_project(state, proj.name)
              if d.get("signal_type", "?") != "?" and d.get("confidence", 0) > 0]
    if drafts:
        t = Table.grid(padding=(0, 1))
        t.add_column(width=1)    # bullet
        t.add_column(width=24)   # signal_type/sid_short
        t.add_column(width=5)    # conf_bar (exactly 5 blocks)
        t.add_column()           # description (takes remaining width)
        t.add_row(Text(""), Text(""), Text(""), Text(""))
        t.add_row(
            Text("drafts", style="bold yellow"),
            Text(""), Text(""), Text(""),
        )
        for d in drafts:
            conf_bar = "█" * int(d["confidence"] * 5) + "░" * (5 - int(d["confidence"] * 5))
            sid_short = d["signal_id"][-8:]
            t.add_row(
                Text("·", style="dim"),
                Text(f"{d['signal_type']}/{sid_short}", style="dim yellow", overflow="ellipsis"),
                Text(conf_bar, style="yellow"),
                Text(d["description"], style="dim", overflow="ellipsis"),
            )
        t.add_row(
            Text(""),
            Text("nervous pattern list", style="dim italic"),
            Text(""), Text(""),
        )
        sections.append(t)

    body = Group(*sections) if len(sections) > 1 else (sections[0] if sections else Text(""))
    title = Text.assemble(
        Text(f"{proj.emoji}  ", ),
        Text(proj.name, style=f"bold {proj.color}"),
        Text("  "),
        header_stats,
    )
    return Panel(body, title=title,
                 border_style=proj.color, padding=(0, 1))


def panel_gpu(state: SysmapState) -> Optional[Panel]:
    with state.lock:
        gpu = state.gpu

    if not gpu.name or gpu.name == "?":
        return None

    vram_pct = 100 * gpu.mem_used_mb / max(1, gpu.mem_total_mb)
    pow_pct  = 100 * gpu.power_w / max(1, gpu.power_limit_w)
    short_name = re.sub(r"NVIDIA GeForce |GeForce |RTX |GTX ", "", gpu.name)

    t = Table.grid(padding=(0, 2), expand=True)
    t.add_column(ratio=1)
    t.add_column(ratio=2)

    # Left: utilization bars
    left = Table.grid(padding=(0, 1), expand=True)
    left.add_column(width=9, style="dim")
    left.add_column(ratio=1)
    left.add_column(width=7)

    def bar_row(label: str, pct: float, suffix: str) -> None:
        color = _cpu_color(pct) if "compute" in label or "power" in label else _mem_color(pct)
        filled = int(16 * pct / 100)
        bar = Text()
        bar.append("█" * filled, style=color)
        bar.append("░" * (16 - filled), style="dim")
        left.add_row(Text(label, style="dim"), bar,
                     Text(suffix, style=f"bold {color}"))

    bar_row("compute", gpu.util_pct, f"{gpu.util_pct}%")
    bar_row("memory",  vram_pct,
            f"{gpu.mem_used_mb / 1024:.1f}/{gpu.mem_total_mb // 1024}GB")
    bar_row("power",   pow_pct,
            f"{gpu.power_w:.0f}/{gpu.power_limit_w:.0f}W")

    # Right: processes
    right = Table.grid(padding=(0, 1), expand=True)
    right.add_column(width=2)    # emoji
    right.add_column(ratio=1)    # name/cmd
    right.add_column(width=7, justify="right")  # mem

    if gpu.processes:
        for p in gpu.processes:
            display = p.container_name or p.cmdline.split("/")[-1][:28]
            right.add_row(
                Text(p.project_emoji),
                Text(display, overflow="ellipsis"),
                Text(f"{p.mem_mb} MB", style="bold blue"),
            )
    else:
        right.add_row(Text(""), Text("(no compute processes)", style="dim italic"), Text(""))

    t.add_row(left, right)

    title = Text.assemble(
        Text("⬡ GPU  ·  ", style="purple"),
        Text(short_name, style="bold purple"),
        Text(f"  ·  {gpu.temp_c}°C", style="dim"),
    )
    return Panel(t, title=title, border_style="purple", padding=(0, 1))


def _age_str(age_s: float) -> str:
    """Compact age formatter — '3s', '45s', '2m', '1h12m', '3d'."""
    if age_s < 0:
        return "?"
    if age_s < 60:
        return f"{int(age_s)}s"
    if age_s < 3600:
        return f"{int(age_s // 60)}m"
    if age_s < 86400:
        h = int(age_s // 3600)
        m = int((age_s % 3600) // 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{int(age_s // 86400)}d"


_SYSMAP_MARKET_PREFIXES = ("tachyonos.",)

def _chan_color_sysmap(chan: str) -> str:
    if chan.startswith("tachyonos."):    return "bright_magenta"
    if chan.startswith("bus.agent."):   return "cyan"
    if chan.startswith("agent."):       return "cyan"
    if chan.startswith("loom."):        return "magenta"
    if chan.startswith("tengine."):     return "yellow"
    if chan.startswith("hearth."):      return "blue"
    if chan.startswith("bus.hearth."):  return "blue"
    if chan.startswith("deer-flow."):   return "bright_blue"
    if chan.startswith("bus.bead."):    return "green"
    if chan == "bus.dead_letter":       return "red"
    return "dim"


def panel_bus(state: SysmapState) -> Panel:
    """BusPanel (kciq) — tiered channel rates, sparklines, dead_letter health."""
    with state.lock:
        bs          = state.bus_status
        chan_hits    = {c: deque(d) for c, d in bs.chan_hits.items()}
        chan_schema  = dict(bs.chan_schema)
        source_seen  = dict(bs.source_last_seen)
        dead_letters = list(bs.dead_letters)

    now = time.time()

    # Compute ev/min per channel over BUS_EVPS_S window
    chan_epm: List[Tuple[str, float, deque]] = []
    for chan, dq in chan_hits.items():
        recent_ts = [ts for ts in dq if now - ts < BUS_EVPS_S]
        if not recent_ts:
            continue
        epm = len(recent_ts) / BUS_EVPS_S * 60
        chan_epm.append((chan, epm, dq))
    chan_epm.sort(key=lambda x: -x[1])

    market = [(c, e, d) for c, e, d in chan_epm
              if any(c.startswith(p) for p in _SYSMAP_MARKET_PREFIXES)]
    ops    = [(c, e, d) for c, e, d in chan_epm
              if not any(c.startswith(p) for p in _SYSMAP_MARKET_PREFIXES)
              and c != "bus.dead_letter"]

    sections: List[object] = []

    def _chan_table(rows, max_rows: int = 6) -> Table:
        ct = Table.grid(padding=(0, 1), expand=True)
        ct.add_column(ratio=1, overflow="ellipsis")  # channel
        ct.add_column(width=12)                       # sparkline
        ct.add_column(width=7, justify="right")       # ev/m
        for chan, epm, dq in rows[:max_rows]:
            col = _chan_color_sysmap(chan)
            ct.add_row(
                Text(chan, style=col),
                _sparkline(dq, width=12),
                Text(f"{epm:4.0f}/m", style="bold" if epm > 1 else "dim"),
            )
        return ct

    if not chan_epm:
        sections.append(Text("(no recent channel activity)", style="dim italic"))
    else:
        if market:
            m_total = sum(e for _, e, _ in market)
            sections.append(Text.assemble(
                Text(" 📈 market ", style="bold dim"),
                Text(f"{m_total:.0f}/m", style="dim"),
            ))
            sections.append(_chan_table(market, max_rows=4))

        if ops:
            o_total = sum(e for _, e, _ in ops)
            sections.append(Text.assemble(
                Text(" ⚙  ops ", style="bold dim"),
                Text(f"{o_total:.0f}/m", style="dim"),
            ))
            sections.append(_chan_table(ops, max_rows=5))

    # Dead letter summary
    recent_dlq = [d for d in dead_letters if now - d.ts < 3600]
    if recent_dlq:
        dlq_t = Table.grid(padding=(0, 1), expand=True)
        dlq_t.add_column(ratio=1, overflow="ellipsis")
        dlq_t.add_column(width=8, justify="right")
        by_type: Dict[str, List[DeadLetterRecord]] = {}
        for d in recent_dlq:
            by_type.setdefault(d.original_type, []).append(d)
        for ot, items in sorted(by_type.items(), key=lambda x: -len(x[1]))[:4]:
            last_detail = items[-1].detail[:30] if items[-1].detail else items[-1].failure_reason
            dlq_t.add_row(
                Text(f"{ot}", style="bright_red", overflow="ellipsis"),
                Text(f"{len(items)}×", style="bright_red"),
            )
            dlq_t.add_row(
                Text(f"  {last_detail}", style="dim", overflow="ellipsis"),
                Text(""),
            )
        sections.append(Text(" ☠ dlq", style="dim red"))
        sections.append(dlq_t)

    # Producer health (compact)
    if source_seen:
        sorted_sources = sorted(source_seen.items(), key=lambda x: -x[1])
        prod_parts: List[object] = []
        for src, ts in sorted_sources[:8]:
            age = now - ts
            color = "green" if age < 300 else ("dim" if age < 3600 else "bright_red")
            prod_parts.append(Text(f"{src} ", style=color))
        sections.append(Text.assemble(*prod_parts))

    n_chan = len(chan_epm)
    n_src  = len(source_seen)
    dlq_count = len(recent_dlq)
    title = Text.assemble(
        Text("🚌 ", style="cyan"),
        Text("bus", style="bold cyan"),
        Text(f"  ·  {n_chan}ch {n_src}src", style="dim"),
        *([] if not dlq_count else [Text(f"  ☠{dlq_count}", style="bright_red")]),
    )
    return Panel(Group(*sections), title=title,
                 border_style="cyan", padding=(0, 1))


def panel_autobench(state: SysmapState) -> Panel:
    """AutobenchPanel (opvt) — session, iter, AHE outcome, queue, requests."""
    with state.lock:
        ab = AutobenchStatus(**dataclasses.asdict(state.autobench))
        worker_n = len(state._autobench_worker_ts)

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=10, style="dim")
    t.add_column(ratio=1)

    sid_short = ab.session_id[-12:] if ab.session_id else "—"
    t.add_row(Text("session", style="dim"),
              Text(sid_short, style="bold blue" if ab.session_id else "dim"))
    t.add_row(Text("iter", style="dim"),
              Text(str(ab.iter) if ab.iter is not None else "—",
                   style="bold" if ab.iter is not None else "dim"))

    outcome_color = {
        "hit":          "bold green",
        "miss":         "bold yellow",
        "refuted_live": "bold bright_red",
        "pending":      "dim",
    }.get(ab.last_ahe_outcome, "dim")
    t.add_row(Text("AHE", style="dim"),
              Text(ab.last_ahe_outcome, style=outcome_color))

    qp_text = ("ON" if ab.queue_pressure else "OFF")
    qp_color = "bright_red" if ab.queue_pressure else "green"
    t.add_row(Text("queue", style="dim"),
              Text.assemble(
                  Text(qp_text, style=f"bold {qp_color}"),
                  Text(f"  (dev×{ab.queue_dev_factor:.2f})",
                       style="dim"),
              ))

    # requests-used vs notional 5h cap
    req = max(ab.requests_5h, worker_n)
    cap = AUTOBENCH_REQ_CAP_5H
    pct = 100.0 * req / max(1, cap)
    req_color = ("bright_red" if pct > 90
                 else ("yellow" if pct > 70 else "green"))
    bar = Text()
    width = 12
    filled = int(width * min(100, pct) / 100)
    bar.append("█" * filled, style=req_color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f"  {req}/{cap}", style=f"bold {req_color}")
    t.add_row(Text("req/5h", style="dim"), bar)

    if ab.last_event_age_s is not None:
        # The age field is stored as 0 at ingest-time; recompute against version
        # tick rate is OK since this panel re-renders ≤ tick.
        pass

    title = Text.assemble(
        Text("🤖 ", style="magenta"),
        Text("autobench", style="bold magenta"),
    )
    return Panel(t, title=title, border_style="magenta", padding=(0, 1))


def panel_worktree_agents(state: SysmapState) -> Panel:
    """WorktreeAgentsPanel (6k8a) — host-side dispatched worktree agents."""
    with state.lock:
        agents = list(state.worktree_agents)

    # Filter: always show running; only show merged/abandoned if < 72h old
    agents = [a for a in agents if a.status == "running" or a.age_s < 3 * 86400]

    if not agents:
        return Panel(Text("(no worktree agents)", style="dim italic"),
                     title=Text("🌿 worktree agents", style="bold green"),
                     border_style="green", padding=(0, 1))

    # Sort: running first, then by age (newest first)
    status_order = {"running": 0, "merged": 1, "abandoned": 2}
    agents.sort(key=lambda a: (status_order.get(a.status, 3), a.age_s))

    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(width=12, overflow="ellipsis")   # id
    t.add_column(width=10)                        # status
    t.add_column(ratio=1, overflow="ellipsis")    # branch
    t.add_column(width=6, justify="right")        # age

    n_running = sum(1 for a in agents if a.status == "running")
    n_merged  = sum(1 for a in agents if a.status == "merged")
    n_aband   = sum(1 for a in agents if a.status == "abandoned")

    status_color = {
        "running":   "green",
        "merged":    "dim",
        "abandoned": "bright_red",
    }

    # Display up to 8 rows to keep the panel compact.
    _MAX = 8
    for a in agents[:_MAX]:
        c = status_color.get(a.status, "white")
        marker = "●" if a.status == "running" else ("✓" if a.status == "merged" else "✗")
        id_text = Text(a.agent_id, style=c)
        if a.dirty and a.status == "running":
            id_text = Text(a.agent_id + "*", style="bold yellow")
        t.add_row(
            id_text,
            Text(f"{marker} {a.status}", style=c),
            Text(a.branch, style="dim"),
            Text(_age_str(a.age_s), style="dim"),
        )
    if len(agents) > _MAX:
        t.add_row(
            Text(""),
            Text(f"+{len(agents) - _MAX} more", style="dim italic"),
            Text(""),
            Text(""),
        )

    title = Text.assemble(
        Text("🌿 ", style="green"),
        Text("worktree agents", style="bold green"),
        Text(f"  ·  {n_running}r {n_merged}m {n_aband}x", style="dim"),
    )
    return Panel(t, title=title, border_style="green", padding=(0, 1))


def panel_combined_shared_nbus(state: SysmapState) -> Panel:
    """Single card for nervous-bus runner + shared services."""
    with state.lock:
        shared_proj = next(p for p in PROJECTS if p.name == "shared")
        nbus_proj   = next(p for p in PROJECTS if p.name == "nervous-bus")
        shared_ctrs = [c for c in state.containers if _classify_container(c.name) is shared_proj]
        nbus_runs   = [r for r in state.runners if r.project == "nervous-bus"]
        nbus_svcs   = [s for s in state.services  if s.project == "nervous-bus"]
        shits       = deque(state.bus_hits.get("shared", deque()))
        nhits       = deque(state.bus_hits.get("nervous-bus", deque()))

    now = time.time()

    sections = []

    # Shared services
    if shared_proj.sub_groups:
        for sg in shared_proj.sub_groups:
            grp = [c for c in shared_ctrs if sg.match(c.name)]
            if not grp:
                continue
            t = Table.grid(padding=(0, 1), expand=True)
            t.add_column(width=1)
            t.add_column(ratio=1)
            t.add_column(width=4)
            t.add_column(width=6)
            t.add_column(width=7)
            for c in grp:
                dot = Text("●", style="yellow" if sg.label == "CI" else ("green" if c.running else "red"))
                name = Text(c.name, overflow="ellipsis",
                            style="dim" if sg.label == "CI" else "default")
                t.add_row(dot, name,
                          Text(c.uptime, style="dim"),
                          Text(f"{c.cpu_pct:.1f}%", style="dim"),
                          Text(f"{c.mem_mb:.0f}MB", style="dim"))
            sections.append(Panel(t, title=Text(sg.label, style="dim"),
                                  border_style="dim", padding=(0, 0)))

    # nervous-bus runner + bus pulse
    nb_t = Table.grid(padding=(0, 1), expand=True)
    nb_t.add_column(width=5)
    nb_t.add_column(ratio=1)
    for r in nbus_runs:
        nb_t.add_row(
            Text("IDLE", style="bold dim"),
            Text(r.name, style="bold cyan"),
        )

    spark_all = Text()
    spark_all.append("bus  ", style="dim")
    all_hits: deque = deque()
    for hits in [shits, nhits]:
        all_hits.extend(hits)
    spark_all.append(_sparkline(all_hits))
    recent_all = sum(1 for ts in all_hits if now - ts < 60)
    spark_all.append(f"  {recent_all}/m all", style="dim")

    # nervous-bus systemd adapters (signal pipeline)
    if nbus_svcs:
        ns_t = Table.grid(padding=(0, 1), expand=True)
        ns_t.add_column(width=1)
        ns_t.add_column(ratio=1)
        ns_t.add_column(width=6)
        ns_t.add_column(width=7)
        for s in sorted(nbus_svcs, key=lambda x: x.cpu_pct, reverse=True):
            dot      = Text("●" if s.state == "active" else "○",
                            style="cyan" if s.state == "active" else "dim")
            mem_str  = f"{s.mem_mb:.0f}MB"
            cpu_str  = f"{s.cpu_pct:5.1f}%" if s.state == "active" else ""
            cpu_sty  = "yellow" if s.cpu_pct > 15 else "dim"
            ns_t.add_row(dot, Text(s.name, style="dim", overflow="ellipsis"),
                         Text(cpu_str, style=cpu_sty), Text(mem_str, style="dim"))
        total_nsvc_cpu = sum(s.cpu_pct for s in nbus_svcs if s.state == "active")
        ns_title = Text.assemble(
            Text("adapters", style="dim"),
            Text(f"  cpu {total_nsvc_cpu:.0f}%",
                 style="yellow" if total_nsvc_cpu > 20 else "dim"),
        )
        nb_t.add_row(Text(""), Text(""))  # spacer between runner and adapters rows
        sections.append(Panel(nb_t, title=Text("🧬 substrate", style="bold cyan"),
                              border_style="cyan", padding=(0, 0)))
        sections.append(Panel(ns_t, title=ns_title, border_style="dim", padding=(0, 0)))
    else:
        sections.append(Panel(nb_t, title=Text("🧬 substrate", style="bold cyan"),
                              border_style="cyan", padding=(0, 0)))

    # ha status from Redis zs keys
    with state.lock:
        zs = dict(state.zs)
    ha_keys = ZS_KEYS_BY_PROJECT.get("shared", [])
    if ha_keys:
        ha_t = Table.grid(padding=(0, 1), expand=True)
        ha_t.add_column(width=14, style="dim")
        ha_t.add_column(ratio=1)
        for key in ha_keys:
            val = zs.get(key, "")
            if not val:
                continue
            label = re.sub(r"^ha-", "", key.split(":")[-1])
            healthy = val in ("ok", "ok/up", "up")
            color   = "green" if healthy else "cyan"
            ha_t.add_row(Text(label, style="dim"), Text(val, style=f"bold {color}"))
        if ha_t.row_count:
            sections.append(ha_t)

    sections.append(spark_all)

    title = Text.assemble(
        Text("🌉  commons", style="bold white"),
        Text("  ·  🧬  substrate", style="dim"),
    )
    return Panel(Group(*sections), title=title, border_style="dim", padding=(0, 1))


def build_sysmap_layout(state: SysmapState, tick_s: float = 2.0) -> Layout:
    layout = Layout()

    # Check if GPU panel has data
    with state.lock:
        has_gpu = bool(state.gpu.name and state.gpu.name != "?")

    # Layout decision (kciq/opvt/6k8a, 2026-05-16):
    # The bottom row hosts three new panels (bus | autobench | worktree-agents).
    # The original 3-line footer collapses to 1 line and tucks under bottom_row.
    # This preserves all 5 existing project panels in the grid; we only shrink
    # the footer's visual real estate. If your terminal is < 40 rows tall the
    # bottom_row will compress before the grid does.
    if has_gpu:
        layout.split_column(
            Layout(name="vitals", size=5),
            Layout(name="grid",   ratio=1),
            Layout(name="gpu",    size=7),
            Layout(name="bottom_row", size=14),
            Layout(name="footer", size=3),
        )
    else:
        layout.split_column(
            Layout(name="vitals", size=5),
            Layout(name="grid",   ratio=1),
            Layout(name="bottom_row", size=14),
            Layout(name="footer", size=3),
        )

    layout["vitals"].update(panel_vitals(state))

    # Project grid: 3 columns, deer-flow spans 2 rows
    layout["grid"].split_row(
        Layout(name="col_df",     ratio=1),
        Layout(name="col_center", ratio=1),
        Layout(name="col_right",  ratio=1),
    )
    layout["grid"]["col_df"].update(
        panel_project(next(p for p in PROJECTS if p.name == "deer-flow"), state)
    )
    layout["grid"]["col_center"].split_column(
        Layout(name="hearth_loom", ratio=1),
        Layout(name="hearth",      ratio=1),
    )
    layout["grid"]["col_center"]["hearth_loom"].update(
        panel_project(next(p for p in PROJECTS if p.name == "hearth-loom"), state)
    )
    layout["grid"]["col_center"]["hearth"].update(
        panel_project(next(p for p in PROJECTS if p.name == "hearth"), state)
    )
    layout["grid"]["col_right"].split_column(
        Layout(name="tengine",    ratio=1),
        Layout(name="shared_nbus",ratio=1),
    )
    layout["grid"]["col_right"]["tengine"].update(
        panel_project(next(p for p in PROJECTS if p.name == "tengine"), state)
    )
    layout["grid"]["col_right"]["shared_nbus"].update(
        panel_combined_shared_nbus(state)
    )

    if has_gpu:
        gpu_panel = panel_gpu(state)
        layout["gpu"].update(gpu_panel or Panel("", border_style="dim"))

    # New row: bus | autobench | worktree-agents (kciq | opvt | 6k8a)
    layout["bottom_row"].split_row(
        Layout(name="bus_panel",       ratio=1),
        Layout(name="autobench_panel", ratio=1),
        Layout(name="wt_panel",        ratio=1),
    )
    layout["bottom_row"]["bus_panel"].update(panel_bus(state))
    layout["bottom_row"]["autobench_panel"].update(panel_autobench(state))
    layout["bottom_row"]["wt_panel"].update(panel_worktree_agents(state))

    # Footer
    with state.lock:
        n_ctr = sum(1 for c in state.containers if c.running)
        n_run = sum(1 for r in state.runners if r.active)
        gpu_procs = len(state.gpu.processes)
        now_hits = sum(len(dq) for dq in state.bus_hits.values())

    uptime = int(time.time() - state.started_at)
    up_str = (f"{uptime // 3600}h{(uptime % 3600) // 60:02d}m"
              if uptime >= 3600 else f"{uptime // 60:02d}m{uptime % 60:02d}s")

    footer = Text()
    footer.append("  [1] bus  ", style="dim")
    footer.append("  [2] sysmap  ", style="bold blue")
    footer.append("  [3] agents  [4] loomies  ", style="dim")
    footer.append("  │  ", style="dim")
    footer.append("  [q] quit  ", style="dim")
    footer.append("  │  ", style="dim")
    footer.append(f"  ctr ", style="dim")
    footer.append(str(n_ctr), style="bold green")
    footer.append(f"  run ", style="dim")
    footer.append(str(n_run), style="bold cyan")
    if gpu_procs:
        footer.append(f"  gpu ", style="dim")
        footer.append(str(gpu_procs), style="bold purple")
    footer.append(f"  up {up_str}", style="dim")
    layout["footer"].update(Panel(footer, border_style="dim", padding=(0, 0)))

    return layout


# ── standalone entry point ────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="cc-sysmap — live system map")
    p.add_argument("--log",  type=Path, default=DEFAULT_LOG)
    p.add_argument("--tick", type=float, default=2.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    state  = SysmapState(log_path=args.log)
    poller = _Poller(state)
    poller.start()

    console = Console()

    if args.once:
        # Wait for docker stats (~3-4s) + GPU + runners
        time.sleep(POLL_DOCKER_S + 1)
        console.print(build_sysmap_layout(state, args.tick))
        return 0

    try:
        last_ver = -1
        with Live(build_sysmap_layout(state, args.tick),
                  console=console, refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(args.tick)
                cur_ver = state.version
                if cur_ver != last_ver:
                    live.update(build_sysmap_layout(state, args.tick))
                    last_ver = cur_ver
    except KeyboardInterrupt:
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
