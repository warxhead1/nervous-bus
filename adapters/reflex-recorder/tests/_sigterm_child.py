#!/usr/bin/env python3
"""_sigterm_child.py — child process driver for test_recorder_shutdown.py.

Deliberately not named ``test_*`` so pytest does not collect it: this module is
only ever executed as a subprocess that the test suite then sends a real
SIGTERM to.

What is real here and what is a stand-in matters, because the point of the test
is to exercise the production stop path rather than a re-implementation of it:

  real         ``Recorder``, ``Segmenter``, ``SQLiteStore``, the
               ``_run_xreadgroup`` loop, ``install_shutdown_handlers``, the
               signal disposition, and ``Recorder.shutdown``.
  stand-in     redis (a fake returning one batch then blocking, so the test
               needs no server) and ``_publish_run`` (the shell SDK would try
               to reach the same absent redis).

The fake's blocking read sleeps for the configured BLOCK interval and, like a
real socket read under PEP 475, resumes after the handler returns — so the
observed stop latency is a genuine measurement of the loop's poll granularity,
not an artefact of an instantly-abortable stub.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import recorder as recorder_mod  # noqa: E402


def _envelope(conversation_id: str, worktree: str | None, seq: int) -> str:
    data = {
        "conversation_id": conversation_id,
        "session_id": conversation_id,
        "agent_id": conversation_id,
        "project": "nervous-bus",
        "agent_kind": "host_claude_code",
        "event": "tool_call",
        "tool_name": "Bash",
        "ts": f"2026-09-06T02:00:{seq:02d}Z",
        "cwd": (
            f"/home/eric/data2/worktrees/nervous-bus/{worktree}"
            if worktree
            else "/home/eric/projects/nervous-bus"
        ),
    }
    if worktree:
        data["worktree"] = worktree
    return json.dumps({"type": recorder_mod.ACTIVITY_TYPE, "data": data})


class _FakeRedis:
    """Serves one batch, then blocks like a real XREADGROUP with BLOCK set."""

    def __init__(self, batch, block_ms: int, ready_file: Path):
        self._batch = batch
        self._block_ms = block_ms
        self._ready_file = ready_file
        self.acked: list[str] = []
        self._served = False

    def xreadgroup(self, groupname, consumername, streams, count, block):
        if not self._served:
            self._served = True
            return [(recorder_mod.STREAM_NAME, self._batch)]
        # Everything in the batch is now ingested AND acked. Announce readiness
        # so the parent can observe the pre-signal durability state, then block.
        if not self._ready_file.exists():
            self._ready_file.write_text(json.dumps({"acked": len(self.acked)}))
        time.sleep(self._block_ms / 1000.0)
        return []

    def xack(self, stream, group, stream_id):
        self.acked.append(stream_id)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--ready-file", type=Path, required=True)
    ap.add_argument("--summary-file", type=Path, required=True)
    ap.add_argument("--mode", default="fast",
                    choices=("fast", "slow_publish", "flush_error"))
    ap.add_argument("--budget-s", type=float, default=10.0)
    args = ap.parse_args()

    published: list[str] = []

    if args.mode == "slow_publish":
        def _publish(payload, timeout=recorder_mod.DEFAULT_PUBLISH_TIMEOUT_S):
            # Burn exactly the budget handed to us, the way a `nervous publish`
            # that hits its subprocess timeout would.
            time.sleep(max(0.0, timeout))
            return False
    else:
        def _publish(payload, timeout=recorder_mod.DEFAULT_PUBLISH_TIMEOUT_S):
            published.append(payload["run_id"])
            return True
    recorder_mod._publish_run = _publish

    cfg = {
        "redis_url": "redis://localhost:6379",
        "redis_db": 0,
        "connect_timeout_s": 5.0,
        "idle_timeout_s": 900.0,
        "metrics_interval_s": 3600.0,
        "tick_interval_s": 3600.0,
        "db_path": args.db,
        "stream_read_count": 200,
        "stream_block_ms": 500,
        "shutdown_budget_s": args.budget_s,
    }

    recorder = recorder_mod.Recorder(cfg)

    if args.mode == "flush_error":
        real_save = recorder.store.save_run
        state = {"failed": False}

        def _save(payload):
            if not state["failed"]:
                state["failed"] = True
                raise RuntimeError("simulated store failure on first run")
            real_save(payload)
        recorder.store.save_run = _save

    # conv-a spans two worktrees (two run_keys); conv-b is a session run.
    # None of the events are `ended`, so all three runs are still open — and
    # their raw envelopes still only in _pending_events — when SIGTERM lands.
    raw = [
        _envelope("conv-a", "wt-one", 1),
        _envelope("conv-a", "wt-one", 2),
        _envelope("conv-a", "wt-two", 3),
        _envelope("conv-a", "wt-two", 4),
        _envelope("conv-b", None, 5),
        _envelope("conv-b", None, 6),
    ]
    batch = [(f"1757116800000-{i}", {"_raw": r}) for i, r in enumerate(raw)]

    fake = _FakeRedis(batch, cfg["stream_block_ms"], args.ready_file)
    shutdown = recorder_mod.install_shutdown_handlers()

    exit_code = 0
    try:
        recorder_mod._run_xreadgroup(fake, recorder, cfg, shutdown=shutdown)
    finally:
        first = recorder.shutdown()
        # Repeated shutdown: the normal `finally` path plus a second signal (or
        # simply a caller that flushes twice) must not re-close or re-append.
        second = recorder.shutdown()
        args.summary_file.write_text(json.dumps({
            "expected_events": len(raw),
            "acked": len(fake.acked),
            "signals": shutdown.count,
            "first_shutdown": first,
            "second_shutdown": second,
            "runs_closed": recorder._runs_closed,
            "runs_published": recorder._runs_published,
            "publishes_skipped": recorder._shutdown_publishes_skipped,
            "flush_errors": recorder._shutdown_flush_errors,
            "published_run_ids": published,
        }))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
