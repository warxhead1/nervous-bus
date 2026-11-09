#!/usr/bin/env python3
"""Replay recent agent shell calls through argshape in a scratch state dir.

Calls come from adapters/transcripts (every agent it can read, sub-agent
sessions included). For each call in session order: plan() what pre would do
(counted, never run), then feed the real result to on_success/on_failure so
learning happens exactly as it would have live. Active SQL catalog learning is
OFF here: a replay must never touch a database.

  python3 backtest.py --days 14 --state /home/eric/data2/claude-tmp/argshape-bt [--agent claude]
Writes <state>/out.json with every rewrite/advice for review.

Dash-leading values (the `-e` / `--` rule) are scored against what really happened:
  prevented  the original call failed with a dash_value rejection and the rule rewrote it
  false      the rule rewrote a call that had succeeded (a correct command was changed)
  unclear    the rule rewrote a call that failed for another reason, or has no recorded result
`--seed-dash-value grep=ugrep,rg,cat` records a rejection for those binaries up front, so the rule
fires on every matching call: the upper bound on how often it acts, and the number that matters
for false rewrites (a natural replay can prevent at most one fewer than it sees, per binary).
Extra transcript roots: NBUS_TRANSCRIPTS_CLAUDE=<dir> (e.g. the reflex transcript mirror); calls
seen twice (live + mirror) are counted once. `--strict-window` drops calls older than --days even
when their session file was touched since.
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
ap.add_argument("--seed-dash-value", default="", help="comma list of binaries, NAME or NAME=ALIAS, treated as having rejected a dash-leading value")
ap.add_argument("--strict-window", action="store_true", help="skip calls whose own timestamp is older than --days")
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
import errclass  # noqa: E402
import store  # noqa: E402
import transcripts  # noqa: E402

cfg = store.load_config()
db = store.connect()
for spec_ in filter(None, a.seed_dash_value.split(",")):
    name_, _, alias_ = spec_.partition("=")
    if alias_:
        store.meta_set(db, f"alias:{name_}", alias_)
    key_, path_ = core.bin_identity(db, name_, "/")
    core._ensure_spec(db, key_, path_, name_, [], proven=True)
    store.upsert_count(db, "rejects", {"bin_key": key_, "ctx": "", "kind": "dash_value", "token": core.DASH_KEY},
                       {"name": name_})
calls = fails = rewrites = advice = dupes = 0
dash = {"failures": 0, "masked": 0, "rewrites": 0, "prevented": 0, "false": 0, "unclear": 0}
dash_by_bin: dict = {}
sessions = set()
seen = set()
per_agent: dict = {}
t_plan = 0.0
out = []
cutoff = time.time() - a.days * 86400
cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
for tc in transcripts.iter_calls(a.agent, cutoff - (86400 if a.strict_window else 0), shell_only=True):
    key = (tc.agent, tc.session, tc.call_id)
    if key in seen:
        dupes += 1
        continue
    seen.add(key)
    if a.strict_window and tc.ts and tc.ts[:19] < cutoff_iso:
        continue
    cmd, cwd = tc.command, tc.cwd or "/"
    calls += 1
    sessions.add((tc.agent, tc.session))
    per_agent[tc.agent] = per_agent.get(tc.agent, 0) + 1
    t0 = time.perf_counter()
    r = core.plan(db, cfg, cmd, cwd)
    t_plan += time.perf_counter() - t0
    # A rejection counts whether or not the call's exit status carried it: `grep ... | head` ends 0.
    rejected_words = ([x.token for x in errclass.classify(tc.output) if x.kind == "dash_value"]
                      if tc.output and not tc.harness else [])
    if rejected_words:
        dash["failures"] += 1
        dash["masked"] += not tc.is_error
    if r["command"] != cmd:
        rewrites += 1
        entry = {"agent": tc.agent, "before": cmd[:400], "after": r["command"][:400], "notes": r["notes"]}
        if any(x["kind"] == "dash_value" for x in r["applied"]):
            bad = r["applied"][0]["bad"]
            hit = any(w in bad or bad.startswith(w) for w in rejected_words)
            verdict = ("prevented" if hit else "false" if tc.output is not None and not tc.is_error else "unclear")
            dash["rewrites"] += 1
            dash[verdict] += 1
            entry.update(dash_rule=verdict, original_failed=bool(tc.is_error), original_output=(tc.output or "")[:200])
            b = dash_by_bin.setdefault(os.path.basename(r["applied"][0]["label"].split()[0]), {"prevented": 0, "false": 0, "unclear": 0})
            b[verdict] += 1
        out.append(entry)
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
print(f"sessions={len(sessions)} calls={calls} dupes_skipped={dupes} by_agent={per_agent} fails={fails} rewrites={rewrites} "
      f"advice={advice} plan_avg_ms={t_plan / max(calls, 1) * 1000:.2f} exceptions={sum('exception' in o for o in out)}")
print(f"dash_value: failures={dash['failures']} (masked_by_pipe={dash['masked']}) rewrites={dash['rewrites']} prevented={dash['prevented']} "
      f"false={dash['false']} unclear={dash['unclear']} by_bin={dash_by_bin}")
for t in ("specs", "rejects", "fixes", "sigs", "columns", "shapes"):
    print(f"  {t:8} {db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")
