#!/usr/bin/env python3
"""Tests for cc_sysmap."""
import sys
import time
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cc_sysmap


class TestPollConstants(unittest.TestCase):
    def test_docker_poll_at_least_10s(self):
        self.assertGreaterEqual(cc_sysmap.POLL_DOCKER_S, 10.0)

    def test_gpu_poll_at_least_8s(self):
        self.assertGreaterEqual(cc_sysmap.POLL_GPU_S, 8.0)

    def test_vitals_poll_constant_exists(self):
        self.assertTrue(hasattr(cc_sysmap, "POLL_VITALS_S"))
        self.assertGreaterEqual(cc_sysmap.POLL_VITALS_S, 3.0)

    def test_poller_has_next_vitals_timer(self):
        state = cc_sysmap.SysmapState()
        poller = cc_sysmap._Poller(state)
        self.assertTrue(hasattr(poller, "_next_vitals"))


class TestTailBusFileHandle(unittest.TestCase):
    def test_state_has_log_fh_field(self):
        state = cc_sysmap.SysmapState()
        self.assertIsNone(state._log_fh)

    def test_tail_bus_opens_handle_on_first_call(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"bus.bead.created","data":{}}\n')
            fname = f.name
        try:
            state = cc_sysmap.SysmapState(log_path=Path(fname))
            cc_sysmap._tail_bus(state)
            self.assertIsNotNone(state._log_fh)
        finally:
            os.unlink(fname)

    def test_tail_bus_reuses_handle_on_second_call(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"bus.bead.created","data":{}}\n')
            fname = f.name
        try:
            state = cc_sysmap.SysmapState(log_path=Path(fname))
            cc_sysmap._tail_bus(state)
            fh1 = state._log_fh
            cc_sysmap._tail_bus(state)
            fh2 = state._log_fh
            self.assertIs(fh1, fh2)
        finally:
            os.unlink(fname)


class TestRedisIntegration(unittest.TestCase):
    def test_state_has_redis_field(self):
        state = cc_sysmap.SysmapState()
        self.assertTrue(hasattr(state, "_redis"))

    def test_state_has_zs_field(self):
        state = cc_sysmap.SysmapState()
        self.assertIsInstance(state.zs, dict)

    def test_state_has_git_commits_field(self):
        state = cc_sysmap.SysmapState()
        self.assertIsInstance(state.git_commits, dict)

    def test_deserialize_containers_roundtrip(self):
        containers = [
            cc_sysmap.ContainerInfo(
                name="deer-flow-gateway", image="nginx:latest",
                uptime="2h", cpu_pct=12.5, mem_mb=256.0, running=True,
            )
        ]
        import dataclasses, json
        serialized = json.dumps([dataclasses.asdict(c) for c in containers])
        restored = cc_sysmap._deserialize_containers(json.loads(serialized))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "deer-flow-gateway")
        self.assertEqual(restored[0].cpu_pct, 12.5)

    def test_deserialize_gpu_roundtrip(self):
        gpu = cc_sysmap.GpuInfo(
            name="RTX 4090", util_pct=42, mem_used_mb=8192,
            mem_total_mb=24576, temp_c=65, power_w=180.0, power_limit_w=450.0,
        )
        import dataclasses, json
        serialized = json.dumps(dataclasses.asdict(gpu))
        restored = cc_sysmap._deserialize_gpu(json.loads(serialized))
        self.assertEqual(restored.name, "RTX 4090")
        self.assertEqual(restored.util_pct, 42)
        self.assertEqual(restored.processes, [])


