"""crucible steward: sync [--once] | status. Exit: 0 done, 5 hearth unreachable or heartbeat stale."""
import argparse
import json
import sys
import time

from crucible_grill import cfg
from crucible_ledger.store import LedgerError, find_root

from . import core


def out(obj, code=0):
    print(json.dumps(obj, sort_keys=True))
    return code


def cmd_sync(a):
    root = find_root(a.root)
    if a.once:
        return out(core.cycle(root))
    poll = cfg.knobs(root)["steward"]["poll_s"]
    while True:
        try:
            print(json.dumps(core.cycle(root), sort_keys=True), flush=True)
        except LedgerError as e:
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)
        time.sleep(poll)


def cmd_status(a):
    res = core.status(find_root(a.root))
    return out(res, 0 if res["ok"] else 5)


def main(argv):
    p = argparse.ArgumentParser(prog="crucible steward", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("sync", help="poll hearth and write ledger records")
    s.add_argument("--once", action="store_true", help="one cycle, then exit (the systemd timer runs this)")
    s.add_argument("--root")
    s = sub.add_parser("status", help="heartbeat age and the last cycle")
    s.add_argument("--root")
    try:
        a = p.parse_args(argv)
        return {"sync": cmd_sync, "status": cmd_status}[a.verb](a)
    except LedgerError as e:
        return out({"ok": False, "error": str(e)}, e.code)
    except SystemExit as e:
        return e.code or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
