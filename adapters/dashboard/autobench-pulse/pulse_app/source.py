"""Event sources for pulse_app.

Three implementations:
  * ``FileSource`` — tails a JSONL file (default: ``~/.cache/nervous-bus/debug.jsonl``).
    Supports a "once" mode that yields every existing line then stops, useful for
    snapshot tests and ``--once`` smoke tests.
  * ``BusSource``  — spawns ``deer obs bus --json`` as a subprocess and yields one
    event dict per line. Auto-falls-back to FileSource if ``deer`` is unavailable
    or the subprocess dies.
  * ``replay_from_file`` — nervous-bus-zynw. Streams events from a finished
    session's JSONL at a controlled speed (1x = real-time, 100x cap). Optional
    ``session_id`` filter so a multi-session log replays only the cycle you
    care about.

All sources expose ``iter_events()`` — a synchronous iterator yielding event
dicts. The Textual app drives them from a ``@work(thread=True)`` worker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_DEBUG_FILE = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"

# Cap on the replay speed multiplier. Above ~100x the sleep math underflows
# the OS scheduler granularity anyway, so we clamp to keep the badge honest.
REPLAY_SPEED_CAP: float = 100.0


class FileSource:
    """Tail a JSONL file. ``follow=True`` keeps reading; ``follow=False`` stops at EOF."""

    def __init__(
        self,
        path: Path = DEFAULT_DEBUG_FILE,
        *,
        follow: bool = True,
        from_start: bool = True,
        poll_interval: float = 0.25,
    ) -> None:
        self.path = Path(path)
        self.follow = follow
        self.from_start = from_start
        self.poll_interval = poll_interval

    def iter_events(self) -> Iterator[dict]:
        if not self.path.exists():
            if not self.follow:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        with open(self.path, "r") as fh:
            if not self.from_start and self.follow:
                fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if not line:
                    if not self.follow:
                        return
                    time.sleep(self.poll_interval)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


class BusSource:
    """Spawn ``deer obs bus --json`` and stream events."""

    def __init__(self, channels: Optional[list[str]] = None) -> None:
        self.channels = channels or [
            "autobench.phase.v1",
            "autobench.iteration.v1",
            "autobench.sandbox.v1",
            "autobench.improver.v1",
            "autobench.worker.v1",
            # bead nervous-bus-cewj — cost + budget channels feed CostRatePanel.
            "autobench.worker.v1",
            "autobench.budget.warning.v1",
            "autobench.budget.rate.v1",
            "autobench.improver.divergence.v1",
            "autobench.improver.delta.diff.v1",
        ]
        self._proc: Optional[subprocess.Popen] = None

    @staticmethod
    def available() -> bool:
        return shutil.which("deer") is not None

    def iter_events(self) -> Iterator[dict]:
        if not self.available():
            return
        cmd = ["deer", "obs", "bus", "--json"]
        # --channels filter is best-effort; some deer versions don't expose it
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1,
                text=True,
            )
        except Exception:
            return
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        finally:
            self.close()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
        except Exception:
            pass
        self._proc = None


class ReplaySource:
    """Stream events from a finished session's JSONL at controlled speed.

    nervous-bus-zynw. Sleeps between yields by
    ``(timestamp_n - timestamp_{n-1}) / speed`` seconds — so 1x replays at
    wall-clock speed and 100x compresses an hour-long cycle into ~36s.
    Speed is clamped to ``REPLAY_SPEED_CAP``.

    Parses the CloudEvents ``time`` field (RFC3339) to derive inter-event
    gaps. If the field is missing or unparseable for an event, that
    event's sleep falls back to 0 — the iterator yields it immediately
    rather than blocking forever.

    Optional ``session_id`` filters events to a single autobench session
    so a multi-session debug.jsonl can replay just the cycle of interest.
    """

    def __init__(
        self,
        path: Path,
        *,
        session_id: Optional[str] = None,
        speed: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.speed = max(min(float(speed), REPLAY_SPEED_CAP), 1e-6)

    @staticmethod
    def _parse_time(evt: dict) -> Optional[float]:
        t = evt.get("time")
        if not t:
            return None
        try:
            # RFC3339 — Z suffix indicates UTC; replace for Python <3.11 compat.
            return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

    def iter_events(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path, "r") as fh:
            prev_ts: Optional[float] = None
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Session-id filter (optional). data.session_id is the
                # canonical location for autobench events; some CloudEvents
                # may carry it elsewhere — keep the filter forgiving.
                if self.session_id is not None:
                    sid = (evt.get("data") or {}).get("session_id")
                    if sid != self.session_id:
                        continue
                ts = self._parse_time(evt)
                if prev_ts is not None and ts is not None:
                    gap = max(0.0, ts - prev_ts)
                    sleep_s = gap / self.speed
                    # Clamp sleep so an enormous gap (e.g. >1h between
                    # events) doesn't strand the operator waiting.
                    if sleep_s > 0:
                        time.sleep(min(sleep_s, 60.0))
                if ts is not None:
                    prev_ts = ts
                yield evt


def replay_from_file(
    path: Path,
    *,
    session_id: Optional[str] = None,
    speed: float = 1.0,
) -> Iterator[dict]:
    """Top-level helper — sugar around ``ReplaySource(...).iter_events()``."""
    return ReplaySource(Path(path), session_id=session_id, speed=speed).iter_events()


# nervous-bus-zynw: module-level flag so HeaderStats / widgets can read
# "are we in replay mode and at what speed?" without threading a config
# object through the widget tree. Set by ``app.PulseApp.__init__`` when
# --replay is passed and never mutated thereafter.
REPLAY_STATE: dict = {
    "active": False,
    "speed": 1.0,
}


def set_replay_state(active: bool, speed: float = 1.0) -> None:
    """Set the global replay-mode flag. Called by app.py at startup only."""
    REPLAY_STATE["active"] = bool(active)
    REPLAY_STATE["speed"] = max(min(float(speed), REPLAY_SPEED_CAP), 1e-6)


def auto_source(
    *,
    debug_file: Path = DEFAULT_DEBUG_FILE,
    prefer_bus: bool = True,
    follow: bool = True,
    from_start: bool = False,
) -> object:
    """Pick a source: BusSource if deer is available and ``prefer_bus``, else FileSource."""
    if prefer_bus and BusSource.available():
        return BusSource()
    return FileSource(debug_file, follow=follow, from_start=from_start)
