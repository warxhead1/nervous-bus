"""--tune: distributions and fire rates come out of a real History file."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_store  # noqa: E402
import hl_tune  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sample(ts, cpu60, mem10, swap, kswapd):
    findings = []
    if swap is not None:
        findings.append({"kind": "memory_pressure", "severity": "warn", "project": "p", "summary": "m",
                         "cpu_cores": 0.0, "rss_bytes": 0,
                         "evidence": {"swap_pages_per_s": swap, "kswapd_cores": kswapd}})
    return {"ts": ts, "psi": {"cpu_some_avg60": cpu60, "memory_some_avg10": mem10}, "mem": {},
            "projects": {}, "findings": findings}


class TuneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "h.sqlite3")
        h = hl_store.History(self.path)
        now = time.time()
        for i in range(100):
            h.record(sample(now - 100 + i, cpu60=float(i), mem10=i / 10,
                            swap=1000.0 * i if i >= 50 else None, kswapd=i / 100))
        h.db.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_quantile_interpolates(self):
        self.assertEqual(hl_tune.quantile([0, 10], 0.5), 5)
        self.assertIsNone(hl_tune.quantile([], 0.5))

    def test_distributions_and_censoring(self):
        res = hl_tune.analyse(hl_tune.load(self.path))
        self.assertEqual(res["samples"], 100)
        self.assertEqual(res["censored"], 50)
        self.assertAlmostEqual(res["series"]["cpu_some60"]["p50"], 49.5, places=1)
        self.assertEqual(res["series"]["swap_pages_per_s"]["n"], 50)

    def test_fire_rate_counts_against_all_samples(self):
        res = hl_tune.analyse(hl_tune.load(self.path), {"cpu_psi_some60": 90.0, "swap_pages_per_s": 90000.0})
        self.assertAlmostEqual(res["thresholds"]["cpu_psi_some60"]["fire_rate"], 0.10)
        self.assertAlmostEqual(res["thresholds"]["swap_pages_per_s"]["fire_rate"], 0.10)

    def test_empty_history_does_not_crash(self):
        empty = os.path.join(self.dir.name, "e.sqlite3")
        hl_store.History(empty).db.close()
        res = hl_tune.analyse(hl_tune.load(empty))
        self.assertEqual(res["samples"], 0)
        self.assertIn("samples: 0", hl_tune.render(res))

    def test_cli_json(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "who-is-loading"), "--tune", "--json",
                              "--db", self.path], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["samples"], 100)


if __name__ == "__main__":
    unittest.main()
