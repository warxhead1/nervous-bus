"""tests/test_edit_build_fail_revert.py — Unit tests for EditBuildFailRevertDetector.

Covers:
  - Positive: full edit->build->fail->revert cycle fires.
  - Positive (strong thrash): >=2 cycles within a run.
  - Negative: edit->build->success (no fail) does NOT fire.
  - Negative: edit->build with no revert does NOT fire.
  - Negative: no edit events at all does NOT fire.
  - Signature stability: signature does NOT contain run_id or timestamp.
  - Signature format: <project>:<detector>:<anchor>.
  - Remediation rung recorded in extra.
  - AUTOMATE rung when 'command not found' detected.
  - Recurrence/dedup: same signature increments recurrence_count across scans.
  - Implicit fail+revert: re-edit of same file after build (without explicit error).
  - Revert via git checkout command.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import pytest

# Add adapter root to sys.path so 'from detectors.base import ...' works.
_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.edit_build_fail_revert import (
    EditBuildFailRevertDetector,
    _find_thrash_cycles,
    _normalize_area,
    _is_build_command,
    _is_revert_command,
    _has_fail_output,
    BUILD_KEYWORDS,
    FAIL_PATTERNS,
    REVERT_KEYWORDS,
)


# ── Minimal schema for tests ──────────────────────────────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    started       TEXT NOT NULL,
    ended         TEXT NOT NULL,
    outcome       TEXT,
    labeled_at    TEXT,
    worktree      TEXT,
    worktree_slug TEXT,
    git_branch    TEXT,
    bead_id       TEXT,
    close_reason  TEXT
);
"""

