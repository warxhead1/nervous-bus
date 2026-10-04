"""Threshold tuning from recorded history: distributions and the fire-rate of each pressure threshold.

Read-only. The memory-pressure signals (swap pages/s, kswapd cores) exist only on samples whose
finding fired, so their distribution is censored at the threshold that was live when recorded;
`censored` says how many samples that leaves out.
"""

from __future__ import annotations

import json
import sqlite3

import hl_detect

QUANTILES = (0.5, 0.75, 0.9, 0.95, 0.99, 1.0)
# threshold key -> (series name, quantile the suggestion sits on)
TARGETS = {
    "swap_pages_per_s": ("swap_pages_per_s", 0.9),
    "kswapd_cores": ("kswapd_cores", 0.9),
    "mem_psi_some10": ("mem_some10", 0.95),
    "cpu_psi_some60": ("cpu_some60", 0.95),
}


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = q * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def load(db_path, since=0.0):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    series = {"cpu_some60": [], "mem_some10": [], "io_some10": [], "swap_pages_per_s": [], "kswapd_cores": []}
    for cpu60, mem10, io10 in db.execute("SELECT cpu_some60, mem_some10, io_some10 FROM psi WHERE ts>=?", (since,)):
        series["cpu_some60"].append(cpu60 or 0.0)
        series["mem_some10"].append(mem10 or 0.0)
        series["io_some10"].append(io10 or 0.0)
    for (ev,) in db.execute("SELECT evidence FROM finding WHERE kind='memory_pressure' AND ts>=?", (since,)):
        try:
            e = json.loads(ev)
        except (TypeError, ValueError):
            continue
        series["swap_pages_per_s"].append(float(e.get("swap_pages_per_s", 0.0)))
        series["kswapd_cores"].append(float(e.get("kswapd_cores", 0.0)))
    db.close()
    return series


def nice(x):
    """Two significant figures, so a suggestion reads as a threshold rather than a measurement."""
    if x is None or x <= 0:
        return x
    return float(f"{x:.2g}")


def analyse(series, cfg=None):
    cfg = {**hl_detect.DEFAULTS, **(cfg or {})}
    n = len(series["cpu_some60"])
    out = {"samples": n, "series": {}, "thresholds": {}}
    for name, vals in series.items():
        out["series"][name] = {"n": len(vals), **{f"p{int(q * 100)}": quantile(vals, q) for q in QUANTILES}}
    out["censored"] = n - len(series["swap_pages_per_s"])
    for key, (name, q) in TARGETS.items():
        vals = series[name]
        cur = cfg[key]
        # fire rate over all samples: a sample with no recorded memory finding is below every old threshold
        fired = sum(1 for v in vals if v >= cur)
        out["thresholds"][key] = {"current": cur, "series": name, "fire_rate": fired / n if n else None,
                                  "suggest": nice(quantile(vals, q)), "suggest_quantile": q}
    return out


def render(res):
    lines = [f"samples: {res['samples']}   (memory-pressure series censored: {res['censored']} samples had no finding)", ""]
    hdr = f"{'series':<18}{'n':>5}" + "".join(f"{'p' + str(int(q * 100)):>10}" for q in QUANTILES)
    lines.append(hdr)
    for name, d in res["series"].items():
        cells = "".join(f"{'' if d[f'p{int(q * 100)}'] is None else format(d[f'p{int(q * 100)}'], '.2f'):>10}"
                        for q in QUANTILES)
        lines.append(f"{name:<18}{d['n']:>5}{cells}")
    lines += ["", f"{'threshold':<20}{'current':>10}{'fires':>8}{'suggest':>10}  (quantile)"]
    for key, t in res["thresholds"].items():
        fr = "-" if t["fire_rate"] is None else f"{t['fire_rate'] * 100:.0f}%"
        lines.append(f"{key:<20}{t['current']:>10}{fr:>8}{str(t['suggest']):>10}  p{int(t['suggest_quantile'] * 100)}")
    return "\n".join(lines)
