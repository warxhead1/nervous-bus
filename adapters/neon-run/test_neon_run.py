"""Tests for adapters/neon-run CLI against a fake `orca` executable on PATH."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ADAPTER_DIR = Path(__file__).resolve().parent
CLI_BIN = ADAPTER_DIR / "neon-run"


FAKE_ORCA_SCRIPT = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_ORCA_STATE"])
log_path = Path(os.environ["FAKE_ORCA_LOG"])

argv = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")

state = json.loads(state_path.read_text(encoding="utf-8"))

if state.get("unreachable"):
    sys.stderr.write("error: connection refused: orca daemon not running\n")
    sys.exit(1)

if state.get("non_json"):
    sys.stdout.write("not json at all\n")
    sys.exit(1)

# Match command route
key = " ".join(argv[:2]) if len(argv) >= 2 else (argv[0] if argv else "")
responses = state.get("responses", {})

if key in responses:
    entry = responses[key]
    if isinstance(entry, list):
        # Pop next response in queue
        resp = entry.pop(0) if entry else {"ok": True, "result": {}}
        state_path.write_text(json.dumps(state), encoding="utf-8")
    else:
        resp = entry
    if isinstance(resp, dict) and "_stderr" in resp:
        sys.stderr.write(resp["_stderr"])
    rc = resp.get("_exit", 0) if isinstance(resp, dict) else 0
    if isinstance(resp, dict) and "_raw" in resp:
        sys.stdout.write(resp["_raw"])
    else:
        out = {k: v for k, v in resp.items() if not k.startswith("_")} if isinstance(resp, dict) else resp
        sys.stdout.write(json.dumps(out) + "\n")
    sys.exit(rc)

sys.stderr.write(f"unexpected fake orca argv: {argv}\n")
sys.exit(1)
"""