_RUN_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA + _RUN_EVENTS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    project: str,
    outcome: Optional[str] = "clean",
    labeled_at: Optional[str] = None,
    close_reason: Optional[str] = "ended",
) -> None:
    # close_reason defaults to "ended" because the real run-store only ever
    # holds CLOSED runs (the recorder writes row + events atomically at close).
    # Pass close_reason=None to simulate an (impossible-in-prod) open run and
    # assert the detector's completeness gate excludes it.
    now = _now_utc()
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, started, ended, outcome, labeled_at, close_reason)
           VALUES (?,?,?,?,?,?,?)""",
        (run_id, project, now, now, outcome, labeled_at or now, close_reason),
    )


def _make_event_json(
    tool_name: str,
    tool_summary: object,
    tool_response_summary: object,
    project: str = "testproj",
    cwd: str = "/home/eric/projects/testproj",
) -> str:
    """Build a raw_json bus.agent.activity.v1 event."""
    ts_str = json.dumps(tool_summary) if not isinstance(tool_summary, str) else tool_summary
    rs_str = json.dumps(tool_response_summary) if not isinstance(tool_response_summary, str) else tool_response_summary
    data = {
        "agent": "claude-code",
        "agent_id": "test-agent",
        "agent_kind": "host_claude_code",
        "conversation_id": "test-conv",
        "cwd": cwd,
        "display": f"{project} [test]",
        "event": "tool_call",
        "pane_id_qualified": "tmux:%1",
        "project": project,
        "session_id": "test-session",
        "tool_name": tool_name,
        "tool_summary": ts_str,
        "tool_response_summary": rs_str,
        "transcript_path": "/tmp/test.jsonl",
        "ts": _now_utc(),
        "worktree": None,
    }
    return json.dumps({
        "specversion": "1.0",
        "id": "test-event-id",
        "source": "/test/source",
        "type": "bus.agent.activity.v1",
        "time": _now_utc(),
        "datacontenttype": "application/json",
        "data": data,
    })


def _insert_event(
    conn: sqlite3.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_summary: object = "",
    tool_response_summary: object = "",
    project: str = "testproj",
    cwd: str = "/home/eric/projects/testproj",
) -> None:
    raw = _make_event_json(tool_name, tool_summary, tool_response_summary, project, cwd)
    conn.execute(
        """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
           VALUES (?,?,?,?,?)""",
        (run_id, seq, _now_utc(), "bus.agent.activity.v1", raw),
    )


# ── Convenience builders for common events ────────────────────────────────────

def _edit_event(file_path: str, cwd: str = "/home/eric/projects/testproj", project: str = "testproj"):
    ts = {"file_path": file_path, "old_string": "old", "new_string": "new"}
    rs = {"filePath": file_path, "type": "edit", "userModified": False}
    return ts, rs, project, cwd


def _write_event(file_path: str, cwd: str = "/home/eric/projects/testproj", project: str = "testproj"):
    ts = {"file_path": file_path, "content": "new content"}
    rs = {"filePath": file_path, "type": "create", "userModified": False}
    return ts, rs, project, cwd


def _bash_event(cmd: str, stdout: str = "", stderr: str = "", cwd: str = "/home/eric/projects/testproj", project: str = "testproj"):
    ts = {"command": cmd}
    rs = {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False, "noOutputExpected": False}
    return ts, rs, project, cwd


# ── Tests: keyword helpers ────────────────────────────────────────────────────

class TestKeywordHelpers:
    def test_is_build_command_cargo_test(self):
        assert _is_build_command("cargo test --lib -p mylib 2>&1")

    def test_is_build_command_go_build(self):
        assert _is_build_command("go build ./... 2>&1")

    def test_is_build_command_pytest(self):
        assert _is_build_command("pytest tests/ -q")

    def test_is_build_command_not_ls(self):
        assert not _is_build_command("ls -la")

    def test_is_build_command_not_git_status(self):
        assert not _is_build_command("git status 2>&1")

    def test_is_revert_command_git_checkout(self):
        assert _is_revert_command("git checkout -- src/foo.rs")

    def test_is_revert_command_git_restore(self):
        assert _is_revert_command("git restore src/foo.rs")

    def test_is_revert_command_git_reset(self):
        assert _is_revert_command("git reset HEAD src/foo.rs")

    def test_is_revert_command_not_cargo(self):
        assert not _is_revert_command("cargo test")

    def test_has_fail_output_rust_error(self):
        assert _has_fail_output("error[E0308]: mismatched types", "")

    def test_has_fail_output_go_fail(self):
        assert _has_fail_output("FAIL\tgithub.com/foo/bar\t0.003s", "")

    def test_has_fail_output_command_not_found(self):
        assert _has_fail_output("zsh: command not found: cargo", "")

    def test_has_fail_output_no_fail_on_pass(self):
        assert not _has_fail_output("ok  \tgithub.com/foo/bar\t0.003s", "")

    def test_has_fail_output_no_fail_on_empty(self):
        assert not _has_fail_output("", "")


class TestNormalizeArea:
    def test_strips_worktree_prefix(self):
        path = "/home/eric/projects/myproj/.claude/worktrees/wf_abc123/internal/sim/foo.go"
        result = _normalize_area(path, "", "myproj")
        assert result == "internal/sim/foo.go"

    def test_strips_project_root(self):
        path = "/home/eric/projects/myproj/internal/util/bar.go"
        result = _normalize_area(path, "", "myproj")
        assert result == "internal/util/bar.go"

    def test_falls_back_to_cwd_when_no_file_path(self):
        cwd = "/home/eric/projects/myproj/internal"
        result = _normalize_area("", cwd, "myproj")
        assert "internal" in result

    def test_falls_back_to_project_when_empty(self):
        result = _normalize_area("", "", "myproj")
        assert result == "myproj"

    def test_same_file_different_worktrees_same_anchor(self):
        """The same logical file in two different worktrees should normalize to the same anchor."""
        path1 = "/home/eric/projects/myproj/.claude/worktrees/wf_aaa/src/main.rs"
        path2 = "/home/eric/projects/myproj/.claude/worktrees/wf_bbb/src/main.rs"
        assert _normalize_area(path1, "", "myproj") == _normalize_area(path2, "", "myproj")


# ── Tests: positive detection ─────────────────────────────────────────────────

class TestPositiveDetection:
    """Full edit->build->fail->revert cycle fires."""

    def test_complete_cycle_detected(self):
        conn = _make_db()
        run_id = "run-positive-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        # Seq 1: Edit a file
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)

        # Seq 2: Run cargo test
        ts2, rs2, p2, c2 = _bash_event(
            "cargo test --lib 2>&1",
            stdout="error[E0308]: mismatched types at lib.rs:42",
            cwd=cwd,
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)

        # Seq 3: git restore (revert)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        c_out = candidates[0]
        assert c_out.project == "testproj"
        assert c_out.detector == "edit_build_fail_revert"
        assert c_out.occurrences == 1
        assert c_out.extra["thrash_cycles"] == 1

    def test_strong_thrash_two_cycles(self):
        """Two complete cycles in one run → strong_thrash=True, occurrences=2."""
        conn = _make_db()
        run_id = "run-strong-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        # Cycle 1: Edit -> Build(fail) -> Revert
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo build 2>&1", stdout="error: build failed", cwd=cwd
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        # Cycle 2: Edit same file -> Build(fail) -> Revert
        ts4, rs4, p4, c4 = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 4, "Edit", ts4, rs4, p4, c4)
        ts5, rs5, p5, c5 = _bash_event(
            "cargo build 2>&1", stdout="error: build failed", cwd=cwd
        )
        _insert_event(conn, run_id, 5, "Bash", ts5, rs5, p5, c5)
        ts6, rs6, p6, c6 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 6, "Bash", ts6, rs6, p6, c6)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].occurrences == 2
        assert candidates[0].extra["strong_thrash"] is True

    def test_implicit_revert_via_re_edit(self):
        """Re-edit of the same file after a build = implicit fail+revert."""
        conn = _make_db()
        run_id = "run-implicit-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        # Edit
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        # Build (no explicit fail output - build was backgrounded)
        ts2, rs2, p2, c2 = _bash_event("cargo test 2>&1", stdout="", cwd=cwd)
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        # Re-edit same file (implicit revert)
        ts3, rs3, p3, c3 = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 3, "Edit", ts3, rs3, p3, c3)
        # Build again (with error)
        ts4, rs4, p4, c4 = _bash_event(
            "cargo test 2>&1", stdout="error: aborting due to previous error", cwd=cwd
        )
        _insert_event(conn, run_id, 4, "Bash", ts4, rs4, p4, c4)
        # Final revert
        ts5, rs5, p5, c5 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 5, "Bash", ts5, rs5, p5, c5)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].occurrences >= 1

    def test_command_not_found_triggers_eliminate_rung(self):
        """'command not found' in build output → ELIMINATE rung (one-time PATH fix)."""
        conn = _make_db()
        run_id = "run-cmdnotfound-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/foo.rs"
        cwd = "/home/eric/projects/testproj"

        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo build 2>&1", stdout="zsh: command not found: cargo", cwd=cwd
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/foo.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].extra["remediation_rung"] == "eliminate"
        assert candidates[0].extra["command_not_found"] is True


# ── Tests: negative (no false fire) ──────────────────────────────────────────

class TestNegativeNoFalseFire:
    """Ensure the detector does NOT fire on clean patterns."""

    def test_no_fire_on_successful_build(self):
        """Edit -> build (passes) -> no revert → no fire."""
        conn = _make_db()
        run_id = "run-success-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo test 2>&1",
            stdout="test result: ok. 5 passed; 0 failed",
            cwd=cwd,
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == []

    def test_no_fire_on_edit_without_build(self):
        """Edit only, no build → no fire."""
        conn = _make_db()
        run_id = "run-noedit-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        # Some other bash command, not a build
        ts2, rs2, p2, c2 = _bash_event("git status", cwd=cwd)
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == []

    def test_no_fire_on_no_events(self):
        """Run with no events → no fire."""
        conn = _make_db()
        run_id = "run-noevents-001"
        _insert_run(conn, run_id, "testproj")

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == []

    def test_no_fire_on_bash_only(self):
        """Only Bash events, no Edit/Write → no fire."""
        conn = _make_db()
        run_id = "run-bashonly-001"
        _insert_run(conn, run_id, "testproj")

        cwd = "/home/eric/projects/testproj"
        for i, cmd in enumerate(["cargo test", "go build", "pytest"], start=1):
            ts, rs, p, c = _bash_event(cmd, stdout="error: build failed", cwd=cwd)
            _insert_event(conn, run_id, i, "Bash", ts, rs, p, c)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == []

    def test_no_fire_when_build_fails_but_no_revert(self):
        """Edit -> build (fails) but no revert → no fire."""
        conn = _make_db()
        run_id = "run-norevert-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"

        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo build 2>&1", stdout="error[E0308]: mismatched types", cwd=cwd
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        # Only more bash commands (no revert, no re-edit of same file)
        ts3, rs3, p3, c3 = _bash_event("git status", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == []


# ── Tests: signature format ────────────────────────────────────────────────────

class TestSignatureFormat:
    """Validate signature stability and format requirements."""

    def test_signature_does_not_contain_run_id(self):
        """The signature MUST NOT contain the run_id."""
        conn = _make_db()
        run_id = "run-sigtest-001"
        _insert_run(conn, run_id, "myproject")

        fp = "/home/eric/projects/myproject/src/main.rs"
        cwd = "/home/eric/projects/myproject"

        ts, rs, p, c = _edit_event(fp, cwd, project="myproject")
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/main.rs", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        sig = candidates[0].signature
        assert run_id not in sig, f"Signature '{sig}' must not contain run_id"

    def test_signature_does_not_contain_timestamp(self):
        """The signature MUST NOT contain any timestamp component."""
        conn = _make_db()
        run_id = "run-sigtest-002"
        _insert_run(conn, run_id, "myproject")

        fp = "/home/eric/projects/myproject/src/main.rs"
        cwd = "/home/eric/projects/myproject"

        ts, rs, p, c = _edit_event(fp, cwd, project="myproject")
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/main.rs", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        sig = candidates[0].signature

        # A timestamp looks like: 2026-06-13T... or 2026-06-13 or similar
        import re
        has_timestamp = bool(re.search(r"\d{4}-\d{2}-\d{2}", sig))
        assert not has_timestamp, f"Signature '{sig}' must not contain a timestamp"

    def test_signature_format_project_detector_anchor(self):
        """Signature must be exactly <project>:<DETECTOR_NAME>:<stable_anchor>."""
        conn = _make_db()
        run_id = "run-sigformat-001"
        _insert_run(conn, run_id, "myproject")

        fp = "/home/eric/projects/myproject/src/main.rs"
        cwd = "/home/eric/projects/myproject"

        ts, rs, p, c = _edit_event(fp, cwd, project="myproject")
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/main.rs", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        sig = candidates[0].signature
        parts = sig.split(":")
        assert len(parts) >= 3, f"Signature '{sig}' must have at least 3 colon-separated parts"
        assert parts[0] == "myproject", f"First part must be project, got '{parts[0]}'"
        assert parts[1] == "edit_build_fail_revert", f"Second part must be DETECTOR_NAME, got '{parts[1]}'"
        assert parts[2], "Anchor part must not be empty"

    def test_signature_stable_across_two_runs_same_area(self):
        """Two runs thrashing on the same file produce the SAME signature."""
        conn = _make_db()
        fp = "/home/eric/projects/myproject/src/main.rs"
        cwd = "/home/eric/projects/myproject"

        def _add_cycle(rid: str, seq_start: int) -> None:
            _insert_run(conn, rid, "myproject")
            ts, rs, p, c = _edit_event(fp, cwd, project="myproject")
            _insert_event(conn, rid, seq_start, "Edit", ts, rs, p, c)
            ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project="myproject")
            _insert_event(conn, rid, seq_start + 1, "Bash", ts2, rs2, p2, c2)
            ts3, rs3, p3, c3 = _bash_event("git restore src/main.rs", cwd=cwd, project="myproject")
            _insert_event(conn, rid, seq_start + 2, "Bash", ts3, rs3, p3, c3)

        _add_cycle("run-stable-001", 1)
        det1 = EditBuildFailRevertDetector(conn)
        cands1 = det1.run()  # run() exercises the full contract (hit-recording + dedup)
        sig1 = cands1[0].signature

        _add_cycle("run-stable-002", 1)
        det2 = EditBuildFailRevertDetector(conn)
        cands2 = det2.run()
        # Both runs have the same anchor → same signature
        sigs = {c.signature for c in cands2}
        assert sig1 in sigs, f"Signature {sig1} should appear in second scan: {sigs}"


# ── Tests: remediation ladder ─────────────────────────────────────────────────

class TestRemediationLadder:
    """remediation_rung must be set in extra and justified."""

    def test_rung_present_in_extra(self):
        conn = _make_db()
        run_id = "run-rung-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd)
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert "remediation_rung" in candidates[0].extra
        rung = candidates[0].extra["remediation_rung"]
        assert rung in ("eliminate", "automate", "inform"), f"Invalid rung: {rung}"

    def test_inform_rung_when_no_specific_error_class(self):
        """Generic build failure (no classifiable error) → INFORM rung."""
        conn = _make_db()
        run_id = "run-inform-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo build 2>&1", stdout="error: build failed", cwd=cwd
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].extra["remediation_rung"] == "inform"

    def test_eliminate_rung_on_command_not_found(self):
        """PATH issue detected → ELIMINATE rung (one-time permanent PATH fix)."""
        conn = _make_db()
        run_id = "run-automate-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(
            "cargo build 2>&1",
            stdout="zsh: command not found: cargo\n",
            cwd=cwd,
        )
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].extra["remediation_rung"] == "eliminate"

    def test_repeated_identical_build_stays_inform(self):
        """Repeated identical build → INFORM (strong): we don't know WHAT broke, so
        escalating to AUTOMATE would violate the detector's own ladder rule."""
        conn = _make_db()
        run_id = "run-automate-repeated-001"
        _insert_run(conn, run_id, "testproj")

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"
        build_cmd = "cargo check --lib 2>&1"

        # Cycle 1
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event(build_cmd, stdout="error: build failed", cwd=cwd)
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        # Cycle 2 (same build command)
        ts4, rs4, p4, c4 = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 4, "Edit", ts4, rs4, p4, c4)
        ts5, rs5, p5, c5 = _bash_event(build_cmd, stdout="error: build failed", cwd=cwd)
        _insert_event(conn, run_id, 5, "Bash", ts5, rs5, p5, c5)
        ts6, rs6, p6, c6 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 6, "Bash", ts6, rs6, p6, c6)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 1
        assert candidates[0].extra["remediation_rung"] == "inform"
        assert candidates[0].extra["repeated_same_build"] is True

    def test_open_run_excluded_by_completeness_gate(self):
        """A run with close_reason IS NULL (in-flight) must NOT be scanned —
        its event stream may be truncated mid-cycle and would fire spuriously."""
        conn = _make_db()
        run_id = "run-open-001"
        _insert_run(conn, run_id, "testproj", close_reason=None)

        fp = "/home/eric/projects/testproj/src/lib.rs"
        cwd = "/home/eric/projects/testproj"
        ts, rs, p, c = _edit_event(fp, cwd)
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd)
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd)
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()
        assert candidates == [], "open (close_reason NULL) run must be excluded"


