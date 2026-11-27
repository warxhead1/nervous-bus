#!/usr/bin/env python3
"""neon-run: blocking-free CLI to drive one Orca agy lane from any orchestrator.

JSON on stdout, errors on stderr.
Exit codes: 0 ok, 1 negative result, 2 usage, 3 Orca unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

DEFAULT_BASE = "origin/main"
DEFAULT_MODEL = "gemini-4-neon-medium"
DEFAULT_AGENT = "antigravity"
DEFAULT_WAIT_TIMEOUT = "9m"
MAX_WAIT_MS = 590_000  # 9m50s hard cap so 10-minute shell tools never kill wait
WAIT_TYPES = "worker_done,question,escalation"

_SETTLED_WORKER_STATES = {"succeeded", "failed", "stopped", "abandoned", "released"}

_UNREACHABLE_CODES = {
    "unreachable",
    "connection_refused",
    "daemon_unavailable",
    "transport_error",
    "econnrefused",
    "socket_error",
    "not_connected",
    "server_unreachable",
}

_UNREACHABLE_HINTS = (
    "connection refused",
    "econnrefused",
    "cannot connect",
    "failed to connect",
    "unreachable",
    "not running",
    "no such file or directory",
    "socket",
    "daemon",
)

_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_MS = {
    "ms": 1.0,
    "s": 1_000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
}


class UsageError(Exception):
    """Invalid arguments or caller preconditions (exit 2)."""


class OrcaUnreachableError(Exception):
    """Orca CLI binary missing or runtime unreachable (exit 3)."""


class OrcaCommandError(Exception):
    """Orca returned an application-level error (exit 1)."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _NeonArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - tested via CLI
        raise UsageError(message)


def parse_duration_ms(raw: str) -> int:
    """Parse a duration string (e.g. '9m', '5m30s', '90s', '500ms', '120') into ms."""
    text = raw.strip().lower()
    if not text:
        raise UsageError("timeout must not be empty")
    try:
        secs = float(text)
    except ValueError:
        secs = None
    if secs is not None:
        if secs <= 0:
            raise UsageError(f"timeout must be positive: {raw}")
        return max(1, int(round(secs * 1_000)))

    pos = 0
    total_ms = 0.0
    for match in _DURATION_TOKEN_RE.finditer(text):
        if match.start() != pos:
            raise UsageError(f"invalid timeout duration: {raw}")
        pos = match.end()
        total_ms += float(match.group(1)) * _UNIT_MS[match.group(2)]
    if pos != len(text) or total_ms <= 0:
        raise UsageError(f"invalid timeout duration: {raw}")
    return max(1, int(round(total_ms)))


def clamp_wait_ms(ms: int) -> int:
    return min(ms, MAX_WAIT_MS)


def orca_argv_prefix() -> list[str]:
    raw = os.environ.get("ORCA_CLI_COMMAND", "").strip()
    if not raw:
        return ["orca"]
    parts = shlex.split(raw)
    return parts if parts else ["orca"]


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Scan lines from the end in case non-JSON lines preceded the final JSON object.
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate or not candidate.startswith("{"):
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _looks_unreachable(code: str, text: str) -> bool:
    if code.lower() in _UNREACHABLE_CODES:
        return True
    lower = text.lower()
    return any(hint in lower for hint in _UNREACHABLE_HINTS)


def run_orca(args: list[str], timeout_s: float | None = None) -> dict[str, Any]:
    """Invoke the Orca CLI and return its parsed `result` dict."""
    cmd = orca_argv_prefix() + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise OrcaUnreachableError(f"failed to execute Orca CLI ({cmd[0]}): {exc}") from exc

    parsed = _parse_json_stdout(proc.stdout)
    if parsed is None:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise OrcaUnreachableError(f"Orca unreachable or returned non-JSON: {detail}")

    if parsed.get("ok") is False:
        err = parsed.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            msg = str(err.get("message") or code or "Orca error")
        else:
            code = ""
            msg = str(err or proc.stderr.strip() or "Orca error")
        if _looks_unreachable(code, f"{msg}\n{proc.stderr}"):
            raise OrcaUnreachableError(msg)
        raise OrcaCommandError(msg, code=code)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        if _looks_unreachable("", detail):
            raise OrcaUnreachableError(detail)
        raise OrcaCommandError(detail)

    result = parsed.get("result")
    if isinstance(result, dict):
        return result
    return parsed


