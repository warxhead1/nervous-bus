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


def _touch(path, day):
    """Set a file's mtime to noon UTC on `day` (YYYY-MM-DD), like a real transcript
    file whose last write was that day."""
    import calendar
    import time
    y, m, dd = map(int, day.split("-"))
    ts = calendar.timegm((y, m, dd, 12, 0, 0, 0, 0, 0))
    os.utime(path, (ts, ts))


class TestMtimePrefilter(unittest.TestCase):
    """struggle_ledger's --days must bound IO (via mtime) before opening any
    transcript file, not just filter the parsed result afterward — a stale
    file cannot hold an in-window record because mtime only grows on append."""

    def test_candidate_files_drops_stale_file(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "old.jsonl")
            new = os.path.join(d, "new.jsonl")
            open(old, "w").close()
            open(new, "w").close()
            _touch(old, "2026-06-01")
            _touch(new, "2026-06-20")
            kept = SL._candidate_files([old, new], days_limit=5)
            self.assertEqual(kept, [new])

    def test_candidate_files_keeps_everything_within_buffer(self):
        with tempfile.TemporaryDirectory() as d:
            edge = os.path.join(d, "edge.jsonl")
            new = os.path.join(d, "new.jsonl")
            open(edge, "w").close()
            open(new, "w").close()
            # 6 days back from the newest file, days_limit=5 -> inside the +1 day buffer
            _touch(edge, "2026-06-14")
            _touch(new, "2026-06-20")
            kept = SL._candidate_files([edge, new], days_limit=5)
            self.assertEqual(sorted(kept), sorted([edge, new]))

    def test_candidate_files_no_days_limit_returns_everything(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.jsonl")
            b = os.path.join(d, "b.jsonl")
            open(a, "w").close()
            open(b, "w").close()
            _touch(a, "2020-01-01")
            _touch(b, "2026-06-20")
            self.assertEqual(sorted(SL._candidate_files([a, b], None)), sorted([a, b]))

    def test_scan_with_days_limit_opens_fewer_files_same_events(self):
        # Old file (well outside the window) + new file (inside it). Bounded scan
        # must skip the old file's open() entirely, yet the windowed events()
        # returned by run() must be byte-identical to the unbounded scan filtered
        # by hand afterward -- proving the IO bound changes nothing observable.
        old_lines = [_rec("2026-05-01", "s0", CWD, result="device or resource busy")]
        new_lines = [_rec(f"2026-06-{dd:02d}", "s1", CWD, result="device or resource busy")
                     for dd in range(16, 21)]
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "myproj")
            _write(proj, "old.jsonl", old_lines)
            _write(proj, "new.jsonl", new_lines)
            _touch(os.path.join(proj, "old.jsonl"), "2026-05-01")
            _touch(os.path.join(proj, "new.jsonl"), "2026-06-20")

            opened = []
            real_open = open

            def spy_open(path, *a, **kw):
                opened.append(path)
                return real_open(path, *a, **kw)

            import builtins
            orig = builtins.open
            builtins.open = spy_open
            try:
                events_bounded, _remed, horizon_bounded = SL.scan(d, adapters=[], days_limit=5)
            finally:
                builtins.open = orig
            self.assertTrue(all("old.jsonl" not in p for p in opened),
                             f"old.jsonl must not be opened, got {opened}")
            self.assertTrue(any("new.jsonl" in p for p in opened))

            # Unbounded scan + the same manual day-cutoff run() applies -> identical
            # windowed result.
            events_full, _remed2, horizon_full = SL.scan(d, adapters=[])
            self.assertEqual(horizon_bounded, horizon_full)
            cutoff = (SL._parse(horizon_full) - SL.timedelta(days=5)).isoformat()
            full_windowed = sorted(
                (e.name, e.day, e.session, e.project) for e in events_full if e.day >= cutoff)
            bounded_sorted = sorted(
                (e.name, e.day, e.session, e.project) for e in events_bounded if e.day >= cutoff)
            self.assertEqual(full_windowed, bounded_sorted)
            self.assertTrue(full_windowed, "fixture must actually produce events")


# ── multi-agent formats (synthetic; no real transcript content) ──────────────

