#!/usr/bin/env python3
"""Replay recent Claude Code Bash calls through argshape in a scratch state dir.

For each call in transcript order: plan() what pre would do (counted, never
run), then feed the real result to on_success/on_failure so learning happens
exactly as it would have live. Active SQL catalog learning is OFF here: a
replay must never touch a database.

  python3 backtest.py --days 14 --state /home/eric/data2/claude-tmp/argshape-bt
Writes <state>/out.json with every rewrite/advice for review.
"""
import argparse
import glob
import json
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=float, default=14)
ap.add_argument("--state", required=True)
ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
a = ap.parse_args()
os.makedirs(a.state, exist_ok=True)
for f in glob.glob(os.path.join(a.state, "argshape.db*")):
    os.remove(f)
os.environ.update({"ARGSHAPE_STATE_DIR": a.state, "ARGSHAPE_NO_BUS": "1",
                   "ARGSHAPE_CONFIG": os.path.join(a.state, "config.json")})
with open(os.environ["ARGSHAPE_CONFIG"], "w") as f:
    json.dump({"mode": "correct", "sql_active_learning": False}, f)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import core   # noqa: E402
import store  # noqa: E402

cfg = store.load_config()
db = store.connect()
cut = time.time() - a.days * 86400
files = sorted((f for f in glob.glob(os.path.join(a.projects, "*", "*.jsonl")) if os.path.getmtime(f) >= cut),
               key=os.path.getmtime)
calls = fails = rewrites = advice = 0
t_plan = 0.0
out = []
for path in files:
    pend = {}
    for line in open(path, errors="ignore"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                cmd, cwd = b["input"].get("command", ""), e.get("cwd") or "/"
                calls += 1
                t0 = time.perf_counter()
                r = core.plan(db, cfg, cmd, cwd)
                t_plan += time.perf_counter() - t0
                if r["command"] != cmd:
                    rewrites += 1
                    out.append({"before": cmd[:400], "after": r["command"][:400], "notes": r["notes"]})
                elif r["notes"]:
                    advice += 1
                    out.append({"advice": cmd[:400], "notes": r["notes"]})
                pend[b["id"]] = (cmd, cwd, e.get("sessionId", ""))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend:
                cmd, cwd, sess = pend.pop(b["tool_use_id"])
                txt = b.get("content")
                txt = txt if isinstance(txt, str) else json.dumps(txt)
                ev = {"session_id": sess, "cwd": cwd, "tool_name": "Bash",
                      "tool_use_id": b["tool_use_id"], "tool_input": {"command": cmd}}
                try:
                    if b.get("is_error"):
                        fails += 1
                        core.on_failure(db, cfg, {**ev, "error": txt})
                    else:
                        core.on_success(db, cfg, {**ev, "tool_response": {"stdout": txt[:20000]}})
                except Exception as ex:  # a replay crash is a finding, keep going
                    out.append({"exception": repr(ex), "cmd": cmd[:300]})
with open(os.path.join(a.state, "out.json"), "w") as f:
    json.dump(out, f, indent=1)
print(f"files={len(files)} calls={calls} fails={fails} rewrites={rewrites} advice={advice} "
      f"plan_avg_ms={t_plan / max(calls, 1) * 1000:.2f} exceptions={sum('exception' in o for o in out)}")
for t in ("specs", "rejects", "fixes", "sigs", "columns", "shapes"):
    print(f"  {t:8} {db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")
