"""Gemini CLI, Cursor CLI and Antigravity CLI sources read synthetic sessions in their real shapes."""
import hashlib
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


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def gem_call(i, command, status, text, key="output", dir_path=None):
    args = {"description": "d", "command": command}
    if dir_path:
        args["dir_path"] = dir_path
    result = None if text is None else [{"functionResponse": {"id": f"c{i}", "name": "run_shell_command",
                                                              "response": {key: text}}}]
    t = {"id": f"c{i}", "name": "run_shell_command", "args": args, "status": status,
         "timestamp": f"2026-10-31T00:00:0{i}Z", "displayName": "Shell"}
    if result is not None:
        t["result"] = result
    return t


def gem_msgs():
    return [
        {"id": "m1", "timestamp": "2026-10-31T00:00:00Z", "type": "user", "content": [{"text": "go"}]},
        {"id": "m2", "timestamp": "2026-10-31T00:00:01Z", "type": "gemini", "content": "ok", "toolCalls": [
            gem_call(1, "make", "success", "Command: make\nDirectory: (root)\nOutput: boom\nError: (none)\nExit Code: 2\nSignal: (none)"),
            gem_call(2, "ls", "success", "Output: a b\nProcess Group PGID: 7", dir_path="/proj"),
            gem_call(3, "rm x", "error", "Tool execution denied by policy", key="error"),
            gem_call(4, "sleep 9", "cancelled", None),
            gem_call(5, "true", "success", "Command: true\nOutput: (empty)\nExit Code: 0"),
            {"id": "c6", "name": "read_file", "args": {"file_path": "x"}, "status": "success"}]},
        {"id": "m3", "timestamp": "2026-10-31T00:00:09Z", "type": "info", "content": "note"},
    ]


class JsonlSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcripts-jsonl-test-")
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp, "USERPROFILE": self.tmp})
        self.env.start()
        for v in ("GEMINI_CLI_HOME", "CURSOR_CONFIG_DIR", "ANTIGRAVITY_CLI_HOME"):
            os.environ.pop(v, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self, agent):
        return list(transcripts.iter_calls([agent]))

    def check_gemini(self, calls):
        by = {c.call_id: c for c in calls}
        self.assertEqual(len(calls), 6)
        c1, c2, c3, c4, c5, c6 = (by[f"c{i}"] for i in range(1, 7))
        self.assertEqual((c1.shell, c1.command, c1.exit_code, c1.is_error), (True, "make", 2, True))
        self.assertIn("boom", c1.output)
        self.assertEqual((c2.exit_code, c2.is_error, c2.cwd), (None, False, "/proj"))
        self.assertEqual((c3.is_error, c3.harness, c3.exit_code), (True, True, None))
        self.assertIn("denied", c3.output)
        self.assertEqual((c4.is_error, c4.output, c4.harness), (True, None, True))
        self.assertEqual((c5.exit_code, c5.is_error), (0, False))
        self.assertEqual((c6.shell, c6.command), (False, ""))
        self.assertEqual(c1.ts, "2026-10-31T00:00:01Z")

    def test_gemini_legacy_json(self):
        path = os.path.join(self.tmp, ".gemini", "tmp", "a" * 64, "chats", "session-2025-01-01T00-00-abcd1234.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"sessionId": "s", "projectHash": "h", "startTime": "t", "lastUpdated": "t",
                       "messages": gem_msgs()}, f)
        self.check_gemini(self.calls("gemini"))

    def test_gemini_current_jsonl(self):
        path = os.path.join(self.tmp, ".gemini", "tmp", "proj", "chats", "session-2026-10-31T00-00-abcd1234.jsonl")
        rows = [{"sessionId": "s", "projectHash": "h", "startTime": "t", "lastUpdated": "t", "kind": "main"}]
        for m in gem_msgs():
            rows += [m, {"$set": {"lastUpdated": "t2"}}]
        rows.append({"lastUpdated": "t3"})
        write_jsonl(path, rows)
        self.check_gemini(self.calls("gemini"))

    def test_gemini_jsonl_repeated_id_keeps_later_line_and_maps_cwd(self):
        root = os.path.join(self.tmp, ".gemini")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "projects.json"), "w") as f:
            json.dump({"projects": {"/work/p": "pshort"}}, f)
        first = {"id": "m", "type": "gemini", "timestamp": "t", "content": "", "toolCalls": [
            gem_call(1, "ls", "success", None)]}
        later = {"id": "m", "type": "gemini", "timestamp": "t", "content": "", "toolCalls": [
            gem_call(1, "ls", "success", "Output: x\nExit Code: 1")]}
        sub = os.path.join(root, "tmp", "pshort", "chats", "parent", "session-x.jsonl")
        write_jsonl(sub, [{"sessionId": "s"}, first, {"$set": {"lastUpdated": "t"}}, later])
        (c,) = self.calls("gemini")
        self.assertEqual((c.exit_code, c.is_error, c.cwd), (1, True, "/work/p"))
        hashed = os.path.join(root, "tmp", hashlib.sha256(b"/work/p").hexdigest(), "chats", "session-y.jsonl")
        write_jsonl(hashed, [{"sessionId": "t"}, first])
        self.assertEqual({r.cwd for r in transcripts.iter_sessions(["gemini"])}, {"/work/p"})

    def test_cursor_transcript_has_no_results(self):
        path = os.path.join(self.tmp, ".cursor", "projects", "slug", "agent-transcripts", "u1", "u1.jsonl")
        write_jsonl(path, [
            {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "running"},
                {"type": "tool_use", "name": "Shell", "input": {"command": "cargo test", "description": "t"}},
                {"type": "tool_use", "name": "Read", "input": {"path": "a", "offset": 1, "limit": 2}}]}},
            {"type": "turn_ended", "status": "error", "error": "x"}])
        calls = self.calls("cursor")
        self.assertEqual([(c.tool, c.shell, c.command) for c in calls],
                         [("Shell", True, "cargo test"), ("Read", False, "")])
        self.assertTrue(all(c.output is None and c.is_error is None and c.exit_code is None for c in calls))
        self.assertEqual({c.session for c in calls}, {"u1"})

    def test_agy_pairs_results_by_position_and_prefers_full(self):
        logs = os.path.join(self.tmp, ".gemini", "antigravity-cli", "brain", "b1", ".system_generated", "logs")

        def planner(i, *cmds):
            return {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                    "created_at": "2026-10-31T00:00:00Z", "content": "",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": c, "Cwd": "/w", "toolAction": "a",
                                                                    "toolSummary": "s", "WaitMsBeforeAsync": 1}}
                                   for c in cmds]}

        def result(i, typ, status, body):
            return {"step_index": i, "source": "MODEL", "type": typ, "status": status,
                    "created_at": "2026-10-31T00:00:01Z",
                    "content": "Created At: t\nCompleted At: t\n\n\t\tThe command " + body if body else "Created At: t\nTool is running as a background task"}

        write_jsonl(os.path.join(logs, "transcript.jsonl"), [planner(0, "SHOULD-NOT-BE-READ")])
        write_jsonl(os.path.join(logs, "transcript_full.jsonl"), [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "go"},
            planner(1, "make", "ls"),
            result(2, "RUN_COMMAND", "DONE", "exited with code 2.\nOutput:\nboom"),
            result(3, "GENERIC", "DONE", "completed successfully.\nOutput:\nx"),
            planner(4, "serve"),
            result(5, "RUN_COMMAND", "RUNNING", None),
            planner(6, "lost"),                        # result step never written
            planner(7, "only-one"),
            {"step_index": 8, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "x"},
            {"step_index": 9, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "y"},
            planner(10, "bad"),
            result(11, "RUN_COMMAND", "ERROR", None)])
        by = {c.command: c for c in self.calls("agy")}
        self.assertNotIn("SHOULD-NOT-BE-READ", by)
        self.assertEqual((by["make"].exit_code, by["make"].is_error, by["make"].cwd), (2, True, "/w"))
        self.assertIn("boom", by["make"].output)
        self.assertEqual((by["ls"].exit_code, by["ls"].is_error), (0, False))
        self.assertEqual((by["serve"].output, by["serve"].exit_code, by["serve"].is_error), (None, None, None))
        self.assertEqual((by["lost"].output, by["lost"].is_error), (None, None))
        self.assertEqual((by["only-one"].output, by["only-one"].is_error), (None, None))   # 2 steps for 1 call: not paired
        self.assertEqual((by["bad"].is_error, by["bad"].harness), (True, True))
        (ref,) = list(transcripts.iter_sessions(["agy"]))
        self.assertEqual((ref.id, ref.extra["full"]), ("b1", True))

    def test_agy_falls_back_to_transcript_jsonl(self):
        logs = os.path.join(self.tmp, ".gemini", "antigravity-cli", "brain", "b2", ".system_generated", "logs")
        write_jsonl(os.path.join(logs, "transcript.jsonl"), [
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "",
             "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "a"}}]},
            {"step_index": 2, "source": "MODEL", "type": "VIEW_FILE", "status": "DONE", "content": "c"}])
        (c,) = self.calls("agy")
        self.assertEqual((c.tool, c.shell), ("view_file", False))


if __name__ == "__main__":
    unittest.main()
