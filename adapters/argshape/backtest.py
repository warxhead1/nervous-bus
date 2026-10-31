#!/usr/bin/env python3
"""Replay recent agent shell calls through argshape in a scratch state dir.

Calls come from adapters/transcripts (every agent it can read, sub-agent
sessions included). For each call in session order: plan() what pre would do
(counted, never run), then feed the real result to on_success/on_failure so
learning happens exactly as it would have live. Active SQL catalog learning is
OFF here: a replay must never touch a database.

  python3 backtest.py --days 14 --state /home/eric/data2/claude-tmp/argshape-bt [--agent claude]
Writes <state>/out.json with every rewrite/advice for review.
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--days", type=float, default=14)
ap.add_argument("--state", required=True)
ap.add_argument("--agent", action="append", help="limit to these transcript sources (default: all)")
a = ap.parse_args()
os.makedirs(a.state, exist_ok=True)
for f in glob.glob(os.path.join(a.state, "argshape.db*")):
    os.remove(f)
os.environ.update({"ARGSHAPE_STATE_DIR": a.state, "ARGSHAPE_NO_BUS": "1",
                   "ARGSHAPE_CONFIG": os.path.join(a.state, "config.json")})
with open(os.environ["ARGSHAPE_CONFIG"], "w") as f:
    json.dump({"mode": "correct", "sql_active_learning": False}, f)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "transcripts"))
import core   # noqa: E402
import store  # noqa: E402
import transcripts  # noqa: E402

cfg = store.load_config()
db = store.connect()
calls = fails = rewrites = advice = 0
sessions = set()
per_agent: dict = {}
t_plan = 0.0
out = []
for tc in transcripts.iter_calls(a.agent, time.time() - a.days * 86400, shell_only=True):
    cmd, cwd = tc.command, tc.cwd or "/"
    calls += 1
    sessions.add((tc.agent, tc.session))
    per_agent[tc.agent] = per_agent.get(tc.agent, 0) + 1
    t0 = time.perf_counter()
    r = core.plan(db, cfg, cmd, cwd)
    t_plan += time.perf_counter() - t0
    if r["command"] != cmd:
        rewrites += 1
        out.append({"agent": tc.agent, "before": cmd[:400], "after": r["command"][:400], "notes": r["notes"]})
    elif r["notes"]:
        advice += 1
        out.append({"agent": tc.agent, "advice": cmd[:400], "notes": r["notes"]})
    if tc.output is None:
        continue
    ev = {"session_id": tc.session, "cwd": cwd, "tool_name": "Bash",
          "tool_use_id": f"{tc.agent}:{tc.call_id}", "tool_input": {"command": cmd}}
    try:
        if tc.is_error:
            fails += 1
            # The live hook sees "Exit code N\n<output>"; agents that store the code apart get it back.
            txt = tc.output
            if tc.exit_code is not None and not tc.harness and not txt.startswith("Exit code"):
                txt = f"Exit code {tc.exit_code}\n{txt}"
            core.on_failure(db, cfg, {**ev, "error": txt})
        else:
            core.on_success(db, cfg, {**ev, "tool_response": {"stdout": tc.output[:20000]}})
    except Exception as ex:  # a replay crash is a finding, keep going
        out.append({"exception": repr(ex), "cmd": cmd[:300]})
with open(os.path.join(a.state, "out.json"), "w") as f:
    json.dump(out, f, indent=1)
print(f"sessions={len(sessions)} calls={calls} by_agent={per_agent} fails={fails} rewrites={rewrites} advice={advice} "
      f"plan_avg_ms={t_plan / max(calls, 1) * 1000:.2f} exceptions={sum('exception' in o for o in out)}")
for t in ("specs", "rejects", "fixes", "sigs", "columns", "shapes"):
    print(f"  {t:8} {db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")
