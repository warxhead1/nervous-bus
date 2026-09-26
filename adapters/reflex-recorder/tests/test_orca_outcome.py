"""tests/test_orca_outcome.py — nervous-bus-48 fix 5: Orca worker_done as a
tier-3 label source.

Uses real tmp_path rollout .jsonl fixtures (the same CommandExecution shape
observed in a live rollout — see orca_outcome.py's module docstring for the
verbatim example this mirrors), never mocks of the parsing regex itself.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orca_outcome
from orca_outcome import (
    _extract_worker_done_outcome,
    _session_id_from_path,
    build_worker_done_index,
    iter_rollout_paths,
    label_from_orca_worker_done,
)


def _rollout_filename(session_id: str, ts: str = "2026-09-06T00-38-34") -> str:
    return f"rollout-{ts}-{session_id}.jsonl"


def _write_rollout(dir_path: Path, session_id: str, command_lines: list[str],
                    day: tuple[str, str, str] = ("2026", "09", "06")) -> Path:
    """Write a minimal rollout .jsonl with one CommandExecution event_msg per
    entry in command_lines (each a full shell command string, as codex would
    join its argv list)."""
    y, mo, d = day
    day_dir = dir_path / y / mo / d
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / _rollout_filename(session_id)
    with open(path, "w") as f:
        for i, cmd in enumerate(command_lines):
            row = {
                "timestamp": f"{y}-{mo}-{d}T00:00:0{i}.000Z",
                "ordinal": i,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": session_id,
                    "item": {
                        "type": "CommandExecution",
                        "id": f"exec-{i}",
                        "command": ["/bin/zsh", "-lc", cmd],
                        "exit_code": 0,
                    },
                },
            }
            f.write(json.dumps(row) + "\n")
    return path


# ── _session_id_from_path / iter_rollout_paths ────────────────────────────────

class TestSessionIdFromPath(unittest.TestCase):
    def test_matches_real_shape(self):
        sid = _session_id_from_path(
            "/home/eric/.codex/sessions/2026/09/06/"
            "rollout-2026-09-06T00-38-34-01a07502-d27d-7c62-b861-c28b1ee4ea9b.jsonl"
        )
        self.assertEqual(sid, "01a07502-d27d-7c62-b861-c28b1ee4ea9b")

    def test_no_match_returns_none(self):
        self.assertIsNone(_session_id_from_path("/some/other/file.jsonl"))


class TestIterRolloutPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dedupes_across_roots_by_session_id(self):
        """Two account roots mirroring the SAME rollout file must collapse to
        one entry (real layout: ~/.codex/sessions vs
        ~/.config/orca/codex-accounts/<acct>/home/sessions duplicate content)."""
        root_a = self.tmp_path / "root_a"
        root_b = self.tmp_path / "root_b"
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9b"
        _write_rollout(root_a, sid, ["echo hi"])
        _write_rollout(root_b, sid, ["echo hi"])

        paths = iter_rollout_paths(roots=[str(root_a), str(root_b)])
        self.assertEqual(len(paths), 1)

    def test_since_days_excludes_old_rollout(self):
        root = self.tmp_path / "root"
        old_sid = "01a00000-0000-7000-8000-000000000001"
        new_sid = "01a00000-0000-7000-8000-000000000002"
        _write_rollout(root, old_sid, ["echo old"], day=("2020", "01", "01"))
        _write_rollout(root, new_sid, ["echo new"])  # defaults to 2026-09-06

        # since_days bounded relative to "now" (real wall clock), so the 2020
        # fixture is always outside any reasonable window and the 2026-09-06
        # one is either in or out depending on today's date — assert only the
        # exclusion, which is date-independent.
        paths = iter_rollout_paths(since_days=30, roots=[str(root)])
        sids = {_session_id_from_path(p) for p in paths}
        self.assertNotIn(old_sid, sids)

    def test_unbounded_includes_everything(self):
        root = self.tmp_path / "root"
        sid = "01a00000-0000-7000-8000-000000000003"
        _write_rollout(root, sid, ["echo x"], day=("2020", "01", "01"))
        paths = iter_rollout_paths(since_days=None, roots=[str(root)])
        sids = {_session_id_from_path(p) for p in paths}
        self.assertIn(sid, sids)


# ── _extract_worker_done_outcome ──────────────────────────────────────────────

class TestExtractWorkerDoneOutcome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_succeeded(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9b"
        path = _write_rollout(self.tmp_path, sid, [
            'orca orchestration send --from term_x --dispatch-capability dcap_y '
            '--type worker_done --subject "done" --body "ok" --outcome succeeded',
        ])
        self.assertEqual(_extract_worker_done_outcome(str(path)), "succeeded")

    def test_extracts_failed(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9c"
        path = _write_rollout(self.tmp_path, sid, [
            'orca orchestration send --type worker_done --outcome failed --body "blocked by rule"',
        ])
        self.assertEqual(_extract_worker_done_outcome(str(path)), "failed")

    def test_no_worker_done_returns_none(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9d"
        path = _write_rollout(self.tmp_path, sid, ['git commit -am "wip"'])
        self.assertIsNone(_extract_worker_done_outcome(str(path)))

    def test_later_send_overrides_earlier(self):
        """A worker that corrects itself (send worker_done twice) reports the
        LAST outcome, not the first."""
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9e"
        path = _write_rollout(self.tmp_path, sid, [
            'orca orchestration send --type worker_done --outcome failed --body "first try"',
            'orca orchestration send --type worker_done --outcome succeeded --body "actually fine"',
        ])
        self.assertEqual(_extract_worker_done_outcome(str(path)), "succeeded")

    def test_status_message_with_worker_done_word_is_not_a_send(self):
        """A message that merely MENTIONS worker_done (e.g. a coordinator
        instructing the worker) must not be parsed as a send."""
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea9f"
        path = _write_rollout(self.tmp_path, sid, [
            'echo "Send worker_done with your report path, then stop."',
        ])
        self.assertIsNone(_extract_worker_done_outcome(str(path)))


# ── build_worker_done_index / label_from_orca_worker_done ────────────────────

class TestLabelFromOrcaWorkerDone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.root = self.tmp_path / "root"

    def tearDown(self):
        self._tmp.cleanup()

    def test_succeeded_maps_to_clean(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea01"
        _write_rollout(self.root, sid, [
            'orca orchestration send --type worker_done --outcome succeeded --body "ok"',
        ])
        run = {"session_id": sid}
        result = label_from_orca_worker_done(run, roots=[str(self.root)])
        self.assertEqual(result, ("clean", "orca_worker_done"))

    def test_failed_maps_to_abandoned(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea02"
        _write_rollout(self.root, sid, [
            'orca orchestration send --type worker_done --outcome failed --body "blocked"',
        ])
        run = {"session_id": sid}
        result = label_from_orca_worker_done(run, roots=[str(self.root)])
        self.assertEqual(result, ("abandoned", "orca_worker_done"))

    def test_blocked_maps_to_none(self):
        """'blocked' is ambiguous (often a REJECT-worked-as-designed audit
        verdict) — must not be asserted as any terminal outcome."""
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea03"
        _write_rollout(self.root, sid, [
            'orca orchestration send --type worker_done --outcome blocked --body "waiting"',
        ])
        run = {"session_id": sid}
        result = label_from_orca_worker_done(run, roots=[str(self.root)])
        self.assertIsNone(result)

    def test_no_session_id_returns_none(self):
        self.assertIsNone(label_from_orca_worker_done({}, roots=[str(self.root)]))

    def test_session_id_with_no_rollout_match_returns_none(self):
        run = {"session_id": "01a07502-d27d-7c62-b861-c28b1ee4eaff"}
        result = label_from_orca_worker_done(run, roots=[str(self.root)])
        self.assertIsNone(result)

    def test_build_worker_done_index_force_refresh(self):
        sid = "01a07502-d27d-7c62-b861-c28b1ee4ea04"
        _write_rollout(self.root, sid, [
            'orca orchestration send --type worker_done --outcome succeeded --body "ok"',
        ])
        index = build_worker_done_index(roots=[str(self.root)], force_refresh=True)
        self.assertEqual(index.get(sid), "succeeded")


if __name__ == "__main__":
    unittest.main()
