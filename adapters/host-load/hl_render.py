"""Text, sparkline and self-contained HTML/SVG rendering of host-load results."""
import html
import time
from collections import defaultdict

BARS = "▁▂▃▄▅▆▇█"
PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1",
           "#ff9da7", "#9c755f", "#bab0ac"]
GREY = "#8c8c8c"


def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def sparkline(values, width=40, top=None):
    values = list(values)[-width:]
    if not values:
        return ""
    hi = top if top is not None else max(values)
    if hi <= 0:
        return BARS[0] * len(values)
    return "".join(BARS[min(7, int(max(v, 0) / hi * 7.999))] for v in values)


def render_text(s, top=8):
    psi = s["psi"]
    lines = [f"host load  ({s['nprocs']} procs, {s['wall_s']:.1f}s window)",
             "  PSI some avg10/60:  cpu {:.1f}/{:.1f}%   mem {:.1f}/{:.1f}%   io {:.1f}/{:.1f}%".format(
                 psi.get("cpu_some_avg10", 0), psi.get("cpu_some_avg60", 0),
                 psi.get("memory_some_avg10", 0), psi.get("memory_some_avg60", 0),
                 psi.get("io_some_avg10", 0), psi.get("io_some_avg60", 0)),
             "", f"  {'PROJECT':34} {'CORES':>6} {'RSS':>8} {'SWAP':>8} {'PROCS':>6}  TOP"]
    rows = sorted(s["projects"].items(), key=lambda kv: (-kv[1]["cores"], -kv[1]["rss"]))[:top]
    for name, r in rows:
        tops = ", ".join(f"{t['comm']}:{t['pid']}" for t in r["top"] if t["cores"] > 0.01)
        lines.append(f"  {name[:34]:34} {r['cores']:6.2f} {human(r['rss']):>8} {human(r['swap']):>8} "
                     f"{r['nproc']:6d}  {tops}")
    if s.get("unknown_share"):
        lines.append(f"  unknown (no cwd/cmdline/cgroup match) = {s['unknown_share']*100:.0f}% of sampled cpu")
    lines += ["", "  flagged:" if s["findings"] else "  flagged: nothing"]
    for f in s["findings"]:
        lines.append(f"  [{f['severity']:4}] {f['kind']:15} {f['project']}: {f['summary']}")
        if f["pids"]:
            lines.append(f"         pids {f['pids']}")
    return "\n".join(lines)


def _scale(vals, lo, hi, size):
    span = (hi - lo) or 1
    return [(v - lo) / span * size for v in vals]


def _polyline(points, color, dash=""):
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (f'<polyline fill="none" stroke="{color}" stroke-width="1.6" {dash} points="{pts}"/>')


def svg_psi(series, w=900, h=220, pad=40):
    if not series:
        return "<p>no PSI samples</p>"
    t0, t1 = series[0]["ts"], series[-1]["ts"]
    ymax = max(10.0, max(max(r[k] or 0 for k in ("cpu_some10", "mem_some10", "io_some10")) for r in series))
    ymax = min(100.0, ymax * 1.1)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="chart">']
    for frac in (0, .25, .5, .75, 1):
        y = h - pad - frac * (h - 2 * pad)
        parts.append(f'<line x1="{pad}" x2="{w-pad}" y1="{y:.1f}" y2="{y:.1f}" stroke="#333"/>'
                     f'<text x="4" y="{y+4:.1f}" fill="#999" font-size="10">{frac*ymax:.0f}%</text>')
    for key, color, dash in (("cpu_some10", "#e15759", ""), ("mem_some10", "#4e79a7", ""),
                             ("io_some10", "#59a14f", ""), ("mem_full10", "#4e79a7", 'stroke-dasharray="4 3"'),
                             ("io_full10", "#59a14f", 'stroke-dasharray="4 3"')):
        pts = [(pad + (r["ts"] - t0) / ((t1 - t0) or 1) * (w - 2 * pad),
                h - pad - (r[key] or 0) / ymax * (h - 2 * pad)) for r in series]
        parts.append(_polyline(pts, color, dash))
    parts.append(_axis_labels(t0, t1, w, h, pad))
    parts.append("</svg>")
    return "".join(parts)


def _axis_labels(t0, t1, w, h, pad):
    out = []
    for frac in (0, .5, 1):
        t = t0 + (t1 - t0) * frac
        out.append(f'<text x="{pad + frac*(w-2*pad):.0f}" y="{h-pad+16}" fill="#999" font-size="10" '
                   f'text-anchor="middle">{time.strftime("%m-%d %H:%M", time.localtime(t))}</text>')
    return "".join(out)


