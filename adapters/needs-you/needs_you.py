#!/usr/bin/env python3
"""needs-you: one cross-project queue of what agent sessions need from the operator.

The operator stops reading full turn output. Every Claude Code turn ends with a
'Needs you' block; this script is both the Stop hook that harvests it and the CLI
that reads the queue.

  needs_you.py hook          Stop hook (stdin = hook JSON). Records the block,
                             publishes agent.attention.v1, and blocks the stop ONCE
                             if a turn that used tools ended without the block.
  needs_you.py [list]        Open items, grouped by project. --all includes acked.
  needs_you.py ack <n|all>   Mark entry n (from list) or every entry as handled.
  needs_you.py --json        Raw open entries.
  needs_you.py bind          SessionStart hook (stdin = hook JSON): record which
                             terminal this session runs in. The Stop hook does the
                             same every turn.
  needs_you.py name [slug]   Name this session (CLAUDE_CODE_SESSION_ID) with a short
                             slug that phone, Slack and Orca show instead of a uuid.
                             No slug: print the current name.

State: one file per session under $NEEDS_YOU_HOME (default
~/.local/state/needs-you/sessions/). Latest turn per session wins, so a later
"Needs you: nothing" clears that session's earlier asks.
Headless runs (CLAUDE_CODE_ENTRYPOINT=sdk-*, i.e. `claude -p` and the Agent SDK)
are skipped: their output is parsed by scripts, and no human reads the turn.
Kill switch: NEEDS_YOU_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("NEEDS_YOU_HOME", Path.home() / ".local/state/needs-you"))
SESSIONS = HOME / "sessions"
NAMES = HOME / "names"
# Session -> terminal bindings, read by hearth-api to route answers (it cannot read
# /proc/<pid>/environ from its ProtectHome sandbox; the session can write its own).
BINDINGS = Path(os.environ.get("HEARTH_BINDINGS_DIR",
                               Path.home() / ".local/state/hearth/session-bindings"))
SLUG_MAX = 32

HEADER_RE = re.compile(
    r"^\s*(?:#+\s*)?\**\s*needs\s+you\s*(?:\*\*)?\s*(?:\([^)]{0,120}\))?\s*(?:\*\*\s*:|:\s*\*\*|:)\s*(.*)$",
    re.I,
)
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
STRETCH_RE = re.compile(r"\{\{([^{}|]*)\|([^{}]*)\}\}")
NOTHING_RE = re.compile(r"^(nothing|none|n/a|—)(\W|$)", re.I)

BLOCK_REASON = (
    "This turn used tools but the reply has no 'Needs you' block. End the reply with "
    "'**Needs you:**' followed by one bullet per decision or action only the user can "
    "take, or '**Needs you:** nothing'. Keep it outside any {{stretch}}. Do not redo "
    "the work or repeat the summary; add the block only."
)


def unstretch(text: str) -> str:
    """Resolve cc-stretch {{short|long}} markup to its long form, innermost first."""
    prev = None
    while prev != text:
        prev, text = text, STRETCH_RE.sub(r"\2", text)
    return text


def extract(reply: str) -> list[str] | None:
    """Items from the LAST 'Needs you' block; [] for 'nothing'; None if absent."""
    lines = unstretch(reply).splitlines()
    start = None
    for i, line in enumerate(lines):
        if HEADER_RE.match(line):
            start = i
    if start is None:
        return None
    rest = HEADER_RE.match(lines[start]).group(1).strip().strip("*").strip()
    items: list[str] = []
    if rest and not NOTHING_RE.match(rest):
        items.append(rest)
    for line in lines[start + 1:]:
        m = BULLET_RE.match(line)
        if m:
            items.append(m.group(1).strip())
        elif line.strip():
            break
    return [i for i in items if not NOTHING_RE.match(i)]


def turn_from_transcript(path: str) -> tuple[str, bool, str]:
    """(final reply, whether this turn used tools, id of the prompt that opened it)."""
    try:
        raw = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return "", False, ""
    entries = []
    turn_id = ""
    for line in reversed(raw):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind = e.get("type")
        content = (e.get("message") or {}).get("content")
        if kind == "user":
            if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                entries.append(e)
                continue
            turn_id = e.get("uuid") or e.get("timestamp") or ""
            break  # a real prompt: start of this turn
        if kind == "assistant":
            entries.append(e)
    entries.reverse()
    used_tools = False
    texts: list[str] = []
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list) or e.get("type") != "assistant":
            continue
        for c in content:
            if c.get("type") == "tool_use":
                used_tools = True
                texts = []  # only text after the last tool call is the reply
            elif c.get("type") == "text":
                texts.append(c.get("text", ""))
    return "\n".join(texts), used_tools, turn_id


def project_of(cwd: str) -> str:
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if common:
        p = Path(common)
        return (p.parent if p.name == ".git" else p).name
    return Path(cwd).name or "unknown"


def log(decision: str, data: dict, **extra) -> None:
    """One line per hook run, so a miss can be told apart from a hook that never ran."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "decision": decision,
               "session_id": data.get("session_id", ""), "cwd": data.get("cwd", ""), **extra}
        with open(HOME / "hook.log", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def already_blocked(session_id: str, turn_id: str) -> bool:
    """Block at most once per turn, by our own record.

    stop_hook_active is NOT used: Claude Code sets it when ANY Stop hook blocked
    earlier (claim-gate, idle-stop-check), which let the final reply skip this check.
    """
    marker = SESSIONS / f"{session_id}.blocked"
    try:
        if marker.read_text() == turn_id:
            return True
    except OSError:
        pass
    SESSIONS.mkdir(parents=True, exist_ok=True)
    marker.write_text(turn_id)
    return False


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:SLUG_MAX].rstrip("-")


