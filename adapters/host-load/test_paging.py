import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_paging
import hl_store
from test_host_load import Base

MB = 1024 * 1024


class FakeCgroups:
    def __init__(self, root):
        self.root = Path(root) / "cg"

    def set(self, rel, pgmajfault=0, refault_anon=0, refault_file=0, high=0, mem=0, swap=0):
        d = self.root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.stat").write_text(
            f"anon 1\npgmajfault {pgmajfault}\nworkingset_refault_anon {refault_anon}\n"
            f"workingset_refault_file {refault_file}\n")
        (d / "memory.events").write_text(f"low 0\nhigh {high}\nmax 0\n")
        (d / "memory.current").write_text(f"{mem}\n")
        (d / "memory.swap.current").write_text(f"{swap}\n")


class TestCgroupPaging(Base):
    def setUp(self):
        super().setUp()
        self.cg = FakeCgroups(self.tmp.name)
        base = "user.slice/user@1000.service/app.slice/"
        self.thrasher = base + "build-box.service"
        self.quiet = base + "idle.service"
        self.cg.set("user.slice", pgmajfault=999999)                 # parent: must not double count
        self.cg.set(self.thrasher, refault_anon=100, pgmajfault=500, mem=1000 * MB, high=0)
        self.cg.set(self.quiet, refault_anon=50, pgmajfault=10, mem=200 * MB)

    def test_leaf_only_and_unit_names(self):
        got = hl_paging.read_cgroups(str(self.cg.root))
        self.assertEqual(set(got), {self.thrasher, self.quiet})
        self.assertEqual(hl_paging.unit_of(self.thrasher), "build-box.service")

    def test_deltas_rank_the_thrasher_and_the_grower(self):
        before = hl_paging.read_cgroups(str(self.cg.root))
        self.cg.set(self.thrasher, refault_anon=100 + 6000, pgmajfault=500 + 7000, mem=1600 * MB, high=4)
        self.cg.set(self.quiet, refault_anon=50, pgmajfault=10, mem=200 * MB)
        d = hl_paging.cgroup_deltas(before, hl_paging.read_cgroups(str(self.cg.root)), 60.0)
        self.assertEqual(d["by_refault"][0]["unit"], "build-box.service")
        self.assertAlmostEqual(d["by_refault"][0]["refault_anon_s"], 100.0)
        self.assertEqual([r["unit"] for r in d["by_growth"]], ["build-box.service"])
        self.assertEqual(d["throttled"][0]["high_events"], 4)

    def test_sample_blames_faulting_process_not_swap_holder(self):
        self.fp.add(100, "rustc", ppid=1145, cwd="/home/eric/projects/alpha", swap_kb=1)
        self.fp.add(101, "bigheap", ppid=1145, cwd="/home/eric/projects/beta", swap_kb=8 * 1024 * 1024)
        self.fp.set_vm(0, 0)
        self.fp.set_psi(mem10=30.0)

        def churn(seconds):
            self.fp.fault(100, int(400 * seconds))
            self.fp.set_vm(int(300 * seconds), int(300 * seconds))
        s = hl_store.sample(str(self.fp.root), interval=10.0, docker={}, sleep=churn,
                            cg_root=str(self.cg.root))
        mp = [f for f in s["findings"] if f["kind"] == "memory_pressure"][0]
        causes = mp["evidence"]["causes"]
        self.assertEqual(causes["faulting_processes"][0]["comm"], "rustc")
        self.assertEqual(causes["faulting_projects"][0]["project"], "alpha")
        self.assertEqual(mp["evidence"]["top_swap_held"][0]["project"], "beta")   # holder is a different story
        self.assertAlmostEqual(s["paging"]["window"]["vm"]["pswpin"], 300.0, places=0)

    def test_interval_paging_uses_previous_record(self):
        self.fp.add(100, "rustc", ppid=1145, cwd="/home/eric/projects/alpha")
        h = hl_store.History(os.path.join(self.tmp.name, "h.sqlite3"))
        self.fp.set_uptime(100000)
        h.record(self.fp.sample(prev=h.load_snapshot(), now=lambda: 1000.0, cg_root=str(self.cg.root)))
        self.fp.fault(100, 6000)
        self.fp.set_vm(0, 12000)
        self.cg.set(self.thrasher, refault_anon=100 + 3000, pgmajfault=500, mem=1000 * MB)
        self.fp.set_uptime(100060)
        s = hl_store.sample(str(self.fp.root), interval=1.0, docker={}, sleep=lambda x: None,
                            prev=h.load_snapshot(), now=lambda: 1060.0, cg_root=str(self.cg.root))
        iv = s["paging"]["interval"]
        self.assertEqual(iv["processes"][0]["comm"], "rustc")
        self.assertAlmostEqual(iv["processes"][0]["majflt_s"], 100.0, places=0)
        self.assertAlmostEqual(s["paging"]["swap_out_pages_s"], 200.0, places=0)
        self.assertEqual(s["paging"]["cgroups"]["interval"]["by_refault"][0]["unit"], "build-box.service")


if __name__ == "__main__":
    unittest.main()
