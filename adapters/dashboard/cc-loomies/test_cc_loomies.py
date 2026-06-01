#!/usr/bin/env python3
"""Tests for cc-loomies panel."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from io import StringIO
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).parent))

from cc_loomies import (
    LoomieHeartbeat,
    LoomieState,
    poll_hearth_loom,
    _format_age,
    _phase_color,
    _risk_color,
    _risk_label,
    panel_header,
    panel_body,
)


class TestLoomieHeartbeat(unittest.TestCase):
    def test_from_json_basic(self):
        data = {
            "task_id": "abc12345",
            "title": "test task",
            "project": "my-project",
            "phase": "agent_active",
            "phase_age_seconds": 120,
            "last_tool_call_age_seconds": 50,
            "kill_risk": "low",
            "tokens_input_total": 1000,
            "tokens_output_total": 2000,
            "estimated_cost_usd": 0.05,
            "token_burn_per_min": 10.0,
            "container_uptime_seconds": 300,
        }
        hb = LoomieHeartbeat.from_json(data)
        self.assertEqual(hb.task_id, "abc12345")
        self.assertEqual(hb.project, "my-project")
        self.assertEqual(hb.phase, "agent_active")
        self.assertEqual(hb.phase_age_seconds, 120)
        self.assertFalse(hb.is_stuck)

    def test_from_json_stuck(self):
        data = {
            "task_id": "stuck-task",
            "phase": "agent_active",
            "last_tool_call_age_seconds": 400,
        }
        hb = LoomieHeartbeat.from_json(data)
        self.assertTrue(hb.is_stuck)

    def test_from_json_missing_fields(self):
        data = {}
        hb = LoomieHeartbeat.from_json(data)
        self.assertEqual(hb.task_id, "?")
        self.assertEqual(hb.phase, "unknown")
        self.assertFalse(hb.is_stuck)


class TestLoomieState(unittest.TestCase):
    def test_phase_counts_empty(self):
        state = LoomieState()
        counts = state.phase_counts()
        self.assertEqual(counts, {})

    def test_phase_counts(self):
        state = LoomieState()
        state.heartbeats = [
            LoomieHeartbeat.from_json({"task_id": "1", "phase": "agent_active"}),
            LoomieHeartbeat.from_json({"task_id": "2", "phase": "agent_active"}),
            LoomieHeartbeat.from_json({"task_id": "3", "phase": "queued"}),
        ]
        counts = state.phase_counts()
        self.assertEqual(counts["agent_active"], 2)
        self.assertEqual(counts["queued"], 1)

    def test_stuck_count(self):
        state = LoomieState()
        state.heartbeats = [
            LoomieHeartbeat.from_json({"task_id": "1", "last_tool_call_age_seconds": 400}),
            LoomieHeartbeat.from_json({"task_id": "2", "last_tool_call_age_seconds": 50}),
        ]
        self.assertEqual(state.stuck_count(), 1)

    def test_total_cost_per_hr(self):
        state = LoomieState()
        state.heartbeats = [
            LoomieHeartbeat.from_json({"task_id": "1", "token_burn_per_min": 10.0}),
            LoomieHeartbeat.from_json({"task_id": "2", "token_burn_per_min": 20.0}),
        ]
        cost = state.total_cost_per_hr()
        self.assertGreater(cost, 0)


class TestHelpers(unittest.TestCase):
    def test_format_age(self):
        self.assertEqual(_format_age(30), "30s")
        self.assertEqual(_format_age(120), "2m")
        self.assertEqual(_format_age(3700), "1h")

    def test_phase_color(self):
        self.assertEqual(_phase_color("agent_active"), "green")
        self.assertEqual(_phase_color("failed"), "bright_red")
        self.assertEqual(_phase_color("unknown"), "dim")

    def test_risk_color(self):
        self.assertEqual(_risk_color("low"), "green")
        self.assertEqual(_risk_color("high"), "bright_red")

    def test_risk_label(self):
        self.assertEqual(_risk_label("medium"), "MEDIUM")
        self.assertEqual(_risk_label("l"), "L")


if __name__ == "__main__":
    unittest.main()