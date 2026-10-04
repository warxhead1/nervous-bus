import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_procs
import hl_services
from test_host_load import WT, Base

SS_TCP = 'tcp LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:* users:(("srv",pid=3100,fd=7),("srv",pid=3101,fd=8))\n'
SVC_CG = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/dev-server.service"
SCOPE_CG = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-u4242.scope"


def systemctl(out):
    return lambda cmd: out


class TestEvidence(Base):
    def probe(self, ss="", main="", pidfiles=()):
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        cands = [p for p in procs.values() if p.pid >= 3000]
        return hl_services.probe(procs, cands, now=time.time(), uptime_s=100000,
                                 ss_runner=lambda cmd: ss if "-lntup" in cmd else "",
                                 systemctl_runner=systemctl(main), pidfile_globs=list(pidfiles))

    def test_listening_socket_is_evidence(self):
        self.fp.add(3100, "node", ppid=1145, cwd=WT)
        self.fp.add(3200, "node", ppid=1145, cwd=WT)
        ev = self.probe(ss=SS_TCP)
        self.assertEqual(ev[3100], ["listening_socket"])
        self.assertNotIn(3200, ev)

    def test_unit_main_pid_batched_in_one_call(self):
        self.fp.add(3300, "sh", ppid=1145, cwd=WT, cgroup=SVC_CG)
        self.fp.add(3301, "sh", ppid=3300, cwd=WT, cgroup=SVC_CG)    # a child inside the unit, not MainPID
        calls = []

        def run(cmd):
            calls.append(cmd)
            return "Id=dev-server.service\nMainPID=3300\n"
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        cands = [procs[3300], procs[3301]]
        ev = hl_services.probe(procs, cands, ss_runner=lambda c: "", systemctl_runner=run, pidfile_globs=[])
        self.assertEqual(ev[3300], ["unit_main:dev-server.service"])
        self.assertNotIn(3301, ev)
        self.assertEqual(len(calls), 1)

    def test_small_dedicated_scope_leader_but_not_shared_scope(self):
        self.fp.add(3400, "python", ppid=1145, cwd=WT, cgroup=SCOPE_CG)
        self.fp.add(3401, "python", ppid=3400, cwd=WT, cgroup=SCOPE_CG)
        shared = "0::/user.slice/user@1000.service/app.slice/orca-daemon-ca84.scope"
        self.fp.add(3500, "sh", ppid=1145, cwd=WT, cgroup=shared)
        ev = self.probe()
        self.assertEqual(ev[3400], ["scope_leader:run-u4242.scope"])
        self.assertNotIn(3401, ev)
        self.assertNotIn(3500, ev)

    def test_pidfile_must_be_newer_than_the_process(self):
        self.fp.add(3600, "daemon", ppid=1145, cwd=WT, start=100)           # started ~1000s after boot
        self.fp.add(3601, "daemon", ppid=1145, cwd=WT, start=100)
        d = Path(self.tmp.name) / "run"
        d.mkdir()
        (d / "fresh.pid").write_text("3600\n")
        stale = d / "stale.pid"
        stale.write_text("3601\n")
        os.utime(stale, (1000, 1000))                                       # written long before the process existed
        ev = self.probe(pidfiles=[str(d / "*.pid")])
        self.assertEqual(ev[3600], ["pidfile"])
        self.assertNotIn(3601, ev)

    def test_dead_tools_degrade_to_no_evidence(self):
        self.fp.add(3700, "node", ppid=1145, cwd=WT)

        def boom(cmd):
            raise FileNotFoundError(cmd[0])
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        ev = hl_services.probe(procs, [procs[3700]], ss_runner=boom, systemctl_runner=boom, pidfile_globs=[])
        self.assertEqual(dict(ev), {})


class TestOrphanFalsePositive(Base):
    def run_sample(self, pids, probe):
        return self.fp.sample(burners=pids, service_probe=probe)

    def test_busy_node_server_with_listener_is_cleared_and_reported(self):
        self.fp.add(3100, "node", ppid=1145, argv=["node", "serve.js"], cwd=WT)
        s = self.run_sample([3100], lambda cands: {3100: ["listening_socket"]})
        self.assertEqual([f for f in s["findings"] if f["kind"] == "orphan_cpu"], [])
        self.assertEqual(s["excused_services"][0]["reasons"], ["listening_socket"])

    def test_same_process_without_evidence_is_still_flagged(self):
        self.fp.add(3100, "node", ppid=1145, argv=["node", "serve.js"], cwd=WT)
        s = self.run_sample([3100], lambda cands: {})
        self.assertEqual(len([f for f in s["findings"] if f["kind"] == "orphan_cpu"]), 1)

    def test_spin_loop_is_not_excused_by_a_listener_or_pidfile(self):
        self.fp.add(3200, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=WT)
        s = self.run_sample([3200], lambda cands: {3200: ["listening_socket", "pidfile"]})
        f = [x for x in s["findings"] if x["kind"] == "orphan_cpu"]
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]["evidence"]["spin_loop"])

    def test_spin_loop_that_is_a_units_main_pid_is_excused(self):
        self.fp.add(3300, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=WT)
        s = self.run_sample([3300], lambda cands: {3300: ["unit_main:burn.service"]})
        self.assertEqual([f for f in s["findings"] if f["kind"] == "orphan_cpu"], [])


if __name__ == "__main__":
    unittest.main()
