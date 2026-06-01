#!/usr/bin/env python3
"""cc-loomies — active-loomie panel for cc-bus-dashboard.

Polls `hearth-loom status --wide --json` every 5 s and renders:
  - Top:    count by phase (queued | container_init | agent_active | gates | complete | failed)
  - Body:   table of heartbeats with id / project / phase / age / risk / tokens / cost
  - Footer: total $/hr burn, stuck count (last_tool_call_age > 300 s), recent fails

Swap-ready: set CC_LOOMIES_SOURCE=bus to tail debug.jsonl filtering
type=loom.lifecycle.v1 instead (Layer 2 — separate bead).

Usage:
  cc-loomies                       # default: poll hearth-loom
  cc-loomies --tick 5              # poll interval (default 5 s)
  cc-loomies --source bus          # tail debug.jsonl for loom.lifecycle.v1 events
  cc-loomies --log /path/debug.jsonl  # when source=bus
  cc-loomies --once                # render one frame and exit (test/screenshot)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ─── constants ─────────────────────────────────────────────────────────────
TICK_S = 5.0
STUCK_THRESHOLD_S = 300
DEFAULT_LOG = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"

PHASE_COLORS = {
    "queued":          "dim",
    "container_init":  "cyan",
    "agent_active":    "green",
    "gates":           "yellow",
    "complete":        "blue",
    "failed":          "bright_red",
}

RISK_COLORS = {
    "low":    "green",
    "medium": "yellow",
    "high":   "bright_red",
    "unknown": "dim",
}


# ─── state ─────────────────────────────────────────────────────────────────
@dataclass
class LoomieHeartbeat:
    task_id: str
    title: str
    project: str
    phase: str
    phase_age_seconds: int
    last_tool_call_age_seconds: int
    kill_risk: str
    tokens_input_total: int
    tokens_output_total: int
    estimated_cost_usd: float
    token_burn_per_min: float
    container_uptime_seconds: int
    is_stuck: bool = False

    @classmethod
    def from_json(cls, data: dict) -> "LoomieHeartbeat":
        # `hearth-loom status --wide --json` (current shape) uses bead_* and
        # project_path field names. Fall back to legacy heartbeat names so a
        # future schema change doesn't blank the pane again.
        task_id = data.get("task_id") or data.get("bead_id", "?")
        title = data.get("title") or data.get("bead_title", "")
        project_raw = data.get("project") or data.get("project_path") or "unknown"
        project = project_raw.rsplit("/", 1)[-1] if "/" in project_raw else project_raw
        phase = data.get("phase", "unknown")
        phase_age = data.get("phase_age_seconds")
        if phase_age is None:
            phase_age = _seconds_since(data.get("phase_started_at"))
        container_uptime = data.get("container_uptime_seconds")
        if container_uptime is None:
            container_uptime = _seconds_since(data.get("container_started_at"))
        last_tool_call_age = data.get("last_tool_call_age_seconds", 0)
        return cls(
            task_id=task_id,
            title=(title or "")[:40],
            project=(project or "unknown")[:10],
            phase=phase,
            phase_age_seconds=phase_age,
            last_tool_call_age_seconds=last_tool_call_age,
            kill_risk=data.get("kill_risk", "unknown"),
            tokens_input_total=data.get("tokens_input_total", 0),
            tokens_output_total=data.get("tokens_output_total", 0),
            estimated_cost_usd=data.get("estimated_cost_usd", 0.0),
            token_burn_per_min=data.get("token_burn_per_min", 0.0),
            container_uptime_seconds=container_uptime,
            is_stuck=last_tool_call_age > STUCK_THRESHOLD_S,
        )


@dataclass
class LoomieState:
    heartbeats: List[LoomieHeartbeat] = field(default_factory=list)
    recent_fails: Deque[str] = field(default_factory=lambda: deque(maxlen=5))
    last_poll_at: float = 0.0
    poll_errors: int = 0

    def phase_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for hb in self.heartbeats:
            counts[hb.phase] += 1
        return dict(counts)

    def stuck_count(self) -> int:
        return sum(1 for hb in self.heartbeats if hb.is_stuck)

    def total_cost_per_hr(self) -> float:
        return sum(hb.token_burn_per_min * 60 / 1000 * 0.01 for hb in self.heartbeats)


# ─── sourcing ──────────────────────────────────────────────────────────────
def poll_hearth_loom() -> List[LoomieHeartbeat]:
    try:
        result = subprocess.run(
            ["hearth-loom", "status", "--wide", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        # hearth-loom emits an INFO log line to stdout before the JSON payload;
        # slice from the first '[' or '{' so json.loads sees clean input.
        out = result.stdout
        idx = min((i for i in (out.find("["), out.find("{")) if i >= 0), default=-1)
        if idx < 0:
            return []
        data = json.loads(out[idx:])
        # `hearth-loom status --wide --json` returns a top-level list of bead
        # heartbeats; tolerate either shape so a future wrap doesn't break us.
        heartbeats = data if isinstance(data, list) else data.get("heartbeats", [])
        return [LoomieHeartbeat.from_json(h) for h in heartbeats]
    except Exception:
        return []


def ingest_bus_event(state: LoomieState, raw: str) -> None:
    try:
        e = json.loads(raw)
    except Exception:
        return
    chan = e.get("type", "")
    if chan != "loom.lifecycle.v1":
        return
    data = e.get("data", {})
    phase = data.get("phase", "")
    if phase == "failed":
        task_id = data.get("task_id", "?")
        state.recent_fails.append(task_id[:8])


def read_bus_lines(state: LoomieState, log_path: Path) -> None:
    if not log_path.exists():
        return
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-200:]:
            line = line.strip()
            if line:
                ingest_bus_event(state, line)
    except Exception:
        pass


# ─── helpers ───────────────────────────────────────────────────────────────
def _seconds_since(iso_ts: Optional[str]) -> int:
    if not iso_ts:
        return 0
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


# ─── rendering ─────────────────────────────────────────────────────────────
def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _risk_color(risk: str) -> str:
    return RISK_COLORS.get(risk.lower(), "dim")


def _risk_label(risk: str) -> str:
    return risk.upper()[:6]


def _phase_color(phase: str) -> str:
    return PHASE_COLORS.get(phase.lower(), "dim")


def panel_header(state: LoomieState) -> Panel:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column("phase", ratio=1)
    table.add_column("count", justify="right")

    phases = ["queued", "container_init", "agent_active", "gates", "complete", "failed"]
    counts = state.phase_counts()
    for phase in phases:
        n = counts.get(phase, 0)
        color = _phase_color(phase)
        label = phase.replace("_", " ")
        table.add_row(
            Text(label, style=color),
            Text(str(n), style=f"bold {color}" if n else "dim"),
        )

    total = len(state.heartbeats)
    table.add_row(Text(""), Text())
    table.add_row(
        Text("total", style="white"),
        Text(str(total), style="bold white" if total else "dim"),
    )

    return Panel(
        table,
        title="[bold]loomies[/]  [dim](by phase)[/]",
        border_style="magenta",
        padding=(0, 1),
    )


def panel_body(state: LoomieState) -> Panel:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column("id",      width=8,  no_wrap=True)
    table.add_column("project", width=10, no_wrap=True)
    table.add_column("phase",   width=14, no_wrap=True)
    table.add_column("age",     width=6,  justify="right", no_wrap=True)
    table.add_column("risk",    width=6,  justify="center", no_wrap=True)
    table.add_column("tokens",  width=12, justify="right", no_wrap=True)
    table.add_column("cost",    width=8,  justify="right", no_wrap=True)

    if not state.heartbeats:
        table.add_row(
            Text("(no active loomies)", style="dim"),
            "", "", "", "", "", "",
        )
    else:
        for hb in state.heartbeats:
            row_style = "bold yellow" if hb.is_stuck else ""
            age_str = _format_age(hb.phase_age_seconds)
            tokens_str = f"{hb.tokens_input_total:,}/{hb.tokens_output_total:,}"
            cost_str = f"${hb.estimated_cost_usd:.4f}"
            risk_color = _risk_color(hb.kill_risk)
            risk_text = Text(_risk_label(hb.kill_risk), style=risk_color)
            phase_color = _phase_color(hb.phase)
            table.add_row(
                Text(hb.task_id[:8], style=row_style),
                Text(hb.project, style=row_style),
                Text(hb.phase.replace("_", " "), style=f"{phase_color}{' bold' if row_style else ''}"),
                Text(age_str, style=row_style),
                risk_text,
                Text(tokens_str, style=f"dim{row_style}"),
                Text(cost_str, style=f"dim{row_style}"),
            )

    return Panel(
        table,
        title="[bold]active loomies[/]  [dim](stuck=yellow)[/]",
        border_style="cyan",
        padding=(0, 1),
    )


def panel_footer(state: LoomieState) -> Panel:
    cost_hr = state.total_cost_per_hr()
    stuck = state.stuck_count()
    fails = len(state.recent_fails)

    pieces = [
        Text("cc-loomies", style="bold magenta"),
        Text(f"  ●  ${cost_hr:.2f}/hr  ", style="cyan"),
        Text(f"stuck {stuck}  ",
             style="bold yellow" if stuck else "dim"),
        Text(f"recent_fails {fails}  ",
             style="bright_red" if fails else "dim"),
        Text(f"poll_err {state.poll_errors}", style="dim" if not state.poll_errors else "bright_red"),
    ]
    return Panel(Text.assemble(*pieces), border_style="dim", padding=(0, 1))


def build_layout(state: LoomieState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=10),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["header"].update(panel_header(state))
    layout["body"].update(panel_body(state))
    layout["footer"].update(panel_footer(state))
    return layout


# ─── main loop ─────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="cc-loomies — active-loomie panel")
    p.add_argument("--tick", type=float, default=TICK_S,
                   help=f"poll interval (default {TICK_S}s)")
    p.add_argument("--source", default="hearth-loom",
                   choices=["hearth-loom", "bus"],
                   help="data source (default: hearth-loom)")
    p.add_argument("--log", type=Path, default=DEFAULT_LOG,
                   help="debug.jsonl path when source=bus")
    p.add_argument("--once", action="store_true",
                   help="render one frame and exit (test/screenshot)")
    args = p.parse_args()

    state = LoomieState()

    if args.source == "bus":
        read_bus_lines(state, args.log)

    console = Console()

    if args.once:
        if args.source == "hearth-loom":
            state.heartbeats = poll_hearth_loom()
            state.last_poll_at = time.time()
        console.print(build_layout(state))
        return 0

    try:
        with Live(build_layout(state), console=console,
                  refresh_per_second=1, screen=True) as live:
            while True:
                if args.source == "hearth-loom":
                    hbs = poll_hearth_loom()
                    if hbs:
                        state.heartbeats = hbs
                        state.poll_errors = 0
                    else:
                        state.poll_errors += 1
                    state.last_poll_at = time.time()
                else:
                    ingest_bus_event(state, "")
                live.update(build_layout(state))
                time.sleep(args.tick)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        console.print(f"[red]cc-loomies crashed:[/] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())