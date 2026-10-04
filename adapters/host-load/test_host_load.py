import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_detect
import hl_procs
import hl_render
import hl_store

TICK = hl_procs.CLK_TCK
WT = "/home/eric/data2/worktrees/p1-epic-research-to-capital-readiness-reconcile/agent-aa9933e9"
DOCKER_CG = "0::/system.slice/docker-" + "a" * 64 + ".scope\n"


class FakeProc:
    """Writes a miniature /proc tree; tick() advances cumulative counters."""

    def __init__(self, root):
        self.root = Path(root)
        self.procs = {}
        (self.root / "pressure").mkdir(parents=True, exist_ok=True)
        self.set_psi(cpu60=10)
        self.set_uptime(100000)
        self.set_vm(0, 0)

    def set_uptime(self, s):
        (self.root / "uptime").write_text(f"{s} 1.0\n")

    def set_psi(self, cpu60=0.0, mem10=0.0):
        line = "some avg10={a:.2f} avg60={b:.2f} avg300=0.00 total=1\n"
        (self.root / "pressure" / "cpu").write_text(line.format(a=cpu60, b=cpu60))
        (self.root / "pressure" / "memory").write_text(
            line.format(a=mem10, b=mem10) + "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        (self.root / "pressure" / "io").write_text(line.format(a=0, b=0))

    def set_vm(self, pin, pout):
        (self.root / "vmstat").write_text(f"pswpin {pin}\npswpout {pout}\n")
        (self.root / "meminfo").write_text(
            "MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 100 kB\nSwapFree: 50 kB\n")

    def add(self, pid, comm, ppid=1, argv=(), cwd=None, cgroup="0::/user.slice/user-1000.slice/session-1.scope",
            uid=1000, ticks=0, start=1000, rss_pages=100, swap_kb=0, io=0, majflt=0, anon_kb=None,
            read_bytes=0, child=0):
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        self.procs[pid] = dict(comm=comm, ppid=ppid, ticks=ticks, start=start, rss=rss_pages, io=io, majflt=majflt,
                          read_bytes=read_bytes, child=child)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))
        (d / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nVmSwap:\t{swap_kb} kB\n"
                                   + (f"RssAnon:\t{anon_kb} kB\n" if anon_kb is not None else ""))
        (d / "cgroup").write_text(cgroup)
        if cwd is not None:
            link = d / "cwd"
            if link.is_symlink():
                link.unlink()
            os.symlink(cwd, link)
        self.write(pid)

    def write(self, pid):
        p = self.procs[pid]
        d = self.root / str(pid)
        f = ["S", p["ppid"]] + [0] * 7 + [p["majflt"], 0] + [p["ticks"], 0, p["child"], 0, 20, 0, 1, 0, p["start"], 0, p["rss"]]
        (d / "stat").write_text(f"{pid} ({p['comm']}) " + " ".join(map(str, f)) + "\n")
        (d / "io").write_text(f"rchar: {p['io']}\nwchar: 0\nread_bytes: {p['read_bytes']}\n")

    def burn(self, pids, seconds, cores=1.0):
        for pid in pids:
            self.procs[pid]["ticks"] += int(seconds * cores * TICK)
            self.write(pid)
        self.busy = getattr(self, "busy", 0) + int(seconds * cores * len(pids) * TICK)
        self.write_stat()

    def write_stat(self, extra=0):
        self.busy = getattr(self, "busy", 0) + extra
        (self.root / "stat").write_text(f"cpu  {self.busy} 0 0 999999 0 0 0 0 0 0\n")

    def remove(self, pid):
        """The process exits: its /proc entry disappears."""
        del self.procs[pid]
        d = self.root / str(pid)
        for f in list(d.iterdir()):
            f.unlink()
        d.rmdir()

    def fault(self, pid, n):
        self.procs[pid]["majflt"] += n
        self.write(pid)

    def sample(self, burners=(), cores=1.0, interval=10.0, docker=None, **kw):
        return hl_store.sample(
            str(self.root), interval=interval, docker=docker if docker is not None else {},
            sleep=lambda s: self.burn(burners, s, cores), **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fp = FakeProc(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.fp.add(1, "systemd", ppid=0, uid=0, cgroup="0::/init.scope")
        self.fp.add(1145, "systemd", ppid=1, argv=["/usr/lib/systemd/systemd", "--user"],
                    cgroup="0::/user.slice/user-1000.slice/user@1000.service/init.scope")


class TestParsing(unittest.TestCase):
    def test_comm_with_parens_and_spaces(self):
        raw = "42 (a (b) c) S 7 " + " ".join(["0"] * 9) + " 11 22 0 0 20 0 3 0 99 0 5\n"
        st = hl_procs.parse_stat(raw)
        self.assertEqual((st["comm"], st["ppid"], st["utime"], st["stime"], st["start"], st["rss_pages"]),
                         ("a (b) c", 7, 11, 22, 99, 5))

    def test_scrub_redacts_secrets(self):
        out = hl_procs.scrub("curl --token=abc123 -H 'Authorization: Bearer sk_live_" + "x" * 40 + "'")
        self.assertNotIn("abc123", out)
        self.assertNotIn("x" * 40, out)


class TestAttribution(Base):
    def project(self, pid):
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)),
                                   {"a" * 64: "temple-stuart-accounting"})
        return procs[pid].project, procs[pid].basis

    def test_cwd_rules(self):
        for pid, cwd, want in ((10, "/home/eric/projects/hearth/crates/x", "hearth"),
                               (11, WT + "/sub", "p1-epic-research-to-capital-readiness-reconcile"),
                               (12, "/home/eric/data2/orca/nervous-bus/_evidence", "nervous-bus")):
            self.fp.add(pid, "x", ppid=1145, cwd=cwd)
            self.assertEqual(self.project(pid), (want, "cwd"))

    def test_deleted_cwd_still_attributed(self):
        self.fp.add(10, "sh", ppid=1145, cwd=WT + " (deleted)")
        self.assertEqual(self.project(10)[0], "p1-epic-research-to-capital-readiness-reconcile")

    def test_cmdline_fallback_and_docker_label_verbatim(self):
        self.fp.add(10, "ld", ppid=1145, cwd="/", argv=["ld", "/home/eric/data/go-tmp/x", "-o",
                                                     "/home/eric/projects/tachyonac/bin/a"])
        self.assertEqual(self.project(10), ("tachyonac", "cmdline"))
        self.fp.add(11, "postgres", ppid=1, cgroup=DOCKER_CG, uid=999)
        self.assertEqual(self.project(11), ("temple-stuart-accounting", "docker"))

    def test_unit_bucket_and_honest_unknown(self):
        self.fp.add(10, "node", ppid=1145, cgroup="0::/user.slice/app-orca-7345123.scope")
        self.fp.add(11, "mystery", ppid=1145, cwd="/home/eric")
        self.assertEqual(self.project(10), ("unit:app-orca", "cgroup"))
        self.assertEqual(self.project(11), ("unknown", "none"))


