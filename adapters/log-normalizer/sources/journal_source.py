from __future__ import annotations
import json, subprocess
from queue import Queue
from threading import Event
import re as _re
import re

_ANSI_RE = _re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJnsu]')
_DECIMAL_BYTES_RE = _re.compile(r'^(?:\d{1,3}\s+){10,}\d{1,3}$')

def _clean_message(msg: str) -> str:
    """Strip ANSI codes and replace decimal-encoded binary blobs."""
    stripped = msg.strip()
    if _DECIMAL_BYTES_RE.match(stripped):
        return "[binary data]"
    return _ANSI_RE.sub("", stripped).strip()

_PRIO_MAP = {0:"critical",1:"critical",2:"critical",3:"error",4:"warn",5:"info",6:"info",7:"debug"}

def parse_journal_entry(j: dict) -> dict | None:
    message = j.get("MESSAGE", "")
    if isinstance(message, list):
        message = " ".join(str(x) for x in message)
    message = _clean_message(str(message))
    if not message:
        return None
    try:
        prio = int(j.get("PRIORITY", 6))
    except (ValueError, TypeError):
        prio = 6
    level = _PRIO_MAP.get(prio, "info")
    unit = (j.get("_SYSTEMD_USER_UNIT") or j.get("_SYSTEMD_UNIT") or
            j.get("SYSLOG_IDENTIFIER") or "unknown").removesuffix(".service")
    return {
        "log_source": "journal",
        "service": unit,
        "level": level,
        "message": message[:500],
        "raw": json.dumps(j)[:1000],
        "parsed_fields": {
            "_SYSTEMD_UNIT": j.get("_SYSTEMD_UNIT", ""),
            "_PID": j.get("_PID", ""),
            "SYSLOG_IDENTIFIER": j.get("SYSLOG_IDENTIFIER", ""),
        },
    }

def journal_source(q: Queue, stop: Event, filters: list[re.Pattern]) -> None:
    while not stop.is_set():
        try:
            proc = subprocess.Popen(
                ["journalctl", "--follow", "--output=json", "--user", "-q"],
                stdout=subprocess.PIPE, text=True, errors="replace",
            )
        except FileNotFoundError:
            return
        for line in proc.stdout:
            if stop.is_set():
                break
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = str(j.get("MESSAGE", ""))
            if any(f.search(message) for f in filters):
                continue
            entry = parse_journal_entry(j)
            if entry:
                try:
                    q.put_nowait(entry)
                except Exception:
                    pass
        proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if not stop.is_set():
            stop.wait(2)  # brief delay before restart
