import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        # Isolate from any real Hearth server on localhost.
        self.env = mock.patch.dict(os.environ, {
            "HEARTH_API_URL": "http://127.0.0.1:1",
            "HEARTH_API_TOKEN": "test-token",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
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
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(ny, "hearth_respond", return_value=("ok", {})) as hr:
            self.assertEqual(ny.cmd_ack("1"), 0)
        hr.assert_called_once_with("ny-s1", None, "terminal")
        self.assertEqual(ny.load(), [])
        self.assertFalse((self.tmp / "sessions" / "s1.json").exists())

    def test_ack_by_session_id(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(ny, "hearth_respond", return_value=("ok", {})) as hr:
            self.assertEqual(ny.cmd_ack("s-unknown"), 1)
            self.assertEqual(ny.cmd_ack("s1"), 0)
        hr.assert_called_once_with("ny-s1", None, "terminal")
        self.assertEqual(ny.load(), [])

    def test_ack_cli_carries_answers_and_surface(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(ny, "hearth_respond", return_value=("ok", {})) as hr:
            self.assertEqual(ny.main(["ack", "s1", "--via", "kiln", "--answer", "ship it"]), 0)
        hr.assert_called_once_with("ny-s1", ["ship it"], "kiln")

    def test_a_failed_answer_keeps_the_ask_open(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(ny, "hearth_respond", return_value=("unreachable", None)):
            self.assertEqual(ny.main(["ack", "s1", "--answer", "ship it"]), 3)
        self.assertIn("answer not sent", err.getvalue())
        self.assertEqual(len(ny.load()), 1)

    def test_answers_need_one_target(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.object(ny, "hearth_respond", return_value=("ok", {})) as hr:
            self.assertEqual(ny.main(["ack", "all", "--answer", "yes"]), 1)
            self.assertEqual(ny.main(["ack", "s1", "--via", "Bad Surface"]), 2)
        hr.assert_not_called()
        self.assertEqual(len(ny.load()), 1)

    def test_ack_unreachable_keeps_local_flip_and_says_so(self):
        self.run_hook(user("go"), tool_use(), tool_result(), text("**Needs you:**\n- x"))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(ny, "hearth_respond", return_value=("unreachable", None)):
            self.assertEqual(ny.cmd_ack("all"), 0)
        self.assertEqual(ny.load(), [])
        self.assertIn("phone row not cleared", out.getvalue())

    def test_hook_links_decision_id_and_hints_once(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.run_hook(user("go"), tool_use(), tool_result(),
                          text("**Needs you:**\n- pick on src-lab-7\n- Hearth -> xmas-mixtape"))
            self.run_hook(user("go"), tool_use(), tool_result(),
                          text("**Needs you:**\n- Hearth → xmas-mixtape"))
        self.assertEqual(ny.load()[0]["items"], ["Hearth → xmas-mixtape"])
        self.assertEqual(err.getvalue().count("name the exact tap path"), 1)


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

    def test_turn_state_follows_the_hooks(self):
        env = {"ORCA_TERMINAL_HANDLE": "term_9", "TMUX_PANE": "", "CLAUDE_PID": "42",
               "CLAUDE_CODE_ENTRYPOINT": "cli"}
        path = self.tmp / "bindings" / "s1.json"
        stdin = json.dumps({"session_id": "s1", "cwd": "/w", "transcript_path": "/t.jsonl"})
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            ny.main(["turn", "busy"])
        rec = json.loads(path.read_text())
        self.assertEqual((rec["turn"], rec["transcript"]), ("busy", "/t.jsonl"))
        with mock.patch.dict(os.environ, env):
            # a Stop this hook blocks continues the turn: still busy
            self.run_hook(user("go"), tool_use(), tool_result(), text("Done."))
            self.assertEqual(json.loads(path.read_text())["turn"], "busy")
            self.run_hook(user("q"), text("An answer."))
        rec = json.loads(path.read_text())
        self.assertEqual(rec["turn"], "idle")
        self.assertTrue(rec["transcript"].endswith(".jsonl"))
        self.assertGreater(rec["turn_ts"], 0)

    def _goal_fire(self, n):
        cond = "finish the open issues"
        return [user(f"Stop hook feedback:\n[{cond}]: Not satisfied. Issues remain.", f"g{n}"),
                {"type": "attachment", "attachment": {"type": "goal_status", "met": False, "condition": cond}},
                text("Still paused, as you asked.\n\n**Needs you:**\n- pause or clear the goal")]

    def _work_turn(self):
        return [user("do the issues", "w1"), tool_use(), tool_result(),
                text("Paused.\n\n**Needs you:**\n- pause or clear the goal")]

    def test_goal_loop_breaker_fires_on_the_third_noop_reopen_not_before(self):
        entries = self._work_turn()
        for n in (1, 2):
            entries += self._goal_fire(n)
            self.assertIsNone(self.run_hook(*entries), f"fired after {n} re-opens")
        entries += self._goal_fire(3)
        out = self.run_hook(*entries)
        self.assertIs(out["continue"], False)
        self.assertIn("3 re-opens", out["stopReason"])
        items = ny.load()[0]["items"]
        self.assertTrue(items[0].startswith("Goal checker is looping"))
        self.assertIn("pause or clear the goal", items)

    def test_goal_reopen_that_does_work_resets_the_streak(self):
        entries = self._work_turn() + self._goal_fire(1) + self._goal_fire(2)
        entries += [user("Stop hook feedback:\n[finish the open issues]: Not satisfied.", "g3"),
                    {"type": "attachment", "attachment": {"type": "goal_status", "met": False}},
                    tool_use(), tool_result(), text("Fixed one.\n\n**Needs you:** nothing")]
        self.assertIsNone(self.run_hook(*entries))
        entries += self._goal_fire(4) + self._goal_fire(5)
        self.assertIsNone(self.run_hook(*entries))

    def test_other_stop_hook_feedback_is_not_a_goal_fire(self):
        entries = self._work_turn()
        for n in range(3):
            entries += [user("Stop hook feedback:\nThis turn used tools but the reply has no block.", f"b{n}"),
                        text("**Needs you:** nothing")]
        self.assertIsNone(self.run_hook(*entries))

    def test_goal_loop_without_a_needs_you_block_still_stops(self):
        entries = self._work_turn()
        for n in range(3):
            entries += self._goal_fire(n)[:2] + [text("Still paused.")]
        self.assertIs(self.run_hook(*entries)["continue"], False)

    def test_no_terminal_no_binding(self):
        with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "", "TMUX_PANE": ""}):
            ny.bind("s2", "/x")
        self.assertFalse((self.tmp / "bindings" / "s2.json").exists())

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"NEEDS_YOU_DISABLE": "1"}), \
                mock.patch.object(sys, "stdin", io.StringIO("{bad json")):
            self.assertEqual(ny.main(["hook"]), 0)


class LinkItem(unittest.TestCase):
    def test_decision_id_gets_link(self):
        self.assertEqual(ny.link_item("answer dec-abc123 please"),
                         "answer dec-abc123 please <hearth://decisions/dec-abc123>")
        self.assertEqual(ny.link_item("src-lab-9."),
                         "src-lab-9. <hearth://decisions/src-lab-9>")

    def test_existing_trailing_link_untouched(self):
        i = "pick dec-1 <hearth://artifacts/a1>"
        self.assertEqual(ny.link_item(i), i)

    def test_inline_url_moves_to_end(self):
        self.assertEqual(ny.link_item("open hearth://artifacts/a1 and pick"),
                         "open and pick <hearth://artifacts/a1>")
        self.assertEqual(ny.link_item("open <hearth://artifacts/a1> now, dec-5"),
                         "open now, dec-5 <hearth://artifacts/a1>")

    def test_plain_and_prose_untouched(self):
        self.assertEqual(ny.link_item("merge PR #5"), "merge PR #5")
        self.assertEqual(ny.link_item("Hearth → xmas-mixtape"), "Hearth → xmas-mixtape")

    def test_extract_applies_links(self):
        self.assertEqual(ny.extract("**Needs you:**\n- decide dec-9"),
                         ["decide dec-9 <hearth://decisions/dec-9>"])


class HearthAck(unittest.TestCase):
    class Resp:
        def __init__(self, status, body=b"{}"):
            self.status, self._b = status, body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_posts_ack_via_cli_with_local_token(self):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append((req.full_url, req.get_header("Authorization")))
            if req.full_url.endswith("/api/auth/local-token"):
                return self.Resp(200, b'{"access_token":"T"}')
            return self.Resp(200)
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch.dict(os.environ, {"HEARTH_API_TOKEN": ""}):
            self.assertTrue(ny.hearth_ack("s9"))
        self.assertTrue(calls[-1][0].endswith("/api/attention/s9/ack?via=cli"))
        self.assertEqual(calls[-1][1], "Bearer T")

    def test_answers_go_in_the_ack_body(self):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append((req.full_url, json.loads(req.data or b"{}")))
            return self.Resp(200)
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch.dict(os.environ, {"HEARTH_API_TOKEN": "T"}):
            self.assertTrue(ny.hearth_ack("s9", ["yes, embers first"], ["Pick the scope"], "kiln"))
        url, body = calls[-1]
        self.assertTrue(url.endswith("/api/attention/s9/ack?via=kiln"))
        self.assertEqual(body, {"answers": ["yes, embers first"], "items": ["Pick the scope"]})

    def test_a_bare_ack_sends_an_empty_body(self):
        calls = []

        def fake(req, timeout=None, context=None):
            calls.append(json.loads(req.data or b"{}"))
            return self.Resp(200)
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch.dict(os.environ, {"HEARTH_API_TOKEN": "T"}):
            self.assertTrue(ny.hearth_ack("s9"))
        self.assertEqual(calls[-1], {})

    def test_unreachable_is_false(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(ny.hearth_ack("s9"))


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


class _FakeHearth:
    """In-process fake Hearth /api/decisions server for integration tests."""

    def __init__(self, *, reject_errand: bool = False, hang_s: float = 0.0):
        self.reject_errand = reject_errand
        self.hang_s = hang_s
        self.decisions: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.attention_acks: list[tuple[str, dict]] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _send(self, code: int, payload: dict):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if parent.hang_s:
                    time.sleep(parent.hang_s)
                parent.requests.append(("GET", self.path, {}))
                if self.path.startswith("/api/decisions"):
                    rows = list(parent.decisions.values())
                    if "status=pending" in self.path:
                        rows = [r for r in rows if r.get("status") == "pending"]
                    self._send(200, {"count": len(rows), "decisions": rows})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if parent.hang_s:
                    time.sleep(parent.hang_s)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                parent.requests.append(("POST", self.path, body))

                if self.path == "/api/auth/local-token":
                    self._send(200, {"access_token": "fake-local-token"})
                    return

                if self.path == "/api/decisions":
                    if parent.reject_errand and body.get("kind") == "errand":
                        self._send(400, {"error": "unsupported kind: errand"})
                        return
                    did = body.get("decision_id") or "dec-auto"
                    row = {
                        **body,
                        "decision_id": did,
                        "correlation_id": did,
                        "status": "pending",
                        "created_at": "2026-11-26T18:00:00Z",
                    }
                    parent.decisions[did] = row
                    self._send(200, row)
                    return

                if self.path.startswith("/api/decisions/") and self.path.endswith("/withdraw"):
                    did = self.path[len("/api/decisions/"):-len("/withdraw")]
                    if parent.reject_errand and did.startswith("ny-"):
                        self._send(400, {"error": "unsupported kind: errand"})
                        return
                    row = parent.decisions.get(did)
                    if not row:
                        self._send(404, {"error": "no such decision"})
                        return
                    row["status"] = "withdrawn"
                    self._send(200, row)
                    return

                if self.path.startswith("/api/decisions/") and self.path.endswith("/respond"):
                    did = self.path[len("/api/decisions/"):-len("/respond")]
                    if parent.reject_errand:
                        self._send(400, {"error": "unsupported kind: errand"})
                        return
                    row = parent.decisions.get(did)
                    if not row:
                        self._send(404, {"error": "no such decision"})
                        return
                    if row.get("status") != "pending":
                        self._send(409, {"error": "already settled", "decision": row})
                        return
                    row["status"] = "answered"
                    row["done"] = body.get("done")
                    row["text"] = body.get("text")
                    row["via"] = body.get("via")
                    self._send(200, row)
                    return

                if self.path.startswith("/api/attention/") and "/ack" in self.path:
                    parent.attention_acks.append((self.path, body))
                    self._send(200, {"ok": True})
                    return

                self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class FakeHearthDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(ny, "SESSIONS", self.tmp / "sessions"),
            mock.patch.object(ny, "HOME", self.tmp),
            mock.patch.object(ny, "NAMES", self.tmp / "names"),
            mock.patch.object(ny, "BINDINGS", self.tmp / "bindings"),
            mock.patch.object(ny, "publish"),
        ]
        for p in self.patches[:-1]:
            p.start()
        self.published = self.patches[-1].start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _run_hook(self, sid: str, reply: str):
        data = {
            "session_id": sid,
            "cwd": str(self.tmp),
            "transcript_path": transcript(self.tmp, user("go"), tool_use(), tool_result(), text(reply)),
        }
        return ny.hook(data)

    def test_upsert_replace_and_withdraw_on_nothing(self):
        srv = _FakeHearth()
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": ""}):
            # 1. Upsert with 2 bullets -> one part per bullet, kind=errand, id=ny-s1
            self.assertIsNone(self._run_hook("s1", "Finished build.\n\n**Needs you:**\n- merge PR #5\n- tag release"))
            row = srv.decisions["ny-s1"]
            self.assertEqual(row["kind"], "errand")
            self.assertEqual(row["relay_mode"], "session_text")
            self.assertEqual(row["session_id"], "s1")
            self.assertEqual([p["label"] for p in row["parts"]], ["merge PR #5", "tag release"])
            self.assertIn("- merge PR #5", row["context"])
            self.assertEqual(self.published.call_count, 1)
            cached = json.loads((self.tmp / "sessions" / "s1.json").read_text())
            self.assertTrue(cached["synced"])
            self.assertFalse(cached["pending_sync"])

            # 2. Replaced block upserts the same ny-s1 id
            self.assertIsNone(self._run_hook("s1", "Updated.\n\n**Needs you:**\n- approve deploy"))
            row = srv.decisions["ny-s1"]
            self.assertEqual([p["label"] for p in row["parts"]], ["approve deploy"])
            self.assertEqual(self.published.call_count, 2)

            # 3. Needs you: nothing withdraws ny-s1 and prunes cache
            self.assertIsNone(self._run_hook("s1", "All clear.\n\n**Needs you:** nothing"))
            self.assertEqual(srv.decisions["ny-s1"]["status"], "withdrawn")
            self.assertFalse((self.tmp / "sessions" / "s1.json").exists())
            self.assertEqual(self.published.call_count, 3)

    def test_offline_cache_and_replay_on_next_successful_run(self):
        # Allocate a closed port so Hearth is unreachable
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()

        with mock.patch.dict(os.environ, {"HEARTH_API_URL": f"http://127.0.0.1:{dead_port}",
                                          "HEARTH_API_TOKEN": "tok"}):
            self._run_hook("s-offline", "**Needs you:**\n- review draft")
            cached = json.loads((self.tmp / "sessions" / "s-offline.json").read_text())
            self.assertTrue(cached["pending_sync"])
            self.assertFalse(cached["synced"])
            # Offline list falls back to cache
            entries = ny.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["session_id"], "s-offline")

        # Now Hearth comes up; next Stop hook replays cached offline row
        srv = _FakeHearth()
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}):
            self._run_hook("s-online", "**Needs you:**\n- ship it")
            self.assertIn("ny-s-online", srv.decisions)
            self.assertIn("ny-s-offline", srv.decisions)
            self.assertEqual(srv.decisions["ny-s-offline"]["parts"][0]["label"], "review draft")
            replayed_cache = json.loads((self.tmp / "sessions" / "s-offline.json").read_text())
            self.assertTrue(replayed_cache["synced"])
            self.assertFalse(replayed_cache["pending_sync"])

    def test_list_and_ack_wrappers_prune_settled_cache_and_handle_409(self):
        srv = _FakeHearth()
        self.addCleanup(srv.close)
        # Add a non-errand choice decision to verify list filters to errand
        srv.decisions["dec-choice-1"] = {
            "decision_id": "dec-choice-1",
            "project": "demo",
            "kind": "choice",
            "title": "Not an errand",
            "options": [{"id": "a", "label": "A"}],
            "status": "pending",
            "created_at": "2026-11-26T18:00:00Z",
        }
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}):
            self._run_hook("s1", "**Needs you:**\n- first bullet")
            self._run_hook("s2", "**Needs you:**\n- second bullet")

            # List wraps GET /api/decisions?status=pending and filters to errand only
            entries = ny.list_entries()
            self.assertEqual({e["decision_id"] for e in entries}, {"ny-s1", "ny-s2"})

            # Remote settlement on another surface -> next list prunes s1 cache
            srv.decisions["ny-s1"]["status"] = "answered"
            entries = ny.list_entries()
            self.assertEqual([e["decision_id"] for e in entries], ["ny-s2"])
            self.assertFalse((self.tmp / "sessions" / "s1.json").exists())
            self.assertTrue((self.tmp / "sessions" / "s2.json").exists())

            # ack wraps POST /api/decisions/{id}/respond with done: true, via: terminal
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ny.main(["ack", "1"]), 0)
            self.assertEqual(srv.decisions["ny-s2"]["status"], "answered")
            self.assertIs(srv.decisions["ny-s2"]["done"], True)
            self.assertEqual(srv.decisions["ny-s2"]["via"], "terminal")
            self.assertFalse((self.tmp / "sessions" / "s2.json").exists())

            # 409 conflict on respond settles cleanly
            outcome, _ = ny.hearth_respond("ny-s2", None, "terminal")
            self.assertEqual(outcome, "conflict")

    def test_kind_unsupported_4xx_falls_back_cleanly(self):
        srv = _FakeHearth(reject_errand=True)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}):
            self.assertIsNone(self._run_hook("s-fb", "**Needs you:**\n- fallback item"))
            # Hook wrote local entry without pending_sync and still published bus event
            cached = json.loads((self.tmp / "sessions" / "s-fb.json").read_text())
            self.assertEqual(cached["items"], ["fallback item"])
            self.assertFalse(cached.get("pending_sync", False))
            self.assertEqual(self.published.call_count, 1)
            rec = json.loads((self.tmp / "hook.log").read_text().splitlines()[-1])
            self.assertEqual(rec["sync"], "fallback_kind")

            # ack falls back to legacy /api/attention/{sid}/ack when respond returns 4xx
            with redirect_stdout(io.StringIO()):
                # Make list fall back to local cache by returning empty decisions from server
                # Wait: server returns 200 with 0 errands, so test hearth_respond + fallback directly
                outcome, _ = ny.hearth_respond("ny-s-fb", ["done"], "terminal")
                self.assertEqual(outcome, "fallback")
                self.assertTrue(ny.hearth_ack("s-fb", ["done"], ["fallback item"], "terminal"))
            self.assertEqual(len(srv.attention_acks), 1)
            self.assertIn("/api/attention/s-fb/ack?via=terminal", srv.attention_acks[0][0])

    def test_stop_hook_latency_under_300ms_when_hearth_hanging(self):
        srv = _FakeHearth(hang_s=2.0)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}):
            t0 = time.monotonic()
            self.assertIsNone(self._run_hook("s-slow", "**Needs you:**\n- check timeout"))
            elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.assertLess(elapsed_ms, 300.0, f"Stop hook took {elapsed_ms:.1f}ms (budget < 300ms)")
        cached = json.loads((self.tmp / "sessions" / "s-slow.json").read_text())
        self.assertTrue(cached["pending_sync"])

    def test_migrate_posts_recent_and_withdraws_old_entries(self):
        now = time.time()
        ny.write_entry({
            "session_id": "recent-1", "project": "demo", "cwd": "/repo",
            "items": ["fresh item"], "summary": "Recent work", "orca_terminal": "",
            "name": "recent-branch", "cache": None, "ts": now - 2 * 86400, "acked": False,
        })
        ny.write_entry({
            "session_id": "old-1", "project": "demo", "cwd": "/repo",
            "items": ["stale item"], "summary": "Old work", "orca_terminal": "",
            "name": "", "cache": None, "ts": now - 9 * 86400, "acked": False,
        })
        ny.write_entry({
            "session_id": "acked-1", "project": "demo", "cwd": "/repo",
            "items": ["already done"], "summary": "Done", "orca_terminal": "",
            "name": "", "cache": None, "ts": now - 1 * 86400, "acked": True,
        })

        srv = _FakeHearth()
        self.addCleanup(srv.close)
        # Pre-seed ny-old-1 on server so we verify withdraw hits it
        srv.decisions["ny-old-1"] = {
            "decision_id": "ny-old-1", "project": "demo", "kind": "errand", "status": "pending",
        }
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}):
            dry_out = io.StringIO()
            with redirect_stdout(dry_out):
                self.assertEqual(ny.main(["migrate", "--dry-run"]), 0)
            self.assertIn("1 posted, 1 withdrawn (>7d), 1 skipped", dry_out.getvalue())
            self.assertEqual(len(srv.requests), 0)

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ny.main(["migrate"]), 0)
            self.assertIn("1 posted, 1 withdrawn (>7d), 1 skipped", out.getvalue())
            self.assertIn("ny-recent-1", srv.decisions)
            self.assertEqual(srv.decisions["ny-recent-1"]["kind"], "errand")
            self.assertEqual(srv.decisions["ny-old-1"]["status"], "withdrawn")
            self.assertFalse((self.tmp / "sessions" / "old-1.json").exists())
            self.assertTrue(json.loads((self.tmp / "sessions" / "recent-1.json").read_text())["synced"])

    def test_migrate_aborts_cleanly_when_errand_unsupported(self):
        ny.write_entry({
            "session_id": "recent-2", "project": "demo", "cwd": "/repo",
            "items": ["item"], "summary": "Work", "orca_terminal": "",
            "name": "", "cache": None, "ts": time.time() - 3600, "acked": False,
        })
        srv = _FakeHearth(reject_errand=True)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"HEARTH_API_URL": srv.url, "HEARTH_API_TOKEN": "tok"}), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ny.main(["migrate"]), 1)
        self.assertIn("does not support errand", err.getvalue())
        self.assertTrue((self.tmp / "sessions" / "recent-2.json").exists())


if __name__ == "__main__":
    unittest.main()
