"""crucible stats [baseline] verbs. Stdout is exactly one JSON document.
Exit: 0 done, 2 usage, 5 environment, 124 timeout, 127 missing tool."""
import argparse
import json
import os
import sys

from crucible_ledger.store import Ledger, LedgerError, find_root

from . import baseline as bl
from . import measures

REPORT_DIR = ("~", ".cache", "nervous-bus", "crucible")


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LedgerError(message, 2)


def _parser():
    p = _Parser(prog="crucible stats", description="Both sides of the ledger: work saved and what wrong takes cost.")
    p.add_argument("--root", help="design worktree root (default: $CRUCIBLE_DESIGN_ROOT or nearest design/ledger)")
    p.add_argument("--since", help="count records from this date (YYYY-MM-DD)")
    p.add_argument("--requests", action="store_true", help="add plan requests per card")
    p.add_argument("--rubber-rate", type=float, default=measures.RUBBER_RATE, help="acceptance that flags a rubber stamp")
    p.add_argument("--rubber-seconds", type=float, default=measures.RUBBER_SECONDS, help="median seconds under which it flags")
    p.add_argument("--rubber-min-n", type=int, default=measures.RUBBER_MIN_N, help="answers a class needs before it can flag")
    return p


def _bparser():
    p = _Parser(prog="crucible stats baseline", description="Hearth rows and asks per landed run, before the grill ships.")
    p.add_argument("--project", help="reflex project (and hearth project unless --hearth-project)")
    p.add_argument("--hearth-project")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--events", action="append", help="bus journal file (repeatable; default the debug journal and its rotations)")
    return p


def _write(name, text):
    d = os.path.join(*[os.path.expanduser(x) for x in REPORT_DIR])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def cmd_baseline(argv):
    a = _bparser().parse_args(argv)
    res = bl.baseline(a.project, a.hearth_project, a.since, a.events)
    res["report"] = _write("baseline.json", json.dumps(res, indent=1, sort_keys=True) + "\n")
    return out(res)


def cmd_stats(argv):
    a = _parser().parse_args(argv)
    if a.since and not bl.DATE.match(a.since):
        raise LedgerError("--since must be YYYY-MM-DD", 2)
    led = Ledger(find_root(a.root))
    records, problems = led.read()
    if problems:
        raise LedgerError("unparseable ledger lines", 5)
    res = measures.compute(records, a.since, (a.rubber_rate, a.rubber_seconds, a.rubber_min_n), a.requests)
    res["since"] = a.since
    res["report"] = _write("report.md", measures.report_md(res, a.since))
    return out(res)


def main(argv):
    try:
        if argv and argv[0] == "baseline":
            return cmd_baseline(argv[1:])
        return cmd_stats(argv)
    except LedgerError as e:
        return out({"error": str(e)}, e.code)
    except SystemExit as e:  # --help
        return e.code or 0