def _parse_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_worktree(record: dict[str, Any]) -> str:
    for key in ("worktree", "worktreePath"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    workspace = record.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        return workspace.split("::")[-1].strip()
    return ""


def _git_head_info(worktree: str) -> tuple[str, str]:
    if not worktree or not os.path.isdir(worktree):
        return "", ""
    branch = ""
    sha = ""
    try:
        b = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if b.returncode == 0:
            branch = b.stdout.strip()
        s = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if s.returncode == 0:
            sha = s.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return branch, sha
    return branch, sha


def _sanitize_name(stem: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem.strip().lower()).strip("-")
    if not cleaned:
        cleaned = "lane"
    if not cleaned.startswith("neon-"):
        cleaned = f"neon-{cleaned}"
    return cleaned


def _pick_primary_worker(workers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not workers:
        return None
    for w in workers:
        state = str(w.get("state") or "").lower()
        if state and state not in _SETTLED_WORKER_STATES:
            return w
    return workers[0]


def _extract_messages(result: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    raw = result.get("messages")
    if raw is None and isinstance(result.get("delivery"), dict):
        raw = result["delivery"].get("messages")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg_run = item.get("run_id") or item.get("runId")
        if msg_run and str(msg_run) != run_id:
            continue
        out.append(item)
    return out


def _emit(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def cmd_start(ns: argparse.Namespace) -> int:
    repo_path = os.path.realpath(ns.repo)
    if not os.path.isdir(repo_path):
        raise UsageError(f"repo path does not exist or is not a directory: {ns.repo}")

    spec_path = os.path.realpath(ns.spec)
    if not os.path.isfile(spec_path):
        raise UsageError(f"spec file does not exist: {ns.spec}")

    name = ns.name.strip() if ns.name else _sanitize_name(Path(spec_path).stem)
    if not name:
        raise UsageError("worker name must not be empty")
    objective = ns.objective.strip() if ns.objective else f"neon-run: {name}"

    repos_res = run_orca(["repo", "list", "--json"])
    repos = repos_res.get("repos")
    if not isinstance(repos, list):
        repos = []
    repo_id = ""
    for entry in repos:
        if not isinstance(entry, dict):
            continue
        entry_path = entry.get("path")
        if isinstance(entry_path, str) and (
            entry_path == repo_path or os.path.realpath(entry_path) == repo_path
        ):
            repo_id = str(entry.get("id") or "")
            break
    if not repo_id:
        raise UsageError(
            f"repo is not registered in Orca: {repo_path} "
            f"(register with: orca repo add --path {repo_path} --kind git)"
        )

    run_res = run_orca(["orchestration", "run-create", "--objective", objective, "--json"])
    run_id = str(
        run_res.get("runId")
        or run_res.get("id")
        or (run_res.get("run") or {}).get("id", "")
        or ""
    )
    if not run_id:
        raise OrcaCommandError("run-create returned no run id")

    start_res = run_orca(
        [
            "orchestration",
            "worker-start",
            "--environment",
            "local",
            "--run",
            run_id,
            "--spec-file",
            spec_path,
            "--worktree",
            "new-top-level",
            "--repo",
            f"id:{repo_id}",
            "--base-branch",
            ns.base,
            "--name",
            name,
            "--task-title",
            name,
            "--agent",
            ns.agent,
            "--model",
            ns.model,
            "--json",
        ]
    )

    dispatch = str(start_res.get("dispatchId") or start_res.get("dispatch") or "")
    terminal = str(start_res.get("terminal") or start_res.get("terminalHandle") or "")
    worktree = _extract_worktree(start_res)
    branch = str(start_res.get("branch") or start_res.get("gitBranch") or "")
    if not branch and worktree:
        branch, _ = _git_head_info(worktree)

    _emit(
        {
            "run": run_id,
            "dispatch": dispatch,
            "terminal": terminal,
            "worktree": worktree,
            "branch": branch,
        }
    )
    return EXIT_OK


def _resolve_git_metadata(run_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    branch = str(payload.get("branch") or "")
    sha = str(payload.get("sha") or "")
    if branch and sha:
        return branch, sha
    try:
        wl = run_orca(["orchestration", "worker-list", "--run", run_id, "--json"])
    except (OrcaUnreachableError, OrcaCommandError):
        return branch, sha
    workers = wl.get("workers")
    worker = _pick_primary_worker(workers) if isinstance(workers, list) else None
    if not worker:
        return branch, sha
    if not branch:
        branch = str(worker.get("branch") or worker.get("gitBranch") or "")
    if not sha:
        sha = str(worker.get("sha") or worker.get("headSha") or "")
    if not branch or not sha:
        wt = _extract_worktree(worker)
        git_branch, git_sha = _git_head_info(wt)
        if not branch:
            branch = git_branch
        if not sha:
            sha = git_sha
    return branch, sha


def cmd_wait(ns: argparse.Namespace) -> int:
    requested_ms = parse_duration_ms(ns.timeout)
    timeout_ms = clamp_wait_ms(requested_ms)

    try:
        res = run_orca(
            [
                "orchestration",
                "check",
                "--run",
                ns.run,
                "--wait",
                "--types",
                WAIT_TYPES,
                "--timeout-ms",
                str(timeout_ms),
                "--full",
                "--json",
            ],
            timeout_s=(timeout_ms / 1_000.0) + 15.0,
        )
    except subprocess.TimeoutExpired:
        _emit({"state": "pending"})
        return EXIT_OK

    messages = _extract_messages(res, ns.run)

    done_msg = next((m for m in messages if m.get("type") == "worker_done"), None)
    if done_msg is not None:
        payload = _parse_payload(done_msg.get("payload"))
        outcome = str(payload.get("outcome") or done_msg.get("outcome") or "succeeded")
        raw_files = (
            payload.get("filesModified")
            if payload.get("filesModified") is not None
            else payload.get("files", done_msg.get("filesModified", []))
        )
        if isinstance(raw_files, str):
            files = [p.strip() for p in raw_files.split(",") if p.strip()]
        elif isinstance(raw_files, list):
            files = [str(p) for p in raw_files]
        else:
            files = []
        branch, sha = _resolve_git_metadata(ns.run, payload)
        body = str(done_msg.get("body") or "")
        _emit(
            {
                "state": "done",
                "outcome": outcome,
                "branch": branch,
                "sha": sha,
                "files": files,
                "body": body,
            }
        )
        return EXIT_OK if outcome == "succeeded" else EXIT_NEGATIVE

    esc_msg = next((m for m in messages if m.get("type") == "escalation"), None)
    if esc_msg is not None:
        body = str(esc_msg.get("body") or esc_msg.get("subject") or "")
        _emit({"state": "escalation", "body": body})
        return EXIT_NEGATIVE

    q_msg = next((m for m in messages if m.get("type") == "question"), None)
    if q_msg is not None:
        qid = str(q_msg.get("id") or "")
        body = str(q_msg.get("body") or q_msg.get("subject") or "")
        _emit({"state": "question", "id": qid, "body": body})
        return EXIT_OK

    _emit({"state": "pending"})
    return EXIT_OK


def cmd_reply(ns: argparse.Namespace) -> int:
    if not ns.text or not ns.text.strip():
        raise UsageError("--text must not be empty")

    wl = run_orca(["orchestration", "worker-list", "--run", ns.run, "--json"])
    workers = wl.get("workers")
    worker = _pick_primary_worker(workers) if isinstance(workers, list) else None
    dispatch_id = str((worker or {}).get("dispatchId") or (worker or {}).get("dispatch") or "")
    if not dispatch_id:
        raise OrcaCommandError(f"no worker dispatch found for run {ns.run}")

    send_res = run_orca(
        [
            "orchestration",
            "send",
            "--run",
            ns.run,
            "--to",
            f"dispatch:{dispatch_id}",
            "--type",
            "status",
            "--subject",
            "Coordinator reply",
            "--body",
            ns.text,
            "--json",
        ]
    )
    msg_id = str(send_res.get("id") or send_res.get("messageId") or "")
    _emit(
        {
            "ok": True,
            "run": ns.run,
            "dispatch": dispatch_id,
            "messageId": msg_id,
        }
    )
    return EXIT_OK


def cmd_status(ns: argparse.Namespace) -> int:
    wl = run_orca(["orchestration", "worker-list", "--run", ns.run, "--json"])
    workers = wl.get("workers") if isinstance(wl.get("workers"), list) else []

    peek_error = ""
    unread_raw: list[dict[str, Any]] = []
    try:
        peek_res = run_orca(
            [
                "orchestration",
                "check",
                "--run",
                ns.run,
                "--peek",
                "--full",
                "--json",
            ]
        )
        unread_raw = _extract_messages(peek_res, ns.run)
    except OrcaCommandError as exc:
        peek_error = exc.message

    unread = [
        {
            "id": str(m.get("id") or ""),
            "type": str(m.get("type") or ""),
            "body": str(m.get("body") or m.get("subject") or ""),
        }
        for m in unread_raw
    ]

    worker = _pick_primary_worker(workers)
    dispatch = str((worker or {}).get("dispatchId") or (worker or {}).get("dispatch") or "")
    terminal = str((worker or {}).get("terminal") or "")
    worktree = _extract_worktree(worker) if worker else ""
    branch = str((worker or {}).get("branch") or (worker or {}).get("gitBranch") or "")
    if not branch and worktree:
        branch, _ = _git_head_info(worktree)
    liveness = str((worker or {}).get("liveness") or "")

    unread_types = {m["type"] for m in unread}
    if "worker_done" in unread_types:
        state = "done"
    elif "escalation" in unread_types:
        state = "escalation"
    elif "question" in unread_types:
        state = "question"
    elif worker:
        state = str(worker.get("state") or "unknown")
    else:
        state = "unknown"

    payload: dict[str, Any] = {
        "run": ns.run,
        "state": state,
        "dispatch": dispatch,
        "terminal": terminal,
        "worktree": worktree,
        "branch": branch,
        "liveness": liveness,
        "workers": workers,
        "unread": unread,
    }
    if peek_error:
        payload["peekError"] = peek_error

    _emit(payload)
    if not workers and not unread:
        return EXIT_NEGATIVE
    return EXIT_OK


def build_parser() -> _NeonArgParser:
    parser = _NeonArgParser(
        prog="neon-run",
        description="Blocking-free CLI to drive one Orca agy lane from any orchestrator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Create an Orca run and start one worker on a new worktree")
    p_start.add_argument("--repo", required=True, help="Path to a git repo registered in Orca")
    p_start.add_argument("--spec", required=True, help="Path to the worker spec file")
    p_start.add_argument("--base", default=DEFAULT_BASE, help=f"Base branch ref (default: {DEFAULT_BASE})")
    p_start.add_argument("--name", default=None, help="Worker/worktree name (default: derived from spec)")
    p_start.add_argument("--model", default=DEFAULT_MODEL, help=f"Model ID (default: {DEFAULT_MODEL})")
    p_start.add_argument("--agent", default=DEFAULT_AGENT, help=f"Agent name (default: {DEFAULT_AGENT})")
    p_start.add_argument("--objective", default=None, help="Run objective text")
    p_start.set_defaults(func=cmd_start)

    p_wait = sub.add_parser("wait", help="Wait up to timeout (max 9m50s) for worker_done, question, or escalation")
    p_wait.add_argument("run", help="Orca run ID")
    p_wait.add_argument(
        "--timeout",
        default=DEFAULT_WAIT_TIMEOUT,
        help=f"Max wait duration, capped at 9m50s (default: {DEFAULT_WAIT_TIMEOUT})",
    )
    p_wait.set_defaults(func=cmd_wait)

    p_reply = sub.add_parser("reply", help="Answer the worker's open question via its dispatch")
    p_reply.add_argument("run", help="Orca run ID")
    p_reply.add_argument("--text", required=True, help="Reply text to send to the worker dispatch")
    p_reply.set_defaults(func=cmd_reply)

    p_status = sub.add_parser("status", help="Inspect worker state without consuming messages (--peek)")
    p_status.add_argument("run", help="Orca run ID")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
        return int(ns.func(ns))
    except UsageError as exc:
        sys.stderr.write(f"neon-run: usage: {exc}\n")
        return EXIT_USAGE
    except OrcaUnreachableError as exc:
        sys.stderr.write(f"neon-run: orca unreachable: {exc}\n")
        return EXIT_UNREACHABLE
    except OrcaCommandError as exc:
        sys.stderr.write(f"neon-run: error: {exc}\n")
        return EXIT_NEGATIVE


if __name__ == "__main__":
    raise SystemExit(main())
