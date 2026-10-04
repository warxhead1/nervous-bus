import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console

import cc_hostload
import hl_store


def sample(ts, cores):
    return {"ts": ts, "psi": {"cpu_some_avg10": 40.0, "cpu_some_avg60": 50.0, "memory_some_avg10": 2.0,
                              "io_some_avg10": 5.0},
            "mem": {"swap_total": 10, "swap_free": 5, "mem_available": 7},
            "projects": {"hearth": {"cores": cores, "rss": 10**9, "swap": 0, "nproc": 3, "top": []},
                         "unknown": {"cores": 0.1, "rss": 10**6, "swap": 0, "nproc": 1, "top": []}},
            "findings": [{"kind": "orphan_cpu", "severity": "crit", "project": "hearth",
                          "summary": "5 orphan sh burning 5.00 cores", "cpu_cores": 5.0,
                          "rss_bytes": 1, "evidence": {}, "pids": [1]}],
            "agents": [{"agent": "hearth/agent-aa11", "kinds": ["claude"], "cores": cores, "cores_int": None,
                        "rss": 10**8, "nproc": 3, "orphans": 9}]}


def render(layout):
    console = Console(width=140, record=True, file=open(os.devnull, "w"))
    console.print(layout)
    return console.export_text()


class TestLoadTab(unittest.TestCase):
    def test_layout_renders_projects_and_findings(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "h.db")
            h = hl_store.History(db)
            now = time.time()
            for i in range(5):
                h.record(sample(now - (5 - i) * 60, 1.0 + i))
            text = render(cc_hostload.build_load_layout(db, hours=1))
            self.assertIn("hearth", text)
            self.assertIn("orphan_cpu", text)
            self.assertIn("pressure", text)
            self.assertIn("hearth/agent-aa11", text)

    def test_empty_history_shows_hint(self):
        with tempfile.TemporaryDirectory() as d:
            text = render(cc_hostload.build_load_layout(os.path.join(d, "e.db")))
            self.assertIn("no samples yet", text)


if __name__ == "__main__":
    unittest.main()