def _write_fake_orca(
    tmp_path: Path,
    responses: dict[str, Any] | None = None,
    *,
    unreachable: bool = False,
    non_json: bool = False,
    name: str = "orca",
) -> tuple[Path, Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    orca_path = bin_dir / name
    orca_path.write_text(FAKE_ORCA_SCRIPT, encoding="utf-8")
    orca_path.chmod(orca_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    state_path = tmp_path / "orca_state.json"
    log_path = tmp_path / "orca_calls.jsonl"
    state_path.write_text(
        json.dumps(
            {
                "responses": responses or {},
                "unreachable": unreachable,
                "non_json": non_json,
            }
        ),
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_ORCA_STATE"] = str(state_path)
    env["FAKE_ORCA_LOG"] = str(log_path)
    env.pop("ORCA_CLI_COMMAND", None)
    return orca_path, log_path, env


def _read_calls(log_path: Path) -> list[list[str]]:
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI_BIN), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_git_worktree(path: Path, branch: str = "neon-sample") -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
    return sha


def test_start_creates_run_and_launches_worker(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    spec_file = tmp_path / "feature-x.md"
    spec_file.write_text("# Spec\nBuild feature X.\n", encoding="utf-8")
    wt_dir = tmp_path / "wt-feature-x"
    _init_git_worktree(wt_dir, branch="neon-feature-x")

    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "repo list": {
                "ok": True,
                "result": {
                    "repos": [
                        {"id": "repo-uuid-1", "path": str(repo_dir)},
                    ]
                },
            },
            "orchestration run-create": {
                "ok": True,
                "result": {"runId": "run_abc123"},
            },
            "orchestration worker-start": {
                "ok": True,
                "result": {
                    "dispatchId": "ctx_def456",
                    "terminal": "term_789",
                    "workspace": f"repo-uuid-1::{wt_dir}",
                },
            },
        },
    )

    proc = _run_cli(
        [
            "start",
            "--repo",
            str(repo_dir),
            "--spec",
            str(spec_file),
            "--objective",
            "Implement feature X",
        ],
        env,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data == {
        "run": "run_abc123",
        "dispatch": "ctx_def456",
        "terminal": "term_789",
        "worktree": str(wt_dir),
        "branch": "neon-feature-x",
    }

    calls = _read_calls(log_path)
    assert calls[0] == ["repo", "list", "--json"]
    assert calls[1] == [
        "orchestration",
        "run-create",
        "--objective",
        "Implement feature X",
        "--json",
    ]
    ws_call = calls[2]
    assert ws_call[:2] == ["orchestration", "worker-start"]
    assert "--run" in ws_call and ws_call[ws_call.index("--run") + 1] == "run_abc123"
    assert "--worktree" in ws_call and ws_call[ws_call.index("--worktree") + 1] == "new-top-level"
    assert "--repo" in ws_call and ws_call[ws_call.index("--repo") + 1] == "id:repo-uuid-1"
    assert "--base-branch" in ws_call and ws_call[ws_call.index("--base-branch") + 1] == "origin/main"
    assert "--model" in ws_call and ws_call[ws_call.index("--model") + 1] == "gemini-4-neon-medium"
    assert "--agent" in ws_call and ws_call[ws_call.index("--agent") + 1] == "antigravity"
    assert "--name" in ws_call and ws_call[ws_call.index("--name") + 1] == "neon-feature-x"


def test_start_respects_orca_cli_command_env(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    spec_file = tmp_path / "task.md"
    spec_file.write_text("# Task\n", encoding="utf-8")

    custom_orca, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "repo list": {"ok": True, "result": {"repos": [{"id": "r1", "path": str(repo_dir)}]}},
            "orchestration run-create": {"ok": True, "result": {"runId": "run_env_1"}},
            "orchestration worker-start": {
                "ok": True,
                "result": {
                    "dispatchId": "ctx_1",
                    "terminal": "term_1",
                    "worktree": "/nonexistent/wt",
                    "branch": "custom-branch",
                },
            },
        },
        name="custom-orca-bin",
    )
    env["ORCA_CLI_COMMAND"] = str(custom_orca)
    # Remove bin_dir from PATH so only ORCA_CLI_COMMAND can resolve it
    env["PATH"] = "/usr/bin:/bin"

    proc = _run_cli(["start", "--repo", str(repo_dir), "--spec", str(spec_file), "--name", "lane-1"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["run"] == "run_env_1"
    assert len(_read_calls(log_path)) == 3


def test_start_unregistered_repo_exits_2(tmp_path: Path) -> None:
    repo_dir = tmp_path / "unregistered"
    repo_dir.mkdir()
    spec_file = tmp_path / "s.md"
    spec_file.write_text("spec\n", encoding="utf-8")

    _, _, env = _write_fake_orca(tmp_path, {"repo list": {"ok": True, "result": {"repos": []}}})
    proc = _run_cli(["start", "--repo", str(repo_dir), "--spec", str(spec_file)], env)
    assert proc.returncode == 2
    assert "not registered" in proc.stderr


def test_wait_pending_passes_run_and_types(tmp_path: Path) -> None:
    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {"messages": [], "timedOut": True},
                "_stderr": '{"_keepalive": true}\n',
            }
        },
    )
    proc = _run_cli(["wait", "run_only_mine", "--timeout", "2m"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"state": "pending"}

    calls = _read_calls(log_path)
    assert len(calls) == 1
    check_argv = calls[0]
    assert check_argv[:2] == ["orchestration", "check"]
    assert check_argv[check_argv.index("--run") + 1] == "run_only_mine"
    assert "--wait" in check_argv
    assert check_argv[check_argv.index("--types") + 1] == "worker_done,question,escalation"
    assert check_argv[check_argv.index("--timeout-ms") + 1] == "120000"


def test_wait_caps_timeout_at_9m50s(tmp_path: Path) -> None:
    _, log_path, env = _write_fake_orca(
        tmp_path,
        {"orchestration check": {"ok": True, "result": {"messages": []}}},
    )
    proc = _run_cli(["wait", "run_cap", "--timeout", "15m"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"state": "pending"}

    calls = _read_calls(log_path)
    assert calls[0][calls[0].index("--timeout-ms") + 1] == "590000"


def test_wait_done_succeeded_and_reads_git_metadata(tmp_path: Path) -> None:
    wt_dir = tmp_path / "wt-done"
    sha = _init_git_worktree(wt_dir, branch="neon-done-branch")

    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_done_1",
                            "run_id": "run_done_1",
                            "type": "worker_done",
                            "body": "Shipped the feature and opened PR.",
                            "payload": json.dumps(
                                {
                                    "taskId": "task_1",
                                    "dispatchId": "ctx_1",
                                    "outcome": "succeeded",
                                    "filesModified": ["adapters/neon-run/neon_run.py"],
                                }
                            ),
                        }
                    ]
                },
            },
            "orchestration worker-list": {
                "ok": True,
                "result": {
                    "workers": [
                        {
                            "dispatchId": "ctx_1",
                            "state": "succeeded",
                            "workspace": f"repo-id::{wt_dir}",
                        }
                    ]
                },
            },
        },
    )

    proc = _run_cli(["wait", "run_done_1"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "state": "done",
        "outcome": "succeeded",
        "branch": "neon-done-branch",
        "sha": sha,
        "files": ["adapters/neon-run/neon_run.py"],
        "body": "Shipped the feature and opened PR.",
    }
    calls = _read_calls(log_path)
    assert calls[0][calls[0].index("--run") + 1] == "run_done_1"
    assert calls[1] == ["orchestration", "worker-list", "--run", "run_done_1", "--json"]


def test_wait_done_failed_exits_1(tmp_path: Path) -> None:
    _, _, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_fail_1",
                            "run_id": "run_fail_1",
                            "type": "worker_done",
                            "body": "Tests failed on lint gate.",
                            "payload": {
                                "outcome": "failed",
                                "branch": "neon-fail",
                                "sha": "deadbeef",
                                "filesModified": ["foo.py"],
                            },
                        }
                    ]
                },
            }
        },
    )
    proc = _run_cli(["wait", "run_fail_1"], env)
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == {
        "state": "done",
        "outcome": "failed",
        "branch": "neon-fail",
        "sha": "deadbeef",
        "files": ["foo.py"],
        "body": "Tests failed on lint gate.",
    }


