"""token_ledger gemini: whole-file chat JSON, re-ingested on (mtime, size) change."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "gemini_chat_synthetic.json"


class TestGeminiLedger(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "tmp"
        self.chat = self.root / "abc" / "chats" / "session-1.json"
        self.chat.parent.mkdir(parents=True)
        (self.root / "abc" / ".project_root").write_text("/home/eric/projects/demo\n")
        shutil.copy(FIX, self.chat)
        self.db = Path(self.td.name) / "tokens.db"

    def tearDown(self):
        self.td.cleanup()

    def _run(self):
        return tl.run(self.db, roots=(), gemini_roots=(self.root,))

    def test_mapping_and_tools(self):
        self.assertEqual(self._run()["requests_added"], 1)
        c = tl.connect(self.db)
        row = c.execute("SELECT model, input, cache_read, cache_create, output, thinking, n_tool_uses"
                        " FROM requests").fetchone()
        # cached is inside input; thoughts are billed as output and kept as a subset
        self.assertEqual(row, ("gemini-fake", 400, 600, 0, 100, 20, 2))
        s = c.execute("SELECT source, session_id, project, cwd FROM sessions").fetchone()
        self.assertEqual(s, ("gemini", "sess-fake-1", "demo", "/home/eric/projects/demo"))
        tools = {r[0]: r[1:] for r in c.execute("SELECT tool, cmd_class, cmd, result_chars FROM tool_calls")}
        self.assertEqual(tools["run_shell_command"][:2], ("test", "pytest -q"))
        self.assertGreater(tools["run_shell_command"][2], 0)
        self.assertEqual(tools["write_file"][:2], (None, None))

    def test_unchanged_file_is_skipped_and_changed_file_adds_only_new(self):
        self._run()
        c = tl.connect(self.db)
        c.execute("UPDATE requests SET output = 7")  # a re-read must not touch existing rows
        c.commit()
        c.close()
        self.assertEqual(self._run()["requests_added"], 0)

        d = json.loads(self.chat.read_text())
        d["messages"].append({"id": "g2", "timestamp": "2026-10-01T00:00:20.000Z", "type": "gemini",
                              "model": "gemini-fake", "content": "more",
                              "tokens": {"input": 50, "output": 5, "cached": 0, "thoughts": 0,
                                         "tool": 0, "total": 55}})
        d["lastUpdated"] = "2026-10-01T00:00:20.000Z"
        self.chat.write_text(json.dumps(d))
        st = self.chat.stat()
        os.utime(self.chat, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self._run()["requests_added"], 1)
        c = tl.connect(self.db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT output FROM requests WHERE key LIKE '%#g1'").fetchone()[0], 7)
        self.assertEqual(c.execute("SELECT ended FROM sessions").fetchone()[0],
                         "2026-10-01T00:00:20.000Z")
        self.assertEqual(self._run()["requests_added"], 0)

    def test_same_session_in_two_files_counts_once(self):
        self._run()
        twin = self.root / "abc" / "chats" / "session-1b.json"
        shutil.copy(FIX, twin)
        self.assertEqual(self._run()["requests_added"], 0)

    def test_torn_file_retries_next_pass(self):
        self.chat.write_text('{"sessionId": "x", "messages": [')
        self.assertEqual(self._run()["requests_added"], 0)
        shutil.copy(FIX, self.chat)
        self.assertEqual(self._run()["requests_added"], 1)


if __name__ == "__main__":
    unittest.main()