SID_A = "019aaaaa-1111-2222-3333-444455556666"
SID_B = "019bbbbb-1111-2222-3333-444455556666"
BUSY = "error: Device or resource busy"


def _codex_line(day, ptype, **payload):
    return json.dumps({"timestamp": f"{day}T10:00:00.000Z", "type": "response_item",
                       "payload": {"type": ptype, **payload}})


def _codex_rollout(root, sid, day, cwd, lines):
    d = os.path.join(root, *day.split("-"))
    meta = json.dumps({"timestamp": f"{day}T09:00:00.000Z", "type": "session_meta",
                       "payload": {"id": sid, "cwd": cwd}})
    _write(d, f"rollout-{day}T09-00-00-{sid}.jsonl", [meta] + lines)


class TestMultiFormat(unittest.TestCase):
    def _roots(self, tmp):
        return {"claude": [os.path.join(tmp, "claude")],
                "mmx": [os.path.join(tmp, "mmx-live"), os.path.join(tmp, "mmx-mirror")],
                "codex": [os.path.join(tmp, "codex"), os.path.join(tmp, "acct-*", "sessions")],
                "gemini": [os.path.join(tmp, "gemini")]}

    def test_blobs_codex_shapes(self):
        recs = [json.loads(_codex_line("2026-08-01", "function_call", name="exec_command",
                                       arguments=json.dumps({"cmd": "make -j8", "workdir": "/x"}))),
                json.loads(_codex_line("2026-08-01", "custom_tool_call", name="apply_patch",
                                       input="*** Begin Patch")),
                json.loads(_codex_line("2026-08-01", "function_call_output", output=BUSY)),
                json.loads(_codex_line("2026-08-01", "custom_tool_call_output",
                                       output=[{"type": "text", "text": BUSY}])),
                json.loads(_codex_line("2026-08-01", "message", role="assistant",
                                       content=[{"type": "output_text", "text": "hello"}])),
                json.loads(_codex_line("2026-08-01", "message", role="developer",
                                       content=[{"type": "input_text", "text": "injected"}])),
                {"type": "event_msg", "payload": {"type": "token_count"}}]
        out = [b for r in recs for b in SL._blobs_codex(r)]
        self.assertEqual(out, [("command", "make -j8\n/x"), ("command", "*** Begin Patch"),
                               ("result", BUSY), ("result", BUSY), ("text", "hello")])

    def test_blobs_gemini_shapes(self):
        msg = {"type": "gemini", "content": "ok", "toolCalls": [
            {"name": "run_shell_command", "args": {"command": "ls"},
             "result": [{"functionResponse": {"name": "x", "response": {"output": BUSY}}}]}]}
        self.assertEqual(list(SL._blobs_gemini(msg)),
                         [("text", "ok"), ("command", "ls"), ("result", BUSY)])
        self.assertEqual(list(SL._blobs_gemini({"content": [{"text": "listy"}]})), [("text", "listy")])

    def test_scan_all_sources_tagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._roots(tmp)
            _write(os.path.join(roots["claude"][0], "p"), "s.jsonl",
                   [_rec("2026-08-01", "claude-sess", CWD, result=BUSY)])
            _write(os.path.join(roots["mmx"][0], "p"), "m.jsonl",
                   [_rec("2026-08-02", "mmx-sess", CWD, result=BUSY)])
            _codex_rollout(roots["codex"][0], SID_A, "2026-08-03", CWD, [
                _codex_line("2026-08-03", "function_call_output", output=BUSY)])
            gdir = os.path.join(roots["gemini"][0], "myproj", "chats")
            os.makedirs(gdir)
            with open(os.path.join(gdir, "session-1.json"), "w") as f:
                json.dump({"sessionId": "gem-session-1", "messages": [
                    {"type": "gemini", "timestamp": "2026-08-04T10:00:00Z", "content": "",
                     "toolCalls": [{"args": {"command": "x"}, "result": [
                         {"functionResponse": {"response": {"output": BUSY}}}]}]}]}, f)
            events, _, horizon = SL.scan("", adapters=[], sources=SL.ALL_SOURCES, roots=roots)
            got = {e.source: (e.project, e.day) for e in events}
            self.assertEqual(got, {"claude": ("myproj", "2026-08-01"), "mmx": ("myproj", "2026-08-02"),
                                   "codex": ("myproj", "2026-08-03"),
                                   "gemini": ("myproj", "2026-08-04")})
            self.assertEqual(horizon, "2026-08-04")
            # default sources stay claude-only
            only, _, _ = SL.scan(roots["claude"][0], adapters=[], roots=roots)
            self.assertEqual({e.source for e in only}, {"claude"})

    def test_codex_dedupes_account_roots_by_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._roots(tmp)
            line = [_codex_line("2026-08-03", "function_call_output", output=BUSY)]
            _codex_rollout(roots["codex"][0], SID_A, "2026-08-03", CWD, line)
            _codex_rollout(os.path.join(tmp, "acct-1", "sessions"), SID_A, "2026-08-03", CWD, line)
            _codex_rollout(os.path.join(tmp, "acct-2", "sessions"), SID_B, "2026-08-03", CWD, line)
            files = SL._gather_files(("codex",), roots)
            self.assertEqual(len(files), 2)
            events, _, _ = SL.scan("", adapters=[], sources=("codex",), roots=roots)
            self.assertEqual(len(events), 2)
            self.assertEqual({e.session for e in events}, {SID_A[:12], SID_B[:12]})

    def test_mmx_live_wins_over_mirror_and_mirror_covers_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._roots(tmp)
            _write(os.path.join(roots["mmx"][0], "p"), "same.jsonl", ["{}"])
            _write(os.path.join(roots["mmx"][1], "p"), "same.jsonl", ["{}"])
            _write(os.path.join(roots["mmx"][1], "p"), "reaped.jsonl", ["{}"])
            files = sorted(f for f, _ in SL._gather_files(("mmx",), roots))
            self.assertEqual(files, sorted([os.path.join(roots["mmx"][1], "p", "reaped.jsonl"),
                                            os.path.join(roots["mmx"][0], "p", "same.jsonl")]))

    def test_gemini_project_from_hash_and_jsonl_format(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._roots(tmp)
            root_path = "/home/eric/projects/hashed"
            h = hashlib.sha256(root_path.encode()).hexdigest()
            names = os.path.join(tmp, "projects.json")
            with open(names, "w") as f:
                json.dump({"projects": {root_path: "hashed"}}, f)
            self.assertEqual(SL._gemini_project_names(names), {h: "hashed"})
            gdir = os.path.join(roots["gemini"][0], h, "chats")
            _write(gdir, "session-2.jsonl", [
                json.dumps({"sessionId": "jsonl-sess-9", "projectHash": h}),
                json.dumps({"id": "1", "timestamp": "2026-08-05T10:00:00Z", "type": "gemini",
                            "content": BUSY}),
                json.dumps({"$set": {"lastUpdated": "x"}})])
            orig = SL.GEMINI_PROJECTS
            SL.GEMINI_PROJECTS = names
            try:
                events, _, _ = SL.scan("", adapters=[], sources=("gemini",), roots=roots)
            finally:
                SL.GEMINI_PROJECTS = orig
            self.assertEqual([(e.project, e.session, e.source) for e in events],
                             [("hashed", "jsonl-sess-9", "gemini")])

    def test_ledger_rows_split_by_source_and_claude_key_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self._roots(tmp)
            _write(os.path.join(roots["claude"][0], "p"), "s.jsonl",
                   [_rec("2026-08-01", "cs", CWD, result=BUSY)])
            _codex_rollout(roots["codex"][0], SID_A, "2026-08-01", CWD, [
                _codex_line("2026-08-01", "function_call_output", output=BUSY)])
            recs, _, _, _ = SL.run(roots["claude"][0], sources=("claude", "codex"), roots=roots)
            self.assertEqual(recs[("myproj", "resource_busy")].source, "claude")
            self.assertEqual(recs[("myproj", "resource_busy", "codex")].source, "codex")
            out = main_json(["--src", roots["claude"][0]])
            self.assertEqual([r["source"] for r in out], ["claude"])


def main_json(argv):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        SL.main(argv + ["--json"])
    return json.loads(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
