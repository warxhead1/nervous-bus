"""scan_denied is one section of the digest: an unreadable transcript must not take the
digest down, and the calls it counts must be the ledger's calls (distinct and dated)."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
import token_ledger as tl  # noqa: E402
import token_report as tr  # noqa: E402
from test_token_report_requests import (  # noqa: E402
    GUARD, SINCE, SLEEP, _turn, _use,
)

TOKEN_CANDIDATES = {"long_context", "cache_rewrites", "startup_context", "big_tool_results",
                    "redundant_reruns", "uncached_input"}


def _good_lane():
    """Three dated calls in the window: a read, a worktree-guard denial, a blocked sleep."""
    L = []
    L += _turn(1, 0, _use("r1", "Read", file_path="/a.rs"))
    L += _turn(2, 10, _use("d2", "Bash", command="git push origin x"), results={"d2": (GUARD, True)})
    L += _turn(3, 20, _use("d3", "Bash", command="sleep 60; ls"), results={"d3": (SLEEP, True)})
    return L


def _other_lane():
    return _turn(101, 100, _use("z1", "Bash", command="git push origin y"), results={"z1": (GUARD, True)})


def _ledger(tmp_path, lanes: dict):
    """One subagent transcript per entry of `lanes` ({agent id: jsonl lines}); returns
    (conn, roots, {agent id: transcript path})."""
    root = tmp_path / "root" / "-proj" / "s1" / "subagents"
    root.mkdir(parents=True)
    paths = {}
    for aid, lines in lanes.items():
        paths[aid] = root / f"agent-{aid}.jsonl"
        paths[aid].write_text("\n".join(lines) + "\n")
        (root / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "worker", "model": "sonnet"}))
    db = tmp_path / "tokens.db"
    tl.run(db, roots=[("claude", tmp_path / "root")])
    return sqlite3.connect(db), [("claude", tmp_path / "root")], paths


def _break(path: Path):
    """The transcript is still listed by the filesystem but cannot be opened as a file."""
    path.unlink()
    path.mkdir()


def _assert_digest_survives(conn, roots, *, unreadable: int, denied_calls: int):
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    scan = view["denied_calls"]["scan"]
    assert (scan["unreadable"], scan["missing"]) == (unreadable, 0)
    assert view["denied_calls"]["calls"] == denied_calls
    rep = tr.build(conn, days=1, roots=roots)
    ids = {c["id"] for c in rep["candidates"]}
    assert TOKEN_CANDIDATES <= ids and {"unbatched_reads", "denied_calls"} <= ids
    assert rep["requests"]["denied_calls"]["scan"]["unreadable"] == unreadable
    tr.to_markdown(rep)
    return view


def test_a_transcript_that_cannot_be_opened_is_counted_not_fatal(tmp_path):
    conn, roots, paths = _ledger(tmp_path, {"aGOOD": _good_lane(), "aBAD": _other_lane()})
    _break(paths["aBAD"])
    view = _assert_digest_survives(conn, roots, unreadable=1, denied_calls=2)
    scan = view["denied_calls"]["scan"]
    assert (scan["sessions"], scan["scanned"]) == (2, 1)
    # the readable session still reports its denials; the unreadable one is not guessed at
    assert {c["class"]: c["calls"] for c in view["denied_calls"]["by_class"]} == {
        "worktree_guard": 1, "blocked_sleep": 1}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_a_transcript_with_no_read_permission_is_counted_not_fatal(tmp_path):
    conn, roots, paths = _ledger(tmp_path, {"aGOOD": _good_lane(), "aBAD": _other_lane()})
    paths["aBAD"].chmod(0)
    try:
        _assert_digest_survives(conn, roots, unreadable=1, denied_calls=2)
    finally:
        paths["aBAD"].chmod(0o644)


def test_a_read_that_fails_midway_drops_the_whole_session(tmp_path, monkeypatch):
    bad = (_turn(101, 100, _use("z1", "Bash", command="git push origin y"), results={"z1": (GUARD, True)})
           + _turn(102, 110, _use("z2", "Bash", command="git push origin z"), results={"z2": (GUARD, True)}))
    conn, roots, _ = _ledger(tmp_path, {"aGOOD": _good_lane(), "aBAD": bad})
    cls = type(tr._claude_source()[0])
    real = cls.tool_calls

    def flaky(self, ref):
        for n, tc in enumerate(real(self, ref)):
            if n and ref.path.endswith("agent-aBAD.jsonl"):
                raise OSError(errno.EIO, "Input/output error")
            yield tc

    monkeypatch.setattr(cls, "tool_calls", flaky)
    view = _assert_digest_survives(conn, roots, unreadable=1, denied_calls=2)
    # z1 was read before the error and is still not counted: a half-read session skews the share
    assert view["denied_calls"]["scan"]["calls"] == 3


def test_a_path_check_that_fails_with_eio_is_counted_not_fatal(tmp_path, monkeypatch):
    conn, roots, _ = _ledger(tmp_path, {"aGOOD": _good_lane()})

    def dead_mount(self):
        raise OSError(errno.EIO, "Input/output error")

    with monkeypatch.context() as m:
        m.setattr(Path, "exists", dead_mount)
        view, _ = tr.request_view(conn, SINCE, roots=roots)
    scan = view["denied_calls"]["scan"]
    assert (scan["unreadable"], scan["scanned"], scan["missing"], scan["calls"]) == (1, 0, 0, 0)
    assert view["denied_calls"]["available"] is True


def _blob(role, blocks):
    """What Claude Code writes when it replays the conversation into the transcript: no timestamp."""
    return json.dumps({"type": "api-request-blob", "sessionId": "s1", "hash": "h",
                       "message": {"role": role, "content": blocks}})


def _replay(*uses_and_results):
    uses = [u for u, _ in uses_and_results]
    results = [{"type": "tool_result", "tool_use_id": u["id"], "content": text, "is_error": True}
               for u, text in uses_and_results]
    return [_blob("assistant", uses), _blob("user", results)]


def test_scan_counts_only_dated_distinct_calls(tmp_path):
    L = _turn(90, -3 * 86400, _use("old1", "Bash", command="git push origin old"), results={"old1": (GUARD, True)})
    L += _good_lane()
    # the replayed history: d2 and d3 again (undated), and x1, which only ever appears undated
    L += _replay((_use("d2", "Bash", command="git push origin x"), GUARD),
                 (_use("d3", "Bash", command="sleep 60; ls"), SLEEP),
                 (_use("x1", "Bash", command="git push origin q"), GUARD))
    conn, roots, _ = _ledger(tmp_path, {"aLANE1": L})
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    den = view["denied_calls"]
    # r1, d2, d3: dated, in the window, once each. Not old1 (before the window), not the replays
    assert den["scan"]["calls"] == 3
    assert den["calls"] == 2 and den["calls_matched_to_requests"] == 2
    ledger_calls = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE ts >= ?", (SINCE,)).fetchone()[0]
    assert den["scan"]["calls"] == ledger_calls


def test_an_undated_call_before_the_until_bound_is_not_counted_either(tmp_path):
    L = _good_lane() + _replay((_use("x1", "Bash", command="git push origin q"), GUARD))
    conn, roots, _ = _ledger(tmp_path, {"aLANE1": L})
    sc = tr.scan_denied(conn, SINCE, until="9999-01-01T00:00:00", roots=roots)
    assert sc["calls"] == 3 and sorted(sc["denied"]) == ["d2", "d3"]


def test_a_call_that_two_transcripts_both_hold_counts_once(tmp_path):
    """A resumed session copies earlier history; the ledger keeps one row per call id."""
    conn, roots, _ = _ledger(tmp_path, {"aLANE1": _good_lane(), "aLANE2": _good_lane()})
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    den = view["denied_calls"]
    assert den["scan"]["sessions"] == 2 and den["scan"]["scanned"] == 2
    assert den["scan"]["calls"] == 3 and den["calls"] == 2
