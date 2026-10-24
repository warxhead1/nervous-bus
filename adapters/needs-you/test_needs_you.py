import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import needs_you as ny  # noqa: E402


def transcript(tmp: Path, *entries: dict) -> str:
    p = tmp / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return str(p)


def user(text, uuid="u1"):
    return {"type": "user", "uuid": uuid, "message": {"content": text}}


def tool_use():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}


def tool_result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def text(t):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}


class Extract(unittest.TestCase):
    def test_bullets(self):
        r = "Done.\n\n**Needs you:**\n- merge PR #5\n- pick a name\n"
        self.assertEqual(ny.extract(r), ["merge PR #5", "pick a name"])

    def test_nothing(self):
        self.assertEqual(ny.extract("Landed.\n**Needs you:** nothing"), [])
        self.assertEqual(ny.extract("**Needs you**: Nothing — all landed."), [])

    def test_inline_item(self):
        self.assertEqual(ny.extract("**Needs you:** run `! gh auth login`"), ["run `! gh auth login`"])

    def test_parenthetical_note_before_colon(self):
        r = "**Needs you** (for Slack, whenever convenient):\n- create the workspace"
        self.assertEqual(ny.extract(r), ["create the workspace"])

    def test_absent(self):
        self.assertIsNone(ny.extract("It needs you to look. Needs you to decide later."))

    def test_last_block_wins_and_stops_at_prose(self):
        r = "**Needs you:** nothing\n\nlater\n**Needs you:**\n\n- a\n- b\nTrailing prose\n- not this"
        self.assertEqual(ny.extract(r), ["a", "b"])

    def test_stretch_markup_resolved(self):
        r = "**Needs you:**\n- {{merge|merge PR #5 after {{CI|the CI rerun}}}}"
        self.assertEqual(ny.extract(r), ["merge PR #5 after the CI rerun"])


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = mock.patch.object(ny, "SESSIONS", self.tmp / "sessions")
        self.home.start()
        self.logdir = mock.patch.object(ny, "HOME", self.tmp)
        self.logdir.start()
        self.names = mock.patch.object(ny, "NAMES", self.tmp / "names")
        self.names.start()
        self.bindings = mock.patch.object(ny, "BINDINGS", self.tmp / "bindings")
        self.bindings.start()
        self.pub = mock.patch.object(ny, "publish")
        self.published = self.pub.start()

    def tearDown(self):
        self.home.stop()
        self.logdir.stop()
        self.names.stop()
        self.bindings.stop()
        self.pub.stop()

    def run_hook(self, *entries, **extra):
        data = {"session_id": "s1", "cwd": str(self.tmp),
                "transcript_path": transcript(self.tmp, *entries), **extra}
        return ny.hook(data)

    def test_tool_turn_without_block_is_blocked_once(self):
        out = self.run_hook(user("go"), tool_use(), tool_result(), text("Done."))
        self.assertEqual(out["decision"], "block")
        again = self.run_hook(user("go"), tool_use(), tool_result(), text("Done."))
        self.assertIsNone(again)

    def test_another_hooks_block_does_not_exempt_the_turn(self):
        # claim-gate blocked first, so Claude Code reports stop_hook_active=True;
        # this hook has not blocked this turn yet and must still enforce.
        out = self.run_hook(user("go"), tool_use(), tool_result(), text("Done."),
                            stop_hook_active=True)
        self.assertEqual(out["decision"], "block")

    def test_new_turn_is_enforced_again(self):
        self.run_hook(user("go", "u1"), tool_use(), tool_result(), text("Done."))
        out = self.run_hook(user("next", "u2"), tool_use(), tool_result(), text("Done."))
        self.assertEqual(out["decision"], "block")

    def test_every_run_is_logged(self):
        self.run_hook(user("q"), text("An answer."))
        rec = json.loads((self.tmp / "hook.log").read_text().splitlines()[-1])
        self.assertEqual(rec["decision"], "pass_chat_turn")

    def test_chat_turn_without_block_passes(self):
        self.assertIsNone(self.run_hook(user("q"), text("An answer.")))

    def test_earlier_turn_tools_do_not_count(self):
        self.assertIsNone(self.run_hook(tool_use(), tool_result(), text("x"), user("q"), text("y")))

    def test_block_recorded_and_superseded(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- merge #5"))
        self.assertEqual([e["items"] for e in ny.load()], [["merge #5"]])
        self.assertEqual(self.published.call_count, 1)
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:** nothing"))
        self.assertEqual(ny.load(), [])

    def test_last_assistant_message_preferred(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("stale"),
                      last_assistant_message="**Needs you:**\n- fresh")
        self.assertEqual(ny.load()[0]["items"], ["fresh"])

    def test_ack(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ny.cmd_ack("1"), 0)
        self.assertEqual(ny.load(), [])
        self.assertEqual(len(ny.load(include_acked=True)), 1)

    def test_headless_runs_are_ignored(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
            self.assertIsNone(self.run_hook(user("go"), tool_use(), tool_result(), text("x")))
            self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- y"))
        self.assertEqual(ny.load(), [])

    def test_summary_and_terminal_recorded(self):
        with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_1", "CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.run_hook(user("go"), tool_use(), tool_result(),
                          text("## Merged PR #88.\n\n**Needs you:**\n- restart"))
        e = ny.load()[0]
        self.assertEqual((e["summary"], e["orca_terminal"]), ("Merged PR #88.", "term_1"))

    def test_name_defaults_from_worktree_dir(self):
        self.assertEqual(ny.default_name("/home/e/data2/worktrees/hearth/Slack_Live"), "slack-live")
        self.assertEqual(ny.default_name("/home/e/data2/worktrees/hearth/agent-a0915112f20f"), "")
        self.assertEqual(ny.default_name("/home/e/projects/hearth"), "")

    def test_chosen_name_wins_and_is_published(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s1"}), \
                mock.patch.object(ny.subprocess, "run"), redirect_stdout(io.StringIO()):
            self.assertEqual(ny.cmd_name("Slack Mentions!"), 0)
        self.assertEqual(ny.name_of("s1", "/w/worktrees/x/other"), "slack-mentions")
        self.assertEqual(ny.slugify("x" * 40), "x" * 32)

    def test_name_needs_a_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(ny.cmd_name("x"), 1)

    def test_stop_hook_records_the_terminal_binding(self):
        env = {"ORCA_TERMINAL_HANDLE": "term_9", "TMUX_PANE": "", "CLAUDE_PID": "42",
               "CLAUDE_CODE_ENTRYPOINT": "cli"}
        with mock.patch.dict(os.environ, env):
            self.run_hook(user("q"), text("An answer."))
        rec = json.loads((self.tmp / "bindings" / "s1.json").read_text())
        self.assertEqual((rec["orca_terminal"], rec["pid"]), ("term_9", 42))

    def test_no_terminal_no_binding(self):
        with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "", "TMUX_PANE": ""}):
            ny.bind("s2", "/x")
        self.assertFalse((self.tmp / "bindings" / "s2.json").exists())

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"NEEDS_YOU_DISABLE": "1"}), \
                mock.patch.object(sys, "stdin", io.StringIO("{bad json")):
            self.assertEqual(ny.main(["hook"]), 0)


