from __future__ import annotations
from queue import Queue
from threading import Event
import re

_PRIO_MAP = {0:"critical",1:"critical",2:"critical",3:"error",4:"warn",5:"info",6:"info",7:"debug"}

def parse_kmsg_line(line: str) -> dict | None:
    line = line.rstrip("\n")
    parts = line.split(";", 1)
    if len(parts) == 2:
        header, message = parts
        hparts = header.split(",")
        try:
            prio_val = int(hparts[0]) & 0x7
            seqnum = hparts[1] if len(hparts) > 1 else ""
        except (ValueError, IndexError):
            prio_val, seqnum = 6, ""
    else:
        message, prio_val, seqnum = line, 6, ""
    message = message.strip()
    if not message:
        return None
    return {
        "log_source": "kernel",
        "service": "kernel",
        "level": _PRIO_MAP.get(prio_val, "info"),
        "message": message[:500],
        "raw": line[:1000],
        "parsed_fields": {"priority": prio_val, "seqnum": seqnum},
    }

def kernel_source(q: Queue, stop: Event, filters: list[re.Pattern]) -> None:
    try:
        fh = open("/dev/kmsg", "r", errors="replace")
    except (PermissionError, FileNotFoundError):
        return
    while not stop.is_set():
        try:
            line = fh.readline()
        except Exception:
            break
        if not line:
            stop.wait(0.5); continue
        message = line.split(";", 1)[-1].strip()
        if any(f.search(message) for f in filters):
            continue
        entry = parse_kmsg_line(line)
        if entry:
            try:
                q.put_nowait(entry)
            except Exception:
                stop.wait(0.005)  # 5ms back-off prevents busy-loop when queue full
    fh.close()
