import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_interval
import hl_procs
import hl_store
from test_host_load import TICK, Base


class TestFields(Base):
    def test_majflt_anon_read_bytes_and_cgroup_path_parsed(self):
        self.fp.add(10, "rustc", ppid=1145, cwd="/home/eric/projects/hearth", majflt=77, anon_kb=2048,
                    read_bytes=4096, cgroup="0::/user.slice/user@1000.service/app.slice/x.scope")
        p = hl_procs.scan(str(self.fp.root))[10]
        self.assertEqual((p.majflt, p.anon_bytes, p.read_bytes), (77, 2048 * 1024, 4096))
        self.assertTrue(p.cgroup_path.endswith("app.slice/x.scope"))

    def test_kernel_threads_are_bucketed_not_unknown(self):
        self.fp.add(2, "kthreadd", ppid=0, uid=0, cgroup="0::/")
        self.fp.add(141, "kswapd0", ppid=2, uid=0, cgroup="0::/")
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        self.assertEqual((procs[141].project, procs[141].basis), ("kernel", "kernel"))
        self.assertEqual(procs[2].project, "kernel")


class TestIntegrated(Base):
    def run_pair(self, between):
        """Two recorded runs 60s apart; `between(fp)` mutates the tree in the gap."""
        h = hl_store.History(os.path.join(self.tmp.name, "h.sqlite3"))
        self.fp.set_uptime(100000)
        s1 = self.fp.sample(prev=h.load_snapshot(), now=lambda: 1000.0)
        h.record(s1)
        between(self.fp)
        self.fp.set_uptime(100060)
        s2 = self.fp.sample(prev=h.load_snapshot(), now=lambda: 1060.0)
        return h, s1, s2

    def test_interval_mean_differs_from_window_mean(self):
        self.fp.add(100, "cc", ppid=1145, cwd="/home/eric/projects/alpha")

        def gap(fp):
            fp.burn([100], 40, cores=8.0)   # 8 cores for 40 of the 60 seconds, then idle
        h, s1, s2 = self.run_pair(gap)
        self.assertIsNone(s1["projects"]["alpha"]["cores_int"])
        row = s2["projects"]["alpha"]
        self.assertAlmostEqual(row["cores_int"], 8 * 40 / 60, places=2)
        self.assertEqual(row["cores"], 0.0)        # the 3s window caught nothing
        self.assertEqual(s2["interval"]["interval_s"], 60.0)

    def test_pid_reuse_and_new_process_in_gap(self):
        self.fp.add(100, "old", ppid=1145, cwd="/home/eric/projects/alpha", ticks=500, start=1000)

        def gap(fp):
            fp.add(100, "reused", ppid=1145, cwd="/home/eric/projects/alpha", ticks=300, start=100030 * TICK - 500)
            fp.add(101, "born", ppid=1145, cwd="/home/eric/projects/beta", ticks=1200, start=100050 * TICK)
        _, _, s2 = self.run_pair(gap)
        # pid 100 now names a different process: its own 300 ticks count, never (300 - 500)
        self.assertAlmostEqual(s2["projects"]["alpha"]["cores_int"], 3 / 60, places=3)
        self.assertAlmostEqual(s2["projects"]["beta"]["cores_int"], 12 / 60 * 1.0, places=3)

    def test_stale_or_rebooted_snapshot_gives_none(self):
        self.fp.add(100, "x", ppid=1145, cwd="/home/eric/projects/alpha")
        procs = hl_procs.scan(str(self.fp.root))
        snap = hl_interval.snapshot(procs, 1000.0, 100000, "boot-a", 0)
        out, info = hl_interval.integrate(snap, procs, 1000.0 + 3600, 103600, "boot-a", 0)
        self.assertIsNone(out)
        self.assertIn("old", info["reason"])
        out, info = hl_interval.integrate(snap, procs, 1060.0, 100060, "boot-b", 0)
        self.assertEqual(info["reason"], "rebooted")

    def test_coverage_reports_cpu_the_scan_could_not_see(self):
        self.fp.add(100, "cc", ppid=1145, cwd="/home/eric/projects/alpha")

        def gap(fp):
            fp.burn([100], 10, cores=1.0)
            fp.write_stat(extra=10 * TICK)    # 10 more core-seconds burned by processes that already exited
        _, _, s2 = self.run_pair(gap)
        iv = s2["interval"]
        self.assertAlmostEqual(iv["coverage"], 0.5, places=2)
        self.assertAlmostEqual(iv["unattributed_cores"], 10 / 60, places=2)

    def test_reaped_children_are_credited_to_the_parent(self):
        self.fp.add(100, "cargo", ppid=1145, cwd="/home/eric/projects/alpha")

        def gap(fp):                       # 30 core-seconds of rustc children exited and were wait()ed
            fp.procs[100]["child"] += 30 * TICK
            fp.write(100)
            fp.write_stat(extra=30 * TICK)
        _, _, s2 = self.run_pair(gap)
        self.assertAlmostEqual(s2["projects"]["alpha"]["cores_int"], 0.5, places=2)
        self.assertAlmostEqual(s2["interval"]["coverage"], 1.0, places=2)

    def test_child_dying_in_gap_does_not_hand_its_whole_life_to_the_parent(self):
        self.fp.add(100, "make", ppid=1145, cwd="/home/eric/projects/alpha")
        self.fp.add(101, "gcc", ppid=100, cwd="/home/eric/projects/alpha", ticks=100 * TICK)  # 100s before the gap

        def gap(fp):                       # gcc burns 20 more seconds, exits; make reaps all 120
            fp.remove(101)
            fp.procs[100]["child"] += 120 * TICK
            fp.write(100)
            fp.write_stat(extra=20 * TICK)
        _, _, s2 = self.run_pair(gap)
        self.assertAlmostEqual(s2["projects"]["alpha"]["cores_int"], 20 / 60, places=2)

    def test_snapshot_roundtrip_migration_and_only_latest_kept(self):
        db = os.path.join(self.tmp.name, "old.sqlite3")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE project (ts REAL NOT NULL, project TEXT NOT NULL, cores REAL, rss INTEGER,"
                    " swap INTEGER, nproc INTEGER, PRIMARY KEY(ts, project))")
        con.execute("INSERT INTO project VALUES (?, 'legacy',1.0,10,0,1)", (time.time(),))
        con.commit()
        con.close()
        h = hl_store.History(db)
        self.fp.add(100, "cc", ppid=1145, cwd="/home/eric/projects/alpha", majflt=5)
        h.record(self.fp.sample(now=lambda: 2000.0))
        self.fp.add(101, "cc2", ppid=1145, cwd="/home/eric/projects/alpha")
        h.record(self.fp.sample(now=lambda: 2060.0))
        snap = h.load_snapshot()
        self.assertEqual(snap["ts"], 2060.0)
        self.assertIn((101, 1000), snap["rows"])
        self.assertEqual(h.db.execute("SELECT COUNT(*) FROM snapshot_meta").fetchone()[0], 1)
        self.assertEqual(h.project_series(0)["legacy"][0][1], 1.0)   # legacy rows still readable


if __name__ == "__main__":
    unittest.main()
