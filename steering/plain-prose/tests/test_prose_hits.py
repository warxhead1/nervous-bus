import json
import os
import subprocess
import sys
import time

from test_prose_guard import AFTER, BEFORE, BODY, HEAVY, HOOK, bash, heredoc, run, write

REPLY = HOOK.with_name("reply-score")
FIELDS = {"v", "ts", "plugin", "check", "surface", "session_id", "cwd", "path", "action", "findings", "rules"}
OPTIONAL = {"words", "avg_sentence", "max_sentence", "lost"}


def records(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def valid(rec):
    assert FIELDS <= set(rec) <= FIELDS | OPTIONAL
    assert rec["v"] == 1 and rec["plugin"] == "plain-prose"
    assert rec["ts"].endswith("Z") and time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ")
    assert rec["check"] in {"prose-guard", "prose-compare", "reply-score"}
    assert rec["surface"] in {"doc", "commit", "pr", "issue", "reply"}
    assert rec["action"] in {"report", "block", "none"}
    assert isinstance(rec["findings"], int) and all(isinstance(v, int) for v in rec["rules"].values())


# --- prose-guard hit records ---

def test_a_document_report_is_logged_with_counts_only(tmp_path, hit_log):
    path = write(tmp_path, "probe.md", BEFORE)
    assert run(tmp_path, path, cwd=str(tmp_path))[0] == 2
    (rec,) = records(hit_log)
    valid(rec)
    assert (rec["check"], rec["surface"], rec["action"]) == ("prose-guard", "doc", "report")
    assert rec["path"] in ("probe.md", str(path))
    assert rec["findings"] >= 1 and rec["rules"] and rec["words"] > 0 and rec["max_sentence"] > 0
    raw = hit_log.read_text()
    for needle in ("Kubernetes", "kubelet", "first line of defense", "initialDelaySeconds"):
        assert needle not in raw                       # no prose in a record


def test_a_quiet_document_logs_nothing(tmp_path, hit_log):
    run(tmp_path, write(tmp_path, "probe.md", AFTER))
    assert records(hit_log) == []


def test_a_blocked_commit_and_pr_body_are_logged_by_surface(tmp_path, hit_log):
    assert bash(tmp_path, heredoc("git commit -F -", BODY))[0] == 2
    assert bash(tmp_path, heredoc("gh pr create --title x --body-file -", BODY), session="m2")[0] == 2
    assert bash(tmp_path, heredoc("gh issue comment 3 --body-file -", BODY), session="m3")[0] == 2
    recs = records(hit_log)
    assert [r["surface"] for r in recs] == ["commit", "pr", "issue"]
    assert all(r["action"] == "block" and r["check"] == "prose-guard" for r in recs)
    for r in recs:
        valid(r)
    assert "breath" not in hit_log.read_text()


def test_logging_off_writes_nothing_and_changes_nothing(tmp_path, hit_log):
    path = write(tmp_path, "probe.md", BEFORE)
    on = run(tmp_path, path, session="a")
    off = run(tmp_path, path, session="b", AGENT_STEERING_LOG="0")
    assert on == off and len(records(hit_log)) == 1


def test_default_path_follows_xdg_state_home(tmp_path):
    env = {"AGENT_STEERING_HITS": "", "XDG_STATE_HOME": str(tmp_path / "xdg")}
    assert run(tmp_path, write(tmp_path, "probe.md", BEFORE), **env)[0] == 2
    assert len(records(tmp_path / "xdg" / "agent-steering" / "hits.jsonl")) == 1


def test_a_log_that_cannot_be_written_changes_nothing(tmp_path, hit_log):
    path = write(tmp_path, "probe.md", BEFORE)
    good = run(tmp_path, path, session="a")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    for bad in (str(blocker / "hits.jsonl"), str(tmp_path)):      # parent is a file; target is a directory
        assert run(tmp_path, path, session=f"b{bad}", AGENT_STEERING_HITS=bad) == good


def test_the_log_rotates_past_20_mb(tmp_path, hit_log):
    hit_log.parent.mkdir(parents=True)
    with open(hit_log, "wb") as fh:
        fh.truncate(20 * 1024 * 1024 + 1)
    older = hit_log.with_name("hits.jsonl.1")
    older.write_text("old generation\n")
    run(tmp_path, write(tmp_path, "probe.md", BEFORE))
    assert older.stat().st_size == 20 * 1024 * 1024 + 1      # the full file replaced the older .1
    assert len(records(hit_log)) == 1


# --- prose-compare ---

OLD = ("The retry limit is 5 and the timeout is 30 seconds. See [the guide](https://example.com/guide) for `--verbose` and "
       "`parse_args` details. The owner said \"never retry a payment\". The cache might expire early under load.")
NEW = "Retries stop after a while. A longer timeout helps. The guide explains the rest. The cache is fine."


def edit(tmp, old, new, tool="Edit", file="doc.md", **kw):
    path = write(tmp, file, new)
    ti = {"old_string": old, "new_string": new} if tool == "Edit" else {"edits": [{"old_string": old, "new_string": new}]}
    return run(tmp, path, tool, ti, **kw)


def compares(path):
    return [r for r in records(path) if r["check"] == "prose-compare"]


def test_a_rewrite_that_drops_facts_reports_each_kind(tmp_path, hit_log):
    rc, err = edit(tmp_path, OLD, NEW, cwd=str(tmp_path))
    assert rc == 2 and "this rewrite lost" in err
    (rec,) = compares(hit_log)
    valid(rec)
    assert rec["action"] == "report" and rec["surface"] == "doc" and rec["path"] == "doc.md"
    assert rec["lost"] == {"numbers": 2, "urls": 1, "code": 1, "flags": 1, "quotes": 1, "hedges": 1}
    assert rec["findings"] == 7 and "words" in rec
    raw = hit_log.read_text()
    assert "example.com" not in raw and "never retry" not in raw and "parse_args" not in raw


def test_multiedit_is_compared_too(tmp_path, hit_log):
    rc, err = edit(tmp_path, OLD, NEW, tool="MultiEdit")
    assert rc == 2 and "number" in err


def test_a_clean_rewrite_logs_a_clean_pass_and_prints_nothing(tmp_path, hit_log):
    old = "The queue drops a job after 3 tries, and a slow disk needs 4 tries before it passes."
    new = "A job is dropped after 3 tries. A slow disk needs 4 tries to pass."
    assert edit(tmp_path, old, new) == (0, "")
    (rec,) = compares(hit_log)
    assert rec["action"] == "none" and rec["findings"] == 0 and set(rec["lost"].values()) == {0}


def test_additions_short_edits_deletions_code_and_write_are_quiet(tmp_path, hit_log):
    old = "The retry limit is 5 and the timeout is 30 seconds for every job."
    assert edit(tmp_path, old, old + " A new sentence follows it.", file="a.md") == (0, "")
    assert edit(tmp_path, "limit is 3", "limit is 5", file="b.md") == (0, "")
    assert edit(tmp_path, old, "", file="c.md")[0] == 0
    assert edit(tmp_path, OLD, NEW, file="main.py")[0] == 0
    path = write(tmp_path, "d.md", NEW)
    assert run(tmp_path, path, "Write")[0] == 0
    assert compares(hit_log) == []


def test_a_compare_report_survives_a_dead_log(tmp_path):
    good = edit(tmp_path, OLD, NEW, session="a")
    assert good[0] == 2
    assert edit(tmp_path, OLD, NEW, session="b", AGENT_STEERING_HITS=str(tmp_path)) == good


# --- reply-score ---

def reply(tmp, text=None, transcript=None, **env):
    ev = {"session_id": "r1", "hook_event_name": "Stop", "cwd": str(tmp)}
    if text is not None:
        ev["last_assistant_message"] = text
    if transcript is not None:
        ev["transcript_path"] = str(transcript)
    p = subprocess.run([sys.executable, str(REPLY)], input=json.dumps(ev), capture_output=True, encoding="utf-8",
                       env={**os.environ, **env})
    return p.returncode, p.stdout, p.stderr


def test_reply_score_logs_counts_and_prints_nothing(tmp_path, hit_log):
    assert reply(tmp_path, BEFORE) == (0, "", "")
    (rec,) = records(hit_log)
    valid(rec)
    assert (rec["check"], rec["surface"], rec["action"]) == ("reply-score", "reply", "none")
    assert rec["findings"] > 0 and rec["rules"]["not-x-but-y"] >= 1
    assert rec["words"] > 50 and rec["max_sentence"] > rec["avg_sentence"] > 0
    assert "kubelet" not in hit_log.read_text()


def test_a_clean_reply_is_logged_with_zero_findings(tmp_path, hit_log):
    reply(tmp_path, AFTER)
    (rec,) = records(hit_log)
    assert rec["findings"] == 0 and rec["rules"] == {} and rec["words"] > 0


def test_code_blocks_are_dropped_and_stretch_scores_the_long_form(tmp_path, hit_log):
    fence = "```\n" + "x = 1; y = 2; " * 30 + "\n```\n"
    long_form = "The build passed. " * 10
    reply(tmp_path, "{{Done.|" + long_form + "}}\n\n" + fence + "\nUnclosed fence next.\n```\n" + "z = 3; " * 40)
    (rec,) = records(hit_log)
    assert rec["words"] == 30 + 3 and "semicolon" not in rec["rules"]


def test_the_transcript_is_the_fallback(tmp_path, hit_log):
    t = tmp_path / "t.jsonl"
    rows = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Earlier reply with some words."}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"content": "next"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "The final reply has six words."}]}}]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert reply(tmp_path, transcript=t) == (0, "", "")
    assert records(hit_log)[0]["words"] == 6


def test_reply_score_is_quiet_when_it_cannot_score_or_log(tmp_path, hit_log):
    assert reply(tmp_path, "") == (0, "", "") and reply(tmp_path, transcript=tmp_path / "missing") == (0, "", "")
    assert reply(tmp_path, BEFORE, AGENT_STEERING_LOG="0") == (0, "", "")
    assert reply(tmp_path, BEFORE, AGENT_STEERING_HITS=str(tmp_path)) == (0, "", "")
    assert reply(tmp_path, BEFORE, PLAIN_PROSE_DISABLE="1") == (0, "", "")
    assert records(hit_log) == []
    bad = subprocess.run([sys.executable, str(REPLY)], input="nope", capture_output=True, text=True)
    assert (bad.returncode, bad.stdout, bad.stderr) == (0, "", "")


def test_a_2000_word_reply_scores_in_under_300_ms(tmp_path, hit_log):
    text = "\n\n".join([(BEFORE + AFTER + HEAVY).strip()] * 12)
    text = " ".join(text.split(" ")[:2000])
    assert len(text.split()) >= 1990
    times = []                               # CPU seconds of the child, so a loaded host cannot fail the test
    for _ in range(5):
        before = os.times()
        assert reply(tmp_path, text) == (0, "", "")
        after = os.times()
        times.append(after.children_user + after.children_system - before.children_user - before.children_system)
    assert sorted(times)[2] < 0.3, times
