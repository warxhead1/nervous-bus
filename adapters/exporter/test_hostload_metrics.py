"""Host-load gauges: built from a real History file, scraped through both exporter backends."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host-load"))
import hl_store  # noqa: E402

from adapters.exporter import hostload_metrics as hm  # noqa: E402


def make_sample(ts, scale=1.0):
    return {
        "ts": ts, "psi": {"cpu_some_avg10": 40.0, "cpu_some_avg60": 30.0, "memory_some_avg10": 5.0,
                          "memory_full_avg10": 1.0, "io_some_avg10": 60.0, "io_full_avg10": 20.0},
        "mem": {"swap_total": 100, "swap_free": 40, "mem_available": 7},
        "projects": {
            "hearth": {"cores": 2.0 * scale, "cores_int": 3.0 * scale, "rss": 3 << 30, "anon": 2 << 30,
                       "swap": 1 << 20, "nproc": 30, "majflt_s": 10.0, "majflt_int": 12.0},
            'we"ird\nname': {"cores": 1.0, "cores_int": None, "rss": 1 << 20, "anon": 1 << 19, "swap": 0,
                             "nproc": 2, "majflt_s": 0.0, "majflt_int": None},
            "unknown": {"cores": 0.1, "cores_int": 0.1, "rss": 0, "anon": 0, "swap": 0, "nproc": 2,
                        "majflt_s": 0.0, "majflt_int": 0.0},
        },
        "findings": [{"kind": "orphan_cpu", "severity": "crit", "project": "hearth", "summary": "x",
                      "cpu_cores": 5.0, "rss_bytes": 1, "evidence": {}}],
        "agents": [{"agent": "hearth/agent-aa", "kinds": ["claude"], "cores": 1.0, "cores_int": 2.5, "rss": 9,
                    "nproc": 4, "orphans": 3}],
        "interval": {"interval_s": 60, "observed_cores": 9.0, "system_busy_cores": 10.0, "coverage": 0.9,
                     "unattributed_cores": 1.0},
        "paging": {"swap_in_pages_s": 5.0, "swap_out_pages_s": 7.0},
    }


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "h.sqlite3"
    h = hl_store.History(str(path))
    h.record(make_sample(time.time() - 30))
    h.db.close()
    return path


def value(text, metric, **labels):
    for line in text.splitlines():
        m = re.match(rf"^{re.escape(metric)}(?:\{{(.*)\}})? (\S+)$", line)
        if m and all(f'{k}="{v}"' in (m.group(1) or "") for k, v in labels.items()):
            return float(m.group(2))
    raise AssertionError(f"{metric}{labels} not in output")


def test_per_project_gauges_prefer_interval_and_cover_the_asked_fields(db):
    text = hm.render_text(hm.collect(db))
    assert value(text, "nbus_host_load_project_cpu_cores", project="hearth") == 3.0
    assert value(text, "nbus_host_load_project_cpu_instant_cores", project="hearth") == 2.0
    assert value(text, "nbus_host_load_project_rss_bytes", project="hearth") == 3 << 30
    assert value(text, "nbus_host_load_project_swap_bytes", project="hearth") == 1 << 20
    share = value(text, "nbus_host_load_project_cpu_share_ratio", project="hearth")
    assert 0.7 < share < 0.8                       # 3.0 / (3.0 + 1.0 + 0.1)
    assert value(text, "nbus_host_load_findings", kind="orphan_cpu", severity="crit", project="hearth") == 1
    assert value(text, "nbus_host_load_agent_orphan_processes", agent="hearth/agent-aa") == 3
    assert value(text, "nbus_host_load_psi_some_avg10_percent", resource="memory") == 5.0
    assert value(text, "nbus_host_load_cpu_coverage_ratio") == 0.9
    assert value(text, "nbus_host_load_up") == 1


def test_label_values_are_escaped(db):
    text = hm.render_text(hm.collect(db))
    assert 'project="we\\"ird\\nname"' in text


def test_stale_sample_exposes_only_age_and_up_zero(tmp_path):
    path = tmp_path / "h.sqlite3"
    h = hl_store.History(str(path))
    h.record(make_sample(time.time() - 3600))
    h.db.close()
    text = hm.render_text(hm.collect(path))
    assert value(text, "nbus_host_load_up") == 0
    assert "nbus_host_load_project_cpu_cores" not in text


def test_missing_database_is_up_zero_not_an_error(tmp_path):
    text = hm.render_text(hm.collect(tmp_path / "nope.sqlite3"))
    assert value(text, "nbus_host_load_up") == 0


def test_departed_project_leaves_no_stale_label(tmp_path):
    path = tmp_path / "h.sqlite3"
    h = hl_store.History(str(path))
    h.record(make_sample(time.time() - 120))
    later = make_sample(time.time() - 10)
    del later["projects"]["hearth"]
    h.record(later)
    h.db.close()
    assert 'project="hearth"' not in hm.render_text(hm.collect(path)).split("nbus_host_load_findings")[0]


def test_both_exporter_backends_serve_the_gauges(db, monkeypatch):
    from adapters.exporter import prometheus_exporter as pe
    reg = pe.MetricRegistry()
    hm.attach(reg, db)
    assert 'nbus_host_load_project_cpu_cores{project="hearth"} 3.0' in reg.generate()
    monkeypatch.setattr(pe, "_HAS_PROM", False)
    text_reg = pe.MetricRegistry()
    hm.attach(text_reg, db)
    assert 'nbus_host_load_project_cpu_cores{project="hearth"} 3.0' in text_reg.generate()


EXPORTER_DIR = Path(__file__).resolve().parent


def test_dashboard_and_rules_only_reference_metrics_the_exporter_emits(tmp_path):
    import json
    h = hl_store.History(str(tmp_path / "n.sqlite3"))
    h.record(make_sample(time.time() - 5))
    h.db.close()
    emitted = {n for n, _, _ in hm.collect(tmp_path / "n.sqlite3")}
    dash = json.loads((EXPORTER_DIR / "dashboards/nervous-bus-host-load.json").read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p["targets"]]
    exprs += re.findall(r"expr: (.*)", (EXPORTER_DIR / "prometheus/host-load.rules.yml").read_text())
    used = {m for e in exprs for m in re.findall(r"nbus_host_load_[a-z_0-9]+", e)}
    assert used and used <= emitted, used - emitted


def test_rules_pass_promtool():
    import shutil
    import subprocess
    if not shutil.which("promtool"):
        pytest.skip("promtool not installed")
    r = subprocess.run(["promtool", "check", "rules", str(EXPORTER_DIR / "prometheus/host-load.rules.yml")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