def default_name(cwd: str) -> str:
    """A worktree's directory name is already a chosen label; agent-<hex> ones are not."""
    p = Path(cwd)
    if "worktrees" not in p.parts[:-1]:
        return ""
    base = p.name
    if re.fullmatch(r"agent-[0-9a-f]{6,}", base):
        return ""
    return slugify(base)


def name_of(session_id: str, cwd: str) -> str:
    try:
        chosen = (NAMES / session_id).read_text().strip()
    except OSError:
        chosen = ""
    return chosen or default_name(cwd)


def bind(session_id: str, cwd: str) -> None:
    """Write {session_id, orca_terminal, tmux_pane, cwd, pid} for this session."""
    if not session_id:
        return
    rec = {
        "session_id": session_id,
        "orca_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", ""),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "cwd": cwd,
        "pid": int(os.environ.get("CLAUDE_PID") or os.getppid()),
        "ts": time.time(),
    }
    if not (rec["orca_terminal"] or rec["tmux_pane"]):
        return
    path = BINDINGS / f"{session_id}.json"
    try:
        if json.loads(path.read_text())  | {"ts": rec["ts"]} == rec:
            return  # unchanged: no write per turn
    except (OSError, ValueError):
        pass
    try:
        BINDINGS.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=BINDINGS, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass


def write_entry(entry: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SESSIONS, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(entry, f)
    os.replace(tmp, SESSIONS / f"{entry['session_id']}.json")


def publish(entry: dict) -> None:
    payload = {k: entry[k] for k in ("session_id", "project", "cwd", "items", "summary", "orca_terminal")}
    if entry.get("name"):
        payload["name"] = entry["name"]
    try:
        subprocess.Popen(
            ["timeout", "2", "nervous", "publish", "agent.attention.v1", json.dumps(payload)],
            cwd=str(Path.home()),  # no project nervous.toml allowlist applies
            env={**os.environ, "NERVOUS_SOURCE": "/claude-code/needs-you"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def summary_of(reply: str) -> str:
    """First non-empty prose line of the reply, for the phone's one-line 'done' text."""
    for line in unstretch(reply).splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not HEADER_RE.match(line) and not BULLET_RE.match(line):
            return line[:300]
    return ""


def hook(data: dict) -> dict | None:
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        log("skip_headless", data)
        return None
    session_id = data.get("session_id") or ""
    if not session_id:
        log("skip_no_session", data)
        return None
    bind(session_id, data.get("cwd") or os.getcwd())
    reply, used_tools, turn_id = turn_from_transcript(data.get("transcript_path") or "")
    if data.get("last_assistant_message"):
        reply = data["last_assistant_message"]
    items = extract(reply)
    if items is None:
        if not used_tools:
            log("pass_chat_turn", data)
            return None
        if already_blocked(session_id, turn_id or reply[-200:]):
            log("pass_already_blocked", data, tail=reply[-160:])
            return None
        log("block_missing", data, tail=reply[-160:])
        return {"decision": "block", "reason": BLOCK_REASON}
    cwd = data.get("cwd") or os.getcwd()
    entry = {
        "session_id": session_id,
        "project": project_of(cwd),
        "cwd": cwd,
        "items": items[:20],
        "summary": summary_of(reply),
        "orca_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", ""),
        "name": name_of(session_id, cwd),
        "ts": time.time(),
        "acked": False,
    }
    write_entry(entry)
    publish(entry)
    log("recorded", data, items=len(entry["items"]))
    return None


def load(include_acked: bool = False) -> list[dict]:
    out = []
    for f in SESSIONS.glob("*.json") if SESSIONS.is_dir() else []:
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if e.get("items") and (include_acked or not e.get("acked")):
            out.append(e)
    return sorted(out, key=lambda e: e["ts"])


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def cmd_list(include_acked: bool) -> int:
    entries = load(include_acked)
    if not entries:
        print("Nothing needs you.")
        return 0
    for n, e in enumerate(entries, 1):
        mark = " (acked)" if e.get("acked") else ""
        print(f"[{n}] {e['project']}  {ago(e['ts'])} ago  {e.get('name') or e['session_id'][:8]}{mark}")
        for item in e["items"]:
            print(f"    - {item}")
    return 0


def cmd_ack(which: str) -> int:
    entries = load()
    targets = entries if which == "all" else []
    if which != "all":
        try:
            targets = [entries[int(which) - 1]]
        except (ValueError, IndexError):
            print(f"no open entry {which!r}; run `needs-you` to list", file=sys.stderr)
            return 1
    for e in targets:
        e["acked"] = True
        write_entry(e)
    print(f"acked {len(targets)}")
    return 0


def cmd_name(slug: str | None) -> int:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        print("needs-you name: CLAUDE_CODE_SESSION_ID is unset; run it from a Claude Code session",
              file=sys.stderr)
        return 1
    cwd = os.getcwd()
    if slug is None:
        print(name_of(session_id, cwd) or "(unnamed)")
        return 0
    name = slugify(slug)
    if len(name) < 2:
        print(f"needs-you name: {slug!r} has no usable slug (a-z, 0-9, -)", file=sys.stderr)
        return 1
    NAMES.mkdir(parents=True, exist_ok=True)
    (NAMES / session_id).write_text(name)
    handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    if handle:
        try:
            subprocess.run(["timeout", "5", "orca", "terminal", "rename", "--terminal", handle,
                            "--title", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    try:  # re-publish the current asks so phone and Slack relabel now, not next turn
        entry = json.loads((SESSIONS / f"{session_id}.json").read_text())
        entry["name"] = name
        write_entry(entry)
        publish(entry)
    except (OSError, ValueError):
        pass
    print(name)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"]:
        if os.environ.get("NEEDS_YOU_DISABLE") == "1":
            return 0
        try:
            out = hook(json.load(sys.stdin))
        except Exception as exc:  # a hook must never wedge the session
            print(f"needs-you: {exc}", file=sys.stderr)
            return 0
        if out:
            print(json.dumps(out))
        return 0
    if argv[:1] == ["bind"]:
        try:
            data = json.load(sys.stdin)
            if not os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
                bind(data.get("session_id") or "", data.get("cwd") or os.getcwd())
        except Exception as exc:  # a hook must never wedge the session
            print(f"needs-you bind: {exc}", file=sys.stderr)
        return 0
    if argv[:1] == ["ack"] and len(argv) == 2:
        return cmd_ack(argv[1])
    if argv[:1] == ["name"] and len(argv) <= 2:
        return cmd_name(argv[1] if len(argv) == 2 else None)
    if "--json" in argv:
        print(json.dumps(load("--all" in argv), indent=1))
        return 0
    if argv in ([], ["list"], ["--all"], ["list", "--all"]):
        return cmd_list("--all" in argv)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