# ── Tests: recurrence/dedup ───────────────────────────────────────────────────

class TestRecurrenceAndDedup:
    """Kyoko recurrence: same signature increments recurrence_count across scans."""

    def test_recurrence_increments_across_two_scans(self):
        """Running the detector twice on the same data increments recurrence_count."""
        conn = _make_db()
        run_id = "run-recur-001"
        _insert_run(conn, run_id, "myproject")

        fp = "/home/eric/projects/myproject/src/foo.rs"
        cwd = "/home/eric/projects/myproject"

        ts, rs, p, c = _edit_event(fp, cwd, project="myproject")
        _insert_event(conn, run_id, 1, "Edit", ts, rs, p, c)
        ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 2, "Bash", ts2, rs2, p2, c2)
        ts3, rs3, p3, c3 = _bash_event("git restore src/foo.rs", cwd=cwd, project="myproject")
        _insert_event(conn, run_id, 3, "Bash", ts3, rs3, p3, c3)

        det1 = EditBuildFailRevertDetector(conn)
        cands1 = det1.run()
        assert len(cands1) == 1
        sig = cands1[0].signature
        issue1 = det1.get_issue(sig)
        assert issue1["recurrence_count"] == 1

        det2 = EditBuildFailRevertDetector(conn)
        cands2 = det2.run()
        issue2 = det2.get_issue(sig)
        assert issue2["recurrence_count"] == 2

    def test_different_projects_different_signatures(self):
        """Same thrash pattern on different projects → different signatures."""
        conn = _make_db()

        for proj in ("proj-alpha", "proj-beta"):
            rid = f"run-{proj}-001"
            _insert_run(conn, rid, proj)
            fp = f"/home/eric/projects/{proj}/src/lib.rs"
            cwd = f"/home/eric/projects/{proj}"
            ts, rs, p, c = _edit_event(fp, cwd, project=proj)
            _insert_event(conn, rid, 1, "Edit", ts, rs, p, c)
            ts2, rs2, p2, c2 = _bash_event("cargo build 2>&1", stdout="error: build failed", cwd=cwd, project=proj)
            _insert_event(conn, rid, 2, "Bash", ts2, rs2, p2, c2)
            ts3, rs3, p3, c3 = _bash_event("git restore src/lib.rs", cwd=cwd, project=proj)
            _insert_event(conn, rid, 3, "Bash", ts3, rs3, p3, c3)

        det = EditBuildFailRevertDetector(conn)
        candidates = det.run()

        assert len(candidates) == 2
        sigs = {c.signature for c in candidates}
        assert len(sigs) == 2, "Different projects must produce different signatures"
        for sig in sigs:
            for bad_id in ["run-proj-alpha-001", "run-proj-beta-001"]:
                assert bad_id not in sig, f"Signature '{sig}' must not contain run_id '{bad_id}'"


# ── Tests: multiple build types ───────────────────────────────────────────────

class TestBuildTypes:
    """Verify all BUILD_KEYWORDS are recognised."""

    @pytest.mark.parametrize("cmd", [
        "cargo test --lib 2>&1",
        "go build ./... 2>&1",
        "pytest tests/ -q",
        "npm test",
        "npm run build",
        "make test",
    ])
    def test_build_keyword_recognised(self, cmd: str):
        assert _is_build_command(cmd), f"Command '{cmd}' should be recognised as a build command"


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
