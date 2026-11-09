"""guard_denial: known answers on real (clipped, path-redacted) workflow-agent lanes.

fix_argshape / repair_argshape / review_argshape are isolated fix-wave lanes that
the worktree guard, the sleep block, a hook and a missing tool all refused.
oss_analysis is a non-isolated lane that asked for a report file once.
mine_precision is a non-isolated lane with no denials: the negative control.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from detectors.base import ensure_detector_schema  # noqa: E402
from detectors.failure_taxonomy import CONSTRAINT_FAILURE, classify_run  # noqa: E402
from detectors.guard_denial import (  # noqa: E402
    MIN_DENIALS, MIN_RATE, GuardDenialDetector, RunDenials, classify, fires,
    hook_basename, remediation, summarize, worktree_sub_reason,
)
from transcript_steps import Step, parse_transcript  # noqa: E402

FIX = _ENGINE / "tests" / "fixtures" / "transcripts"


def _run(name):
    return summarize(parse_transcript(FIX / f"guard_denial_{name}.jsonl"))


def _err(text, tool="Bash", inp=None, line=1):
    return Step(id=str(line), tool=tool, input=inp or {"command": "x"}, t0=float(line),
                t1=float(line) + 1, output=text, is_error=True, line=line)


def _wg(clause):
    return ("This agent is isolated in the worktree <R>, but this command " + clause
            + ". Refusing to run it — a worktree-isolated agent's git operations must "
              "target its own worktree. Split it into plain, separate commands.")


# Clause texts are the harness's own wording, one per distinct shape seen in the fix wave.
CLAUSES = [
    ("is too complex to verify that it stays inside the worktree", "too_complex"),
    ("names git in a form too complex to verify that it stays inside the worktree", "names_git"),
    ("runs setsid with the text python3 in a plain command, so what it runs cannot be shown "
     "not to be git", "wrapper"),
    ("hands nice the text file:<R> which runs a command whose name is computed at runtime in a "
     "plain command, so it cannot be shown not to be git", "wrapper"),
    ("runs nice with the text -n inside a construct too complex to verify, so what it runs "
     "cannot be shown not to be git", "wrapper"),
    ("runs setsid with a value computed at runtime (the variable S) inside a construct too "
     "complex to verify, so what it runs cannot be shown not to be git", "wrapper"),
    ("feeds python text naming git in a plain command, which cannot be shown to stay inside "
     "the worktree", "heredoc_git"),
    ("feeds python a descriptor it names inside a construct too complex to verify, whose "
     "content is assembled at runtime and may be read as a program", "heredoc_git"),
    ("runs python with a value computed at runtime (the variable S) (a computed argument goes "
     "after the script or -c)", "runtime_value"),
    ("runs rg with a value computed at runtime (the variable a) where an option may stand",
     "runtime_value"),
    ("feeds ugrep input assembled by the command inside a construct too complex to verify; a "
     "program this guard does not know may run that input", "runtime_value"),
    ("changes directory to a location computed at runtime before running git", "cd_runtime"),
    ("points git at a directory computed at runtime (-C <R> which can't be verified before "
     "it runs", "git_dir_runtime"),
    ("runs awk with --help inside a construct too complex to verify, so what it runs cannot "
     "be shown not to be git", "too_complex"),
    ("runs sqlite3 with the text file:<R> inside a construct too complex to verify, so what it "
     "runs cannot be shown not to be git", "too_complex"),
    ("runs sqlite3 with the text select count(*) in a plain command, so what it runs cannot be "
     "shown not to be git", "other"),
    ("runs a string through alias, which can't be verified to stay inside the worktree", "other"),
]


def test_worktree_sub_reasons_on_real_clauses():
    for clause, want in CLAUSES:
        assert worktree_sub_reason(_wg(clause)) == want, clause
        d = classify(_err(_wg(clause)))
        assert (d.family, d.sub_reason) == ("worktree_guard", want), clause
    assert worktree_sub_reason("This agent is isolated in the worktree <R>.") == "other"


def test_other_families_on_real_text():
    cases = [
        ("<tool_use_error>Subagents should return findings as text, not write report files. "
         "Include this content in your final response instead.</tool_use_error>",
         "report_write_block", "report_write"),
        ("<tool_use_error>Blocked: sleep 60 followed by: ls -la <R>. To wait for a condition, use "
         "Monitor with an until-loop. To wait for a command you started, use run_in_background: "
         "true.</tool_use_error>", "sleep_block", "sleep"),
        ("PreToolUse:Bash hook error: [<R>/recursive-delete-guard.py]: BLOCKED: recursive delete "
         "(rm -r) on r2-main.", "hook_block", "recursive-delete-guard.py"),
        ("<tool_use_error>Error: No such tool available: Grep. Grep is not available in this "
         "session.</tool_use_error>", "tool_unavailable", "Grep"),
        ("The user doesn't want to proceed with this tool use. The tool use was rejected.",
         "permission_denied", "user_declined"),
        ("Permission for this action was denied by the Claude Code auto mode classifier. "
         "Reason: [Unreviewed]", "permission_denied", "auto_classifier"),
        ("Permission for this command was denied by a built-in Claude Code safety check, not by "
         "the rules.", "permission_denied", "safety_check"),
        ("Permission to use Bash with command rm -rf <R> has been denied.",
         "permission_denied", "deny_rule"),
        ("Claude requested permissions to use Write, but you haven't granted it yet.",
         "permission_denied", "not_granted"),
    ]
    for text, fam, sub in cases:
        d = classify(_err(text))
        assert d is not None and (d.family, d.sub_reason) == (fam, sub), text


def test_ordinary_failures_are_not_denials():
    quoted = _wg("is too complex to verify that it stays inside the worktree")
    assert classify(_err("Exit code 1\n" + quoted)) is None            # a command that printed it
    assert classify(_err("Exit code 2\nBlocked: sleep 5 followed by x")) is None
    assert classify(_err("Traceback (most recent call last):\n  File \"x.py\"\nValueError")) is None
    assert classify(_err("<tool_use_error>File has not been read yet.</tool_use_error>")) is None
    assert classify(_err("")) is None
    ok = _err(quoted)
    ok.is_error = False                                                  # only is_error results count
    assert classify(ok) is None


def test_hook_basename_shapes():
    assert hook_basename("PreToolUse:Bash hook error: [/a/b/c.py]: BLOCKED") == "c.py"
    assert hook_basename("PreToolUse:Bash hook error: [python3 /a/b/guard.py --strict]: BLOCKED") == "guard.py"
    assert hook_basename("PreToolUse:Bash hook error: [guard]: BLOCKED") == "guard"
    assert hook_basename("PreToolUse:Bash hook error: BLOCKED") == "unknown_hook"


def test_fix_argshape_lane_loses_a_twentieth_of_its_calls():
    r = _run("fix_argshape")
    assert r.total_calls == 276 and r.count == 14
    assert r.by_family == {"worktree_guard": 13, "sleep_block": 1}
    assert r.by_sub_reason == {
        "worktree_guard/wrapper": 4, "worktree_guard/too_complex": 3, "worktree_guard/names_git": 2,
        "worktree_guard/heredoc_git": 1, "worktree_guard/runtime_value": 1,
        "worktree_guard/git_dir_runtime": 1, "worktree_guard/other": 1, "sleep_block/sleep": 1,
    }
    assert r.repeats == 6 and r.verbatim_repeats == 0
    assert round(r.rate, 4) == 0.0507
    assert fires(r)
    # 20 error results: 14 denials plus 6 ordinary failures that must not be counted.
    steps = parse_transcript(FIX / "guard_denial_fix_argshape.jsonl")
    assert sum(1 for s in steps if s.is_error) == 20


def test_repair_lane_names_the_hook_script():
    r = _run("repair_argshape")
    assert r.by_family == {"worktree_guard": 6, "hook_block": 1}
    assert r.by_sub_reason["hook_block/recursive-delete-guard.py"] == 1
    assert fires(r)


def test_review_lane_names_the_missing_tool():
    r = _run("review_argshape")
    assert r.by_family == {"worktree_guard": 3, "sleep_block": 1, "tool_unavailable": 2}
    assert r.by_sub_reason["tool_unavailable/Grep"] == 2
    assert r.repeats == 2 and fires(r)


def test_one_report_write_is_below_the_floor():
    r = _run("oss_analysis")
    assert r.by_family == {"report_write_block": 1} and r.count < MIN_DENIALS
    assert not fires(r)


def test_negative_control_has_no_denials():
    r = _run("mine_precision")
    assert r.total_calls == 79 and r.count == 0 and r.repeats == 0
    assert not fires(r)


def test_repeats_count_same_sub_reason_and_verbatim_calls():
    wrap = _wg("runs nice with the text -n in a plain command, so what it runs cannot be shown "
               "not to be git")
    cplx = _wg("is too complex to verify that it stays inside the worktree")
    steps = [
        _err(wrap, inp={"command": "nice -n 10 scan"}, line=1),
        _err(wrap, inp={"command": "nice -n 10 scan"}, line=2),          # verbatim retry
        _err(wrap, inp={"command": "nice -n 19 scan2"}, line=3),         # same reason, new text
        _err(cplx, inp={"command": "a | b"}, line=4),
        Step(id="5", tool="Bash", input={"command": "ls"}, t0=5.0, t1=6.0, output="ok"),
    ]
    r = summarize(steps)
    assert r.total_calls == 5 and r.count == 4
    assert r.by_sub_reason == {"worktree_guard/wrapper": 3, "worktree_guard/too_complex": 1}
    assert r.repeats == 2 and r.verbatim_repeats == 1


def test_thresholds_need_both_count_and_share():
    def run(den, calls):
        return RunDenials(total_calls=calls, denials=[classify(_err(
            "<tool_use_error>Blocked: sleep 9 followed by x</tool_use_error>", line=i + 1))
            for i in range(den)])
    assert not fires(run(MIN_DENIALS - 1, 10))                 # share is high, count is low
    assert fires(run(MIN_DENIALS, 100))
    assert not fires(run(MIN_DENIALS, int(MIN_DENIALS / MIN_RATE) + 50))   # count met, share too low
    assert not fires(run(0, 0))


def test_remediation_names_the_brief_fix_per_family():
    r = _run("fix_argshape")
    txt = remediation(r)
    assert "nice" in txt and "setsid" in txt and "host governor" in txt
    assert "python3 -I /abs/script.py" in txt
    assert "run_in_background" in txt
    t2 = remediation(_run("review_argshape"))
    assert "Grep" in t2 and "Bash equivalent" in t2
    assert "findings as text" in remediation(_run("oss_analysis"))
    assert "recursive-delete-guard.py" in remediation(_run("repair_argshape"))
    assert "brief-lint" in txt


def _store(pairs):
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, started TEXT, ended TEXT,
                           event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    for rid, path in pairs:
        st = parse_transcript(path)
        lo, hi = min(s.t0 for s in st), max(s.t0 for s in st)
        conn.execute("INSERT INTO runs VALUES (?, 'demo', ?, ?, ?)", (rid, iso(lo), iso(hi), len(st)))
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES (?, 1, ?, ?)",
                     (rid, iso(lo), json.dumps({"data": {"transcript_path": str(path)}})))
    return conn


def test_detector_through_the_store():
    conn = _store([("RW", FIX / "guard_denial_fix_argshape.jsonl"),
                   ("RH", FIX / "guard_denial_repair_argshape.jsonl"),
                   ("RR", FIX / "guard_denial_oss_analysis.jsonl"),
                   ("RN", FIX / "guard_denial_mine_precision.jsonl")])
    det = GuardDenialDetector(conn)
    det.transcript_roots = []
    cands = det.run(conn)
    assert sorted(c.run_ids[0] for c in cands) == ["RH", "RW"]
    c = next(c for c in cands if c.run_ids == ["RW"])
    assert c.signature == "demo:guard_denial" and c.detector == "guard_denial"
    assert c.occurrences == 14
    assert c.extra["remediation_rung"] == "automate"
    assert c.extra["total_calls"] == 276 and c.extra["denials"] == 14 and c.extra["repeats"] == 6
    assert c.extra["by_family"] == {"worktree_guard": 13, "sleep_block": 1}
    assert c.extra["thresholds"] == {"min_denials": MIN_DENIALS, "min_rate": MIN_RATE}
    json.dumps(c.extra)                                         # extra must be JSON-safe
    assert any("14 of 276 tool calls denied" in e for e in c.evidence)
    assert any("worktree_guard/wrapper x4" in e for e in c.evidence)
    assert "run_in_background" in c.proposed_remediation


def test_registered_before_the_taxonomy_and_mapped_to_constraint_failure():
    import synthesis
    classes = synthesis._BUILTIN_DETECTOR_CLASSES
    names = [c.__name__ for c in classes]
    assert names.index("GuardDenialDetector") < names.index("FailureTaxonomyDetector")
    buckets = classify_run({"guard_denial"}, outcome=None, labeled_at=None,
                           permission_count=0, event_count=10)
    assert list(buckets) == [CONSTRAINT_FAILURE]
    assert classify_run(set(), None, None, 0, 10) == {}
