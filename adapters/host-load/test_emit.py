import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_emit
from test_host_load import WT, Base

REPO = Path(__file__).resolve().parents[2]
FINDING_SCHEMA = json.loads((REPO / "schemas/bus.host.load.finding.v1.json").read_text())["properties"]["data"]
NOTIFY_SCHEMA = json.loads((REPO / "schemas/bus.notify.v1.json").read_text())["properties"]["data"]
SCHEMAS = {hl_emit.CHANNEL: FINDING_SCHEMA, "bus.notify.v1": NOTIFY_SCHEMA}


def finding(kind="orphan_cpu", sev="crit", project="alpha", cmd="sh -c while :; do :; done", pids=(1, 2)):
    return {"kind": kind, "severity": sev, "project": project, "summary": f"{kind} in {project}",
            "count": 128, "cpu_cores": 5.0, "rss_bytes": 1 << 20, "pids": list(pids),
            "agents": [{"agent": "proj/agent-aa99", "count": 128}],
            "evidence": {"cmd": cmd, "cwd": WT, "spin_loop": True, "ppid": [1], "members": [{"pid": 1}]}}


class Bus:
    def __init__(self):
        self.sent = []

    def __call__(self, channel, payload):
        jsonschema.validate(payload, SCHEMAS[channel])      # every published payload honours its schema
        self.sent.append((channel, payload))


class TestEmitter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = Bus()
        self.em = lambda: hl_emit.Emitter(os.path.join(self.tmp.name, "s.json"), self.bus, host="cacheman")

    def test_nothing_published_until_confirmed_then_critical_notifies(self):
        self.assertEqual(self.em().process([finding()], 1000), [])
        self.assertEqual(self.bus.sent, [])
        done = self.em().process([finding()], 1060)           # a NEW Emitter: state survives between runs
        self.assertEqual(done[0][1:], ("ok", "critical"))
        chans = [c for c, _ in self.bus.sent]
        self.assertEqual(chans, [hl_emit.CHANNEL, "bus.notify.v1"])
        ev, note = self.bus.sent[0][1], self.bus.sent[1][1]
        self.assertEqual((ev["level"], ev["prev_level"], ev["agents"][0]["agent"]), ("critical", "ok", "proj/agent-aa99"))
        self.assertEqual(note["channels"], ["session", "phone"])
        self.assertIn("proj/agent-aa99", note["summary"])
        self.assertLessEqual(len(note["summary"]), 140)

    def test_sustained_finding_is_silent_then_reminds_after_six_hours(self):
        for t in (1000, 1060):
            self.em().process([finding()], t)
        self.bus.sent.clear()
        for t in (1120, 1180, 1240):
            self.assertEqual(self.em().process([finding()], t), [])
        self.assertEqual(self.bus.sent, [])
        out = self.em().process([finding()], 1060 + 6 * 3600 + 1)
        self.assertEqual(out[0][1:], ("critical", "critical"))

    def test_one_sample_spike_and_flapping_never_publish(self):
        for t, present in ((1000, True), (1060, False), (1120, True), (1180, False), (1240, True), (1300, False)):
            self.em().process([finding()] if present else [], t)
        self.assertEqual(self.bus.sent, [])

    def test_recovery_published_after_two_clear_samples(self):
        for t in (1000, 1060):
            self.em().process([finding(kind="cpu_loop", sev="warn")], t)
        self.bus.sent.clear()
        self.assertEqual(self.em().process([], 1120), [])
        out = self.em().process([], 1180)
        self.assertEqual(out[0][1:], ("warn", "ok"))
        self.assertEqual(self.bus.sent[0][1]["level"], "ok")
        self.assertTrue(self.bus.sent[0][1]["summary"].startswith("recovered:"))

    def test_info_findings_and_noncritical_kinds_do_not_notify(self):
        for t in (1000, 1060):
            self.em().process([finding(kind="stale_cwd", sev="info"), finding(kind="cpu_pressure", sev="crit",
                                                                              project="x")], t)
        chans = [c for c, _ in self.bus.sent]
        self.assertEqual(chans, [hl_emit.CHANNEL])           # crit cpu_pressure is an event, not a phone buzz
        self.assertEqual(self.bus.sent[0][1]["kind"], "cpu_pressure")

    def test_escalation_warn_to_critical_needs_confirmation_too(self):
        for t in (1000, 1060):
            self.em().process([finding(sev="warn")], t)
        self.bus.sent.clear()
        self.em().process([finding(sev="crit")], 1120)
        self.assertEqual(self.bus.sent, [])
        self.em().process([finding(sev="crit")], 1180)
        self.assertEqual(self.bus.sent[0][1]["prev_level"], "warn")

    def test_publish_failure_is_retried_next_run_not_lost(self):
        fails = {"n": 1}

        def flaky(channel, payload):
            if fails["n"]:
                fails["n"] -= 1
                raise OSError("bus down")
            self.bus(channel, payload)
        mk = lambda: hl_emit.Emitter(os.path.join(self.tmp.name, "s.json"), flaky, host="h")  # noqa: E731
        mk().process([finding()], 1000)
        mk().process([finding()], 1060)           # confirmed, publish fails
        self.assertEqual(self.bus.sent, [])
        mk().process([finding()], 1120)           # retried
        self.assertEqual(self.bus.sent[0][1]["level"], "critical")

    def test_key_is_stable_across_pid_churn_and_secret_free(self):
        a = hl_emit.finding_key(finding(pids=(1, 2)))
        b = hl_emit.finding_key(finding(pids=(77, 88)))
        self.assertEqual(a, b)
        self.assertNotEqual(a, hl_emit.finding_key(finding(project="beta")))


class TestEndToEnd(Base):
    def test_real_sample_findings_publish_valid_payloads(self):
        pids = list(range(7000, 7006))
        for pid in pids:
            self.fp.add(pid, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=WT)
        bus = Bus()
        em = hl_emit.Emitter(os.path.join(self.tmp.name, "s.json"), bus, host="cacheman")
        for k in range(2):
            s = self.fp.sample(burners=pids)
            em.process(s["findings"], 1000 + 60 * k)
        ev = [p for c, p in bus.sent if c == hl_emit.CHANNEL][0]
        self.assertEqual(ev["agents"][0]["agent"], "p1-epic-research-to-capital-readiness-reconcile/agent-aa9933e9")
        self.assertEqual(ev["count"], 6)


if __name__ == "__main__":
    unittest.main()
