"""Route host-load findings onto the nervous bus: transition-only, confirmed, deduped.

Mirrors adapters/system-pressure: `nervous publish` is the only way out (never raw XADD), state lives in one
JSON file keyed by finding identity, and an event goes out only when a level CHANGES (plus a bounded
re-reminder). Two additions that a per-minute sampler needs:
  confirm  a level must hold for CONFIRM consecutive samples before it is published (and before a recovery
           is), so one-sample spikes and flapping findings never reach the bus
  notify   critical orphan/memory findings also fan out a bus.notify.v1 (session + phone), the same path other
           alerts use; hearth turns the phone channel into a notification
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CHANNEL = "bus.host.load.finding.v1"
NERVOUS_BIN = os.environ.get("NERVOUS_BIN") or str(Path(__file__).resolve().parents[2] / "sdk/shell/nervous")
STATE = Path(os.environ.get("NERVOUS_HOST_LOAD_EMIT_STATE",
                            str(Path.home() / ".cache/nervous-bus/host-load/emit-state.json")))
CONFIRM = 2
REMIND_S = 6 * 3600
NOTIFY_KINDS = {"orphan_cpu", "memory_pressure"}
NOTIFY_CHANNELS = ["session", "phone"]
LEVEL = {"crit": "critical", "warn": "warn"}       # info findings stay in history, never on the bus
EVIDENCE_KEEP = ("cmd", "cwd", "spin_loop", "same_target", "tool", "verb", "age_s", "lifetime_cores",
                 "mem_psi_some10", "cpu_some_avg60", "swap_pages_per_s", "kswapd_cores", "causes", "agents")


def finding_key(f):
    ev = f.get("evidence", {})
    kind, project = f["kind"], f["project"]
    if kind == "orphan_cpu":
        sig = str(ev.get("cmd", ""))[:50]
    elif kind == "stale_cwd":
        sig = str(ev.get("cwd", ""))
    elif kind == "cpu_loop":
        sig = str(ev.get("cmd", ""))[:40] + ":" + ",".join(map(str, f.get("pids", [])[:1]))
    elif kind == "duplicate_build":
        sig = f"{ev.get('tool')}:{ev.get('verb')}"
    else:
        sig = ""
    digest = hashlib.sha1(sig.encode()).hexdigest()[:10] if sig else "-"
    return f"{kind}:{project}:{digest}"


def brief_evidence(f):
    ev = {k: v for k, v in f.get("evidence", {}).items() if k in EVIDENCE_KEEP}
    if len(json.dumps(ev, default=str)) > 3000:
        ev = {k: v for k, v in ev.items() if not isinstance(v, (dict, list))}
    return ev


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_publish(channel, payload):
    subprocess.run([NERVOUS_BIN, "publish", channel, json.dumps(payload, default=str)],
                   check=True, capture_output=True, text=True, timeout=10)


class Emitter:
    def __init__(self, state_path=STATE, publish=default_publish, host=None, confirm=CONFIRM, dry_run=False):
        self.path, self.confirm, self.dry_run = Path(state_path), confirm, dry_run
        self.publish_fn = publish
        self.host = host or socket.gethostname()
        try:
            self.items = json.loads(self.path.read_text()).get("items", {})
        except (OSError, ValueError):
            self.items = {}

    def _publish(self, channel, payload):
        if self.dry_run:
            sys.stderr.write(f"[host-load] (dry-run) would publish {channel}: {json.dumps(payload, default=str)[:300]}\n")
            return True
        try:
            self.publish_fn(channel, payload)
            return True
        except Exception as e:
            sys.stderr.write(f"[host-load] publish {channel} failed for {payload.get('key')}: {e}\n")
            return False

    def _event(self, key, f, level, prev, ts):
        agents = f.get("agents") or []
        payload = {"host": self.host, "kind": f["kind"], "key": key, "level": level, "prev_level": prev,
                   "project": f["project"], "summary": f["summary"][:200], "ts": _iso(ts),
                   "cpu_cores": f.get("cpu_cores", 0), "rss_bytes": f.get("rss_bytes", 0),
                   "count": f.get("count", 0), "pids": list(f.get("pids", []))[:8],
                   "agents": agents[:5], "evidence": brief_evidence(f)}
        ok = self._publish(CHANNEL, payload)
        if ok and level == "critical" and f["kind"] in NOTIFY_KINDS:
            who = f" [{agents[0]['agent']}]" if agents else ""
            self._publish("bus.notify.v1", {
                "priority": "critical", "channels": NOTIFY_CHANNELS,
                "summary": f"{f['project']}{who}: {f['summary']}"[:140],
                "source_project": "nervous-bus", "ts": _iso(ts), "source_event_type": CHANNEL,
                "dedup_key": f"host-load:{key}",
                "body": f"{f['summary']}\nhost {self.host}; agents {[a['agent'] for a in agents]}; pids {f.get('pids', [])[:8]}"[:600],
            })
        return ok

    def process(self, findings, ts=None):
        """Apply one sample's findings; returns [(key, prev_level, level)] actually published."""
        ts = ts or time.time()
        current = {}
        for f in findings:
            level = LEVEL.get(f["severity"])
            if level:
                key = finding_key(f)
                if key not in current or level == "critical":
                    current[key] = (level, f)
        emitted = []
        for key, (level, f) in current.items():
            st = self.items.setdefault(key, {"level": "ok", "pending": None, "n": 0, "absent": 0, "last_emit": 0.0})
            st["absent"], st["last"] = 0, {k: f[k] for k in ("kind", "project", "summary", "cpu_cores", "rss_bytes",
                                                                "count", "pids", "agents", "evidence") if k in f}
            if level == st["level"]:
                st["pending"], st["n"] = None, 0
                if now_gap(ts, st["last_emit"]) >= REMIND_S and self._event(key, f, level, level, ts):
                    st["last_emit"] = ts
                    emitted.append((key, level, level))
                continue
            st["n"] = st["n"] + 1 if st["pending"] == level else 1
            st["pending"] = level
            if st["n"] >= self.confirm:
                if self._event(key, f, level, st["level"], ts):
                    emitted.append((key, st["level"], level))
                    st["level"], st["last_emit"] = level, ts
                    st["pending"], st["n"] = None, 0       # on failure keep counting: retried next run
        for key in list(self.items):
            if key in current:
                continue
            st = self.items[key]
            if st["level"] == "ok":
                del self.items[key]
                continue
            st["absent"] += 1
            if st["absent"] >= self.confirm:
                last = dict(st.get("last") or {"kind": key.split(":")[0], "project": key.split(":")[1],
                                               "summary": "condition cleared"})
                last["summary"] = "recovered: " + last["summary"]
                last.setdefault("kind", key.split(":")[0])
                last.setdefault("project", key.split(":")[1])
                if self._event(key, last, "ok", st["level"], ts):
                    emitted.append((key, st["level"], "ok"))
                    del self.items[key]
        self.save()
        return emitted

    def save(self):
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "items": self.items}, default=str))
        tmp.replace(self.path)


def now_gap(ts, last):
    return ts - last if last else float("inf")