class TestOrphanSpinLoop(Base):
    def spin_fixture(self, cwd=WT):
        pids = list(range(2000, 2005))
        for pid in pids:
            self.fp.add(pid, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=cwd,
                        cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/spin.scope")
        return pids

    def test_spin_loop_orphans_flagged_with_evidence(self):
        pids = self.spin_fixture()
        s = self.fp.sample(burners=pids, cores=1.0)
        f = [x for x in s["findings"] if x["kind"] == "orphan_cpu"]
        self.assertEqual(len(f), 1)
        f = f[0]
        self.assertEqual(f["severity"], "crit")
        self.assertEqual(f["count"], 5)
        self.assertAlmostEqual(f["cpu_cores"], 5.0, places=1)
        self.assertEqual(f["project"], "p1-epic-research-to-capital-readiness-reconcile")
        self.assertTrue(f["evidence"]["spin_loop"])
        self.assertEqual(f["evidence"]["ppid"], [1145])
        self.assertEqual(sorted(f["pids"]), pids)
        top = max(s["projects"].items(), key=lambda kv: kv[1]["cores"])
        self.assertEqual(top[0], "p1-epic-research-to-capital-readiness-reconcile")

    def test_spin_loop_with_deleted_worktree_also_flags_stale_cwd(self):
        pids = self.spin_fixture(cwd=WT + " (deleted)")
        s = self.fp.sample(burners=pids)
        kinds = {x["kind"] for x in s["findings"]}
        self.assertLessEqual({"orphan_cpu", "stale_cwd"}, kinds)

    def test_idle_orphans_and_normal_children_not_flagged(self):
        self.fp.add(3000, "sh", ppid=1145, argv=["sh"], cwd=WT)              # idle orphan
        self.fp.add(3001, "bash", ppid=3002, argv=["bash", "-c", "make"], cwd=WT)
        self.fp.add(3002, "claude", ppid=3000, argv=["claude"], cwd=WT)      # busy, parent is a live shell
        s = self.fp.sample(burners=[3000 + 1, 3002])
        self.assertEqual([x for x in s["findings"] if x["kind"] == "orphan_cpu"], [])

    def test_service_main_process_not_an_orphan(self):
        self.fp.add(3100, "hearth-api", ppid=1145, cwd="/home/eric/projects/hearth",
                    cgroup="0::/user.slice/user@1000.service/app.slice/hearth-api.service")
        s = self.fp.sample(burners=[3100])
        self.assertEqual([x for x in s["findings"] if x["kind"] == "orphan_cpu"], [])


class TestOtherDetectors(Base):
    def test_stale_cwd_path_missing_without_deleted_suffix(self):
        self.fp.add(10, "node", ppid=1145, cwd=WT + "/x")
        s = self.fp.sample(exists=lambda p: False)
        self.assertEqual([f["kind"] for f in s["findings"] if f["kind"] == "stale_cwd"], ["stale_cwd"])
        s = self.fp.sample(exists=lambda p: True)
        self.assertEqual([f for f in s["findings"] if f["kind"] == "stale_cwd"], [])

    def test_long_pure_cpu_loop_needs_age_and_no_io(self):
        self.fp.add(10, "python3", ppid=1145, argv=["python3", "sim.py"], cwd="/home/eric/projects/p",
                    start=1000, ticks=int(3000 * TICK))  # lifetime ~ 0.97 cores over ~3000s
        self.fp.set_uptime(3010)
        s = self.fp.sample(burners=[10])
        self.assertEqual([f["kind"] for f in s["findings"] if f["kind"] == "cpu_loop"], ["cpu_loop"])
        # young process: not flagged
        self.fp.add(11, "python3", ppid=1145, argv=["python3", "y.py"], cwd="/home/eric/projects/p",
                    start=int(3000 * TICK), ticks=0)
        s = self.fp.sample(burners=[11])
        self.assertNotIn(11, [pid for f in s["findings"] if f["kind"] == "cpu_loop" for pid in f["pids"]])

    def test_busy_io_process_is_not_a_pure_loop(self):
        self.fp.add(10, "python3", ppid=1145, argv=["python3", "etl.py"], cwd="/home/eric/projects/p",
                    start=1000, ticks=int(3000 * TICK))
        self.fp.set_uptime(3010)

        def sleep(s):
            self.fp.burn([10], s)
            self.fp.procs[10]["io"] += 50_000_000
            self.fp.write(10)
        s = hl_store.sample(str(self.fp.root), interval=10, docker={}, sleep=sleep)
        self.assertEqual([f for f in s["findings"] if f["kind"] == "cpu_loop"], [])

    def test_duplicate_builds_same_vs_different_cwd(self):
        proj = "/home/eric/projects/tachyonac"
        self.fp.add(10, "go", ppid=1145, argv=["go", "test", "./..."], cwd=proj)
        self.fp.add(11, "go", ppid=1145, argv=["go", "test", "./..."], cwd=proj)
        self.fp.add(12, "go", ppid=10, argv=["go", "test", "./x"], cwd=proj)   # child of same tool
        f = [x for x in self.fp.sample()["findings"] if x["kind"] == "duplicate_build"]
        self.assertEqual((len(f), f[0]["count"], f[0]["evidence"]["same_target"]), (1, 2, True))
        self.fp.add(11, "go", ppid=1145, argv=["go", "test", "./other"], cwd=proj + "/sub")
        f = [x for x in self.fp.sample()["findings"] if x["kind"] == "duplicate_build"]
        self.assertFalse(f[0]["evidence"]["same_target"])
        self.assertIn("not proven same target", f[0]["summary"])

    def test_swap_pressure_attributes_held_swap_by_project(self):
        self.fp.add(10, "big", ppid=1145, cwd="/home/eric/projects/hog", swap_kb=8_000_000)
        self.fp.add(11, "kswapd0", ppid=2, argv=[], uid=0)
        self.fp.set_psi(mem10=3)

        def sleep(s):
            self.fp.set_vm(0, 5000)
            self.fp.burn([11], s, 0.5)
        s = hl_store.sample(str(self.fp.root), interval=10, docker={}, sleep=sleep)
        f = [x for x in s["findings"] if x["kind"] == "memory_pressure"][0]
        self.assertEqual(f["evidence"]["top_swap_held"][0]["project"], "hog")
        self.assertAlmostEqual(f["evidence"]["swap_pages_per_s"], 500, places=0)
        self.assertGreater(f["evidence"]["kswapd_cores"], 0.4)

    def test_pid_reuse_does_not_inflate(self):
        self.fp.add(10, "a", ppid=1145, cwd="/home/eric/projects/p", ticks=0, start=1000)

        def sleep(s):  # same pid, new process with a huge counter
            self.fp.add(10, "b", ppid=1145, cwd="/home/eric/projects/p", ticks=10**7, start=5000)
        s = hl_store.sample(str(self.fp.root), interval=10, docker={}, sleep=sleep)
        self.assertEqual(s["projects"]["p"]["cores"], 0.0)

    def test_cpu_pressure_names_top_project(self):
        self.fp.add(10, "x", ppid=1145, cwd="/home/eric/projects/hog")
        self.fp.set_psi(cpu60=98)
        s = self.fp.sample(burners=[10])
        f = [x for x in s["findings"] if x["kind"] == "cpu_pressure"][0]
        self.assertEqual(f["project"], "hog")


class TestHistoryAndRender(Base):
    def test_record_series_html_and_prune(self):
        pids = []
        for i in range(3):
            self.fp.add(100 + i, "x", ppid=1145, cwd=f"/home/eric/projects/proj{i}", rss_pages=1000 * (i + 1))
            pids.append(100 + i)
        h = hl_store.History(os.path.join(self.tmp.name, "h.sqlite3"))
        now = time.time()
        for k in range(5):
            s = self.fp.sample(burners=pids, cores=0.5)
            s["ts"] = now - (4 - k) * 60
            h.record(s)
        old = self.fp.sample()
        old["ts"] = now - 30 * 86400
        h.record(old, retention_days=7)
        self.assertEqual(len(h.psi_series(0)), 5)  # 30-day-old sample pruned
        series = h.project_series(0)
        self.assertEqual({"proj0", "proj1", "proj2"} <= set(series), True)
        page = hl_render.render_html(h.psi_series(0), series, h.recent_findings(0), 1)
        self.assertIn("<polygon", page)
        self.assertIn("proj2", page)
        self.assertIn("polyline", page)

    def test_text_render_lists_flag_and_top(self):
        pids = list(range(2000, 2003))
        for pid in pids:
            self.fp.add(pid, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=WT)
        out = hl_render.render_text(self.fp.sample(burners=pids))
        self.assertIn("p1-epic-research", out)
        self.assertIn("orphan_cpu", out)

    def test_sparkline(self):
        self.assertEqual(hl_render.sparkline([0, 1], top=1)[0], "▁")
        self.assertEqual(hl_render.sparkline([0, 1], top=1)[-1], "█")


class TestLiveSmoke(unittest.TestCase):
    def test_real_proc_sample_and_cli(self):
        s = hl_store.sample(interval=0.3, docker={})
        self.assertGreater(s["nprocs"], 10)
        self.assertIn("cpu_some_avg10", s["psi"])
        with tempfile.TemporaryDirectory() as d:
            exe = str(Path(__file__).resolve().parent / "who-is-loading")
            db, out = os.path.join(d, "h.db"), os.path.join(d, "g.html")
            subprocess.run([exe, "--interval", "0.3", "--record", "--db", db, "--json"],
                           check=True, capture_output=True)
            subprocess.run([exe, "--html", out, "--db", db], check=True, capture_output=True)
            self.assertIn("<svg", Path(out).read_text())


if __name__ == "__main__":
    unittest.main()
