"""Regression tests for `nervous publish --json` (shell SDK pass-through).

Guards two coupled invariants that a kernel-publish double-wrap bug exposed:

  * stdin is read EXACTLY ONCE — an earlier version read it twice (once to
    sniff the event type, once for the payload), so the second read got an
    empty string and the publish failed with "failed to parse event type".
  * the envelope is forwarded VERBATIM — `--json` must not re-wrap the
    incoming envelope as the `data` field of a fresh envelope (the non-json
    path does that, which is exactly the double-wrap we are avoiding).

Runs the real shell script with Redis + zellij disabled and the debug log
redirected to a tmp file, so it needs no live services.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

NERVOUS = Path(__file__).resolve().parents[1] / "sdk" / "shell" / "nervous"

ENVELOPE = {
    "specversion": "1.0",
    "id": "01JSONPASSTHRU",
    "source": "/autobench/e2e_kernel",
    "type": "e2e.kernel.started.v1",
    "datacontenttype": "application/json",
    "time": "2026-06-02T00:00:00Z",
    "data": {"run_id": "01E2ETEST", "marker": "passthru"},
}


@pytest.fixture()
def debug_log(tmp_path: Path) -> Path:
    return tmp_path / "debug.jsonl"


def _publish_json(payload: str, debug_log: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(NERVOUS), "publish", "--json"],
        input=payload,
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "NERVOUS_NO_REDIS": "1",
            "NERVOUS_NO_ZELLIJ": "1",
            "NERVOUS_DEBUG_LOG": str(debug_log),
            "HOME": str(debug_log.parent),
        },
    )


def test_json_passthrough_writes_envelope_verbatim(debug_log: Path):
    proc = _publish_json(json.dumps(ENVELOPE), debug_log)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in debug_log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one debug line, got {lines}"
    written = json.loads(lines[0])
    # Verbatim — same id/source/type, data NOT nested under another envelope.
    assert written["id"] == ENVELOPE["id"]
    assert written["source"] == ENVELOPE["source"]
    assert written["type"] == ENVELOPE["type"]
    assert written["data"] == ENVELOPE["data"]
    assert "specversion" not in written["data"]  # not a re-wrapped envelope


def test_json_passthrough_reads_stdin_once(debug_log: Path):
    # A second internal stdin read would yield an empty payload and this would
    # fail with a non-zero exit and "failed to parse event type".
    proc = _publish_json(json.dumps(ENVELOPE), debug_log)
    assert proc.returncode == 0
    assert "failed to parse event type" not in proc.stderr
