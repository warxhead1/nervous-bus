import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_gpw
import hl_procs
import hl_render
from test_host_load import Base

JOURNAL = ("1791070000.1 cacheman game-priority-watch[1]: game-priority-watch: mode=IDLE no game units\n"
           "1791070100.5 cacheman game-priority-watch[1]: game-priority-watch: mode=GAMING "
           "app-steam@0d1b.service <- pid=9 /steamapps/common/x\n")
SHOW = """Id=hearth-api.service
CPUWeight=20
CPUQuotaPerSecUSec=infinity
MemoryHigh=4294967296
MemorySwapMax=infinity

Id=tachyonac-gate-77.scope
CPUWeight=[not set]
CPUQuotaPerSecUSec=4s
MemoryHigh=infinity
MemorySwapMax=8589934592

Id=ac-worldserver.service
CPUWeight=[not set]
CPUQuotaPerSecUSec=infinity
MemoryHigh=infinity
MemorySwapMax=infinity

Id=session.slice
CPUWeight=300
CPUQuotaPerSecUSec=infinity
MemoryHigh=infinity
MemorySwapMax=infinity
"""


class TestWatcherView(Base):
    def annotate(self, **kw):
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        seen = []

        def show(cmd):
            seen.append(cmd)
            return SHOW

        def journal(cmd):
            seen.append(cmd)
            return JOURNAL
        out = hl_gpw.annotate(procs, {p.project for p in procs.values()}, journal, show, **kw)
        return out, seen

    def test_states_from_live_properties_and_last_mode_line(self):
        cg = "0::/user.slice/user@1000.service/app.slice/"
        self.fp.add(10, "hearth-api", ppid=1145, cwd="/home/eric/projects/hearth", cgroup=cg + "hearth-api.service")
        self.fp.add(11, "go", ppid=1145, cwd="/home/eric/projects/tachyonac-engine", cgroup=cg + "tachyonac-gate-77.scope")
        self.fp.add(12, "worldserver", ppid=1145, cwd="/home/eric/projects/azerothcore", cgroup=cg + "ac-worldserver.service")
        out, seen = self.annotate()
        self.assertEqual(out["mode"]["mode"], "GAMING")
        self.assertEqual(out["mode"]["ts"], 1791070100.5)
        self.assertEqual(out["projects"]["hearth"]["state"], "batched")
        self.assertEqual(out["projects"]["tachyonac-engine"]["state"], "capped")
        self.assertEqual(hl_gpw.label(out["projects"]["tachyonac-engine"]), "capped 400%")
        self.assertEqual(out["projects"]["azerothcore"]["state"], "protected")
        self.assertEqual(hl_gpw.label(out["projects"]["hearth"]), "batched w20")

    def test_geforce_now_chrome_app_is_protected_whatever_its_unit(self):
        self.fp.add(20, "chrome", ppid=1145, argv=["chrome", "--app=https://play.geforce.com/mall/"],
                    cwd="/home/eric/projects/hearth", cgroup="0::/user.slice/user@1000.service/app.slice/app-chrome.scope")
        out, _ = self.annotate()
        self.assertEqual(out["projects"]["hearth"]["state"], "protected")

    def test_strictly_read_only_and_batched_into_two_commands(self):
        for pid in range(30, 36):
            self.fp.add(pid, "x", ppid=1145, cwd=f"/home/eric/projects/p{pid}",
                        cgroup=f"0::/user.slice/user@1000.service/app.slice/u{pid}.service")
        _, seen = self.annotate()
        self.assertEqual(len(seen), 2)
        flat = " ".join(" ".join(c) for c in seen)
        for forbidden in ("set-property", "kill", "renice", "chrt", "cpu.weight"):
            self.assertNotIn(forbidden, flat)
        self.assertIn("show", seen[0])

    def test_tools_down_degrades_to_unknown(self):
        self.fp.add(10, "x", ppid=1145, cwd="/home/eric/projects/hearth",
                    cgroup="0::/user.slice/user@1000.service/app.slice/x.service")

        def boom(cmd):
            raise OSError("no systemctl")
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        out = hl_gpw.annotate(procs, {"hearth"}, boom, boom)
        self.assertEqual(out["mode"]["mode"], "unknown")
        self.assertEqual(out["projects"]["hearth"]["state"], "unknown")

    def test_render_shows_policy_column_and_mode(self):
        cg = "0::/user.slice/user@1000.service/app.slice/"
        self.fp.add(10, "hearth-api", ppid=1145, cwd="/home/eric/projects/hearth", cgroup=cg + "hearth-api.service")
        out, _ = self.annotate()
        s = self.fp.sample(priority=lambda: out)
        text = hl_render.render_text(s)
        self.assertIn("batched w20", text)
        self.assertIn("game-priority-watch: GAMING", text)


if __name__ == "__main__":
    unittest.main()
