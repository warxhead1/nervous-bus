"""Tests for StretchOpensDetector. Transcripts are SYNTHETIC: the form strings are
placeholders, never real reply text (this repo is public)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.stretch_opens import (
    StretchOpensDetector, parse_reminder, project_of, scan,
)

NO_CLICK = ("Stretchtext: no click on a stretch of your last reply was recorded. The reader "
            "may have read only the short forms; every stretch was drawn open.")


def opened(forms, not_opened=()):
    text = "Stretchtext: since your last reply the reader opened " + ", ".join(f'"{f}"' for f in forms)
    if not_opened:
        text += ". They did not open " + ", ".join(f'"{f}"' for f in not_opened)
    return text + "."


def line(tool_id, ts, cwd, text):
    return json.dumps({
        "type": "attachment", "timestamp": ts, "cwd": cwd, "uuid": "u-" + tool_id,
        "attachment": {"type": "hook_additional_context", "content": [text],
                       "hookName": "UserPromptSubmit", "toolUseID": tool_id,
                       "hookEvent": "UserPromptSubmit"},
    })


class StretchOpensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "proj").mkdir()
        (self.root / "proj" / "sub").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, *lines):
        p = self.root / rel
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_parse_variants(self):
        self.assertEqual(parse_reminder(NO_CLICK), ([], []))
        self.assertEqual(parse_reminder(opened(["a b"])), (["a b"], []))
        self.assertEqual(parse_reminder(opened(["a", "b"], ["c", "d", "e"])),
                         (["a", "b"], ["c", "d", "e"]))
        self.assertIsNone(parse_reminder("Stretchtext: something else"))
        self.assertIsNone(parse_reminder("unrelated"))

    def test_project_of(self):
        self.assertEqual(project_of("/home/x/data2/worktrees/nervous-bus/agent-1"), "nervous-bus")
        self.assertEqual(project_of("/home/x/projects/hearth"), "hearth")
        self.assertEqual(project_of("/srv/other"), "other")
        self.assertEqual(project_of(""), "unknown")

    def test_counts_per_day_project_and_dedup_across_files(self):
        cwd = "/home/x/projects/alpha"
        dup = line("t1", "2026-11-20T10:00:00Z", cwd, NO_CLICK)
        self.write("proj/s1.jsonl", dup,
                   line("t2", "2026-11-20T11:00:00Z", cwd, opened(["one", "two"], ["three"])),
                   line("t3", "2026-11-21T09:00:00Z", cwd, NO_CLICK))
        # a resumed copy and a subagent transcript repeat t1 and t2
        self.write("proj/s1-resume.jsonl", dup)
        self.write("proj/sub/agent.jsonl",
                   line("t2", "2026-11-20T11:00:00Z", cwd, opened(["one", "two"], ["three"])))
        agg = scan(self.root)
        self.assertEqual(agg["reminders"], 3)
        self.assertEqual(agg["daily"][("2026-11-20", "alpha")], [1, 1, 2, 1])
        self.assertEqual(agg["daily"][("2026-11-21", "alpha")], [1, 0, 0, 0])

    def test_non_reminder_lines_ignored(self):
        cwd = "/home/x/projects/alpha"
        self.write("proj/s.jsonl",
                   json.dumps({"type": "assistant", "timestamp": "2026-11-20T10:00:00Z",
                               "message": {"content": [{"type": "text", "text": NO_CLICK}]}}),
                   "not json Stretchtext: \"attachment\"",
                   line("t1", "2026-11-20T10:00:00Z", cwd, NO_CLICK))
        self.assertEqual(scan(self.root)["reminders"], 1)

    def test_forms_stored_as_hash_and_length_only(self):
        cwd = "/home/x/projects/alpha"
        secret = "a distinctive short form"
        self.write("proj/s.jsonl", line("t1", "2026-11-20T10:00:00Z", cwd, opened([secret], ["other"])),
                   line("t2", "2026-11-21T10:00:00Z", cwd, opened([secret])))
        db = sqlite3.connect(":memory:", isolation_level=None)
        det = StretchOpensDetector(db)
        det.transcript_root = self.root
        self.assertEqual(det.detect(db), [])
        rows = db.execute("SELECT kind, form_hash, length, n FROM stretch_open_forms "
                          "WHERE kind='opened' ORDER BY day").fetchall()
        self.assertEqual([r[3] for r in rows], [1, 1])
        self.assertEqual(rows[0][2], len(secret))
        dump = "\n".join(str(r) for t in ("stretch_open_forms", "stretch_open_daily")
                         for r in db.execute(f"SELECT * FROM {t}"))
        self.assertNotIn(secret, dump)

    def test_rerun_is_idempotent_and_since_keeps_older_days(self):
        cwd = "/home/x/projects/alpha"
        self.write("proj/s.jsonl", line("t1", "2026-11-10T10:00:00Z", cwd, NO_CLICK),
                   line("t2", "2026-11-22T10:00:00Z", cwd, opened(["x"])))
        db = sqlite3.connect(":memory:", isolation_level=None)
        det = StretchOpensDetector(db)
        det.transcript_root = self.root
        det.detect(db)
        det.detect(db)
        det.detect(db, since_ts="2026-11-20T00:00:00Z")
        rows = db.execute("SELECT day, turns_no_click, turns_open FROM stretch_open_daily "
                          "ORDER BY day").fetchall()
        self.assertEqual(rows, [("2026-11-10", 1, 0), ("2026-11-22", 0, 1)])

    def test_missing_root_is_a_noop(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        det = StretchOpensDetector(db)
        det.transcript_root = self.root / "absent"
        self.assertEqual(det.detect(db), [])


if __name__ == "__main__":
    unittest.main()