def stacked(project_series, key_idx=1, w=900, h=260, pad=40, top=8, unit="cores"):
    """Stacked area over the union of timestamps; biggest by integral get colours, rest fold to ~other."""
    if not project_series:
        return "<p>no samples</p>", []
    stamps = sorted({t for rows in project_series.values() for t, *_ in rows})
    t0, t1 = stamps[0], stamps[-1]
    area = {p: sum(r[key_idx] for r in rows) for p, rows in project_series.items()}
    ranked = [p for p, _ in sorted(area.items(), key=lambda kv: -kv[1]) if p != "unknown" and p != "~other"]
    keep = ranked[:top]
    layers = keep + [p for p in ("unknown", "~other") if p in project_series]
    cols = {t: defaultdict(float) for t in stamps}
    for p, rows in project_series.items():
        tgt = p if p in layers else "~other"
        if tgt not in layers:
            layers.append(tgt)
        for row in rows:
            cols[row[0]][tgt] += row[key_idx]
    ymax = max(sum(c.values()) for c in cols.values()) or 1.0
    ymax *= 1.1
    X = lambda t: pad + (t - t0) / ((t1 - t0) or 1) * (w - 2 * pad)
    Y = lambda v: h - pad - v / ymax * (h - 2 * pad)
    base = {t: 0.0 for t in stamps}
    parts = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="chart">']
    legend = []
    for i, p in enumerate(layers):
        color = GREY if p in ("unknown", "~other") else PALETTE[i % len(PALETTE)]
        upper = {t: base[t] + cols[t].get(p, 0.0) for t in stamps}
        top_pts = [(X(t), Y(upper[t])) for t in stamps]
        bot_pts = [(X(t), Y(base[t])) for t in reversed(stamps)]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in top_pts + bot_pts)
        parts.append(f'<polygon points="{pts}" fill="{color}" fill-opacity="0.85">'
                     f'<title>{html.escape(p)}</title></polygon>')
        legend.append((p, color, sum(cols[t].get(p, 0.0) for t in stamps) / len(stamps)))
        base = upper
    for frac in (0, .5, 1):
        label = f"{frac*ymax:.1f}c" if unit == "cores" else human(frac * ymax)
        parts.append(f'<text x="4" y="{Y(frac*ymax)+4:.1f}" fill="#999" font-size="10">{label}</text>')
    parts.append(_axis_labels(t0, t1, w, h, pad))
    parts.append("</svg>")
    return "".join(parts), legend


def render_html(psi_series, project_series, findings, hours):
    cpu_svg, cpu_leg = stacked({k: list(v) for k, v in project_series.items()}, 1)
    rss_svg, rss_leg = stacked({k: list(v) for k, v in project_series.items()}, 2, unit="bytes")

    def leg(items, fmt):
        return "<ul>" + "".join(
            f'<li><span style="background:{c}"></span>{html.escape(p)} <em>{fmt(v)}</em></li>'
            for p, c, v in items) + "</ul>"

    rows = "".join(
        f'<tr class="{html.escape(f["severity"])}"><td>{time.strftime("%H:%M:%S", time.localtime(f["ts"]))}</td>'
        f'<td>{html.escape(f["severity"])}</td><td>{html.escape(f["kind"])}</td>'
        f'<td>{html.escape(f["project"] or "")}</td><td>{html.escape(f["summary"])}</td></tr>'
        for f in findings)
    return f"""<!doctype html><meta charset="utf-8"><title>host load</title>
<style>body{{background:#161616;color:#ddd;font:13px sans-serif;margin:20px}}
h2{{margin:22px 0 4px}}ul{{list-style:none;padding:0;columns:3}}li span{{display:inline-block;width:10px;
height:10px;margin-right:6px}}em{{color:#999}}.chart{{background:#1e1e1e}}table{{border-collapse:collapse}}
td{{padding:2px 8px;border-bottom:1px solid #333}}.crit td{{color:#ff8080}}.warn td{{color:#f0c060}}</style>
<h1>host load, last {hours:g}h</h1>
<h2>pressure (PSI some avg10; dashed = full)</h2>{svg_psi(psi_series)}
<p><b style="color:#e15759">cpu</b> <b style="color:#4e79a7">memory</b> <b style="color:#59a14f">io</b></p>
<h2>CPU by project (cores, stacked)</h2>{cpu_svg}{leg(cpu_leg, lambda v: f"{v:.2f} avg")}
<h2>RSS by project (stacked)</h2>{rss_svg}{leg(rss_leg, human)}
<h2>flagged</h2><table>{rows or "<tr><td>nothing flagged in window</td></tr>"}</table>"""
