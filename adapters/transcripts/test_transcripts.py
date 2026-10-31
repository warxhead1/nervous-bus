"""Each source reads a synthetic session in its agent's real on-disk shape."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import transcripts  # noqa: E402
from model import shell_text  # noqa: E402


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def claude_pair(i, command, result, is_error, sid="s1"):
    use = {"sessionId": sid, "cwd": "/w", "timestamp": f"2026-10-31T00:00:0{i}Z",
           "message": {"role": "assistant", "content": [
               {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": command}}]}}
    res = {"sessionId": sid, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": is_error, "content": result}]}}
    return [use, res]


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcripts-test-")
        self.env = mock.patch.dict(os.environ, {
            "HOME": self.tmp, "USERPROFILE": self.tmp,
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, ".claude"),
            "CODEX_HOME": os.path.join(self.tmp, ".codex")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self, agent):
        return list(transcripts.iter_calls([agent]))

    def test_claude_session_and_subagent(self):
        proj = os.path.join(self.tmp, ".claude", "projects", "-w")
        write_jsonl(os.path.join(proj, "s1.jsonl"),
                    claude_pair(1, "ls", "a\nb", False) +
                    claude_pair(2, "tail -2 a b", "Exit code 1\ntail: option used in invalid context -- 2", True) +
                    claude_pair(3, "rm x", "This agent is isolated in the worktree /w, but ...", True))
        write_jsonl(os.path.join(proj, "s1", "subagents", "agent-a1.jsonl"),
                    claude_pair(4, "git status", "Exit code 1\nCommand output was lost: tmp full", True, sid="s1"))
        by_cmd = {c.command: c for c in self.calls("claude")}
        self.assertEqual(set(by_cmd), {"ls", "tail -2 a b", "rm x", "git status"})
        self.assertEqual((by_cmd["ls"].is_error, by_cmd["ls"].exit_code), (False, 0))
        self.assertEqual((by_cmd["tail -2 a b"].exit_code, by_cmd["tail -2 a b"].harness), (1, False))
        self.assertTrue(by_cmd["rm x"].harness)          # deny text: no Exit code line
        self.assertTrue(by_cmd["git status"].harness)    # output lost
        self.assertTrue(all(c.shell and c.cwd == "/w" for c in by_cmd.values()))

    def test_codex_modern_command_execution_wins(self):
        path = os.path.join(self.tmp, ".codex", "sessions", "2026", "10", "31", "rollout-x.jsonl")
        write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/repo"}},
            # The same call also shows up as a function_call; it must not be counted twice.
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                  "call_id": "k1", "arguments": "{\"cmd\": \"make\"}"}},
            {"type": "event_msg", "timestamp": "2026-10-31T00:00:00Z", "payload": {
                "type": "item_completed", "item": {
                    "type": "CommandExecution", "id": "k1", "command": ["bash", "-lc", "make"],
                    "cwd": "/repo", "exit_code": 2, "aggregated_output": "make: *** no rule", "status": "failed"}}}])
        (c,) = self.calls("codex")
        self.assertEqual((c.session, c.command, c.exit_code, c.is_error, c.cwd), ("c1", "make", 2, True, "/repo"))

    def test_codex_legacy_function_call(self):
        path = os.path.join(self.tmp, ".codex", "sessions", "2025", "09", "01", "rollout-y.jsonl")
        write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": "c2", "cwd": "/old"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "a",
                                                  "arguments": json.dumps({"command": ["bash", "-lc", "ls -Z"]})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "a",
                                                  "output": "Exit code: 2\nWall time: 0 seconds\nOutput:\nls: bad"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "b",
                                                  "arguments": json.dumps({"command": ["ls"], "workdir": "/d"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "b",
                                                  "output": json.dumps({"output": "x", "metadata": {"exit_code": 0}})}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "call_id": "c",
                                                  "arguments": "{}"}}])
        calls = self.calls("codex")
        shells = [c for c in calls if c.shell]
        self.assertEqual([(c.command, c.exit_code, c.cwd) for c in shells], [("ls -Z", 2, "/old"), ("ls", 0, "/d")])
        self.assertEqual([c.tool for c in calls if not c.shell], ["update_plan"])

    def test_codex_dedupes_one_session_in_two_roots(self):
        row = [{"type": "session_meta", "payload": {"id": "same", "cwd": "/r"}}]
        write_jsonl(os.path.join(self.tmp, ".codex", "sessions", "a", "rollout-1.jsonl"), row)
        acct = os.path.join(self.tmp, ".config", "orca", "codex-accounts", "u1", "home", "sessions")
        write_jsonl(os.path.join(acct, "b", "rollout-1.jsonl"), row)
        self.assertEqual(len(list(transcripts.iter_sessions(["codex"]))), 1)

    def test_shell_text(self):
        self.assertEqual(shell_text(["/bin/zsh", "-lc", "echo hi"]), "echo hi")
        self.assertEqual(shell_text(["git", "status"]), "git status")
        self.assertEqual(shell_text("x"), "x")

    def test_unknown_agent_is_an_error(self):
        with self.assertRaises(SystemExit):
            list(transcripts.iter_calls(["nope"]))


if __name__ == "__main__":
    unittest.main()