class TestPollZs(unittest.TestCase):
    def test_zs_keys_map_exists(self):
        self.assertTrue(hasattr(cc_sysmap, "ZS_KEYS_BY_PROJECT"))
        self.assertIn("hearth-loom", cc_sysmap.ZS_KEYS_BY_PROJECT)
        self.assertIn("tengine", cc_sysmap.ZS_KEYS_BY_PROJECT)
        self.assertIn("shared", cc_sysmap.ZS_KEYS_BY_PROJECT)

    def test_poll_zs_no_redis_is_noop(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        cc_sysmap._poll_zs(state)
        self.assertEqual(state.zs, {})

    def test_poller_has_next_zs_timer(self):
        state = cc_sysmap.SysmapState()
        poller = cc_sysmap._Poller(state)
        self.assertTrue(hasattr(poller, "_next_zs"))

    def test_git_path_on_project_def(self):
        df = next(p for p in cc_sysmap.PROJECTS if p.name == "deer-flow")
        self.assertIsNotNone(df.git_path)
        self.assertIn("deer-flow", df.git_path)


class TestIdlePanelEnrichment(unittest.TestCase):
    def _make_state_with_zs(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        state.zs = {
            "zs:hl-kanban":       "P0:3 P1:10",
            "zs:hl-loomies":      "18 running:5 skipped:13",
            "zs:hl-dispatcher":   "live",
            "zs:hl-ccm-by-agent": "claude:14 opencode:4",
        }
        state.git_commits = {
            "hearth-loom": ("3 hours ago · feat: add loomie retry logic", time.time()),
        }
        return state

    def test_idle_panel_renders_without_error(self):
        state = self._make_state_with_zs()
        proj = next(p for p in cc_sysmap.PROJECTS if p.name == "hearth-loom")
        panel = cc_sysmap.panel_project(proj, state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    def test_panel_project_renders_with_containers(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        state.containers = [
            cc_sysmap.ContainerInfo(
                name="hearth-loom-agent-abc", image="ubuntu",
                uptime="1h", cpu_pct=5.0, mem_mb=128.0, running=True,
            )
        ]
        proj = next(p for p in cc_sysmap.PROJECTS if p.name == "hearth-loom")
        panel = cc_sysmap.panel_project(proj, state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)


class TestGroupedContainerPeek(unittest.TestCase):
    def _make_agents(self, n: int):
        return [
            cc_sysmap.ContainerInfo(
                name=f"hearth-loom-agent-{i:08x}", image="ubuntu",
                uptime="2h", cpu_pct=float(n - i) * 5, mem_mb=256.0 + i * 20,
                running=True,
            )
            for i in range(n)
        ]

    def test_grouped_panel_renders_with_many_containers(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        state.containers = self._make_agents(6)
        proj = next(p for p in cc_sysmap.PROJECTS if p.name == "hearth-loom")
        panel = cc_sysmap.panel_project(proj, state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    def test_grouped_panel_renders_with_exactly_4_containers(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        state.containers = self._make_agents(4)
        proj = next(p for p in cc_sysmap.PROJECTS if p.name == "hearth-loom")
        panel = cc_sysmap.panel_project(proj, state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)

    def test_grouped_panel_renders_with_loomie_projects_enrichment(self):
        state = cc_sysmap.SysmapState()
        state._redis = None
        agents = self._make_agents(5)
        state.containers = agents
        state.loomie_projects = {
            agents[0].name: "tengine",
            agents[1].name: "hearth-loom",
        }
        proj = next(p for p in cc_sysmap.PROJECTS if p.name == "hearth-loom")
        panel = cc_sysmap.panel_project(proj, state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)


class TestSparklineConstants(unittest.TestCase):
    def test_bus_window_is_30min(self):
        self.assertEqual(cc_sysmap.BUS_WINDOW_S, 1800)

    def test_spark_width_is_24(self):
        self.assertEqual(cc_sysmap.SPARK_W, 24)

    def test_bucket_size_is_consistent(self):
        expected = cc_sysmap.BUS_WINDOW_S / cc_sysmap.SPARK_W
        self.assertAlmostEqual(cc_sysmap.SPARK_BUCKET_S, expected)


class TestBusPanel(unittest.TestCase):
    """BusPanel (nervous-bus-kciq) — channels, schema rate, producer health."""

    def _state_with_events(self, events):
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
            fname = f.name
        state = cc_sysmap.SysmapState(log_path=Path(fname))
        cc_sysmap._tail_bus(state)
        os.unlink(fname)
        return state

    def test_bus_status_field_exists(self):
        state = cc_sysmap.SysmapState()
        self.assertTrue(hasattr(state, "bus_status"))
        self.assertIsInstance(state.bus_status, cc_sysmap.BusStatus)

    def test_tail_bus_populates_chan_hits(self):
        events = [
            {"specversion": "1.0", "type": "tengine.session.frame",
             "source": "/tengine", "data": {}},
            {"specversion": "1.0", "type": "tengine.session.frame",
             "source": "/tengine", "data": {}},
            {"specversion": "1.0", "type": "hearth.presence.v1",
             "source": "/hearth-bridge", "data": {}},
        ]
        state = self._state_with_events(events)
        self.assertIn("tengine.session.frame", state.bus_status.chan_hits)
        self.assertEqual(len(state.bus_status.chan_hits["tengine.session.frame"]), 2)
        self.assertIn("hearth.presence.v1", state.bus_status.chan_hits)

    def test_tail_bus_populates_source_last_seen(self):
        events = [
            {"specversion": "1.0", "type": "tengine.session.frame",
             "source": "/tengine", "data": {}},
            {"specversion": "1.0", "type": "hearth.presence.v1",
             "source": "/hearth-bridge", "data": {}},
        ]
        state = self._state_with_events(events)
        self.assertIn("/tengine", state.bus_status.source_last_seen)
        self.assertIn("/hearth-bridge", state.bus_status.source_last_seen)

    def test_tail_bus_buckets_agent_sources(self):
        # Many /agent-* sources should collapse to "/agents" bucket
        events = [
            {"specversion": "1.0", "type": "bus.bead.updated",
             "source": f"/agent-{i:016x}", "data": {}}
            for i in range(5)
        ]
        state = self._state_with_events(events)
        self.assertIn("/agents", state.bus_status.source_last_seen)
        for i in range(5):
            self.assertNotIn(f"/agent-{i:016x}", state.bus_status.source_last_seen)

    def test_bus_panel_renders_empty(self):
        state = cc_sysmap.SysmapState()
        panel = cc_sysmap.panel_bus(state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)
        # Render to string and assert fragment
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=80, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("bus", out)
        self.assertIn("no recent channel activity", out)

    def test_bus_panel_renders_with_data(self):
        events = [
            {"specversion": "1.0", "type": "tengine.session.frame",
             "source": "/tengine", "data": {}},
            {"specversion": "1.0", "type": "tengine.session.frame",
             "source": "/tengine", "data": {}},
        ]
        state = self._state_with_events(events)
        panel = cc_sysmap.panel_bus(state)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=120, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("tengine.session.frame", out)
        self.assertIn("/tengine", out)
        # Schema-validation column should show "n/a" since we never tracked it.
        self.assertIn("n/a", out)


class TestAutobenchPanel(unittest.TestCase):
    """AutobenchPanel (nervous-bus-opvt) — session/iter/AHE/queue/requests."""

    def _state_with_events(self, events):
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
            fname = f.name
        state = cc_sysmap.SysmapState(log_path=Path(fname))
        cc_sysmap._tail_bus(state)
        os.unlink(fname)
        return state

    def test_autobench_field_exists(self):
        state = cc_sysmap.SysmapState()
        self.assertTrue(hasattr(state, "autobench"))
        self.assertIsInstance(state.autobench, cc_sysmap.AutobenchStatus)

    def test_tail_bus_captures_session_iter(self):
        events = [
            {"specversion": "1.0", "type": "autobench.iteration.v1",
             "source": "/autobench",
             "data": {"session_id": "01KSESSXYZ", "iter": 7}},
        ]
        state = self._state_with_events(events)
        self.assertEqual(state.autobench.session_id, "01KSESSXYZ")
        self.assertEqual(state.autobench.iter, 7)

    def test_tail_bus_captures_ahe_refuted_live(self):
        events = [
            {"specversion": "1.0",
             "type": "autobench.improver.prediction.refuted_live.v1",
             "source": "/autobench", "data": {}},
        ]
        state = self._state_with_events(events)
        self.assertEqual(state.autobench.last_ahe_outcome, "refuted_live")

    def test_tail_bus_captures_queue_pressure(self):
        events = [
            {"specversion": "1.0", "type": "autobench.worker.queue_pressure.v1",
             "source": "/autobench", "data": {"deviation_factor": 0.4}},
        ]
        state = self._state_with_events(events)
        self.assertTrue(state.autobench.queue_pressure)
        self.assertAlmostEqual(state.autobench.queue_dev_factor, 0.4)

    def test_tail_bus_queue_pressure_off_below_threshold(self):
        events = [
            {"specversion": "1.0", "type": "autobench.worker.queue_pressure.v1",
             "source": "/autobench", "data": {"deviation_factor": 0.1}},
        ]
        state = self._state_with_events(events)
        self.assertFalse(state.autobench.queue_pressure)

    def test_tail_bus_counts_worker_events(self):
        events = [
            {"specversion": "1.0", "type": "autobench.worker.v1",
             "source": "/autobench", "data": {}},
            {"specversion": "1.0", "type": "autobench.worker.v1",
             "source": "/autobench", "data": {}},
            {"specversion": "1.0", "type": "autobench.worker.v1",
             "source": "/autobench", "data": {}},
        ]
        state = self._state_with_events(events)
        self.assertGreaterEqual(state.autobench.requests_5h, 3)

    def test_autobench_panel_renders_empty(self):
        state = cc_sysmap.SysmapState()
        panel = cc_sysmap.panel_autobench(state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=80, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("autobench", out)
        self.assertIn("session", out)
        self.assertIn("iter", out)
        self.assertIn("AHE", out)
        self.assertIn("queue", out)
        self.assertIn("pending", out)

    def test_autobench_panel_renders_with_data(self):
        events = [
            {"specversion": "1.0", "type": "autobench.iteration.v1",
             "source": "/autobench",
             "data": {"session_id": "01KSESSXYZ123", "iter": 42}},
            {"specversion": "1.0", "type": "autobench.worker.queue_pressure.v1",
             "source": "/autobench", "data": {"deviation_factor": 0.6}},
            {"specversion": "1.0",
             "type": "autobench.improver.prediction.refuted_live.v1",
             "source": "/autobench", "data": {}},
        ]
        state = self._state_with_events(events)
        panel = cc_sysmap.panel_autobench(state)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=80, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("01KSESSXYZ123"[-12:], out)
        self.assertIn("42", out)
        self.assertIn("refuted_live", out)
        self.assertIn("ON", out)


class TestWorktreeAgentsPanel(unittest.TestCase):
    """WorktreeAgentsPanel (nervous-bus-6k8a) — host-side dispatched worktrees."""

    def test_worktree_agents_field_exists(self):
        state = cc_sysmap.SysmapState()
        self.assertTrue(hasattr(state, "worktree_agents"))
        self.assertEqual(state.worktree_agents, [])

    def test_poll_worktrees_with_missing_dir_is_safe(self):
        state = cc_sysmap.SysmapState()
        cc_sysmap._poll_worktrees(state, worktrees_dir=Path("/nonexistent/path"))
        self.assertEqual(state.worktree_agents, [])

    def test_worktree_panel_renders_empty(self):
        state = cc_sysmap.SysmapState()
        panel = cc_sysmap.panel_worktree_agents(state)
        from rich.panel import Panel
        self.assertIsInstance(panel, Panel)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=80, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("worktree agents", out)
        self.assertIn("no worktree agents", out)

    def test_worktree_panel_renders_with_data(self):
        state = cc_sysmap.SysmapState()
        state.worktree_agents = [
            cc_sysmap.WorktreeAgent(
                agent_id="aa286ca",
                branch="worktree-agent-aa286cafecf60b892",
                status="running", age_s=120.0, dirty=True,
            ),
            cc_sysmap.WorktreeAgent(
                agent_id="bb999",
                branch="worktree-agent-bb999",
                status="merged", age_s=3600.0,
            ),
            cc_sysmap.WorktreeAgent(
                agent_id="ccdead",
                branch="worktree-agent-ccdead",
                status="abandoned", age_s=86400.0 * 2,
            ),
        ]
        panel = cc_sysmap.panel_worktree_agents(state)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=120, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("aa286ca", out)
        self.assertIn("running", out)
        self.assertIn("merged", out)
        self.assertIn("abandoned", out)
        # The dirty marker should appear
        self.assertIn("aa286ca*", out)

    def test_worktree_panel_truncates_long_lists(self):
        state = cc_sysmap.SysmapState()
        state.worktree_agents = [
            cc_sysmap.WorktreeAgent(
                agent_id=f"agent{i:02d}",
                branch=f"worktree-agent-{i:02d}",
                status="running", age_s=float(i * 60),
            )
            for i in range(15)
        ]
        panel = cc_sysmap.panel_worktree_agents(state)
        from rich.console import Console
        import io
        console = Console(file=io.StringIO(), width=120, force_terminal=False)
        console.print(panel)
        out = console.file.getvalue()
        self.assertIn("more", out)

    def test_poller_has_next_wt_timer(self):
        state = cc_sysmap.SysmapState()
        poller = cc_sysmap._Poller(state)
        self.assertTrue(hasattr(poller, "_next_wt"))


class TestLayoutWithNewPanels(unittest.TestCase):
    def test_build_sysmap_layout_includes_bottom_row(self):
        state = cc_sysmap.SysmapState()
        layout = cc_sysmap.build_sysmap_layout(state)
        # Bottom row should exist
        names = []
        def walk(node):
            if node.name:
                names.append(node.name)
            for child in node.children:
                walk(child)
        walk(layout)
        self.assertIn("bottom_row", names)
        self.assertIn("bus_panel", names)
        self.assertIn("autobench_panel", names)
        self.assertIn("wt_panel", names)


class TestAgeStr(unittest.TestCase):
    def test_age_str_seconds(self):
        self.assertEqual(cc_sysmap._age_str(3), "3s")

    def test_age_str_minutes(self):
        self.assertEqual(cc_sysmap._age_str(125), "2m")

    def test_age_str_hours(self):
        self.assertEqual(cc_sysmap._age_str(3700), "1h01m")

    def test_age_str_days(self):
        self.assertEqual(cc_sysmap._age_str(86400 * 3), "3d")


if __name__ == "__main__":
    unittest.main()
