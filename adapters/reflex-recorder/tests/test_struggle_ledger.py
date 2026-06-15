"""tests/test_struggle_ledger.py — friction-telemetry engine (synthetic fixtures).

Covers: generic + adapter-contributed struggle classification, longitudinal status
(open/dormant/resolved), and fix-correlation verdicts. No real transcripts.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from adapter_api import ProjectAdapter, StruggleClass, struggle_classes_for
import struggle_ledger as SL


def _rec(day, sess, cwd, *, command=None, result=None, text=None):
    content = []
    if command is not None:
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": command}})
    if result is not None:
        content.append({"type": "tool_result", "content": result})
    if text is not None:
        content.append({"type": "text", "text": text})
    return json.dumps({
        "timestamp": f"{day}T12:00:00Z", "sessionId": sess,
        "cwd": cwd, "type": "assistant", "message": {"content": content},
    })


def _write(dirpath, name, lines):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, name), "w") as f:
        f.write("\n".join(lines) + "\n")


class _FakeAdapter(ProjectAdapter):
    name = "myproj"
    def matches(self, project): return project == "myproj"
    def struggle_classes(self):
        return [StruggleClass("widget_jam", re.compile(r"widget jammed", re.I),
                              "the widget jams", "crash", fix_keywords=("widget", "jam"))]


CWD = "/home/eric/projects/myproj/x"


class TestClassification(unittest.TestCase):
    def test_generic_class_matches(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s1.jsonl", [
                _rec("2026-06-01", "s1", CWD, result="Blocking waiting for file lock on package cache"),
            ])
            events, _, _h = SL.scan(d, adapters=[])
            self.assertEqual([e.name for e in events], ["cargo_build_lock"])
            self.assertEqual(events[0].project, "myproj")

    def test_adapter_class_appended(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s1.jsonl", [
                _rec("2026-06-01", "s1", CWD, result="ERROR: widget jammed at stage 3"),
            ])
            events, _, _h = SL.scan(d, adapters=[_FakeAdapter()])
            self.assertIn("widget_jam", [e.name for e in events])

    def test_base64_image_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s1.jsonl", [
                json.dumps({"timestamp": "2026-06-01T00:00:00Z", "sessionId": "s1", "cwd": CWD,
                            "type": "assistant", "message": {"content": [
                                {"type": "tool_result",
                                 "content": '[{"type": "image", "source": {"data": "lock busy error"}}]'}]}}),
            ])
            events, _, _h = SL.scan(d, adapters=[])
            self.assertEqual(events, [])


class TestTemporalStatus(unittest.TestCase):
    def _ledger(self, lines, adapters=()):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s.jsonl", lines)
            recs, remed, maxday, days = SL.run(d, adapters=None) if False else (None,)*4
            events, rem, _h = SL.scan(d, adapters=list(adapters))
            maxday = max(e.day for e in events)
            recs = SL.build_ledger(events)
            for r in recs.values():
                r.status = SL._status(r.last, maxday)
            return recs, rem, maxday

    def test_open_when_recent(self):
        lines = [_rec(f"2026-06-1{i}", "s1", CWD, result="device or resource busy") for i in range(0, 6)]
        recs, _, _ = self._ledger(lines)
        r = recs[("myproj", "resource_busy")]
        self.assertEqual(r.status, "open")
        self.assertGreaterEqual(len(r.sessions), 1)

    def test_resolved_when_stale(self):
        lines = ([_rec(f"2026-06-0{i}", "s1", CWD, result="device or resource busy") for i in range(1, 4)]
                 + [_rec("2026-06-20", "s2", CWD, result="address already in use")])
        recs, _, _ = self._ledger(lines)
        self.assertEqual(recs[("myproj", "resource_busy")].status, "resolved")  # last 06-03 vs max 06-20


class TestFixCorrelation(unittest.TestCase):
    def test_remediation_extracted_unescaped(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s.jsonl", [
                _rec("2026-06-06", "s1", CWD, command='git commit -m "fix the resource busy contention"'),
            ])
            _, remed, _h = SL.scan(d, adapters=[])
            self.assertEqual(len(remed), 1)
            self.assertIn("resource busy", remed[0].message)
            self.assertEqual(remed[0].kind, "commit")

    def test_fixed_verdict(self):
        # heavy friction days 1-5, a matching fix on day 6, silence after -> 'fixed'
        lines = [_rec(f"2026-06-0{i}", f"s{i}", CWD, result="device or resource busy") for i in range(1, 6)]
        lines.append(_rec("2026-06-06", "s9", CWD, command='git commit -m "fix resource busy by serializing"'))
        lines.append(_rec("2026-06-20", "s9", CWD, text="unrelated later activity to set maxday"))
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s.jsonl", lines)
            recs, remed, maxday, days = SL.run(d)
            r = recs[("myproj", "resource_busy")]
            self.assertEqual(r.status, "resolved")
            self.assertEqual(r.fix_verdict, "fixed")
            self.assertIn("before=", r.fix_evidence)

    def test_unfixed_open_when_no_drop(self):
        # steady friction through the end, a fix attempt that did NOT reduce it
        lines = [_rec(f"2026-06-{d:02d}", "s1", CWD, result="device or resource busy")
                 for d in range(1, 16)]
        lines.append(_rec("2026-06-08", "s1", CWD, command='git commit -m "attempt fix resource busy"'))
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s.jsonl", lines)
            recs, remed, maxday, days = SL.run(d)
            r = recs[("myproj", "resource_busy")]
            self.assertEqual(r.status, "open")
            self.assertIn(r.fix_verdict, ("unfixed_open", "partial_still_open"))

    def test_no_attempt_verdict(self):
        lines = [_rec(f"2026-06-{d:02d}", "s1", CWD, result="device or resource busy")
                 for d in range(1, 16)]
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "myproj"), "s.jsonl", lines)
            recs, _, _, _ = SL.run(d)
            self.assertEqual(recs[("myproj", "resource_busy")].fix_verdict, "unfixed_no_attempt")


if __name__ == "__main__":
    unittest.main()
