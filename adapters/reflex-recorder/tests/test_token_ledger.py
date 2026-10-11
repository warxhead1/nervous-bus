"""token_ledger: transcripts -> per-request usage rows, incremental and deduped."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402


def _asst(mid, ts, usage, content):
    return json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "s1",
                       "cwd": "/home/eric/data2/worktrees/proj/wt",
                       "message": {"id": mid, "model": "m", "usage": usage, "content": content}})


def _usage(inp=1, cr=0, cc=0, out=5):
    return {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc,
            "cache_creation": {"ephemeral_1h_input_tokens": cc}, "output_tokens": out}


def _bash(tid, cmd):
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}


def _result(tid, text):
    return json.dumps({"type": "user", "timestamp": "t", "message": {
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}})


class TestLedger(unittest.TestCase):
    def test_ingest_dedupes_streamed_blocks_and_is_incremental(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            sess = root / "-home-eric-p" / "s1.jsonl"
            sess.parent.mkdir(parents=True)
            instr = json.dumps({"type": "attachment", "timestamp": "2026-10-01T00:00:00Z",
                                "attachment": {"type": "instructions", "files": [
                                    {"path": "/h/CLAUDE.md", "type": "User", "content": "x" * 40}]}})
            lines = [
                instr,
                _asst("m1", "2026-10-01T00:00:01Z", _usage(cc=1000), [{"type": "thinking"}]),
                _asst("m1", "2026-10-01T00:00:01Z", _usage(cc=1000), [_bash("t1", "cd x && pytest -q")]),
                _result("t1", "y" * 123),
            ]
            sess.write_text("\n".join(lines) + "\n")
            sub = root / "-home-eric-p" / "s1" / "subagents" / "agent-a.jsonl"
            sub.parent.mkdir(parents=True)
            sub.write_text(_asst("m9", "2026-10-01T00:00:05Z", _usage(cc=50), []) + "\n")
            (sub.parent / "agent-a.meta.json").write_text(json.dumps({"agentType": "sonnet-worker"}))

            db = Path(td) / "tokens.db"
            roots = (("claude", root),)
            self.assertEqual(tl.run(db, roots)["requests_added"], 2)

            with open(sess, "a") as f:
                f.write(_asst("m2", "2026-10-01T00:00:09Z", _usage(cr=1000), [_bash("t2", "git commit -m x")]) + "\n")
                f.write('{"type":"assistant","partial')  # incomplete trailing line: not consumed
            self.assertEqual(tl.run(db, roots)["requests_added"], 1)
            self.assertEqual(tl.run(db, roots)["requests_added"], 0)

            c = sqlite3.connect(db)
            reqs = c.execute("SELECT seq, cache_create, cache_read, n_tool_uses FROM requests"
                             " WHERE session_key LIKE '%s1.jsonl' ORDER BY seq").fetchall()
            self.assertEqual(reqs, [(1, 1000, 0, 1), (2, 0, 1000, 1)])
            tcs = dict(c.execute("SELECT id, cmd_class FROM tool_calls"))
            self.assertEqual(tcs, {"t1": "test", "t2": "commit"})
            self.assertEqual(c.execute("SELECT result_chars FROM tool_calls WHERE id='t1'").fetchone()[0], 123)
            self.assertEqual(c.execute("SELECT chars FROM instructions").fetchone()[0], 40)
            s = c.execute("SELECT agent_type, is_subagent FROM sessions WHERE key LIKE '%agent-a%'").fetchone()
            self.assertEqual(s, ("sonnet-worker", 1))
            self.assertEqual(c.execute("SELECT DISTINCT project FROM sessions").fetchall(), [("proj",)])

    def test_classify_and_project(self):
        self.assertEqual(tl.classify_command("cargo test -p x"), "test")
        self.assertEqual(tl.classify_command("go build ./..."), "build")
        self.assertEqual(tl.classify_command("ls"), "other")
        self.assertEqual(tl.project_of("/home/eric/projects/hearth/src"), "hearth")


if __name__ == "__main__":
    unittest.main()
