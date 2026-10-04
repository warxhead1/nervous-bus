import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hl_agents
import hl_procs
import hl_render
from test_host_load import WT, Base

WT2 = "/home/eric/data2/worktrees/hearth/bb-feed"
SECRET = "placeholder-not-a-credential"


class TestAgentView(Base):
    def spin_orphans(self, n=5, cwd=WT):
        pids = list(range(7000, 7000 + n))
        for pid in pids:
            self.fp.add(pid, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd=cwd)
        return pids

    def test_incident_leaked_orphans_are_named_by_worktree(self):
        """128-orphan incident shape: ancestry gone (ppid = systemd --user), cwd in the agent's worktree."""
        pids = self.spin_orphans(6)
        s = self.fp.sample(burners=pids)
        f = [x for x in s["findings"] if x["kind"] == "orphan_cpu"][0]
        want = "p1-epic-research-to-capital-readiness-reconcile/agent-aa9933e9"
        self.assertEqual(f["agents"][0], {"agent": want, "count": 6})
        top = s["agents"][0]
        self.assertEqual((top["agent"], top["nproc"], top["orphans"]), (want, 6, 6))
        self.assertIn(want, hl_render.render_text(s))
        self.assertIn(want, hl_render.render_who(s, "while :"))

    def test_children_follow_the_claude_ancestor_even_outside_the_worktree(self):
        self.fp.add(500, "claude", ppid=1145, argv=["claude"], cwd=WT2)
        self.fp.add(501, "zsh", ppid=500, argv=["zsh"], cwd=WT2)
        self.fp.add(502, "cargo", ppid=501, argv=["cargo", "build"], cwd="/home/eric/projects/hearth")
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        info = hl_agents.assign(procs, lambda pid: {})
        self.assertEqual((procs[502].agent, procs[502].agent_basis), ("hearth/bb-feed", "ancestor"))
        self.assertEqual(procs[502].project, "hearth")           # project attribution is unchanged
        self.assertEqual(info["hearth/bb-feed"]["kinds"], {"claude"})

    def test_interactive_session_outside_a_worktree(self):
        self.fp.add(510, "codex", ppid=1145, argv=["codex"], cwd="/home/eric/projects/nervous-bus")
        procs = hl_procs.attribute(hl_procs.scan(str(self.fp.root)), {})
        hl_agents.assign(procs, lambda pid: {})
        self.assertEqual(procs[510].agent, "codex@nervous-bus#510")

    def test_environ_allowlist_never_leaks_tokens(self):
        self.fp.add(520, "claude", ppid=1145, argv=["claude"], cwd=WT2)
        env = (f"PATH=/bin\0ORCA_AGENT_HOOK_TOKEN={SECRET}\0ORCA_AGENT_LAUNCH_TOKEN={SECRET}\0"
               "ORCA_WORKTREE_ID=wt-1234\0ORCA_PANE_KEY=pane-9\0")
        (self.fp.root / "520" / "environ").write_bytes(env.encode())
        got = hl_agents.read_env(520, str(self.fp.root))
        self.assertEqual(got, {"ORCA_WORKTREE_ID": "wt-1234", "ORCA_PANE_KEY": "pane-9"})
        s = self.fp.sample()
        self.assertNotIn(SECRET, json.dumps(hl_render.render_text(s)) + json.dumps(s["agents"]))
        row = [a for a in s["agents"] if a["agent"] == "hearth/bb-feed"][0]
        self.assertEqual((row["orca_worktree_id"], row["orca_pane"]), ("wt-1234", "pane-9"))

    def test_orphan_with_no_cwd_signal_is_named_from_inherited_environ(self):
        self.fp.add(530, "sh", ppid=1145, argv=["sh", "-c", "while :; do :; done"], cwd="/tmp")
        (self.fp.root / "530" / "environ").write_bytes(b"ORCA_WORKTREE_ID=wt-feed-1\0ORCA_AGENT_HOOK_TOKEN=x\0")
        s = self.fp.sample(burners=[530])
        f = [x for x in s["findings"] if x["kind"] == "orphan_cpu"][0]
        self.assertEqual(f["agents"][0]["agent"], "orca:wt-feed-1")

    def test_history_totals_rank_agents(self):
        pids = self.spin_orphans(3)
        h = __import__("hl_store").History(os.path.join(self.tmp.name, "h.sqlite3"))
        for k in range(3):
            s = self.fp.sample(burners=pids)
            s["ts"] = __import__("time").time() - (3 - k) * 60
            h.record(s)
        rows = h.agent_totals(0)
        self.assertEqual(rows[0]["agent"], "p1-epic-research-to-capital-readiness-reconcile/agent-aa9933e9")
        self.assertEqual(rows[0]["peak_orphans"], 3)
        self.assertIn("agent-aa9933e9", hl_render.render_agent_history(rows))


if __name__ == "__main__":
    unittest.main()
