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


def user(text):
    return {"type": "user", "message": {"content": text}}


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
        self.pub = mock.patch.object(ny, "publish")
        self.published = self.pub.start()

    def tearDown(self):
        self.home.stop()
        self.pub.stop()

    def run_hook(self, *entries, **extra):
        data = {"session_id": "s1", "cwd": str(self.tmp),
                "transcript_path": transcript(self.tmp, *entries), **extra}
        return ny.hook(data)

    def test_tool_turn_without_block_is_blocked_once(self):
        out = self.run_hook(user("go"), tool_use(), tool_result(), text("Done."))
        self.assertEqual(out["decision"], "block")
        again = self.run_hook(user("go"), tool_use(), tool_result(), text("Done."),
                              stop_hook_active=True)
        self.assertIsNone(again)

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

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"NEEDS_YOU_DISABLE": "1"}), \
                mock.patch.object(sys, "stdin", io.StringIO("{bad json")):
            self.assertEqual(ny.main(["hook"]), 0)


class Schema(unittest.TestCase):
    def test_payload_validates(self):
        jsonschema = __import__("jsonschema")
        schema = json.loads((Path(__file__).parents[2] / "schemas/agent.attention.v1.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(
            {"session_id": "s", "project": "p", "cwd": "/x", "items": ["a"]})


if __name__ == "__main__":
    unittest.main()
