#!/usr/bin/env python3
"""Tests for cc-agents panel."""
import json
import sys
import unittest
from pathlib import Path
from io import StringIO
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).parent))

from cc_agents import (
    SessionInfo,
    _derive_project,
    _agent_color,
    _state_color,
    enrich,
    filter_active,
    render_table,
    panel_cc_agents,
    AGENT_COLORS,
    STATE_COLORS,
    KNOWN_PROJECTS,
)


class TestSessionInfo(unittest.TestCase):
    def test_agent_property_claudecode(self):
        s = SessionInfo(
            pane_id="zellij:default:0", agent_type="claudecode",
            state="active", message_count=10, started_at="12:00:00",
            execution_id=None, task_id=None, title=None,
        )
        self.assertEqual(s.agent, "claude-code")

    def test_agent_property_opencode(self):
        s = SessionInfo(
            pane_id="zellij:default:0", agent_type="opencode",
            state="active", message_count=10, started_at="12:00:00",
            execution_id=None, task_id=None, title=None,
        )
        self.assertEqual(s.agent, "opencode")

    def test_agent_property_unknown(self):
        s = SessionInfo(
            pane_id="zellij:default:0", agent_type="",
            state="active", message_count=10, started_at="12:00:00",
            execution_id=None, task_id=None, title=None,
        )
        self.assertEqual(s.agent, "?")


class TestDeriveProject(unittest.TestCase):
    def test_zellij_session(self):
        self.assertEqual(_derive_project("zellij:tengine:0"), "tengine")
        self.assertEqual(_derive_project("zellij:home-automation:5"), "home-automation")
        self.assertEqual(_derive_project("zellij:hearth-loom:1"), "hearth-loom")

    def test_docker_loomie(self):
        self.assertEqual(_derive_project("docker:hearth-loom-agent-abc123"), "hearth-loom")
        self.assertEqual(_derive_project("docker:something-else"), "loomie")

    def test_host(self):
        self.assertEqual(_derive_project("host:myhost:0"), "myhost")

    def test_empty(self):
        self.assertEqual(_derive_project(""), "?")

    def test_unknown_format(self):
        self.assertEqual(_derive_project("command-center:10"), "command-center")


class TestAgentColor(unittest.TestCase):
    def test_known_agents(self):
        self.assertEqual(_agent_color("claude-code"), "cyan")
        self.assertEqual(_agent_color("opencode"), "magenta")
        self.assertEqual(_agent_color("codex"), "yellow")
        self.assertEqual(_agent_color("gemini"), "green")
        self.assertEqual(_agent_color("cursor"), "blue")

    def test_unknown(self):
        self.assertEqual(_agent_color("unknown"), "dim")
        self.assertEqual(_agent_color("foobar"), "dim")


class TestStateColor(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(_state_color("active"), "green")
        self.assertEqual(_state_color("running"), "green")
        self.assertEqual(_state_color("busy"), "yellow")
        self.assertEqual(_state_color("idle"), "dim")
        self.assertEqual(_state_color("stopped"), "dim")

    def test_unknown(self):
        self.assertEqual(_state_color("?"), "dim")
        self.assertEqual(_state_color("foobar"), "dim")


class TestFilterActive(unittest.TestCase):
    def test_filters_active(self):
        sessions = [
            SessionInfo(pane_id="zellij:tengine:0", agent_type="claudecode",
                        state="active", message_count=1, started_at="",
                        execution_id=None, task_id=None, title=None),
            SessionInfo(pane_id="zellij:tengine:1", agent_type="opencode",
                        state="stopped", message_count=5, started_at="",
                        execution_id=None, task_id=None, title=None),
        ]
        active = filter_active(sessions)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].pane_id, "zellij:tengine:0")

    def test_passes_running(self):
        sessions = [
            SessionInfo(pane_id="zellij:tengine:0", agent_type="claudecode",
                        state="running", message_count=1, started_at="",
                        execution_id=None, task_id=None, title=None),
        ]
        self.assertEqual(len(filter_active(sessions)), 1)

    def test_passes_busy(self):
        sessions = [
            SessionInfo(pane_id="zellij:tengine:0", agent_type="claudecode",
                        state="busy", message_count=1, started_at="",
                        execution_id=None, task_id=None, title=None),
        ]
        self.assertEqual(len(filter_active(sessions)), 1)


class TestEnrich(unittest.TestCase):
    def test_enrich_no_execution_ids(self):
        sessions = [
            SessionInfo(pane_id="zellij:tengine:0", agent_type="claudecode",
                        state="active", message_count=1, started_at="",
                        execution_id=None, task_id=None, title=None),
        ]
        result = enrich(sessions)
        self.assertEqual(result[0].project, "?")   # enrich() doesn't re-derive project; only fetch_sessions() does
        self.assertEqual(result[0].bead, "")

    def test_enrich_matches_task(self):
        sessions = [
            SessionInfo(pane_id="zellij:tengine:0", agent_type="claudecode",
                        state="active", message_count=1, started_at="",
                        execution_id="exec-abc", task_id=None, title=None,
                        project="tengine"),
        ]
        tasks_by_eid = {
            "exec-abc": {
                "execution_id": "exec-abc",
                "title": "EXEC-123 fix bug",
                "project": "tengine",
            }
        }
        import cc_agents
        original_fetch = cc_agents.fetch_loom_tasks
        cc_agents.fetch_loom_tasks = lambda ids: tasks_by_eid
        try:
            result = enrich(sessions)
            self.assertEqual(result[0].bead, "EXEC-123 fix bug")
            self.assertEqual(result[0].project, "tengine")
        finally:
            cc_agents.fetch_loom_tasks = original_fetch


class TestKnownProjects(unittest.TestCase):
    def test_has_expected_projects(self):
        for proj in ["hearth-loom", "tengine", "nervous-bus", "home-automation"]:
            self.assertIn(proj, KNOWN_PROJECTS)


class TestHashDedup(unittest.TestCase):
    def _make_session(self, pane="zellij:tengine:0", state="active", msgs=1):
        return SessionInfo(
            pane_id=pane, agent_type="claudecode", state=state,
            message_count=msgs, started_at="", execution_id=None,
            task_id=None, title=None, project="tengine",
        )

    def test_same_sessions_produce_same_hash(self):
        import json
        s1 = self._make_session()
        s2 = self._make_session()
        h1 = json.dumps([(s.pane_id, s.state, s.message_count, s.bead, s.project) for s in [s1]])
        h2 = json.dumps([(s.pane_id, s.state, s.message_count, s.bead, s.project) for s in [s2]])
        self.assertEqual(h1, h2)

    def test_changed_message_count_produces_different_hash(self):
        import json
        s1 = self._make_session(msgs=1)
        s2 = self._make_session(msgs=2)
        h1 = json.dumps([(s.pane_id, s.state, s.message_count, s.bead, s.project) for s in [s1]])
        h2 = json.dumps([(s.pane_id, s.state, s.message_count, s.bead, s.project) for s in [s2]])
        self.assertNotEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
