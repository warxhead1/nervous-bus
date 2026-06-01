#!/usr/bin/env python3
"""cc-agents — active-agent sessions pane for command-center.

Polls CCM v2 /api/v2/ccm/sessions every 5s and renders a table:
  agent | project | bead | msgs

The raw pane_id (e.g. 'command-center:10') is useless without context.
We try to enrich each session by:
  1. Extracting project from pane_id (zellij:<session_name>:<pane_n> — session_name is the project)
  2. Fetching /api/v2/loom/tasks in parallel and joining on execution_id
     to get bead title + project name.

Layouts that publish agent sessions: default, command-center, tengine,
hearth-loom, home-automation, tachyonos, deer-flow, mobile, cli-wall,
rooftop, system-monitor, _status, _loom-extras.

Usage:
  cc-agents                       # default tick 5s
  cc-agents --tick 2              # custom interval
  cc-agents --once                # one-shot render (test/screenshot)
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ─── constants ─────────────────────────────────────────────────────────────
TICK_S = 5.0
CCM_BASE = "http://localhost:5908"
LOOM_BASE = "http://localhost:5908"
TIMEOUT = 3

# Projects whose zellij session names are the project name
KNOWN_PROJECTS = frozenset([
    "hearth-loom", "tengine", "nervous-bus", "home-automation",
    "tachyonos", "deer-flow", "mobile", "cli-wall", "rooftop",
    "system-monitor", "_status", "_loom-extras", "default",
])


# ─── types ─────────────────────────────────────────────────────────────────
@dataclass
class SessionInfo:
    pane_id: str
    agent_type: str
    state: str
    message_count: int
    started_at: str
    execution_id: Optional[str]
    task_id: Optional[str]
    title: Optional[str]
    project: str = "?"
    bead: str = ""

    @property
    def agent(self) -> str:
        if self.agent_type == "claudecode":
            return "claude-code"
        if self.agent_type == "opencode":
            return "opencode"
        return self.agent_type or "?"


# ─── CCM fetching ────────────────────────────────────────────────────────────
def fetch_sessions() -> List[SessionInfo]:
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(TIMEOUT),
             f"{CCM_BASE}/api/v2/ccm/sessions"],
            capture_output=True, text=True, timeout=TIMEOUT + 1,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        # CCM v2 returns a top-level list; tolerate either shape so the pane
        # survives a future wrap in {"sessions": [...]}.
        raw = data if isinstance(data, list) else (data.get("sessions") or [])
        sessions = []
        for s in raw:
            pane_id = s.get("pane_id") or ""
            project = _derive_project(pane_id)
            sessions.append(SessionInfo(
                pane_id=pane_id,
                agent_type=s.get("agent_type") or "",
                state=s.get("state") or "",
                message_count=s.get("message_count") or 0,
                started_at=(s.get("started_at") or "")[11:19],
                execution_id=s.get("execution_id"),
                task_id=s.get("task_id"),
                title=s.get("title"),
                project=project,
            ))
        return sessions
    except Exception:
        return []


def fetch_loom_tasks(execution_ids: List[str]) -> Dict[str, dict]:
    """Fetch /api/v2/loom/tasks and index by execution_id."""
    if not execution_ids:
        return {}
    ids_param = ",".join(execution_ids)
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(TIMEOUT),
             f"{LOOM_BASE}/api/v2/loom/tasks?execution_id={ids_param}"],
            capture_output=True, text=True, timeout=TIMEOUT + 1,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        tasks = data.get("tasks") or []
        out = {}
        for t in tasks:
            eid = t.get("execution_id")
            if eid:
                out[eid] = t
        return out
    except Exception:
        return {}


# ─── project derivation ────────────────────────────────────────────────────
def _derive_project(pane_id: str) -> str:
    """Extract project name from pane_id.

    pane_id formats:
      zellij:<session_name>:<pane_n>  → session_name is the project
      docker:hearth-loom-agent-<id>   → hearth-loom
      host:<hostname>:<pane_n>         → use hostname
    """
    if not pane_id:
        return "?"
    parts = pane_id.split(":")
    if len(parts) >= 2 and parts[0] == "zellij":
        return parts[1]
    if len(parts) >= 2 and parts[0] == "docker":
        agent = parts[1]
        for proj in KNOWN_PROJECTS:
            if proj in agent:
                return proj
        return "loomie"
    if len(parts) >= 2 and parts[0] == "host":
        return parts[1]
    return pane_id.split(":")[0]


# ─── enrichment ─────────────────────────────────────────────────────────────
def enrich(sessions: List[SessionInfo]) -> List[SessionInfo]:
    """Populate bead + project fields by joining with loom tasks."""
    execution_ids = [s.execution_id for s in sessions if s.execution_id]
    if not execution_ids:
        return sessions
    tasks_by_eid = fetch_loom_tasks(execution_ids)
    for s in sessions:
        if s.execution_id and s.execution_id in tasks_by_eid:
            t = tasks_by_eid[s.execution_id]
            bead_title = t.get("title") or t.get("bead_title") or ""
            s.bead = bead_title[:40]
            proj = t.get("project")
            if proj:
                s.project = proj
    return sessions


# ─── filtering ─────────────────────────────────────────────────────────────
def filter_active(sessions: List[SessionInfo]) -> List[SessionInfo]:
    # CCM v2 returns every zellij pane (including plain shells with empty
    # agent_type). A real coding-agent session is identified by having an
    # agent_type set — that's how the gateway tags claude/opencode panes.
    # Idle is a normal alive state, not a terminal one.
    LIVE = ("active", "running", "busy", "idle")
    return [s for s in sessions if s.state in LIVE and (s.agent_type or "").strip()]


# ─── rendering ─────────────────────────────────────────────────────────────
AGENT_COLORS = {
    "claude-code": "cyan",
    "opencode":    "magenta",
    "codex":       "yellow",
    "gemini":      "green",
    "cursor":      "blue",
    "?":           "dim",
}

STATE_COLORS = {
    "active":  "green",
    "running": "green",
    "busy":    "yellow",
    "idle":    "dim",
    "stopped": "dim",
    "?":       "dim",
}


def _agent_color(agent: str) -> str:
    return AGENT_COLORS.get(agent, "dim")


def _state_color(state: str) -> str:
    return STATE_COLORS.get(state.lower(), "dim")


def render_table(sessions: List[SessionInfo]) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column("agent",    width=12, no_wrap=True)
    table.add_column("project",  width=16, no_wrap=True)
    table.add_column("bead",     overflow="ellipsis", no_wrap=True)
    table.add_column("state",    width=8,  no_wrap=True)
    table.add_column("msgs",     width=5,  justify="right", no_wrap=True)
    table.add_column("pane",     width=22, no_wrap=True)

    if not sessions:
        table.add_row(
            Text("(no active sessions)", style="dim"),
            "", "", "", "", "",
        )
        return table

    for s in sessions:
        a_col = _agent_color(s.agent)
        st_col = _state_color(s.state)
        table.add_row(
            Text(s.agent, style=a_col),
            Text(s.project, style="bright_white"),
            Text(s.bead or "—", style="dim"),
            Text(s.state, style=st_col),
            Text(str(s.message_count), style="dim"),
            Text(s.pane_id, style="dim"),
        )
    return table


def panel_cc_agents(sessions: List[SessionInfo], poll_errors: int) -> Panel:
    active = filter_active(sessions)
    total = len(sessions)
    table = render_table(sessions)
    title = f"[bold]agents[/]  [dim]{len(active)} active / {total} total[/]"
    footer = Text()
    footer.append("cc-agents", style="bold cyan")
    footer.append(f"  ●  err {poll_errors}", style="dim")
    panel = Panel(
        table,
        title=title,
        border_style="cyan",
        padding=(0, 1),
    )
    return panel


# ─── main ──────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="cc-agents — active-agent sessions pane")
    p.add_argument("--tick", type=float, default=TICK_S,
                   help=f"poll interval (default {TICK_S}s)")
    p.add_argument("--once", action="store_true",
                   help="render one frame and exit (test/screenshot)")
    args = p.parse_args()

    console = Console()

    if args.once:
        sessions = enrich(fetch_sessions())
        console.print(panel_cc_agents(sessions, 0))
        return 0

    try:
        from rich.live import Live
        from rich.layout import Layout

        poll_errors = 0
        last_json = ""
        sessions: List[SessionInfo] = []

        def _refresh() -> bool:
            nonlocal sessions, last_json, poll_errors
            try:
                new_sessions = enrich(fetch_sessions())
                new_json = json.dumps(
                    [(s.pane_id, s.state, s.message_count, s.bead, s.project)
                     for s in new_sessions]
                )
                if new_json == last_json:
                    return False
                last_json = new_json
                sessions = new_sessions
                return True
            except Exception:
                poll_errors += 1
                return False

        def buildLayout() -> Layout:
            layout = Layout()
            layout.split_column(
                Layout(name="body", ratio=1),
                Layout(name="footer", size=3),
            )
            layout["body"].update(panel_cc_agents(sessions, poll_errors))
            footer = Text()
            footer.append("cc-agents", style="bold cyan")
            footer.append(f"  ●  tick {args.tick:.1f}s  ", style="dim")
            footer.append(f"err {poll_errors}", style="dim")
            layout["footer"].update(
                Panel(footer, border_style="dim", padding=(0, 1))
            )
            return layout

        _refresh()
        with Live(buildLayout(), console=console,
                  refresh_per_second=1, screen=True) as live:
            while True:
                if _refresh():
                    live.update(buildLayout())
                time.sleep(args.tick)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        console.print(f"[red]cc-agents crashed:[/] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
