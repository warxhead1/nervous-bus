#!/usr/bin/env python3
"""needs-you: one cross-project queue of what agent sessions need from the operator.

The operator stops reading full turn output. Every Claude Code turn ends with a
'Needs you' block; this script is both the Stop hook that harvests it and the CLI
that reads the queue.

  needs_you.py hook          Stop hook (stdin = hook JSON). Upserts an errand
                             decision (ny-<session_id>) in Hearth, publishes
                             agent.attention.v1, and blocks the stop ONCE if a
                             turn that used tools ended without the block.
  needs_you.py [list]        Open errand decisions from Hearth (offline cache
                             fallback). --all includes settled entries.
  needs_you.py ack <n|all|session-id|decision-id> [--via SURFACE] [--answer TEXT]...
                             Settle entry n (from list), a session's entry, or
                             every entry via POST /api/decisions/{id}/respond
                             (done: true, via: terminal). Each --answer sends
                             text; if hearth does not take the answer, exit 3
                             and the ask stays open.
  needs_you.py migrate [--dry-run]
                             One-time migration: post open local session files
                             (<= 7 days old) as errand decisions and withdraw
                             local entries older than 7 days.
  needs_you.py --json        Raw open entries.
  needs_you.py bind          SessionStart hook (stdin = hook JSON): record which
                             terminal this session runs in. The Stop hook does the
                             same every turn.
  needs_you.py name [slug]   Name this session (CLAUDE_CODE_SESSION_ID) with a short
                             slug that phone, Slack and Orca show instead of a uuid.
                             No slug: print the current name.

Cache: one file per session under $NEEDS_YOU_HOME (default
~/.local/state/needs-you/sessions/), used as an offline cache and pruned once
its decision settles.
Headless runs (CLAUDE_CODE_ENTRYPOINT=sdk-*, i.e. `claude -p` and the Agent SDK)
are skipped: their output is parsed by scripts, and no human reads the turn.
Kill switch: NEEDS_YOU_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("NEEDS_YOU_HOME", Path.home() / ".local/state/needs-you"))
SESSIONS = HOME / "sessions"
NAMES = HOME / "names"
# Session -> terminal bindings, read by hearth-api to route answers (it cannot read
# /proc/<pid>/environ from its ProtectHome sandbox; the session can write its own).
BINDINGS = Path(os.environ.get("HEARTH_BINDINGS_DIR",
                               Path.home() / ".local/state/hearth/session-bindings"))
SLUG_MAX = 32
HOOK_TIMEOUT = 0.2
CLI_TIMEOUT = 2.0
MIGRATE_MAX_AGE_DAYS = 7.0

HEADER_RE = re.compile(
    r"^\s*(?:#+\s*)?\**\s*needs\s+you\s*(?:\*\*)?\s*(?:\([^)]{0,120}\))?\s*(?:\*\*\s*:|:\s*\*\*|:)\s*(.*)$",
    re.I,
)
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
STRETCH_RE = re.compile(r"\{\{([^{}|]*)\|([^{}]*)\}\}")
NOTHING_RE = re.compile(r"^(nothing|none|n/a|—)(\W|$)", re.I)
TRAILING_LINK_RE = re.compile(r" <hearth://[^\s<>]+>$")
INLINE_LINK_RE = re.compile(r"<?(hearth://[^\s<>)\]]+)>?")
DECISION_ID_RE = re.compile(r"\b((?:dec|src)-[A-Za-z0-9][A-Za-z0-9_-]*)")
PROSE_PATH_RE = re.compile(r"hearth\s*(?:→|->)", re.I)
PROSE_PATH_HINT = ("needs-you: name the exact tap path or file a decision; "
                   "project slugs are not screens")

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


def link_item(item: str) -> str:
    """C5: an item ends with ` <hearth://...>`. An inline hearth:// URL moves to the
    end; a bare decision id (dec-/src-) gets /decisions/{id}. Prose is left alone."""
    if TRAILING_LINK_RE.search(item):
        return item
    m = INLINE_LINK_RE.search(item)
    if m:
        url = m.group(1).rstrip(".,;:")
        rest = INLINE_LINK_RE.sub("", item)
        rest = re.sub(r"\s{2,}", " ", rest).strip()
        return f"{rest} <{url}>"
    m = DECISION_ID_RE.search(item)
    if m:
        return f"{item} <hearth://decisions/{m.group(1).rstrip('-_')}>"
    return item


def prose_path_hint(session_id: str, items: list[str]) -> None:
    """Once per session, stderr only: an item says 'Hearth -> ...' with no link."""
    if not any(PROSE_PATH_RE.search(i) and "hearth://" not in i for i in items):
        return
    marker = HOME / "hinted" / session_id
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
    except OSError:
        pass
    print(PROSE_PATH_HINT, file=sys.stderr)


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
    return [link_item(i) for i in items if not NOTHING_RE.match(i)]


CACHE_TAIL_BYTES = 1 << 20


def cache_from_transcript(path: str) -> dict | None:
    """Prompt-cache state after the last model call: when it goes cold, and how much
    context a reply after that point re-reads at full price.

    TTL comes from the call's own cache writes (ephemeral_1h vs ephemeral_5m);
    a call that only read the cache inherits the TTL of the nearest earlier write.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - CACHE_TAIL_BYTES))
            raw = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    last = None
    ttl = 0
    for line in reversed(raw):
        if '"usage"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        usage = (e.get("message") or {}).get("usage")
        if e.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        if last is None:
            last = (e, usage)
        writes = usage.get("cache_creation") or {}
        if writes.get("ephemeral_1h_input_tokens"):
            ttl = 3600
        elif writes.get("ephemeral_5m_input_tokens"):
            ttl = 300
        if ttl:
            break
    if last is None:
        return None
    e, usage = last
    try:
        at = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None
    ttl = ttl or 300
    context = sum(int(usage.get(k) or 0) for k in
                  ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return {
        "last_call_at": datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_s": ttl,
        "warm_until": datetime.fromtimestamp(at + ttl, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context_tokens": context,
        "model": (e.get("message") or {}).get("model") or "",
    }


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


GOAL_LOOP_AT = 3
GOAL_FEEDBACK_RE = re.compile(r"^Stop hook feedback:\s*\n?\[")


def _prompt_text(content) -> str | None:
    """The text of a real prompt, or None for a tool_result carrier."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return None


def goal_noop_streak(lines: list[str]) -> int:
    """How many turns at the end of the transcript were opened by the /goal checker
    ("Stop hook feedback: [<condition>]: Not satisfied", with a goal_status attachment
    that is not met) and made no tool call. A turn opened by anything else, or one
    that used a tool, ends the streak."""
    turns: list[dict] = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind = e.get("type")
        if kind == "user":
            txt = _prompt_text((e.get("message") or {}).get("content"))
            if txt is not None:
                turns.append({"feedback": bool(GOAL_FEEDBACK_RE.match(txt)), "goal": False, "tools": False})
        elif kind == "attachment" and turns:
            att = e.get("attachment") or {}
            if att.get("type") == "goal_status" and att.get("met") is False:
                turns[-1]["goal"] = True
        elif kind == "assistant" and turns:
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_use" for c in content
            ):
                turns[-1]["tools"] = True
    streak = 0
    for t in reversed(turns):
        if not (t["feedback"] and t["goal"]) or t["tools"]:
            break
        streak += 1
    return streak


def goal_loop_streak(path: str) -> int:
    try:
        return goal_noop_streak(Path(path).read_text(errors="replace").splitlines())
    except OSError:
        return 0


GOAL_LOOP_ITEM = ("Goal checker is looping: {n} re-opens in a row with no tool call. "
                  "The session is stopped. Clear or pause the goal, or give it the next step.")


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


def bind(session_id: str, cwd: str, turn: str = "", transcript: str = "") -> None:
    """Write {session_id, orca_terminal, tmux_pane, cwd, pid} for this session.

    `turn` is "busy" (UserPromptSubmit) or "idle" (SessionStart, a Stop that
    lets the turn end), stamped `turn_ts`, so hearth knows a session can take
    a message now instead of guessing from transcript mtime. "" keeps the
    recorded turn state.
    """
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
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = {}
    for k in ("turn", "turn_ts", "transcript"):
        if k in old:
            rec[k] = old[k]
    if transcript:
        rec["transcript"] = transcript
    if turn:
        rec["turn"] = turn
        rec["turn_ts"] = rec["ts"]
    if old and old | {"ts": rec["ts"]} == rec:
        return  # unchanged: no write
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


def prune_entry(session_id: str) -> None:
    if not session_id:
        return
    try:
        (SESSIONS / f"{session_id}.json").unlink()
    except OSError:
        pass


def decision_id_for(session_id: str) -> str:
    return session_id if session_id.startswith("ny-") else f"ny-{session_id}"


def _hearth_bases() -> list[str]:
    env_url = (os.environ.get("HEARTH_API_URL") or "").strip()
    if env_url:
        return [env_url.rstrip("/")]
    return ["http://localhost:8420", "https://localhost:8421"]


def _is_kind_unsupported(status: int | None, body: dict | None) -> bool:
    return status in (400, 422)


def entry_to_decision_payload(entry: dict) -> dict:
    """Build POST /api/decisions payload for an errand decision (ny-<session_id>)."""
    items = entry.get("items") or []
    first = items[0] if items else (entry.get("summary") or "Needs your attention")
    title = (first if len(items) == 1 else (entry.get("summary") or first))[:200]
    say = (entry.get("summary") or first)[:140]
    context = "\n".join(f"- {item}" for item in items)
    parts = [{"id": f"p{i}", "label": item} for i, item in enumerate(items, 1)]
    metadata: dict = {
        "source": "needs-you",
        "cwd": entry.get("cwd", ""),
        "orca_terminal": entry.get("orca_terminal", ""),
        "name": entry.get("name", ""),
        "summary": entry.get("summary", ""),
        "ts": entry.get("ts"),
    }
    if entry.get("cache"):
        metadata["cache"] = entry["cache"]
    if len(json.dumps(metadata).encode("utf-8")) > 4096:
        metadata.pop("summary", None)
        metadata.pop("cwd", None)
    return {
        "decision_id": decision_id_for(entry["session_id"]),
        "project": entry.get("project") or "unknown",
        "session_id": entry["session_id"],
        "kind": "errand",
        "relay_mode": "session_text",
        "title": title,
        "say": say,
        "context": context,
        "parts": parts,
        "audience": "owner",
        "metadata": metadata,
    }


def _parse_ts(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val:
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def decision_to_entry(d: dict) -> dict:
    """Convert a Hearth decision JSON row into a needs-you entry dict."""
    did = str(d.get("decision_id") or "")
    sid = str(d.get("session_id") or (did[3:] if did.startswith("ny-") else did))
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    parts = d.get("parts") or []
    items = [p["label"] for p in parts if isinstance(p, dict) and p.get("label")]
    if not items:
        ctx = str(d.get("context") or "").strip()
        if ctx:
            items = [re.sub(r"^\s*[-*•]\s+", "", line).strip()
                     for line in ctx.splitlines() if line.strip()]
        elif d.get("title"):
            items = [str(d["title"])]
    ts = _parse_ts(d.get("created_at") or meta.get("ts"))
    return {
        "decision_id": did,
        "session_id": sid,
        "project": str(d.get("project") or "unknown"),
        "cwd": str(meta.get("cwd") or ""),
        "items": items,
        "summary": str(meta.get("summary") or d.get("title") or ""),
        "orca_terminal": str(meta.get("orca_terminal") or ""),
        "name": str(meta.get("name") or ""),
        "cache": meta.get("cache"),
        "ts": ts,
        "acked": d.get("status", "pending") != "pending",
        "status": d.get("status", "pending"),
    }


class _HearthClient:
    """Thin stdlib HTTP client for Hearth /api/decisions with a strict deadline."""

    def __init__(self, timeout: float = CLI_TIMEOUT, total_budget: float | None = None):
        self.timeout = timeout
        self.deadline = (time.monotonic() + total_budget) if total_budget is not None else None
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._base: str | None = None
        self._token: str | None = None

    def _remaining(self) -> float | None:
        if self.deadline is None:
            return self.timeout
        rem = self.deadline - time.monotonic()
        if rem <= 0.005:
            return None
        return min(self.timeout, rem)

    def _raw(self, method: str, url: str, body: dict | None, token: str | None,
             timeout: float) -> tuple[int | None, dict | None]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                raw = r.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except ValueError:
                    parsed = {}
                return r.status, parsed
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {}
            return exc.code, parsed
        except Exception:
            return None, None

    def _resolve(self) -> tuple[str | None, str | None]:
        if self._base is not None:
            return self._base, self._token
        env_token = (os.environ.get("HEARTH_API_TOKEN") or "").strip()
        for base in _hearth_bases():
            rem = self._remaining()
            if rem is None:
                return None, None
            if env_token:
                self._base, self._token = base, env_token
                return self._base, self._token
            status, parsed = self._raw(
                "POST", base + "/api/auth/local-token", {"subject": "needs-you"}, None, rem,
            )
            if status and 200 <= status < 300 and isinstance(parsed, dict) and parsed.get("access_token"):
                self._base, self._token = base, parsed["access_token"]
                return self._base, self._token
        return None, None

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int | None, dict | None]:
        env_token = (os.environ.get("HEARTH_API_TOKEN") or "").strip()
        if self._base is not None:
            rem = self._remaining()
            if rem is None:
                return None, None
            return self._raw(method, self._base + path, body, self._token, rem)
        bases = _hearth_bases()
        for base in bases:
            rem = self._remaining()
            if rem is None:
                return None, None
            token = env_token
            if not token:
                st, parsed = self._raw(
                    "POST", base + "/api/auth/local-token", {"subject": "needs-you"}, None, rem,
                )
                if not (st and 200 <= st < 300 and isinstance(parsed, dict) and parsed.get("access_token")):
                    continue
                token = parsed["access_token"]
                rem = self._remaining()
                if rem is None:
                    return None, None
            st, parsed = self._raw(method, base + path, body, token, rem)
            if st is None:
                continue
            self._base, self._token = base, token
            return st, parsed
        return None, None

    def get(self, path: str) -> tuple[int | None, dict | None]:
        return self.request("GET", path, None)

    def post(self, path: str, body: dict) -> tuple[int | None, dict | None]:
        return self.request("POST", path, body)


def replay_pending(client: _HearthClient, skip_session: str = "") -> int:
    """Replay cached offline mutations onto Hearth once it is reachable again."""
    if not SESSIONS.is_dir():
        return 0
    replayed = 0
    for f in sorted(SESSIONS.glob("*.json")):
        if client._remaining() is None:
            break
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not e.get("pending_sync"):
            continue
        sid = e.get("session_id") or f.stem
        if sid == skip_session:
            continue
        did = e.get("decision_id") or decision_id_for(sid)
        if e.get("acked"):
            st, _ = client.post(
                f"/api/decisions/{urllib.parse.quote(did, safe='')}/respond",
                {"done": True, "via": e.get("acked_via") or "terminal"},
            )
            if st is None:
                break
            if (200 <= st < 300) or st in (404, 409):
                prune_entry(sid)
                replayed += 1
        elif e.get("items"):
            st, body = client.post("/api/decisions", entry_to_decision_payload(e))
            if st is None or _is_kind_unsupported(st, body):
                break
            if 200 <= st < 300:
                write_entry({**e, "decision_id": did, "synced": True, "pending_sync": False})
                replayed += 1
        else:
            st, body = client.post(
                f"/api/decisions/{urllib.parse.quote(did, safe='')}/withdraw",
                {"reason": "needs-you: nothing"},
            )
            if st is None or _is_kind_unsupported(st, body):
                break
            if (200 <= st < 300) or st in (404, 409):
                prune_entry(sid)
                replayed += 1
    return replayed


def sync_entry_to_hearth(entry: dict, timeout: float = HOOK_TIMEOUT) -> str:
    """Upsert or withdraw ny-<session_id> in Hearth, updating the local offline cache."""
    client = _HearthClient(timeout=timeout, total_budget=timeout)
    sid = entry["session_id"]
    did = decision_id_for(sid)
    if entry.get("items"):
        st, body = client.post("/api/decisions", entry_to_decision_payload(entry))
        if st and 200 <= st < 300:
            write_entry({**entry, "decision_id": did, "synced": True, "pending_sync": False})
            replay_pending(client, skip_session=sid)
            return "synced"
        if _is_kind_unsupported(st, body):
            write_entry(entry)
            return "fallback_kind"
        write_entry({**entry, "decision_id": did, "synced": False, "pending_sync": True})
        return "cached_offline"
    st, body = client.post(
        f"/api/decisions/{urllib.parse.quote(did, safe='')}/withdraw",
        {"reason": "needs-you: nothing"},
    )
    if st and ((200 <= st < 300) or st in (404, 409)):
        prune_entry(sid)
        replay_pending(client, skip_session=sid)
        return "withdrawn"
    if _is_kind_unsupported(st, body):
        write_entry(entry)
        return "fallback_kind"
    write_entry({**entry, "decision_id": did, "items": [], "synced": False, "pending_sync": True})
    return "cached_offline_withdraw"


def publish(entry: dict) -> None:
    payload = {k: entry[k] for k in ("session_id", "project", "cwd", "items", "summary", "orca_terminal")}
    if entry.get("name"):
        payload["name"] = entry["name"]
    if entry.get("cache"):
        payload["cache"] = entry["cache"]
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
    transcript = data.get("transcript_path") or ""
    out = turn_end(data, session_id, transcript)
    # Only a Stop that ends the turn makes the session idle; a block continues it.
    busy = bool(out) and out.get("decision") == "block"
    bind(session_id, data.get("cwd") or os.getcwd(), "busy" if busy else "idle", transcript)
    return out


def turn_end(data: dict, session_id: str, transcript: str) -> dict | None:
    reply, used_tools, turn_id = turn_from_transcript(transcript)
    if data.get("last_assistant_message"):
        reply = data["last_assistant_message"]
    items = extract(reply)
    if items is None:
        if not used_tools:
            streak = goal_loop_streak(transcript)
            if streak >= GOAL_LOOP_AT:
                log("goal_loop_stop", data, streak=streak)
                return {"continue": False, "stopReason": GOAL_LOOP_ITEM.format(n=streak)}
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
        "cache": cache_from_transcript(data.get("transcript_path") or ""),
        "ts": time.time(),
        "acked": False,
    }
    streak = goal_loop_streak(transcript) if not used_tools else 0
    if streak >= GOAL_LOOP_AT:
        loop_item = GOAL_LOOP_ITEM.format(n=streak)
        if loop_item not in entry["items"]:
            entry["items"] = [loop_item] + entry["items"][:19]
    sync_status = sync_entry_to_hearth(entry)
    publish(entry)
    prose_path_hint(session_id, entry["items"])
    log("recorded", data, items=len(entry["items"]), sync=sync_status)
    if streak >= GOAL_LOOP_AT:
        log("goal_loop_stop", data, streak=streak)
        # continue:false ends the turn even when another Stop hook (the goal checker) blocks.
        return {"continue": False, "stopReason": GOAL_LOOP_ITEM.format(n=streak)}
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


def _prune_settled_cache(pending_ids: set[str]) -> None:
    if not SESSIONS.is_dir():
        return
    for f in SESSIONS.glob("*.json"):
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if e.get("synced") and not e.get("pending_sync"):
            sid = e.get("session_id") or f.stem
            did = e.get("decision_id") or decision_id_for(sid)
            if did not in pending_ids:
                prune_entry(sid)


def list_entries(include_acked: bool = False, timeout: float = CLI_TIMEOUT) -> list[dict]:
    """Fetch open errand decisions from Hearth; fall back to local cache offline."""
    client = _HearthClient(timeout=timeout)
    replay_pending(client)
    path = "/api/decisions" if include_acked else "/api/decisions?status=pending"
    st, body = client.get(path)
    if st and 200 <= st < 300 and isinstance(body, dict) and isinstance(body.get("decisions"), list):
        errands = [
            d for d in body["decisions"]
            if isinstance(d, dict) and (
                d.get("kind") == "errand" or str(d.get("decision_id") or "").startswith("ny-")
            )
        ]
        entries = [decision_to_entry(d) for d in errands]
        if not include_acked:
            _prune_settled_cache({e["decision_id"] for e in entries})
        else:
            _prune_settled_cache({e["decision_id"] for e in entries if not e.get("acked")})
        return sorted(entries, key=lambda e: e["ts"])
    return load(include_acked)


def ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def cmd_list(include_acked: bool) -> int:
    entries = list_entries(include_acked)
    if not entries:
        print("Nothing needs you.")
        return 0
    for n, e in enumerate(entries, 1):
        mark = " (acked)" if e.get("acked") else ""
        print(f"[{n}] {e['project']}  {ago(e['ts'])} ago  {e.get('name') or e['session_id'][:8]}{mark}")
        for item in e["items"]:
            print(f"    - {item}")
    return 0


def hearth_ack(session_id: str, answers: list[str] | None = None, items: list[str] | None = None,
               via: str = "cli") -> bool:
    """Legacy fallback: POST /api/attention/{id}/ack?via=<via>. True only on 2xx."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    bases = _hearth_bases()

    def call(base, path, body, token=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base.rstrip("/") + path, json.dumps(body).encode(),
                                     headers, method="POST")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            return r.status, r.read()

    for base in bases:
        try:
            token = os.environ.get("HEARTH_API_TOKEN")
            if not token:
                _, raw = call(base, "/api/auth/local-token", {"subject": "needs-you-cli"})
                token = json.loads(raw).get("access_token")
            if not token:
                continue
            body = {}
            if answers:
                body = {"answers": answers, "items": items or []}
            q = urllib.parse.quote(via, safe="")
            status, _ = call(base, f"/api/attention/{session_id}/ack?via={q}", body, token)
            if 200 <= status < 300:
                return True
        except Exception:  # unreachable, 403, bad JSON: try the next base
            continue
    return False


def hearth_respond(decision_id: str, answers: list[str] | None = None, via: str = "terminal",
                   timeout: float = CLI_TIMEOUT) -> tuple[str, dict | None]:
    """POST /api/decisions/{id}/respond with done: true (and text if answers given)."""
    client = _HearthClient(timeout=timeout)
    body: dict = {"done": True, "via": via}
    if answers:
        body["text"] = "\n".join(answers)
    st, parsed = client.post(
        f"/api/decisions/{urllib.parse.quote(decision_id, safe='')}/respond",
        body,
    )
    if st is None:
        return "unreachable", None
    if 200 <= st < 300:
        return "ok", parsed
    if st == 409:
        return "conflict", parsed
    if st == 404 or _is_kind_unsupported(st, parsed):
        return "fallback", parsed
    return "error", parsed


def cmd_ack(which: str, answers: list[str] | None = None, via: str = "terminal") -> int:
    entries = list_entries(include_acked=False)
    targets = entries if which == "all" else []
    if which != "all" and not which.isdigit():  # a session id or decision id
        targets = [
            e for e in entries
            if e.get("session_id") == which
            or e.get("decision_id") == which
            or decision_id_for(e.get("session_id", "")) == which
        ]
        if not targets:
            print(f"no open entry for session {which!r}", file=sys.stderr)
            return 1
    elif which != "all":
        try:
            targets = [entries[int(which) - 1]]
        except (ValueError, IndexError):
            print(f"no open entry {which!r}; run `needs-you` to list", file=sys.stderr)
            return 1
    if answers and (which == "all" or len(targets) != 1):
        print("--answer needs one target: a session id or an entry number", file=sys.stderr)
        return 1
    if answers:
        # An answer exists only once hearth takes it: on failure the ask stays
        # open, so the answering surface can say so and retry.
        e = targets[0]
        did = e.get("decision_id") or decision_id_for(e["session_id"])
        outcome, body = hearth_respond(did, answers, via)
        if outcome == "fallback":
            if hearth_ack(e["session_id"], answers, e.get("items") or [], via):
                prune_entry(e["session_id"])
                print("answered 1")
                return 0
            outcome = "unreachable"
        if outcome in ("ok", "conflict"):
            prune_entry(e["session_id"])
            if outcome == "conflict":
                print("already settled")
            else:
                print("answered 1")
            return 0
        print("hearth unreachable or refused: answer not sent; the ask stays open",
              file=sys.stderr)
        return 3

    failed = 0
    for e in targets:
        did = e.get("decision_id") or decision_id_for(e["session_id"])
        outcome, _ = hearth_respond(did, None, via)
        if outcome == "fallback":
            if hearth_ack(e["session_id"], None, e.get("items") or [], via):
                outcome = "ok"
            else:
                outcome = "unreachable"
        if outcome in ("ok", "conflict"):
            prune_entry(e["session_id"])
        else:
            write_entry({**e, "decision_id": did, "acked": True, "acked_via": via, "pending_sync": True})
            failed += 1
    print(f"acked {len(targets)}")
    if failed:
        print(f"hearth unreachable or refused: phone row not cleared for {failed} "
              "session(s); local ack kept")
    return 0


def cmd_migrate(dry_run: bool = False, max_age_days: float = MIGRATE_MAX_AGE_DAYS) -> int:
    """Post every open local session file <= 7d old as an errand decision; withdraw > 7d."""
    if not SESSIONS.is_dir():
        print("migrated: 0 posted, 0 withdrawn (>7d), 0 skipped")
        return 0
    now = time.time()
    cutoff = max_age_days * 86400.0
    client = None if dry_run else _HearthClient(timeout=CLI_TIMEOUT)
    posted = 0
    withdrawn = 0
    skipped = 0
    for f in sorted(SESSIONS.glob("*.json")):
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            skipped += 1
            continue
        if not e.get("items") or e.get("acked"):
            skipped += 1
            continue
        sid = e.get("session_id") or f.stem
        e["session_id"] = sid
        did = decision_id_for(sid)
        age_s = now - float(e.get("ts") or 0)
        if age_s > cutoff:
            if not dry_run and client is not None:
                st, body = client.post(
                    f"/api/decisions/{urllib.parse.quote(did, safe='')}/withdraw",
                    {"reason": f"migrated: older than {int(max_age_days)} days"},
                )
                if st is None:
                    print("needs-you migrate: hearth unreachable", file=sys.stderr)
                    return 1
                if _is_kind_unsupported(st, body):
                    print("needs-you migrate: hearth rejected request (errand unsupported)",
                          file=sys.stderr)
                    return 1
                prune_entry(sid)
            withdrawn += 1
        else:
            if not dry_run and client is not None:
                st, body = client.post("/api/decisions", entry_to_decision_payload(e))
                if st is None:
                    print("needs-you migrate: hearth unreachable", file=sys.stderr)
                    return 1
                if _is_kind_unsupported(st, body):
                    print("needs-you migrate: hearth does not support errand decisions yet",
                          file=sys.stderr)
                    return 1
                if not (200 <= st < 300):
                    print(f"needs-you migrate: unexpected HTTP {st} for {sid}", file=sys.stderr)
                    return 1
                write_entry({**e, "decision_id": did, "synced": True, "pending_sync": False})
            posted += 1
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}migrated: {posted} posted, {withdrawn} withdrawn (>{int(max_age_days)}d), {skipped} skipped")
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
        sync_entry_to_hearth(entry)
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
    if argv[:1] == ["bind"] or argv[:2] == ["turn", "busy"]:
        try:
            data = json.load(sys.stdin)
            if not os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
                bind(
                    data.get("session_id") or "",
                    data.get("cwd") or os.getcwd(),
                    "busy" if argv[0] == "turn" else "idle",
                    data.get("transcript_path") or "",
                )
        except Exception as exc:  # a hook must never wedge the session
            print(f"needs-you bind: {exc}", file=sys.stderr)
        return 0
    if argv[:1] == ["ack"] and len(argv) >= 2:
        rest, answers, via = argv[2:], [], "terminal"
        while rest:
            flag = rest.pop(0)
            if flag not in ("--answer", "--via") or not rest:
                print(__doc__, file=sys.stderr)
                return 2
            val = rest.pop(0)
            if flag == "--answer":
                answers.append(val)
            elif re.fullmatch(r"[a-z][a-z0-9-]{0,31}", val):
                via = val
            else:
                print(f"bad --via {val!r}", file=sys.stderr)
                return 2
        return cmd_ack(argv[1], answers, via)
    if argv[:1] == ["migrate"] and set(argv[1:]).issubset({"--dry-run"}):
        return cmd_migrate(dry_run="--dry-run" in argv[1:])
    if argv[:1] == ["name"] and len(argv) <= 2:
        return cmd_name(argv[1] if len(argv) == 2 else None)
    if "--json" in argv:
        print(json.dumps(list_entries("--all" in argv), indent=1))
        return 0
    if argv in ([], ["list"], ["--all"], ["list", "--all"]):
        return cmd_list("--all" in argv)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
