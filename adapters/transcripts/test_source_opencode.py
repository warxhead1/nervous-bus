"""opencode source reads a synthetic db built with the real table/column names."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import transcripts  # noqa: E402

SESSION_DDL = """CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text,
  parent_id text, slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL,
  version text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL)"""
MESSAGE_DDL = """CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"""
PART_DDL = """CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"""

T0 = 1_790_000_000_000


def tool_part(call_id, tool, status, command=None, exit_=None, output="", error=None):
    state = {"status": status, "input": {"command": command} if command is not None else {"path": "/x"},
             "time": {"start": T0, "end": T0 + 5}}
    if status == "completed":
        state["output"] = output
        state["metadata"] = {"output": output, "exit": exit_} if exit_ is not None else {}
    else:
        state["error"] = error
    return {"type": "tool", "callID": call_id, "tool": tool, "state": state}


class OpenCodeSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transcripts-oc-")
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp, "USERPROFILE": self.tmp,
                                                "XDG_DATA_HOME": os.path.join(self.tmp, "share")})
        self.env.start()
        d = os.path.join(self.tmp, "share", "opencode")
        os.makedirs(d)
        self.db = os.path.join(d, "opencode-.db")
        con = sqlite3.connect(self.db)
        for ddl in (SESSION_DDL, MESSAGE_DDL, PART_DDL):
            con.execute(ddl)
        con.execute("INSERT INTO session VALUES ('ses_a','p',NULL,NULL,'a','/w','','t','1',?,?)", (T0, T0 + 9))
        con.execute("INSERT INTO session VALUES ('ses_b','p',NULL,'ses_a','b','/w/sub','','t','1',?,?)", (T0, T0 + 9))
        parts = [
            ("ses_a", tool_part("c1", "bash", "completed", "ls", 0, "a\nb")),
            ("ses_a", tool_part("c2", "bash", "completed", "false", 1, "")),
            ("ses_a", tool_part("c3", "bash", "error", "rm x", error="The user rejected permission")),
            ("ses_a", tool_part("c4", "read", "completed", output="file body")),
            ("ses_a", {"type": "text", "text": "hello"}),
            ("ses_b", tool_part("c5", "bash", "completed", "git status", 0, "clean")),
        ]
        for i, (sid, p) in enumerate(parts):
            con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (f"prt_{i}", "msg_1", sid, T0 + i, T0 + i, json.dumps(p)))
        con.commit()
        con.close()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_calls(self):
        by = {c.call_id: c for c in transcripts.iter_calls(["opencode"])}
        self.assertEqual(set(by), {"c1", "c2", "c3", "c4", "c5"})
        self.assertEqual((by["c1"].shell, by["c1"].command, by["c1"].exit_code, by["c1"].is_error),
                         (True, "ls", 0, False))
        self.assertEqual((by["c2"].exit_code, by["c2"].is_error, by["c2"].harness), (1, True, False))
        self.assertEqual((by["c3"].is_error, by["c3"].exit_code, by["c3"].harness), (True, None, True))
        self.assertFalse(by["c4"].shell)
        self.assertEqual((by["c4"].tool, by["c4"].is_error, by["c4"].command), ("read", False, ""))
        self.assertEqual(by["c1"].cwd, "/w")
        self.assertTrue(by["c1"].ts.endswith("Z") and by["c1"].ts.startswith("2026"))

    def test_subagent_session(self):
        from sources import SOURCES
        refs = {r.id: r for r in SOURCES["opencode"].sessions()}
        self.assertEqual(set(refs), {"ses_a", "ses_b"})
        self.assertFalse(refs["ses_a"].extra["subagent"])
        self.assertTrue(refs["ses_b"].extra["subagent"])
        self.assertEqual(refs["ses_b"].extra["parent_id"], "ses_a")
        self.assertEqual((refs["ses_b"].cwd, refs["ses_b"].path, refs["ses_b"].extra["session_row"]),
                         ("/w/sub", self.db, "ses_b"))
        self.assertEqual([c.command for c in SOURCES["opencode"].tool_calls(refs["ses_b"])], ["git status"])

    def test_db_opened_read_only(self):
        from sources import opencode
        con = opencode._ro(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM part")
        finally:
            con.close()
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT count(*) FROM part").fetchone()[0], 6)
        con.close()

    def test_locked_db_is_skipped(self):
        from sources import SOURCES
        w = sqlite3.connect(self.db, isolation_level=None)
        w.execute("PRAGMA locking_mode=EXCLUSIVE")
        w.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(list(SOURCES["opencode"].sessions()), [])
        finally:
            w.execute("ROLLBACK")
            w.close()


if __name__ == "__main__":
    unittest.main()
