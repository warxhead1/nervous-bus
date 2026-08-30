#!/usr/bin/env python3
"""Tests for deploy-staleness. All process/systemd/proc I/O is stubbed via
injected functions -- these tests never touch the real systemd user session
or /proc, per house convention (adapters/ci-watch/test_watch.py stubs `gh`/
`bd` the same way for the same reason: this adapter's substrate is external
process state, not something safe to fabricate against the real box in CI).

Run: python3 test_check.py -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import check


class TestParseExecStart(unittest.TestCase):
    def test_binary_backed(self):
        raw = ("{ path=/home/eric/.local/bin/kb ; argv[]=/home/eric/.local/bin/kb watch --live ; "
               "ignore_errors=no ; start_time=[x] ; stop_time=[n/a] ; pid=918036 ; code=(null) ; status=0/0 }")
        path, argv = check.parse_exec_start(raw)
        self.assertEqual(path, "/home/eric/.local/bin/kb")
        self.assertEqual(argv, ["/home/eric/.local/bin/kb", "watch", "--live"])

    def test_interpreter_backed(self):
        raw = ("{ path=/home/eric/.pyenv/versions/3.11.13/bin/python3 ; "
               "argv[]=/home/eric/.pyenv/versions/3.11.13/bin/python3 /home/eric/projects/nervous-bus/adapters/dlq/dlq.py ; "
               "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
        path, argv = check.parse_exec_start(raw)
        self.assertEqual(path, "/home/eric/.pyenv/versions/3.11.13/bin/python3")
        self.assertEqual(argv[1], "/home/eric/projects/nervous-bus/adapters/dlq/dlq.py")

    def test_empty(self):
        self.assertEqual(check.parse_exec_start(None), (None, []))
        self.assertEqual(check.parse_exec_start(""), (None, []))


class TestResolveTargetFile(unittest.TestCase):
    def test_binary_backed_target_is_path(self):
        target = check.resolve_target_file("/home/eric/.local/bin/kb", ["/home/eric/.local/bin/kb", "watch", "--live"])
        self.assertEqual(target, "/home/eric/.local/bin/kb")

    def test_interpreter_backed_target_is_script(self):
        target = check.resolve_target_file(
            "/home/eric/.pyenv/versions/3.11.13/bin/python3",
            ["/home/eric/.pyenv/versions/3.11.13/bin/python3", "/home/eric/projects/nervous-bus/adapters/dlq/dlq.py"],
        )
        self.assertEqual(target, "/home/eric/projects/nervous-bus/adapters/dlq/dlq.py")

    def test_no_path(self):
        self.assertIsNone(check.resolve_target_file(None, []))


class TestEvaluateUnit(unittest.TestCase):
    def setUp(self):
        self.base_props = {
            "Type": "simple",
            "ExecStart": ("{ path=/home/eric/.local/bin/kb ; argv[]=/home/eric/.local/bin/kb watch --live ; "
                          "ignore_errors=no ; pid=918036 ; code=(null) ; status=0/0 }"),
            "ExecMainStartTimestamp": "Sun 2026-08-30 16:47:05 EDT",
            "ExecMainPID": "918036",
        }

    def test_oneshot_is_skipped(self):
        props = dict(self.base_props, Type="oneshot")
        result = check.evaluate_unit("kb-autoingest.service", props)
        self.assertIsNone(result)

    def test_stale_by_mtime(self):
        # code newer than process start -> stale
        result = check.evaluate_unit(
            "kb-watch.service", self.base_props,
            git_root_fn=lambda p: None,
            mtime_fn=lambda p: check.parse_iso("2026-08-30T21:00:00Z"),
            proc_deleted_fn=lambda pid: False,
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-30T20:47:05Z"),
        )
        self.assertEqual(result["verdict"], "stale")
        self.assertEqual(result["reason"], "mtime")

    def test_fresh_when_restarted_after_code_change(self):
        result = check.evaluate_unit(
            "kb-watch.service", self.base_props,
            git_root_fn=lambda p: None,
            mtime_fn=lambda p: check.parse_iso("2026-08-24T10:00:00Z"),
            proc_deleted_fn=lambda pid: False,
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-30T20:47:05Z"),
        )
        self.assertEqual(result["verdict"], "fresh")

    def test_stale_by_git_commit_newer_than_mtime(self):
        # mtime says fresh, but a later git commit touched the file (e.g.
        # checkout mtime doesn't reflect the real edit time) -> git wins.
        result = check.evaluate_unit(
            "dlq.service", self.base_props,
            git_root_fn=lambda p: "/home/eric/projects/nervous-bus",
            git_time_fn=lambda root, p: check.parse_iso("2026-08-30T22:00:00Z"),
            mtime_fn=lambda p: check.parse_iso("2026-08-24T10:00:00Z"),
            proc_deleted_fn=lambda pid: False,
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-30T20:47:05Z"),
        )
        self.assertEqual(result["verdict"], "stale")
        self.assertEqual(result["reason"], "git_commit")

    def test_stale_by_deleted_exe_overrides_timestamps(self):
        # Even if mtime/commit look fresh-enough, a deleted /proc/pid/exe
        # means the running process is executing bytes gone from disk.
        result = check.evaluate_unit(
            "kb-watch.service", self.base_props,
            git_root_fn=lambda p: None,
            mtime_fn=lambda p: check.parse_iso("2026-08-01T00:00:00Z"),
            proc_deleted_fn=lambda pid: True,
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-30T20:47:05Z"),
        )
        self.assertEqual(result["verdict"], "stale")
        self.assertEqual(result["reason"], "deleted_exe")

    def test_unknown_when_no_exec_start(self):
        props = dict(self.base_props, ExecStart="")
        result = check.evaluate_unit("mystery.service", props)
        self.assertEqual(result["verdict"], "unknown")


class TestDiscoverUnits(unittest.TestCase):
    def test_filters_by_root_and_excludes_oneshot_and_roster(self):
        roster = {
            "project_roots": ["/home/eric/projects"],
            "local_bin_roots": ["/home/eric/.local/bin"],
            "exclude_units": ["staleness.service"],
            "include_units": ["some-external.service"],
        }

        def list_fn():
            return ["dlq.service", "staleness.service", "kb-watch.service", "unrelated.service"]

        def show_fn(unit):
            table = {
                "dlq.service": {
                    "Type": "simple",
                    "ExecStart": "{ path=/x/python3 ; argv[]=/x/python3 /home/eric/projects/nervous-bus/adapters/dlq/dlq.py ; ignore_errors=no }",
                },
                "staleness.service": {
                    "Type": "oneshot",
                    "ExecStart": "{ path=/x/python3 ; argv[]=/x/python3 /home/eric/projects/nervous-bus/adapters/staleness/monitor.py ; ignore_errors=no }",
                },
                "kb-watch.service": {
                    "Type": "simple",
                    "ExecStart": "{ path=/home/eric/.local/bin/kb ; argv[]=/home/eric/.local/bin/kb watch --live ; ignore_errors=no }",
                },
                "unrelated.service": {
                    "Type": "simple",
                    "ExecStart": "{ path=/usr/bin/something ; argv[]=/usr/bin/something ; ignore_errors=no }",
                },
            }
            return table[unit]

        discovered = check.discover_units(roster, list_fn=list_fn, show_fn=show_fn)
        self.assertIn("dlq.service", discovered)
        self.assertIn("kb-watch.service", discovered)
        self.assertIn("some-external.service", discovered)  # roster addition
        self.assertNotIn("staleness.service", discovered)  # explicit exclude
        self.assertNotIn("unrelated.service", discovered)  # outside configured roots


class TestAppimageMatcher(unittest.TestCase):
    def test_stale_file_newer_than_process(self):
        matcher = {"name": "orca", "cmdline_contains": "orca-linux.AppImage"}
        result = check.evaluate_appimage(
            matcher,
            pid_list_fn=lambda: [1500947],
            cmdline_fn=lambda pid: ["/home/eric/projects/orca/dist/orca-linux.AppImage", "--disable-vulkan"],
            lstart_fn=lambda pid: "Sat Aug 29 13:44:55 2026",
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-29T17:44:55Z"),
            mtime_fn=lambda p: check.parse_iso("2026-08-30T17:15:00Z"),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["verdict"], "stale")
        self.assertEqual(result[0]["reason"], "file_newer_than_process")
        self.assertEqual(result[0]["pid"], 1500947)

    def test_fresh_process_started_after_file(self):
        matcher = {"name": "orca", "cmdline_contains": "orca-linux.AppImage"}
        result = check.evaluate_appimage(
            matcher,
            pid_list_fn=lambda: [974108],
            cmdline_fn=lambda pid: ["/home/eric/projects/orca/dist/orca-linux.AppImage", "-e", "..."],
            lstart_fn=lambda pid: "Sun Aug 30 16:48:46 2026",
            ts_parse_fn=lambda raw: check.parse_iso("2026-08-30T20:48:46Z"),
            mtime_fn=lambda p: check.parse_iso("2026-08-30T17:15:00Z"),
        )
        self.assertEqual(result[0]["verdict"], "fresh")

    def test_no_match_returns_empty(self):
        matcher = {"name": "orca", "cmdline_contains": "orca-linux.AppImage"}
        result = check.evaluate_appimage(
            matcher,
            pid_list_fn=lambda: [1],
            cmdline_fn=lambda pid: ["/usr/bin/bash"],
        )
        self.assertEqual(result, [])


class TestTransitionDedupe(unittest.TestCase):
    def test_first_seen_stale_publishes_once(self):
        published = []
        orig_publish = check.publish_transition
        check.publish_transition = lambda r, dry_run: published.append(r["target"]) or True
        try:
            state = {}
            results = [{
                "target": "kb-watch.service", "kind": "unit", "pid": 1, "verdict": "stale",
                "reason": "mtime", "running_since": "2026-08-30T10:00:00Z",
                "code_newest_at": "2026-08-30T20:00:00Z", "remedy_hint": "restart",
            }]
            check.process_results(results, state, dry_run=False, now=1000.0)
            self.assertEqual(published, ["kb-watch.service"])
            self.assertTrue(results[0]["transitioned"])

            # Second run, still stale, well within cooldown -> no re-publish.
            published.clear()
            results2 = [dict(results[0])]
            check.process_results(results2, state, dry_run=False, now=1000.0 + 60)
            self.assertEqual(published, [])
            self.assertFalse(results2[0]["transitioned"])

            # Recovers to fresh -> publishes the recovery transition.
            published.clear()
            results3 = [dict(results[0], verdict="fresh", reason="mtime")]
            check.process_results(results3, state, dry_run=False, now=1000.0 + 120)
            self.assertEqual(published, ["kb-watch.service"])
            self.assertTrue(results3[0]["transitioned"])

            # Still fresh -> no re-publish.
            published.clear()
            results4 = [dict(results3[0])]
            check.process_results(results4, state, dry_run=False, now=1000.0 + 180)
            self.assertEqual(published, [])
        finally:
            check.publish_transition = orig_publish

    def test_persistent_stale_reminder_after_cooldown(self):
        published = []
        orig_publish = check.publish_transition
        check.publish_transition = lambda r, dry_run: published.append(r["target"]) or True
        try:
            state = {}
            r = {
                "target": "orca appimage", "kind": "process", "pid": 1500947, "verdict": "stale",
                "reason": "file_newer_than_process", "running_since": "2026-08-29T13:44:55Z",
                "code_newest_at": "2026-08-30T13:15:00Z", "remedy_hint": "relaunch",
            }
            check.process_results([dict(r)], state, dry_run=False, now=0.0)
            self.assertEqual(published, ["orca appimage"])

            published.clear()
            check.process_results([dict(r)], state, dry_run=False, now=3600.0)  # 1h later, within 24h cooldown
            self.assertEqual(published, [])

            published.clear()
            check.process_results([dict(r)], state, dry_run=False, now=25 * 3600.0)  # past cooldown
            self.assertEqual(published, ["orca appimage"])
        finally:
            check.publish_transition = orig_publish


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.roster_path = self.tmp / "roster.json"
        self.roster_path.write_text(json.dumps({
            "project_roots": ["/home/eric/projects"],
            "local_bin_roots": ["/home/eric/.local/bin"],
            "exclude_units": [],
            "include_units": ["kb-watch.service"],
            "appimage_matchers": [{"name": "orca", "cmdline_contains": "orca-linux.AppImage"}],
        }))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_writes_report_and_summary(self):
        def list_fn():
            return []

        def show_fn(unit):
            return {
                "Type": "simple",
                "ExecStart": "{ path=/home/eric/.local/bin/kb ; argv[]=/home/eric/.local/bin/kb watch --live ; ignore_errors=no }",
                "ExecMainStartTimestamp": "Sun 2026-08-30 16:47:05 EDT",
                "ExecMainPID": "918036",
            }

        results = check.run(
            roster_path=self.roster_path,
            state_path=self.tmp / "state.json",
            report_path=self.tmp / "report.md",
            summary_path=self.tmp / "summary.json",
            dry_run=True,
            list_fn=list_fn,
            show_fn=show_fn,
            pid_list_fn=lambda: [1500947],
            cmdline_fn=lambda pid: ["/home/eric/projects/orca/dist/orca-linux.AppImage", "--disable-vulkan"],
            lstart_fn=lambda pid: "Sat Aug 29 13:44:55 2026",
            now=check.parse_iso("2026-08-30T21:00:00Z"),
        )
        self.assertTrue((self.tmp / "report.md").exists())
        self.assertTrue((self.tmp / "summary.json").exists())
        targets = {r["target"]: r for r in results}
        self.assertIn("kb-watch.service", targets)
        self.assertIn("appimage:orca", targets)


if __name__ == "__main__":
    unittest.main()