class Schema(unittest.TestCase):
    def test_payload_validates(self):
        jsonschema = __import__("jsonschema")
        schema = json.loads((Path(__file__).parents[2] / "schemas/agent.attention.v1.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(
            {"session_id": "s", "project": "p", "cwd": "/x", "items": ["a"],
             "summary": "done", "orca_terminal": "term_1", "name": "slack-live"})



class CacheFromTranscript(unittest.TestCase):
    def _write(self, rows):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    @staticmethod
    def _call(ts, w1h=0, w5m=0, read=0):
        return {"type": "assistant", "timestamp": ts, "message": {"model": "m", "usage": {
            "input_tokens": 2, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": w1h + w5m,
            "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}}}

    def test_one_hour_write_sets_warm_until_and_context(self):
        p = self._write([self._call("2026-10-24T16:00:00.000Z", w1h=500, read=100000)])
        c = ny.cache_from_transcript(p)
        self.assertEqual(c["ttl_s"], 3600)
        self.assertEqual(c["warm_until"], "2026-10-24T17:00:00Z")
        self.assertEqual(c["context_tokens"], 100502)

    def test_read_only_call_inherits_earlier_write_ttl(self):
        p = self._write([self._call("2026-10-24T15:00:00Z", w5m=900),
                         {"type": "user", "message": {"content": "hi"}},
                         self._call("2026-10-24T15:02:00Z", read=5000)])
        c = ny.cache_from_transcript(p)
        self.assertEqual((c["ttl_s"], c["warm_until"]), (300, "2026-10-24T15:07:00Z"))

    def test_missing_transcript_gives_none(self):
        self.assertIsNone(ny.cache_from_transcript("/nonexistent.jsonl"))

if __name__ == "__main__":
    unittest.main()