def test_wait_question_returns_id_and_body(tmp_path: Path) -> None:
    _, _, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_q_42",
                            "run_id": "run_q_1",
                            "type": "question",
                            "body": "Should I target main or release?",
                        }
                    ]
                },
            }
        },
    )
    proc = _run_cli(["wait", "run_q_1"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "state": "question",
        "id": "msg_q_42",
        "body": "Should I target main or release?",
    }


def test_wait_escalation_exits_1(tmp_path: Path) -> None:
    _, _, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_esc_1",
                            "run_id": "run_esc_1",
                            "type": "escalation",
                            "body": "Blocked: external service offline",
                        }
                    ]
                },
            }
        },
    )
    proc = _run_cli(["wait", "run_esc_1"], env)
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == {
        "state": "escalation",
        "body": "Blocked: external service offline",
    }


def test_wait_ignores_messages_from_other_runs(tmp_path: Path) -> None:
    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_foreign",
                            "run_id": "run_other",
                            "type": "worker_done",
                            "body": "Foreign run completed",
                            "payload": {"outcome": "succeeded"},
                        }
                    ]
                },
            }
        },
    )
    proc = _run_cli(["wait", "run_mine"], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"state": "pending"}
    calls = _read_calls(log_path)
    assert calls[0][calls[0].index("--run") + 1] == "run_mine"


def test_reply_sends_to_worker_dispatch(tmp_path: Path) -> None:
    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration worker-list": {
                "ok": True,
                "result": {
                    "workers": [
                        {"dispatchId": "ctx_old", "state": "succeeded"},
                        {"dispatchId": "ctx_live", "state": "ready"},
                    ]
                },
            },
            "orchestration send": {
                "ok": True,
                "result": {"id": "msg_reply_9"},
            },
        },
    )
    proc = _run_cli(["reply", "run_r_1", "--text", "Target origin/main."], env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "ok": True,
        "run": "run_r_1",
        "dispatch": "ctx_live",
        "messageId": "msg_reply_9",
    }

    calls = _read_calls(log_path)
    assert calls[0] == ["orchestration", "worker-list", "--run", "run_r_1", "--json"]
    send_argv = calls[1]
    assert send_argv[:2] == ["orchestration", "send"]
    assert send_argv[send_argv.index("--run") + 1] == "run_r_1"
    assert send_argv[send_argv.index("--to") + 1] == "dispatch:ctx_live"
    assert send_argv[send_argv.index("--body") + 1] == "Target origin/main."


def test_status_uses_peek_without_consuming(tmp_path: Path) -> None:
    _, log_path, env = _write_fake_orca(
        tmp_path,
        {
            "orchestration worker-list": {
                "ok": True,
                "result": {
                    "workers": [
                        {
                            "dispatchId": "ctx_s_1",
                            "state": "ready",
                            "liveness": "live",
                            "terminal": "term_s_1",
                            "workspace": "repo-id::/tmp/wt-s-1",
                        }
                    ]
                },
            },
            "orchestration check": {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "msg_q_1",
                            "run_id": "run_s_1",
                            "type": "question",
                            "body": "Need confirmation?",
                        }
                    ]
                },
            },
        },
    )
    proc = _run_cli(["status", "run_s_1"], env)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["run"] == "run_s_1"
    assert data["state"] == "question"
    assert data["dispatch"] == "ctx_s_1"
    assert data["terminal"] == "term_s_1"
    assert data["liveness"] == "live"
    assert data["unread"] == [{"id": "msg_q_1", "type": "question", "body": "Need confirmation?"}]

    calls = _read_calls(log_path)
    assert calls[0] == ["orchestration", "worker-list", "--run", "run_s_1", "--json"]
    assert "--peek" in calls[1]
    assert "--wait" not in calls[1]
    assert calls[1][calls[1].index("--run") + 1] == "run_s_1"


def test_orca_unreachable_exits_3(tmp_path: Path) -> None:
    _, _, env = _write_fake_orca(tmp_path, unreachable=True)
    proc = _run_cli(["status", "run_down"], env)
    assert proc.returncode == 3
    assert "unreachable" in proc.stderr.lower()


def test_missing_orca_binary_exits_3(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["ORCA_CLI_COMMAND"] = str(tmp_path / "does-not-exist-orca")
    proc = _run_cli(["wait", "run_missing"], env)
    assert proc.returncode == 3
    assert "unreachable" in proc.stderr.lower()


def test_bad_timeout_exits_2(tmp_path: Path) -> None:
    _, _, env = _write_fake_orca(tmp_path, {})
    proc = _run_cli(["wait", "run_bad", "--timeout", "not-a-duration"], env)
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
