"""Dashboard tab 5 — host load by project, drawn from the host-load adapter's history."""
import sys
import time
from pathlib import Path

from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

_ADAPTER = Path(__file__).resolve().parents[1] / "host-load"
if str(_ADAPTER) not in sys.path:
    sys.path.insert(0, str(_ADAPTER))

import hl_render  # noqa: E402
import hl_store  # noqa: E402

SEV = {"crit": "bold red", "warn": "yellow", "info": "dim"}
_history = {}


def _hist(path):
    if path not in _history:
        _history[path] = hl_store.History(path)
    return _history[path]


def panel_psi(h, since):
    series = h.psi_series(since)
    t = Text()
    if not series:
        t.append("no samples yet — enable host-load.timer or run: who-is-loading --record", style="dim")
    for key, label in (("cpu_some10", "cpu "), ("mem_some10", "mem "), ("io_some10", "io  ")):
        vals = [r[key] or 0 for r in series]
        if vals:
            t.append(f"{label} {hl_render.sparkline(vals, 60, top=100)}  now {vals[-1]:5.1f}%  "
                     f"max {max(vals):5.1f}%\n")
    return Panel(t, title="[bold]pressure[/] [dim](PSI some avg10, scale 0-100%)[/]", border_style="dim")


def panel_projects(h, since, rows=12):
    series = h.project_series(since)
    ranked = sorted(series.items(), key=lambda kv: -sum(c for _, c, _ in kv[1]))[:rows]
    t = Text()
    t.append(f"{'PROJECT':34} {'NOW':>6} {'AVG':>6} {'RSS':>8}  CORES OVER TIME\n", style="bold")
    top = max([c for _, v in ranked for _, c, _ in v] or [1.0])
    for name, v in ranked:
        cores = [c for _, c, _ in v]
        t.append(f"{name[:34]:34} {cores[-1]:6.2f} {sum(cores)/len(cores):6.2f} "
                 f"{hl_render.human(v[-1][2]):>8}  {hl_render.sparkline(cores, 50, top=top)}\n")
    return Panel(t, title="[bold]cpu by project[/]", border_style="dim")


def panel_findings(h, since):
    t = Text()
    seen = set()
    for f in h.recent_findings(since, 60):
        key = (f["kind"], f["project"])
        if key in seen:
            continue
        seen.add(key)
        t.append(f"{time.strftime('%H:%M', time.localtime(f['ts']))} [{f['severity']}] ", style=SEV[f["severity"]])
        t.append(f"{f['kind']} {f['project']}: {f['summary']}\n")
    if not seen:
        t.append("nothing flagged in window", style="dim")
    return Panel(t, title="[bold]flagged[/]", border_style="dim")


def build_load_layout(db_path=None, hours=1.0):
    h = _hist(str(db_path or hl_store.DEFAULT_DB))
    since = time.time() - hours * 3600
    layout = Layout()
    layout.split_column(Layout(name="body", ratio=1), Layout(name="footer", size=3))
    layout["body"].split_column(Layout(panel_psi(h, since), size=7),
                                Layout(panel_projects(h, since), ratio=2),
                                Layout(panel_findings(h, since), ratio=1))
    return layout
