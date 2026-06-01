from __future__ import annotations
import threading
from pathlib import Path
from queue import Queue
from threading import Event
import re
from .helpers import infer_level

def app_source(q: Queue, stop: Event, paths: list[str], filters: list[re.Pattern]) -> None:
    for p in paths:
        path = Path(p).expanduser()
        t = threading.Thread(target=_tail_file, args=(path, q, stop, filters), daemon=True)
        t.start()

def _tail_file(path: Path, q: Queue, stop: Event, filters: list[re.Pattern]) -> None:
    fh = None
    inode = None
    service = path.stem
    while not stop.is_set():
        if not path.exists():
            if fh:
                fh.close(); fh = None
            stop.wait(5); continue
        st = path.stat()
        if fh is None or st.st_ino != inode:
            if fh: fh.close()
            fh = path.open("r", encoding="utf-8", errors="replace")
            fh.seek(0, 2)
            inode = st.st_ino
        line = fh.readline()
        if not line:
            stop.wait(0.5); continue
        line = line.rstrip("\n")
        if any(f.search(line) for f in filters):
            continue
        try:
            q.put_nowait({
                "log_source": "app",
                "service": service,
                "level": infer_level(line),
                "message": line[:500],
                "raw": line[:1000],
                "parsed_fields": {"path": str(path)},
            })
        except Exception:
            pass
    if fh: fh.close()
