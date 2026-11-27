"""Append-only JSONL ledger: one writer lock, validation on write, tolerant read."""
import datetime
import fcntl
import glob
import json
import os

from . import schema
from .ulid import ulid

LEDGER_REL = os.path.join("design", "ledger")


class LedgerError(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def now_ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(explicit=None):
    """The design worktree root: --root, $CRUCIBLE_DESIGN_ROOT, or the nearest
    ancestor of cwd holding design/ledger."""
    for cand in (explicit, os.environ.get("CRUCIBLE_DESIGN_ROOT")):
        if cand:
            return os.path.abspath(cand)
    d = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(d, LEDGER_REL)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise LedgerError("no design/ledger found; pass --root or set CRUCIBLE_DESIGN_ROOT", 2)
        d = parent


def bulk_dir(game):
    base = os.environ.get("CRUCIBLE_BULK") or os.path.join(os.path.expanduser("~"), ".cache", "crucible")
    return os.path.join(base, game)


class Ledger:
    def __init__(self, root, writer="crucible"):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, LEDGER_REL)
        self.writer = writer

    def exists(self):
        return os.path.isdir(self.dir)

    def files(self):
        return sorted(glob.glob(os.path.join(self.dir, "*.jsonl")))

    def _path(self):
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self.dir, f"{day}.{self.writer}.jsonl")

    def append(self, rec):
        """Fill id/v/ts, validate, append one line under the file lock."""
        rec = dict(rec)
        rec.setdefault("id", ulid())
        rec.setdefault("v", 1)
        rec.setdefault("ts", now_ts())
        errs = schema.validate_record(rec)
        if errs:
            raise LedgerError("invalid record: " + "; ".join(errs), 1)
        line = (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        os.makedirs(self.dir, exist_ok=True)
        lock_fd = os.open(os.path.join(self.dir, ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(self._path(), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
            try:
                view = memoryview(line)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        return rec

    def read(self):
        """Return (records sorted by id, problems). Problems are unparseable
        lines or non-object lines; schema checks live in doctor."""
        recs, problems = [], []
        for path in self.files():
            with open(path, "rb") as f:
                data = f.read()
            if data and not data.endswith(b"\n"):
                problems.append({"file": os.path.basename(path), "line": data.count(b"\n") + 1,
                                 "problem": "unterminated last line"})
            for n, raw in enumerate(data.split(b"\n"), 1):
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8"))
                    if not isinstance(r, dict):
                        raise ValueError("not an object")
                except ValueError as e:
                    problems.append({"file": os.path.basename(path), "line": n, "problem": str(e)})
                    continue
                recs.append(r)
        recs.sort(key=lambda r: (str(r.get("id", "")), str(r.get("ts", ""))))
        return recs, problems
