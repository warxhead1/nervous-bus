#!/usr/bin/env python3
"""Unit tests for adapters/github-relay/watch.py -- pure functions + stubbed
IO (bd/gh/nervous never invoked for real). Run:
    python3 -m unittest adapters.github-relay.test_watch -v
or  python3 adapters/github-relay/test_watch.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch  # noqa: E402


# --------------------------------------------------------------------------
# PR filtering
# --------------------------------------------------------------------------

class PullRequestFilterTests(unittest.TestCase):
    def test_pull_url_is_filtered(self):
        raw = {"number": 5, "title": "x", "state": "OPEN", "labels": [],
               "author": {"login": "a"}, "url": "https://github.com/o/r/pull/5",
               "body": "", "comments": [], "updatedAt": "2026-08-30T00:00:00Z"}
        self.assertIsNone(watch.normalize_issue(raw, "o/r"))

    def test_issue_url_passes(self):
        raw = {"number": 5, "title": "x", "state": "OPEN", "labels": [],
               "author": {"login": "a"}, "url": "https://github.com/o/r/issues/5",
               "body": "", "comments": [], "updatedAt": "2026-08-30T00:00:00Z"}
        self.assertIsNotNone(watch.normalize_issue(raw, "o/r"))


# --------------------------------------------------------------------------
# normalize_issue
# --------------------------------------------------------------------------

def raw_issue(**overrides):
    base = {
        "number": 7, "title": "Something broke", "state": "OPEN",
        "labels": [{"name": "bug"}, {"name": "P1"}],
        "author": {"login": "someuser"},
        "url": "https://github.com/o/r/issues/7",
        "body": "x" * 600,
        "comments": [{"id": 1}, {"id": 2}],
        "updatedAt": "2026-08-30T12:00:00Z",
    }
    base.update(overrides)
    return base


class NormalizeIssueTests(unittest.TestCase):
    def test_basic_fields(self):
        cur = watch.normalize_issue(raw_issue(), "o/r")
        self.assertEqual(cur["repo"], "o/r")
        self.assertEqual(cur["number"], 7)
        self.assertEqual(cur["state"], "open")
        self.assertEqual(cur["labels"], ["P1", "bug"])
        self.assertEqual(cur["author"], "someuser")
        self.assertEqual(cur["comment_count"], 2)

    def test_body_truncated_to_500(self):
        cur = watch.normalize_issue(raw_issue(), "o/r")
        self.assertEqual(len(cur["body_excerpt"]), 500)

    def test_short_body_not_padded(self):
        cur = watch.normalize_issue(raw_issue(body="short"), "o/r")
        self.assertEqual(cur["body_excerpt"], "short")

    def test_closed_state_lowercased(self):
        cur = watch.normalize_issue(raw_issue(state="CLOSED"), "o/r")
        self.assertEqual(cur["state"], "closed")


# --------------------------------------------------------------------------
# Transition detection
# --------------------------------------------------------------------------

class TransitionTests(unittest.TestCase):
    def test_first_seen_open_emits_opened(self):
        cur = watch.normalize_issue(raw_issue(), "o/r")
        self.assertEqual(watch.classify_issue_transition(None, cur), "opened")

    def test_first_seen_closed_emits_nothing(self):
        cur = watch.normalize_issue(raw_issue(state="CLOSED"), "o/r")
        self.assertIsNone(watch.classify_issue_transition(None, cur))

    def test_open_to_closed(self):
        prior = {"state": "open", "labels": ["bug"], "comment_count": 0, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(state="CLOSED", labels=[{"name": "bug"}],
                                                updatedAt="t1"), "o/r")
        self.assertEqual(watch.classify_issue_transition(prior, cur), "closed")

    def test_closed_to_open(self):
        prior = {"state": "closed", "labels": ["bug"], "comment_count": 0, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(state="OPEN", labels=[{"name": "bug"}],
                                                updatedAt="t1"), "o/r")
        self.assertEqual(watch.classify_issue_transition(prior, cur), "reopened")

    def test_new_comment_detected(self):
        prior = {"state": "open", "labels": ["bug"], "comment_count": 1, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(labels=[{"name": "bug"}], updatedAt="t1"), "o/r")
        self.assertEqual(cur["comment_count"], 2)
        self.assertEqual(watch.classify_issue_transition(prior, cur), "commented")

    def test_label_change_detected(self):
        prior = {"state": "open", "labels": ["bug"], "comment_count": 2, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(updatedAt="t1"), "o/r")  # labels bug+P1
        self.assertEqual(watch.classify_issue_transition(prior, cur), "labeled")

    def test_updated_catchall(self):
        prior = {"state": "open", "labels": ["P1", "bug"], "comment_count": 2, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(updatedAt="t1"), "o/r")
        self.assertEqual(watch.classify_issue_transition(prior, cur), "updated")

    def test_no_change_emits_nothing(self):
        prior = {"state": "open", "labels": ["P1", "bug"], "comment_count": 2, "updated_at": "2026-08-30T12:00:00Z"}
        cur = watch.normalize_issue(raw_issue(), "o/r")
        self.assertIsNone(watch.classify_issue_transition(prior, cur))

    def test_priority_state_change_over_comment_or_label(self):
        # A closed issue whose comment count and labels also changed should
        # still report "closed" -- state transitions take priority.
        prior = {"state": "open", "labels": ["bug"], "comment_count": 0, "updated_at": "t0"}
        cur = watch.normalize_issue(raw_issue(state="CLOSED", labels=[{"name": "wontfix"}],
                                                updatedAt="t1"), "o/r")
        self.assertEqual(watch.classify_issue_transition(prior, cur), "closed")


# --------------------------------------------------------------------------
# build_event_data / schema-shape
# --------------------------------------------------------------------------

class BuildEventDataTests(unittest.TestCase):
    def test_is_pull_request_always_false(self):
        cur = watch.normalize_issue(raw_issue(), "o/r")
        data = watch.build_event_data(cur, "opened")
        self.assertFalse(data["is_pull_request"])
        self.assertIn(data["action"], watch.VALID_ACTIONS)

    def test_ts_falls_back_when_updated_at_empty(self):
        cur = watch.normalize_issue(raw_issue(updatedAt=""), "o/r")
        data = watch.build_event_data(cur, "opened")
        self.assertTrue(data["ts"])


# --------------------------------------------------------------------------
# relay-config gating
# --------------------------------------------------------------------------

class RelayConfigTests(unittest.TestCase):
    def test_repo_not_listed_returns_none(self):
        cfg = {"default": {"outbound": "dry-run"}, "repos": {"o/a": {}}}
        self.assertIsNone(watch.get_repo_config(cfg, "o/unknown"))

    def test_defaults_applied(self):
        cfg = {"default": {"ingest": True, "file_beads": True, "outbound": "dry-run"},
               "repos": {"o/a": {}}}
        merged = watch.get_repo_config(cfg, "o/a")
        self.assertEqual(merged["outbound"], "dry-run")
        self.assertTrue(merged["ingest"])
        self.assertTrue(merged["file_beads"])

    def test_per_repo_override(self):
        cfg = {"default": {"outbound": "dry-run"}, "repos": {"o/a": {"outbound": "off"}}}
        merged = watch.get_repo_config(cfg, "o/a")
        self.assertEqual(merged["outbound"], "off")

    def test_unknown_outbound_mode_fails_closed_to_off(self):
        cfg = {"default": {"outbound": "dry-run"}, "repos": {"o/a": {"outbound": "bogus"}}}
        merged = watch.get_repo_config(cfg, "o/a")
        self.assertEqual(merged["outbound"], "off")

    def test_shipped_config_never_live(self):
        real_cfg = watch.load_relay_config(watch.RELAY_CONFIG_FILE)
        for repo in real_cfg.get("repos", {}):
            merged = watch.get_repo_config(real_cfg, repo)
            self.assertIn(merged["outbound"], ("off", "dry-run"),
                          f"{repo} shipped with outbound={merged['outbound']!r} -- must never ship 'live'")
        self.assertIn(real_cfg.get("default", {}).get("outbound"), ("off", "dry-run"))


# --------------------------------------------------------------------------
# Outbound gating (post_issue_comment) -- no network
# --------------------------------------------------------------------------

class PostIssueCommentTests(unittest.TestCase):
    def test_off_mode_never_calls_gh(self):
        with mock.patch("subprocess.run") as m:
            ok = watch.post_issue_comment("o/r", 1, "hi", "off")
            self.assertTrue(ok)
            m.assert_not_called()

    def test_dry_run_never_calls_gh_but_reports(self):
        lines = []
        with mock.patch("subprocess.run") as m:
            ok = watch.post_issue_comment("o/r", 1, "hi", "dry-run", report_lines=lines)
            self.assertTrue(ok)
            m.assert_not_called()
        self.assertEqual(len(lines), 1)
        self.assertIn("DRY-RUN", lines[0])

    def test_live_mode_calls_gh_issue_comment(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=fake) as m:
            ok = watch.post_issue_comment("o/r", 1, "hi", "live")
            self.assertTrue(ok)
            args = m.call_args[0][0]
            self.assertEqual(args[:3], ["gh", "issue", "comment"])

    def test_live_mode_gh_failure_returns_false(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=fake):
            ok = watch.post_issue_comment("o/r", 1, "hi", "live")
            self.assertFalse(ok)


# --------------------------------------------------------------------------
# Bead title / dedup
# --------------------------------------------------------------------------

class BeadTitleTests(unittest.TestCase):
    def test_title_format(self):
        t = watch.bead_title_for_issue("warxhead1/tengine", 42, "crash on load")
        self.assertEqual(t, "gh-issue: warxhead1/tengine#42: crash on load")

    def test_long_title_truncated(self):
        t = watch.bead_title_for_issue("o/r", 1, "x" * 200)
        self.assertLessEqual(len(t), len("gh-issue: o/r#1: ") + 80)
        self.assertTrue(t.endswith("..."))


# --------------------------------------------------------------------------
# End-to-end pass with stubbed fetch_fn/bd -- exercises run_relay_pass
# --------------------------------------------------------------------------

class RunRelayPassTests(unittest.TestCase):
    def _cfg(self, outbound="dry-run"):
        return {
            "default": {"ingest": True, "file_beads": True, "outbound": outbound},
            "repos": {"o/r": {}},
        }

    def test_new_open_issue_files_one_bead_and_emits_opened(self):
        state = {}

        def fetch(repo, limit):
            return [raw_issue()]

        with mock.patch.object(watch, "file_issue_bead", return_value="bd-1") as file_bead, \
             mock.patch.object(watch, "publish_event", return_value=True) as pub, \
             mock.patch.object(watch, "bead_is_closed_with_external_ref", return_value=None):
            results = watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "opened")
        self.assertEqual(results[0]["bead_id"], "bd-1")
        file_bead.assert_called_once()
        pub.assert_called_once()
        self.assertEqual(state["o/r#7"]["bead_id"], "bd-1")

    def test_bead_not_refiled_on_second_poll(self):
        state = {"o/r#7": {"state": "open", "labels": ["P1", "bug"], "comment_count": 2,
                            "updated_at": "2026-08-30T12:00:00Z", "bead_id": "bd-1",
                            "pr_comment_posted": False}}

        def fetch(repo, limit):
            return [raw_issue()]  # unchanged

        with mock.patch.object(watch, "file_issue_bead") as file_bead, \
             mock.patch.object(watch, "publish_event") as pub, \
             mock.patch.object(watch, "bead_is_closed_with_external_ref", return_value=None):
            results = watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False)

        file_bead.assert_not_called()
        pub.assert_not_called()  # no transition
        self.assertIsNone(results[0]["action"])
        self.assertEqual(results[0]["bead_id"], "bd-1")

    def test_pr_link_comment_posted_once_when_bead_closes_with_ref(self):
        state = {"o/r#7": {"state": "open", "labels": ["P1", "bug"], "comment_count": 2,
                            "updated_at": "2026-08-30T12:00:00Z", "bead_id": "bd-1",
                            "pr_comment_posted": False}}

        def fetch(repo, limit):
            return [raw_issue()]

        with mock.patch.object(watch, "file_issue_bead") as file_bead, \
             mock.patch.object(watch, "publish_event"), \
             mock.patch.object(watch, "bead_is_closed_with_external_ref",
                                return_value="https://github.com/o/r/pull/9") as ref_check, \
             mock.patch.object(watch, "post_issue_comment", return_value=True) as comment:
            results = watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False)

        file_bead.assert_not_called()
        ref_check.assert_called_once_with("bd-1")
        comment.assert_called_once()
        self.assertTrue(results[0]["pr_comment_posted"])
        self.assertTrue(state["o/r#7"]["pr_comment_posted"])

        # Second poll: already posted, must not post again.
        with mock.patch.object(watch, "bead_is_closed_with_external_ref",
                                return_value="https://github.com/o/r/pull/9"), \
             mock.patch.object(watch, "post_issue_comment") as comment2:
            watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False)
        comment2.assert_not_called()

    def test_pr_ends_are_skipped_entirely(self):
        def fetch(repo, limit):
            return [{"number": 9, "title": "PR", "state": "OPEN", "labels": [],
                      "author": {"login": "a"}, "url": "https://github.com/o/r/pull/9",
                      "body": "", "comments": [], "updatedAt": "t"}]

        state = {}
        with mock.patch.object(watch, "file_issue_bead") as file_bead, \
             mock.patch.object(watch, "publish_event") as pub:
            results = watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False)

        self.assertEqual(results, [])
        file_bead.assert_not_called()
        pub.assert_not_called()

    def test_no_beads_flag_skips_bd_and_still_reports_issue(self):
        def fetch(repo, limit):
            return [raw_issue()]

        state = {}
        with mock.patch.object(watch, "bd_json") as bd, \
             mock.patch.object(watch, "publish_event"):
            results = watch.run_relay_pass(self._cfg(), state, fetch_fn=fetch, dry_run=False, no_beads=True)

        bd.assert_not_called()
        self.assertIsNone(results[0]["bead_id"])

    def test_repo_out_of_scope_is_skipped(self):
        cfg = {"default": {"ingest": True}, "repos": {}}  # nothing listed
        with mock.patch.object(watch, "fetch_issues") as fetch:
            results = watch.run_relay_pass(cfg, {}, fetch_fn=lambda r, l: [raw_issue()], dry_run=True)
        self.assertEqual(results, [])


# --------------------------------------------------------------------------
# state key / roundtrip
# --------------------------------------------------------------------------

class StateIOTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            data = {"o/r#7": {"state": "open"}}
            watch.save_state(data, path)
            loaded = watch.load_state(path)
            self.assertEqual(loaded, data)

    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nope.json"
            self.assertEqual(watch.load_state(path), {})

    def test_issue_state_key(self):
        self.assertEqual(watch.issue_state_key("o/r", 7), "o/r#7")


class RunStatePersistenceTests(unittest.TestCase):
    """A dry run must not consume transitions by persisting state (2026-08-31)."""

    def _run(self, tmp: Path, *, dry_run: bool) -> Path:
        cfg = tmp / "relay-config.json"
        cfg.write_text(json.dumps({"defaults": {"outbound": "off"},
                                   "repos": {"o/r": {"ingest": True, "file_beads": False,
                                                     "outbound": "off"}}}))
        state = tmp / "state.json"

        def fetch(repo, *args):
            return [{"number": 1, "title": "t", "state": "OPEN", "labels": [],
                     "author": {"login": "a"}, "updatedAt": "2026-08-31T00:00:00Z",
                     "url": "https://x", "body": "", "comments": []}]

        watch.run(relay_config_path=cfg, state_path=state,
                  report_path=tmp / "report.md", snapshot_path=tmp / "snap.json",
                  dry_run=dry_run, no_beads=True, fetch_fn=fetch)
        return state

    def test_dry_run_does_not_persist_state(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._run(Path(d), dry_run=True)
            self.assertFalse(state.exists(),
                             "dry-run persisted state; next real run would miss transitions")

    def test_real_run_persists_state(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._run(Path(d), dry_run=False)
            self.assertTrue(state.exists())
            self.assertIn("o/r#1", json.loads(state.read_text()))


if __name__ == "__main__":
    unittest.main()
