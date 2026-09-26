#!/usr/bin/env python3
"""reflex-recorder — Reflexarc FLYWHEEL: segment agent activity into runs.

Consumes nbus:all via XREADGROUP (at-least-once), filters to
bus.agent.activity.v1, segments into runs using the hardened composite-key
model, persists to SQLite, and emits bus.agent.run.closed.v1 via
`nervous publish` on each run close.

Usage:
    python recorder.py                  # run continuously
    python recorder.py --once           # drain current stream and exit
    python recorder.py --config foo.toml
    python recorder.py --replay /path/to/debug.jsonl   # offline fixture mode

See reflex-recorder.toml for configuration.

Durability boundary (XREADGROUP ack timing vs run_events persistence)
---------------------------------------------------------------------
`_run_xreadgroup` does not XACK a stream entry until the WHOLE batch it
arrived in has been journaled to SQLite.  `_ingest_batch` parses every
entry, writes every accepted `bus.agent.activity.v1` event to the
`pending_events` table in ONE transaction (`store.journal_events`), and only
after that commit returns does it fold each event into the in-memory
`Segmenter` run; the caller then XACKs the batch.  So the ack means
"durable", not merely "delivered" — the event is on disk in `pending_events`
before Redis is told it can forget it, independent of whether the run it
belongs to ever closes cleanly.

A run's events leave `pending_events` only when that run closes (`ended`,
`idle_timeout` after idle_timeout_s = 900s by default, or
`recorder_shutdown`): `store.close_run()` moves them into `run_events` and
deletes the journal rows, in the same transaction as the `runs` upsert.

Fold-cursor invariant: a single journaled batch can contain more than one
run under the same run_key (an `ended` event followed by a reopen further
in the batch). `close_run` must only drain rows folded so far for that
run_key, never rows still waiting to be folded — see `_fold_cursor` and
`_persist_and_publish`.

  covered      systemctl stop/restart, `kill <pid>` (SIGTERM — the handler
               sets a latch, the read loop breaks, and the `finally` runs
               `Recorder.shutdown()`); SIGKILL / OOM kill / host power loss
               / a hard interpreter crash at any point after a batch's
               `journal_events` commit — those events are already durable,
               and `Recorder._recover_pending_events()` re-folds them into
               the Segmenter at the next startup (the runs they belonged to
               simply reopen and close normally later; see
               `Recorder.__init__`).
  NOT covered  a crash strictly between a batch's XREADGROUP delivery and
               its `journal_events` commit. Those entries are undelivered
               to any durable store and sit only in Redis's pending-entries
               list (PEL) under the dead consumer's name; this recorder does
               not yet reclaim PEL entries from a prior consumer name on
               restart (each process uses a pid-suffixed consumer name), so
               closing that residual window is a separate follow-up
               (PEL reclaim via XCLAIM/XAUTOCLAIM).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis

# ── Repo root / sibling paths ─────────────────────────────────────────────────
_ADAPTER_DIR = Path(__file__).parent
_NBUS_ROOT = _ADAPTER_DIR.parent.parent
_NERVOUS_BIN = _NBUS_ROOT / "sdk" / "shell" / "nervous"

sys.path.insert(0, str(_ADAPTER_DIR))
from segment import Segmenter
from store import SQLiteStore, DEFAULT_DB_PATH

# ── Constants ─────────────────────────────────────────────────────────────────
CONSUMER_GROUP = "reflex-recorder"
CONSUMER_NAME = f"reflex-recorder-{os.getpid()}"
STREAM_NAME = "nbus:all"
ACTIVITY_TYPE = "bus.agent.activity.v1"
PUBLISH_CHANNEL = "bus.agent.run.closed.v1"

DEFAULT_IDLE_TIMEOUT_S = 900.0   # 15 min
DEFAULT_METRICS_INTERVAL_S = 60.0
DEFAULT_TICK_INTERVAL_S = 30.0

# Wall-clock budget for the graceful-shutdown flush.  systemd's
# TimeoutStopSec=60 (systemd/reflex-recorder.service) is the hard ceiling:
# once it expires the unit is SIGKILLed mid-flush.  Persistence itself is a
# single `store.close_run()` transaction per run, so it is not the tall
# pole; this budget mainly bounds the best-effort `nervous publish`
# fan-out, which is the only thing dropped once the budget is spent.
DEFAULT_SHUTDOWN_BUDGET_S = 10.0
DEFAULT_PUBLISH_TIMEOUT_S = 10.0


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = _ADAPTER_DIR / "reflex-recorder.toml"


def _load_config(path: Path) -> dict:
    cfg = {
        "redis_url": "redis://localhost:6379",
        "redis_db": 0,
        "connect_timeout_s": 5.0,
        "idle_timeout_s": DEFAULT_IDLE_TIMEOUT_S,
        "metrics_interval_s": DEFAULT_METRICS_INTERVAL_S,
        "tick_interval_s": DEFAULT_TICK_INTERVAL_S,
        "db_path": None,  # None → use DEFAULT_DB_PATH
        "stream_read_count": 200,
        "stream_block_ms": 2000,
        "shutdown_budget_s": DEFAULT_SHUTDOWN_BUDGET_S,
    }
    if not path.exists():
        return cfg
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] config parse error {path}: {e}\n")
        return cfg

    redis_cfg = raw.get("redis", {})
    if "url" in redis_cfg:
        cfg["redis_url"] = redis_cfg["url"]
    if "db" in redis_cfg:
        cfg["redis_db"] = int(redis_cfg["db"])
    if "connect_timeout_s" in redis_cfg:
        cfg["connect_timeout_s"] = float(redis_cfg["connect_timeout_s"])

    rec_cfg = raw.get("recorder", {})
    for key in ("idle_timeout_s", "metrics_interval_s", "tick_interval_s",
                "shutdown_budget_s"):
        if key in rec_cfg:
            cfg[key] = float(rec_cfg[key])
    if "stream_read_count" in rec_cfg:
        cfg["stream_read_count"] = int(rec_cfg["stream_read_count"])
    if "stream_block_ms" in rec_cfg:
        cfg["stream_block_ms"] = int(rec_cfg["stream_block_ms"])

    store_cfg = raw.get("store", {})
    if "db_path" in store_cfg:
        cfg["db_path"] = Path(store_cfg["db_path"]).expanduser()

    return cfg


# ── Graceful shutdown ─────────────────────────────────────────────────────────

class ShutdownSignal:
    """A latch set from a signal handler and polled by the read loop.

    The handler does nothing but flip a bool and bump a counter, so it is
    re-entrancy safe: the flush itself runs on the main thread inside the
    ordinary ``finally`` path rather than inside the handler.  That is what
    makes a second SIGTERM arriving mid-flush harmless.
    """

    def __init__(self) -> None:
        self._set = False
        self.count = 0
        self.signum: Optional[int] = None

    def request(self, signum=None, frame=None) -> None:  # noqa: ARG002 (signal ABI)
        self._set = True
        self.count += 1
        if signum is not None:
            self.signum = signum

    def is_set(self) -> bool:
        return self._set


def install_shutdown_handlers(
    flag: Optional[ShutdownSignal] = None,
    signums=(signal.SIGTERM,),
) -> ShutdownSignal:
    """Route ``signums`` at the shutdown latch instead of the default action.

    Python's default disposition for SIGTERM is the C default — the process
    dies inside the kernel, so no ``finally``/``atexit`` runs and every run
    still open in the segmenter is lost (its already-journaled events survive
    in ``pending_events``, but the open-run aggregates live only in the
    Segmenter and only ``Recorder.shutdown()`` closes them out cleanly).  A
    ``systemctl restart`` sends exactly that signal.
    """
    flag = flag or ShutdownSignal()
    for signum in signums:
        try:
            signal.signal(signum, flag.request)
        except (ValueError, OSError) as e:
            # ValueError: not on the main thread. OSError: signal not settable.
            sys.stderr.write(f"[reflex-recorder] cannot install handler for {signum}: {e}\n")
            sys.stderr.flush()
    return flag


def _sleep_interruptible(seconds: float, shutdown: Optional[ShutdownSignal],
                         slice_s: float = 0.25) -> None:
    """Sleep, but give up early once shutdown is requested.

    Backoff sleeps are the one place a bounded stop can silently become an
    unbounded one: a redis outage during a restart would otherwise hold the
    loop for the full retry interval before the latch is ever polled.
    """
    deadline = time.time() + seconds
    while True:
        if shutdown is not None and shutdown.is_set():
            return
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(slice_s, remaining))


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _ensure_consumer_group(r: redis.Redis) -> None:
    """Create the consumer group if it doesn't exist.

    Start from '$' (new events only — backfill is a separate follow-up task).
    The MKSTREAM flag ensures the stream is created if it doesn't exist yet.
    """
    try:
        r.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="$", mkstream=True)
        sys.stderr.write(f"[reflex-recorder] created consumer group '{CONSUMER_GROUP}' on {STREAM_NAME}\n")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            # Group already exists — normal on restart
            sys.stderr.write(f"[reflex-recorder] consumer group '{CONSUMER_GROUP}' already exists\n")
        else:
            raise
    sys.stderr.flush()


# ── Publish via shell SDK ─────────────────────────────────────────────────────

def _publish_run(payload: dict, timeout: float = DEFAULT_PUBLISH_TIMEOUT_S) -> bool:
    """Emit a bus.agent.run.closed.v1 via `nervous publish` (shell SDK).

    Returns True on success, False on failure (non-fatal — run is already
    persisted to SQLite, publish failure just means no live bus delivery).

    `timeout` is squeezed by the caller during shutdown so that N open runs
    cannot multiply one 10s subprocess timeout into an N*10s stop.
    """
    try:
        result = subprocess.run(
            [str(_NERVOUS_BIN), "publish", PUBLISH_CHANNEL, json.dumps(payload)],
            capture_output=True,
            timeout=max(0.1, timeout),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:300]
            sys.stderr.write(f"[reflex-recorder] publish failed (rc={result.returncode}): {stderr}\n")
            sys.stderr.flush()
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] publish exception: {e}\n")
        sys.stderr.flush()
        return False


# ── Recorder state ────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        db_path = cfg.get("db_path") or DEFAULT_DB_PATH
        self.store = SQLiteStore(db_path)
        self._runs_closed = 0
        self._runs_published = 0
        self._events_ingested = 0
        self._events_skipped = 0
        self._started_at = time.time()
        # Graceful-shutdown bookkeeping.
        self._shutdown_started = False
        self._shutdown_deadline: Optional[float] = None
        self._shutdown_publishes_skipped = 0
        self._shutdown_flush_errors = 0
        # Cumulative wall-clock spent in store.close_run() vs _publish_run(),
        # surfaced in the shutdown log line.
        self._persist_s = 0.0
        self._publish_s = 0.0
        # run_key -> highest pending_events.id folded into the Segmenter so
        # far for that key. `close_run` uses this to bound its drain: a
        # single journaled batch can contain an `ended` event followed by a
        # reopen of the same run_key, and only rows up to this cursor belong
        # to the run that is closing. See _ingest_batch / _persist_and_publish.
        self._fold_cursor: dict[str, int] = {}

        def on_run_closed(payload: dict) -> None:
            self._on_run_closed(payload)

        self.segmenter = Segmenter(
            idle_timeout_s=cfg["idle_timeout_s"],
            on_run_closed=on_run_closed,
        )

        # B1 fix: rebuild _last_closed_id from the DB so continues_run_id
        # survives recorder restarts.  Without this, every restart wipes the
        # in-memory map and any new event on an existing run_key starts with
        # continues_run_id=None instead of pointing at the pre-restart run.
        self._rebuild_last_closed_id()
        self._recover_pending_events()

    def _recover_pending_events(self) -> None:
        """Re-fold journaled pre-crash events back into the live Segmenter.

        `pending_events` is the write-ahead journal that made these events
        durable before their batch's XACK, independent of whether the run
        they belonged to ever closed. On a fresh start we replay them
        (oldest first, across all run_keys) through the ordinary
        `Segmenter.ingest` path — NOT through `_ingest_batch`/journal_events
        again, since re-journaling would duplicate rows already sitting in
        the table. The runs they belonged to simply reopen in memory and
        close normally later (idle timeout, `ended`, or the next shutdown),
        at which point `store.close_run()` drains their now-larger
        `pending_events` set (recovered rows plus anything ingested since
        restart) into `run_events` and clears the journal for that run_key.
        Idempotent across repeated crashes: nothing here removes journal
        rows, so re-running recovery after another crash just re-folds the
        same (now possibly larger) set into a fresh in-memory Segmenter.

        Sets `_fold_cursor` per row before folding it, same as the live
        path, so a recovered `ended` event closes correctly bounded to only
        the rows recovered up to that point.
        """
        try:
            rows = self.store.recover_pending_events()
        except Exception as e:
            sys.stderr.write(f"[reflex-recorder] _recover_pending_events failed: {e}\n")
            sys.stderr.flush()
            return
        if not rows:
            return
        now = time.time()
        recovered = 0
        for pending_id, run_key, _event_ts, _event_type, raw_json in rows:
            parsed = self._parse_envelope(raw_json)
            if parsed is None:
                continue
            data, _etype, _ts = parsed
            self._fold_cursor[run_key] = pending_id
            self.segmenter.ingest(data, now=now)
            self._events_ingested += 1
            recovered += 1
        sys.stderr.write(
            f"[reflex-recorder] recovered {recovered}/{len(rows)} journaled "
            f"event(s) from a prior crash/kill into {self.segmenter.open_run_count} "
            f"open run(s)\n"
        )
        sys.stderr.flush()

    def _rebuild_last_closed_id(self) -> None:
        """Rebuild _last_closed_id from the DB at startup.

        B1 fix: queries runs.db for the most recently closed run_id per run_key
        so that continues_run_id linkage works across recorder restarts.
        Only considers runs closed with reasons other than recorder_shutdown to
        avoid re-stitching a shutdown-closed run as the predecessor (those are
        operational closes, not semantic pauses).
        """
        try:
            rows = self.store.latest_run_id_per_key()
            self.segmenter._last_closed_id.update(rows)
            if rows:
                sys.stderr.write(
                    f"[reflex-recorder] rebuilt _last_closed_id: {len(rows)} run_keys\n"
                )
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[reflex-recorder] _rebuild_last_closed_id failed: {e}\n")
            sys.stderr.flush()

    def _on_run_closed(self, payload: dict) -> None:
        """Called by the segmenter when a run is closed.

        During shutdown any failure here is contained: one unpersistable run
        must not abort the flush of the runs behind it in the queue.  On the
        normal path the exception still propagates so the read loop logs it.
        """
        if not self._shutdown_started:
            self._persist_and_publish(payload)
            return
        try:
            self._persist_and_publish(payload)
        except Exception as e:
            self._shutdown_flush_errors += 1
            sys.stderr.write(
                f"[reflex-recorder] shutdown flush failed for run "
                f"{payload.get('run_id')}: {e}\n"
            )
            sys.stderr.flush()

    def _persist_and_publish(self, payload: dict) -> None:
        run_id = payload["run_id"]
        run_key = payload["run_key"]

        # 1+2. Persist the run AND drain its journaled pending_events into
        #    run_events, in ONE transaction (store.close_run). Bounded by
        #    the fold cursor for this run_key: a single journaled batch can
        #    contain an `ended` event followed by a reopen of the same key,
        #    and only rows folded up to (and including) this close belong
        #    to the run that is closing — later rows in the same batch
        #    belong to the run that reopens after it. Popped (not merely
        #    read) so a later close under the same run_key, once its own
        #    events have been folded, can't reuse this stale value.
        upto_id = self._fold_cursor.pop(run_key, None)
        t0 = time.time()
        self.store.close_run(payload, upto_id=upto_id)
        t1 = time.time()
        self._persist_s += t1 - t0

        # 3. Emit via nervous publish — best effort, and the only step allowed
        #    to be dropped when the shutdown budget is spent.  Persistence
        #    above has already happened, so a skipped publish costs live bus
        #    delivery, never the record.
        budget = self._publish_budget()
        if budget is None:
            ok = False
            self._shutdown_publishes_skipped += 1
        else:
            ok = _publish_run(payload, timeout=budget)
        self._publish_s += time.time() - t1
        self._runs_closed += 1
        if ok:
            self._runs_published += 1

        sys.stderr.write(
            f"[reflex-recorder] closed run {run_id} "
            f"key={run_key!r} reason={payload.get('close_reason')} "
            f"events={payload['event_count']} "
            f"tools={list(payload['tool_histogram'].keys())[:5]} "
            f"published={'ok' if ok else ('skipped_budget' if budget is None else 'FAILED')}\n"
        )
        sys.stderr.flush()

    def _publish_budget(self) -> Optional[float]:
        """Seconds `nervous publish` may take, or None to skip it entirely."""
        if self._shutdown_deadline is None:
            return DEFAULT_PUBLISH_TIMEOUT_S
        remaining = self._shutdown_deadline - time.time()
        if remaining <= 0:
            return None
        return min(DEFAULT_PUBLISH_TIMEOUT_S, remaining)

    def _parse_envelope(self, raw_json: str) -> Optional[tuple[dict, str, str]]:
        """Parse one raw CloudEvents envelope. Returns (data, event_type, ts)
        or None if it is not an accepted `bus.agent.activity.v1` event."""
        try:
            envelope = json.loads(raw_json)
        except Exception:
            return None
        event_type = envelope.get("type", "")
        if event_type != ACTIVITY_TYPE:
            return None
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            return None
        ts = data.get("ts") or data.get("time") or _now_utc()
        return data, event_type, ts

    def _ingest_activity(self, raw_json: str, stream_id: str, *, journal: bool = True) -> None:
        """Parse+ingest a single envelope (replay mode / unit tests).

        The live XREADGROUP loop uses `_ingest_batch` instead, which journals
        a whole batch in ONE transaction rather than one commit per event —
        this wraps it as a one-entry batch so callers that only have one
        envelope in hand keep working unchanged.
        """
        self._ingest_batch([(stream_id, raw_json)], journal=journal)

    def _ingest_batch(self, entries: list[tuple[str, str]], *, journal: bool = True) -> None:
        """Parse a batch of (stream_id, raw_json) and fold each into the
        Segmenter.

        When `journal` is True (the live path), every accepted event is
        written to the `pending_events` write-ahead journal in ONE
        transaction BEFORE any of them are folded into the Segmenter — the
        caller (`_run_xreadgroup`) must not XACK any stream id in this batch
        until this call returns, so the durability boundary is the journal
        commit, not the in-memory fold. `journal=False` is for
        `_recover_pending_events`, which is re-folding rows that are already
        the journal and must not be re-inserted.

        Before folding a journaled event, `_fold_cursor[run_key]` is set to
        that event's `pending_events.id`. A batch can contain more than one
        run under the same run_key (an `ended` event followed by a reopen
        later in the same batch); the cursor is what lets `close_run`
        (called synchronously from inside `segmenter.ingest` when a run
        closes) drain only the rows folded so far, not rows still waiting
        their turn in this same loop.
        """
        # Each entry: [data_or_None, run_key_or_None, pending_id_or_None].
        # A mutable list per entry so journal_events' assigned ids can be
        # filled in after journaling, before the fold loop below reads them.
        plan: list[list] = []
        journal_rows: list[tuple[str, str, str, str, str]] = []
        journal_slots: list[list] = []
        for stream_id, raw_json in entries:
            result = self._parse_envelope(raw_json)
            if result is None:
                self._events_skipped += 1
                continue
            data, event_type, ts = result
            # An empty run_key is refused by Segmenter.ingest, so journaling
            # under it would accumulate events no run close can ever drain.
            run_key_tuple = self._get_run_key(data)
            run_key = run_key_tuple[0] if (run_key_tuple and run_key_tuple[0]) else None
            slot = [data, run_key, None]
            plan.append(slot)
            if journal and run_key:
                journal_rows.append((run_key, stream_id, ts, event_type, raw_json))
                journal_slots.append(slot)

        if journal_rows:
            ids = self.store.journal_events(journal_rows)
            for slot, pending_id in zip(journal_slots, ids):
                slot[2] = pending_id

        now = time.time()
        for data, run_key, pending_id in plan:
            if pending_id is not None:
                self._fold_cursor[run_key] = pending_id
            self.segmenter.ingest(data, now=now)
            self._events_ingested += 1

    def _get_run_key(self, activity: dict) -> Optional[tuple]:
        """Compute run key for buffering without duplicating logic."""
        from segment import compute_run_key
        try:
            return compute_run_key(activity)
        except Exception:
            return None

    def log_metrics(self) -> None:
        elapsed = time.time() - self._started_at
        rate = self._events_ingested / max(1, elapsed)
        sys.stderr.write(
            f"[{_now_utc()}] reflex-recorder metrics: "
            f"ingested={self._events_ingested} skipped={self._events_skipped} "
            f"closed={self._runs_closed} published={self._runs_published} "
            f"open_runs={self.segmenter.open_run_count} "
            f"rate={rate:.2f}/s\n"
        )
        sys.stderr.flush()

    def shutdown(self, budget_s: Optional[float] = None) -> bool:
        """Flush every open run to SQLite, then close the store.

        Idempotent: the second call is a no-op and returns False, so a signal
        arriving while the flush is already running, or a signal followed by
        the normal ``finally`` path, cannot double-close or double-append.
        (The underlying writes are idempotent too — ``store.close_run``'s
        ``runs`` upsert is INSERT OR REPLACE on the run_id primary key, and
        it deletes each ``pending_events`` row it drains in the same
        transaction — but the latch is what keeps the accounting honest.)

        `budget_s` bounds the wall clock spent on best-effort publishing; the
        SQLite writes themselves are never skipped.
        """
        if self._shutdown_started:
            return False
        self._shutdown_started = True
        if budget_s is None:
            budget_s = float(self.cfg.get("shutdown_budget_s", DEFAULT_SHUTDOWN_BUDGET_S))
        self._shutdown_deadline = time.time() + budget_s

        open_runs = self.segmenter.open_run_count
        started = time.time()
        persist_before, publish_before = self._persist_s, self._publish_s
        sys.stderr.write(
            f"[reflex-recorder] graceful shutdown: flushing {open_runs} open run(s), "
            f"budget={budget_s:.1f}s\n"
        )
        sys.stderr.flush()
        try:
            self.segmenter.shutdown()
        finally:
            elapsed = time.time() - started
            # Per-phase timing: how much of the flush was SQLite persistence
            # (store.close_run, never skipped) vs the best-effort `nervous
            # publish` fan-out (bounded by budget_s).
            persist_s = self._persist_s - persist_before
            publish_s = self._publish_s - publish_before
            sys.stderr.write(
                f"[reflex-recorder] shutdown flush done in {elapsed:.2f}s "
                f"(persist={persist_s:.3f}s publish={publish_s:.3f}s): "
                f"runs_closed={self._runs_closed} "
                f"publishes_skipped={self._shutdown_publishes_skipped} "
                f"flush_errors={self._shutdown_flush_errors}\n"
            )
            sys.stderr.flush()
            self.log_metrics()
            self.store.close()
        return True


# ── Main loop ─────────────────────────────────────────────────────────────────

def _run_xreadgroup(r: redis.Redis, recorder: Recorder, cfg: dict, once: bool = False,
                    shutdown: Optional[ShutdownSignal] = None) -> None:
    last_metrics = time.time()
    last_tick = time.time()
    read_count = cfg["stream_read_count"]
    block_ms = cfg["stream_block_ms"]
    metrics_interval = cfg["metrics_interval_s"]
    tick_interval = cfg["tick_interval_s"]

    while True:
        if shutdown is not None and shutdown.is_set():
            sys.stderr.write(
                f"[reflex-recorder] shutdown requested (signal={shutdown.signum}); "
                f"leaving read loop\n"
            )
            sys.stderr.flush()
            break

        try:
            results = r.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=read_count,
                block=block_ms if not once else 0,
            )

            if results:
                for _stream, entries in results:
                    # Journal the whole batch in one transaction, fold each
                    # event into the Segmenter, THEN ack the whole batch —
                    # the ack must not precede the journal commit. See
                    # _ingest_batch / store.journal_events.
                    batch = [
                        (stream_id, fields.get("_raw", "{}"))
                        for stream_id, fields in entries
                    ]
                    recorder._ingest_batch(batch)
                    for stream_id, _fields in entries:
                        r.xack(STREAM_NAME, CONSUMER_GROUP, stream_id)

            now = time.time()

            # Idle-timeout check
            if now - last_tick >= tick_interval:
                recorder.segmenter.tick(now=now)
                last_tick = now

            # Metrics log
            if now - last_metrics >= metrics_interval:
                recorder.log_metrics()
                last_metrics = now

            if once and not results:
                break

        except KeyboardInterrupt:
            break
        except redis.ConnectionError as e:
            sys.stderr.write(f"[reflex-recorder] redis connection lost: {e}; retrying in 5s\n")
            sys.stderr.flush()
            _sleep_interruptible(5, shutdown)
        except Exception as e:
            sys.stderr.write(f"[reflex-recorder] error: {e}\n")
            sys.stderr.flush()
            _sleep_interruptible(1, shutdown)

        if once:
            break


def _run_replay(recorder: Recorder, replay_path: Path) -> None:
    """Offline mode: read bus.agent.activity.v1 lines from debug.jsonl fixture."""
    sys.stderr.write(f"[reflex-recorder] replay mode: {replay_path}\n")
    count = 0
    skipped = 0
    try:
        with open(replay_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except Exception:
                    continue
                if envelope.get("type") != ACTIVITY_TYPE:
                    skipped += 1
                    continue
                recorder._ingest_activity(line, "replay")
                count += 1
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] replay error: {e}\n")
        sys.stderr.flush()

    # After replay, close all open runs
    sys.stderr.write(f"[reflex-recorder] replay done: {count} activity events, {skipped} skipped; closing open runs\n")
    sys.stderr.flush()
    recorder.segmenter.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="reflex-recorder — Reflexarc run capture")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Path to config TOML (default: {DEFAULT_CONFIG})")
    parser.add_argument("--once", action="store_true",
                        help="Drain current PEL+stream entries and exit")
    parser.add_argument("--replay", type=Path, default=None,
                        help="Offline replay mode: read activity events from debug.jsonl file")
    parser.add_argument("--db-path", type=Path, default=None,
                        help="Override SQLite DB path")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    if args.db_path:
        cfg["db_path"] = args.db_path

    recorder = Recorder(cfg)

    sys.stderr.write(
        f"[reflex-recorder] starting: db={recorder.store.db_path} "
        f"idle_timeout={cfg['idle_timeout_s']}s\n"
    )
    sys.stderr.flush()

    if args.replay:
        _run_replay(recorder, args.replay)
        recorder.log_metrics()
        # Print a brief store dump
        recent = recorder.store.recent_runs(10)
        sys.stderr.write(f"[reflex-recorder] store dump ({len(recent)} runs):\n")
        for r in recent:
            sys.stderr.write(
                f"  run_id={r['run_id'][:12]}.. key_kind={r['run_key_kind']} "
                f"project={r['project']} events={r['event_count']} "
                f"close={r['close_reason']} wt={r['worktree_slug']}\n"
            )
        sys.stderr.flush()
        recorder.store.close()
        return 0

    # Live mode: XREADGROUP.
    # The latch is armed here rather than at the top of main() so --replay, which
    # has no read loop to poll it, keeps the default disposition instead of
    # silently ignoring SIGTERM.
    shutdown = install_shutdown_handlers()

    try:
        r = redis.Redis.from_url(
            cfg["redis_url"],
            db=cfg["redis_db"],
            socket_timeout=cfg["connect_timeout_s"],
            socket_connect_timeout=cfg["connect_timeout_s"],
            decode_responses=True,
        )
        r.ping()
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] redis connect failed: {e}\n")
        return 1

    _ensure_consumer_group(r)

    try:
        _run_xreadgroup(r, recorder, cfg, once=args.once, shutdown=shutdown)
    finally:
        recorder.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
