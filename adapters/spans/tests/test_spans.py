"""spans tests over synthetic transcripts and a fake `nervous` that records each publish."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).absolute().parent
ADAPTER = HERE.parent
import spans
from classify import classify

SCHEMA = json.loads((ADAPTER.parents[1] / "schemas" / "agent.tool.span.v1.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)


def at(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def use(cid, tool, sec, cwd, command=None, session="s1", agent=None):
    inp = {"command": command} if command is not None else {"file_path": "x"}
    e = {"type": "assistant", "timestamp": at(sec), "cwd": cwd, "sessionId": session,
         "message": {"content": [{"type": "tool_use", "id": cid, "name": tool, "input": inp}]}}
    if agent:
        e["agentId"] = agent
    return json.dumps(e)


def result(cid, sec, text="ok", error=False):
    return json.dumps({"type": "user", "timestamp": at(sec),
                       "message": {"content": [{"type": "tool_result", "tool_use_id": cid, "content": text, "is_error": error}]}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    projects = tmp_path / "work" / "projects"
    for p in ("alpha", "beta"):
        (projects / p).mkdir(parents=True)
    monkeypatch.setenv("NBUS_PROJECT_ROOTS", str(projects))
    monkeypatch.delenv("NBUS_PROJECT_ALIASES", raising=False)
    root = tmp_path / "claude" / "projects"
    (root / "-alpha").mkdir(parents=True)
    fake = tmp_path / "nervous"
    fake.write_text(f'#!/bin/sh\n[ -f "{tmp_path}/fail" ] && exit 7\n'
                    f'[ "$1" = publish ] && [ "$2" = agent.tool.span.v1 ] && printf "%s\\n" "$3" >> "{tmp_path}/sent"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    class W:
        pass
    w = W()
    w.tmp, w.root, w.fake, w.alpha, w.beta = tmp_path, root, fake, str(projects / "alpha"), str(projects / "beta")
    w.cursor = tmp_path / "cursor.json"
    w.session = root / "-alpha" / "s1.jsonl"
    w.sent = lambda: [json.loads(x) for x in (tmp_path / "sent").read_text().splitlines()] if (tmp_path / "sent").exists() else []
    w.write = lambda *lines, path=None, mode="w": (path or w.session).open(mode).write("\n".join(lines) + "\n")
    w.scan = lambda *extra: spans.main(["scan", "--root", str(root), "--cursor", str(w.cursor), "--nervous", str(fake), *extra])
    return w


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("tool,cmd,expect", [
    ("Bash", "sleep 30", ("wait", "time")),
    ("Bash", "until grep -q DONE build.log; do sleep 5; done", ("wait", "build")),
    ("Bash", "until [ -f e2e-report.json ]; do sleep 20; done", ("wait", "e2e")),
    ("Bash", "flock /tmp/build.lock true", ("wait", "lock")),
    ("Bash", "while [ -e slot.pid ]; do sleep 2; done", ("wait", "lock")),
    ("Bash", "until ./gate.sh >/dev/null; do sleep 9; done", ("wait", "gate")),
    ("Bash", "gh run watch 1234", ("wait", "ci")),
    ("Bash", "neon-run wait run-1 --timeout 9m", ("wait", "background_task")),
    ("Bash", "until kill -0 $PID 2>/dev/null; do sleep 3; done", ("wait", "background_task")),
    ("Bash", "until pgrep -f worker; do sleep 3; done", ("wait", "background_task")),
    ("Bash", "until cargo test -q; do sleep 3; done", ("wait", "test")),
    ("Bash", "until [ -s out ]; do sleep 3; done", ("wait", "unknown")),
    ("Bash", "./scripts/gate.sh", ("gate", None)),
    ("Bash", "cd app && npx playwright test", ("e2e", None)),
    ("Bash", "cargo nextest run", ("test", None)),
    ("Bash", "cargo build --release", ("build", None)),
    ("Bash", "cd repo && git status", ("vcs", None)),
    ("Bash", "gh pr view 3", ("github", None)),
    ("Bash", "bd ready", ("orchestration", None)),
    ("Bash", "ls /data/orca/proj/notes", ("shell-other", None)),
    ("Bash", "ls -la", ("shell-other", None)),
    # prose never decides a class: the word wait, sleep and e2e only appear in quoted text
    ("Bash", "git commit -m 'wait for e2e, then sleep 5'", ("vcs", None)),
    ("Bash", "echo \"until the gate passes\"", ("shell-other", None)),
    ("Bash", "cat <<'EOF'\nuntil done; sleep 5\nEOF", ("shell-other", None)),
    ("TaskOutput", None, ("wait", "background_task")),
    ("Monitor", "until [ -f build.done ]; do sleep 5; done", ("wait", "build")),
    ("Read", None, ("read", None)),
    ("Grep", None, ("search", None)),
    ("Edit", None, ("edit", None)),
    ("Agent", None, ("subagent", None)),
    ("WebFetch", None, ("web", None)),
    ("SomethingNew", None, ("other", None)),
])
def test_classify(tool, cmd, expect):
    assert classify(tool, cmd) == expect


# ------------------------------------------------------------ pairing

def test_pairing_duration_status_and_exit_code(world):
    world.write(
        use("a", "Bash", 0, world.alpha, "cargo build"), result("a", 42.5),
        use("b", "Bash", 50, world.alpha, "false"), result("b", 51, "Exit code 3\nboom", error=True),
        use("c", "Read", 60, world.alpha), result("c", 60.2),
        "not json {", json.dumps({"type": "summary"}),
    )
    got, _, _ = spans.read_spans(str(world.session), "-alpha/s1.jsonl")
    by = {s["tool"] + s["class"]: s for s in got}
    assert len(got) == 3
    assert by["Bashbuild"]["duration_ms"] == 42500 and by["Bashbuild"]["status"] == "ok" and by["Bashbuild"]["exit_code"] == 0
    assert by["Bashshell-other"]["status"] == "error" and by["Bashshell-other"]["exit_code"] == 3
    assert by["Readread"]["duration_ms"] == 200 and "exit_code" not in by["Readread"]
    assert all(s["project"] == "alpha" and s["session"] == "s1" for s in got)


def test_lost_call_after_stale_window_and_pending_while_recent(world):
    far = spans.STALE_PENDING_S + 100
    world.write(
        use("old", "Bash", 0, world.alpha, "sleep 5"),          # never gets a result
        use("x", "Read", far, world.alpha), result("x", far + 1),
        use("new", "Bash", far + 5, world.alpha, "sleep 5"),    # recent, still running
    )
    got, offset, _ = spans.read_spans(str(world.session), "r")
    assert [(s["status"], "duration_ms" in s) for s in got if s["tool"] == "Bash"] == [("lost", False)]
    assert any(s["tool"] == "Read" for s in got)
    assert offset == world.session.read_text().index(use("new", "Bash", far + 5, world.alpha, "sleep 5"))


def test_workflow_lane_carries_ids_and_label_and_project(world):
    lane_dir = world.root / "-alpha" / "s1" / "subagents" / "workflows" / "wf_abc-123"
    lane_dir.mkdir(parents=True)
    lane = lane_dir / "agent-9.jsonl"
    lane.write_text(use("a", "Bash", 0, world.beta, "make", agent="9") + "\n" + result("a", 3) + "\n")
    (lane_dir / "agent-9.meta.json").write_text(json.dumps({"description": "research:lane-one"}))
    got, _, _ = spans.read_spans(str(lane), "rel")
    assert got[0]["workflow_id"] == "wf_abc-123" and got[0]["label"] == "research:lane-one"
    assert got[0]["agent_id"] == "9" and got[0]["project"] == "beta"


def test_span_never_carries_command_text(world):
    secret = "hunter2-token-value"
    world.write(use("a", "Bash", 0, world.alpha, f"curl -H 'x: {secret}' https://example.invalid/{secret}"), result("a", 1))
    got, _, _ = spans.read_spans(str(world.session), "r")
    assert len(got) == 1
    assert secret not in json.dumps(got) and "curl" not in json.dumps(got)


# ------------------------------------------------------------ scan, cursor, schema

def test_scan_publishes_valid_spans_and_rerun_publishes_nothing(world):
    world.write(use("a", "Bash", 0, world.alpha, "sleep 3"), result("a", 3),
                use("b", "Bash", 4, world.alpha, "./gate.sh"), result("b", 9))
    assert world.scan() == 0
    first = world.sent()
    assert len(first) == 2
    for s in first:
        VALIDATOR.validate(s)
    assert len({s["span_id"] for s in first}) == 2
    assert world.scan() == 0
    assert world.sent() == first


def test_scan_after_growth_publishes_only_new_spans(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    world.scan()
    world.write(use("b", "Bash", 5, world.alpha, "ls"), result("b", 6), mode="a")
    world.scan()
    world.scan()
    sent = world.sent()
    assert len(sent) == 2 and len({s["span_id"] for s in sent}) == 2


def test_call_open_across_scans_is_published_once_when_it_finishes(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1),
                use("slow", "Bash", 2, world.alpha, "cargo test"),
                use("b", "Bash", 3, world.alpha, "ls"), result("b", 4))
    world.scan()
    assert sorted(s["tool"] + s["class"] for s in world.sent()) == ["Bashshell-other"] * 2
    world.scan()
    assert len(world.sent()) == 2          # the pairs behind the open call are not re-sent
    world.write(result("slow", 100), mode="a")
    world.scan()
    world.scan()
    sent = world.sent()
    assert len(sent) == 3 and len({s["span_id"] for s in sent}) == 3
    done = [s for s in sent if s["class"] == "test"]
    assert done[0]["duration_ms"] == 98000
    cur = json.loads(world.cursor.read_text())
    assert next(iter(cur.values()))["done"] == []


def test_half_written_last_line_waits_for_its_newline(world):
    full = use("a", "Bash", 0, world.alpha, "ls") + "\n" + result("a", 1)
    world.session.write_text(full[:-10])
    world.scan()
    assert world.sent() == [] or len(world.sent()) == 0
    world.session.write_text(full + "\n")
    world.scan()
    assert len(world.sent()) == 1


def test_publish_failure_keeps_cursor_so_rerun_retries(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    (world.tmp / "fail").write_text("")
    assert world.scan() == 1
    (world.tmp / "fail").unlink()
    assert world.scan() == 0
    assert len(world.sent()) == 1


def test_dry_run_prints_and_leaves_cursor_alone(world, capsys):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    assert world.scan("--dry-run") == 0
    VALIDATOR.validate(json.loads(capsys.readouterr().out.splitlines()[0]))
    assert world.sent() == [] and not world.cursor.exists()


def test_replaced_file_is_read_from_the_start(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    world.scan()
    world.session.unlink()
    world.write(use("z", "Bash", 0, world.alpha, "ls"), result("z", 1))
    world.scan()
    assert len(world.sent()) == 2


def test_cli_entrypoint_runs_in_isolated_mode(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    r = subprocess.run([str(ADAPTER / "spans"), "top", "--root", str(world.root), "--since", "24h"],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["calls"] == 1


# ------------------------------------------------------------ top

def top(world, capsys, *extra):
    assert spans.main(["top", "--root", str(world.root), *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_top_ranks_classes_wait_targets_and_agents(world, capsys):
    lane_dir = world.root / "-alpha" / "s1" / "subagents" / "workflows" / "wf_1"
    lane_dir.mkdir(parents=True)
    (lane_dir / "agent-1.meta.json").write_text(json.dumps({"description": "slow-lane"}))
    world.write(
        use("a", "Bash", 0, world.alpha, "until [ -f e2e.done ]; do sleep 5; done"), result("a", 3600),
        use("b", "Bash", 3600, world.alpha, "cargo build"), result("b", 3700),
        use("c", "Bash", 3700, world.alpha, "until flock -n x.lock true; do sleep 1; done"), result("c", 3760),
        use("d", "Read", 3760, world.alpha), result("d", 3761),
    )
    (lane_dir / "agent-1.jsonl").write_text(use("e", "Bash", 0, world.beta, "./gate.sh", agent="1") + "\n" + result("e", 7200) + "\n")
    res = top(world, capsys, "--since", "24h")
    assert [r["key"] for r in res["classes"]][:4] == ["gate", "wait", "build", "read"]
    assert res["classes"][0]["hours"] == 2.0 and res["classes"][1]["calls"] == 2
    assert [r["key"] for r in res["wait_targets"]] == ["e2e", "lock"]
    assert res["agents"][0]["key"] == "wf_1 slow-lane" and res["agents"][0]["hours"] == 2.0
    assert res["calls"] == 5


def test_top_filters_by_project_and_window(world, capsys):
    world.write(use("a", "Bash", 0, world.alpha, "cargo build"), result("a", 60),
                use("b", "Bash", 100, world.beta, "./gate.sh"), result("b", 400))
    only_beta = top(world, capsys, "--project", "beta")
    assert [r["key"] for r in only_beta["classes"]] == ["gate"] and only_beta["project"] == "beta"
    nothing = top(world, capsys, "--since", "1m")
    assert nothing["calls"] == 0 and nothing["classes"] == []


def test_top_text_view(world, capsys):
    world.write(use("a", "Bash", 0, world.alpha, "cargo build"), result("a", 90))
    assert spans.main(["top", "--root", str(world.root), "--text"]) == 0
    out = capsys.readouterr().out
    assert "TOOL CLASS" in out and "build" in out and "WAIT TARGET" in out


@pytest.mark.parametrize("verb", ["top", "scan"])
def test_bad_since_exits_2_on_stderr(world, capsys, verb):
    extra = ["--cursor", str(world.cursor)] if verb == "scan" else []
    assert spans.main([verb, "--root", str(world.root), "--since", "soon", *extra]) == 2
    out = capsys.readouterr()
    assert "bad --since" in out.err and out.out == ""


# ------------------------------------------------------------ review fixes

def test_open_call_at_offset_zero_does_not_resend_finished_spans(world):
    world.write(use("slow", "Bash", 0, world.alpha, "cargo test"),
                use("a", "Bash", 1, world.alpha, "ls"), result("a", 2))
    world.scan()
    world.scan()
    world.scan()
    assert len(world.sent()) == 1
    world.write(result("slow", 50), mode="a")
    world.scan()
    world.scan()
    assert len(world.sent()) == 2


def test_failure_on_second_span_keeps_the_first_in_the_cursor(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1),
                use("b", "Bash", 2, world.alpha, "ls"), result("b", 3))
    flaky = world.tmp / "flaky"
    flaky.write_text(f'#!/bin/sh\nn=$(cat "{world.tmp}/n" 2>/dev/null || echo 0)\n'
                     f'echo $((n+1)) > "{world.tmp}/n"\n[ "$n" -ge 1 ] && exit 7\nprintf "%s\\n" "$3" >> "{world.tmp}/sent"\n')
    flaky.chmod(flaky.stat().st_mode | stat.S_IEXEC)
    assert spans.main(["scan", "--root", str(world.root), "--cursor", str(world.cursor), "--nervous", str(flaky)]) == 1
    assert len(world.sent()) == 1
    assert world.scan() == 0
    sent = world.sent()
    assert len(sent) == 2 and len({s["span_id"] for s in sent}) == 2


def test_label_is_a_slug_never_free_text(world):
    lane_dir = world.root / "-alpha" / "s1" / "subagents" / "workflows" / "wf_1"
    lane_dir.mkdir(parents=True)
    (lane_dir / "agent-1.jsonl").write_text("")
    meta = lane_dir / "agent-1.meta.json"
    for body, expect in [({"description": "fix /some/private/path now"}, ""),
                         ({"description": "run the build", "label": "lane-a"}, "lane-a"),
                         ({"label": "has space"}, ""),
                         ({"description": "research:lane-one"}, "research:lane-one")]:
        meta.write_text(json.dumps(body))
        assert spans.lane_meta(str(lane_dir / "agent-1.jsonl"))[1] == expect


def test_abandoned_open_call_expires_on_file_idle_time(world):
    world.write(use("gone", "Bash", 0, world.alpha, "sleep 5"))
    old = spans.time.time() - spans.STALE_PENDING_S - 60
    os.utime(world.session, (old, old))
    got, offset, _ = spans.read_spans(str(world.session), "r")
    assert [(s["status"], "duration_ms" in s) for s in got] == [("lost", False)]
    assert offset == world.session.stat().st_size


def test_long_fields_are_capped_to_the_schema(world):
    world.write(use("a", "T" * 300, 0, world.alpha, None, session="S" * 500, agent="A" * 500), result("a", 1))
    got, _, _ = spans.read_spans(str(world.session), "r")
    assert len(got) == 1
    VALIDATOR.validate(spans.public(got[0]))


def test_project_resolves_from_the_launching_transcript(world):
    launch = world.root / re.sub(r"[^A-Za-z0-9]", "-", world.alpha)
    launch.mkdir()
    session = launch / "s1.jsonl"
    session.write_text(use("a", "Bash", 0, "/nowhere/unknown-dir", "ls") + "\n" + result("a", 1) + "\n")
    got, _, _ = spans.read_spans(str(session), "r")
    assert got[0]["project"] == "alpha"


def test_null_ids_in_a_transcript_line_do_not_crash(world):
    e = json.loads(use("a", "Bash", 0, world.alpha, "ls"))
    e.update(agentId=None, sessionId=None, cwd=None)
    world.write(json.dumps(e), result("a", 1))
    got, _, _ = spans.read_spans(str(world.session), "r")
    assert len(got) == 1 and "agent_id" not in got[0] and got[0]["session"] == "s1"


@pytest.mark.parametrize("cmd,expect", [
    ("echo start\nsleep 30", ("wait", "unknown")),
    ("cd x\ngit status", ("vcs", None)),
    ("echo hi\ngh pr view 1", ("github", None)),
    ("echo hi\nbd ready", ("orchestration", None)),
    ("echo hi\nuntil [ -f e2e.done ]; do sleep 5; done", ("wait", "e2e")),
])
def test_multiline_commands_classify_by_any_line(cmd, expect):
    assert classify("Bash", cmd) == expect


# ------------------------------------------------------------ boundaries

def parallel_use(sec, cwd, *ids):
    blocks = [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": "ls"}} for i in ids]
    return json.dumps({"type": "assistant", "timestamp": at(sec), "cwd": cwd, "sessionId": "s1",
                       "message": {"content": blocks}})


def test_orphan_results_and_foreign_blocks_are_ignored(world):
    stray = {"type": "assistant", "timestamp": at(2), "message": {"content": [
        {"type": "text", "tool_use_id": "open", "text": "x"}]}}
    world.write(result("never-opened", 0), use("open", "Bash", 1, world.alpha, "ls"), json.dumps(stray))
    got, offset, _ = spans.read_spans(str(world.session), "r")
    assert got == []
    assert offset == world.session.read_text().index(use("open", "Bash", 1, world.alpha, "ls"))


def test_call_on_a_shared_line_with_an_open_sibling_is_sent_once(world):
    world.write(parallel_use(0, world.alpha, "a", "b"), result("a", 5))
    world.scan()
    world.scan()
    assert [s["duration_ms"] for s in world.sent()] == [5000]
    world.write(result("b", 9), mode="a")
    world.scan()
    world.scan()
    assert sorted(s["duration_ms"] for s in world.sent()) == [5000, 9000]


def test_call_open_exactly_the_stale_window_is_still_open(world, monkeypatch):
    world.write(use("old", "Bash", 0, world.alpha, "ls"), use("x", "Read", spans.STALE_PENDING_S, world.alpha),
                result("x", spans.STALE_PENDING_S))
    mtime = int(T0.timestamp())
    os.utime(world.session, (mtime, mtime))
    monkeypatch.setattr(spans.time, "time", lambda: mtime)
    got, offset, _ = spans.read_spans(str(world.session), "r")
    assert [s["tool"] for s in got] == ["Read"]
    assert offset == 0
    world.write(use("y", "Read", spans.STALE_PENDING_S + 2, world.alpha), result("y", spans.STALE_PENDING_S + 3), mode="a")
    got, _, _ = spans.read_spans(str(world.session), "r")
    assert sorted(s["status"] for s in got) == ["lost", "ok", "ok"]


def test_file_idle_exactly_the_stale_window_is_not_abandoned(world, monkeypatch):
    world.write(use("open", "Bash", 0, world.alpha, "ls"))
    mtime = int(T0.timestamp())
    os.utime(world.session, (mtime, mtime))
    monkeypatch.setattr(spans.time, "time", lambda: mtime + spans.STALE_PENDING_S)
    assert spans.read_spans(str(world.session), "r")[0] == []
    monkeypatch.setattr(spans.time, "time", lambda: mtime + spans.STALE_PENDING_S + 1)
    assert [s["status"] for s in spans.read_spans(str(world.session), "r")[0]] == ["lost"]


def test_zero_and_negative_durations_clamp_to_zero(world):
    world.write(use("a", "Bash", 5, world.alpha, "ls"), result("a", 5),
                use("b", "Bash", 9, world.alpha, "ls"), result("b", 8))
    got, _, _ = spans.read_spans(str(world.session), "r")
    assert [s["duration_ms"] for s in got] == [0, 0]


def test_files_modified_exactly_at_since_are_read(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    os.utime(world.session, (1_000_000, 1_000_000))
    assert [r for r, _ in spans.recent_files(str(world.root), 1_000_000)] == ["-alpha/s1.jsonl"]
    assert list(spans.recent_files(str(world.root), 1_000_001)) == []


def test_span_starting_exactly_at_since_is_ranked(world):
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1))
    start = T0.timestamp()
    assert len(list(spans.top_spans(str(world.root), start, None))) == 1
    assert list(spans.top_spans(str(world.root), start + 0.001, None)) == []


def test_rank_average_and_top_totals(world, capsys):
    assert spans.rank({"k": [10.0, 4]}, 5) == [{"key": "k", "hours": 0.003, "calls": 4, "avg_s": 2.5}]
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 60),
                use("b", "Bash", 100, world.alpha, "ls"), result("b", 220))
    res = top(world, capsys, "--since", "24h")
    assert res["calls"] == 2 and res["tool_hours"] == 0.05
    assert res["classes"] == [{"key": "shell-other", "hours": 0.05, "calls": 2, "avg_s": 90.0}]
