"""struggle_ledger.py — longitudinal, cross-agent friction telemetry.

The quality detectors answer "did the agent verify its work?". This answers a
different, more actionable question the scorecards throw away: **what are the
agents actually FIGHTING, is it shared across many of them, and has it been
fixed?** A GPU lock that 22 sessions thrash on for a month is invisible to a
"79% verified" baseline but obvious here.

It reads the DURABLE transcript archive (mirrored by transcript_snapshot.py — the
full content the capped bus tier never had), classifies records against a set of
``StruggleClass`` patterns (generic friction from the engine + project-specific
ones from private adapters), and for each struggle builds a timeline:

  * how many events, across how many distinct sessions/agents
  * first seen / last seen, daily sparkline
  * STATUS — open (still happening) / dormant / resolved
  * FIX VERDICT — correlate the friction curve against remediation events
    (fix-commits, bead closes) to score whether a claimed fix actually dropped it

This turns "here is a wall" into "here is a wall nobody has torn down" (or "this
one was fixed on May 31 and stayed fixed").

Outcome-vs-friction is the point: outcomes are scored elsewhere; this measures the
lived cost of the work, longitudinally and across agents.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapter_api import StruggleClass, struggle_classes_for, load_adapters  # noqa: E402

DEFAULT_SRC = os.path.expanduser("~/.cache/nervous-bus/reflex/transcripts")
FALLBACK_SRC = os.path.expanduser("~/.claude/projects")
MMX_MIRROR = os.path.join(DEFAULT_SRC, "mmx")
MMX_LIVE = os.path.expanduser("~/.mmx/projects")
CODEX_ROOTS = (
    os.path.expanduser("~/.codex/sessions"),
    os.path.expanduser("~/.config/orca/codex-accounts/*/home/sessions"),
)
GEMINI_TMP = os.path.expanduser("~/.gemini/tmp")
GEMINI_PROJECTS = os.path.expanduser("~/.gemini/projects.json")
ALL_SOURCES = ("claude", "mmx", "codex", "gemini")

# Cheap line pre-filter: only JSON-parse records that look like friction OR a
# remediation marker. Struggles are by definition error/contention text, so this
# is a large speedup with no recall loss for friction-shaped classes.
_FRICTION_TRIGGERS = ("lock", "busy", "error", "fail", "unavailable", "in use",
                      "device", "memory", "oom", "timeout", "timed out", "refused",
                      "denied", "panic", "retry", "waiting", "contention", "eaddr",
                      "device_lost", "device lost", "blocking", "stale")
_FIX_TRIGGERS = ("git commit", "bd close", "bd update", "fixed by", "resolve")

# Remediation extraction.
_COMMIT_RE = re.compile(r'git commit[^\n]*?-m\s*(["\'])(.+?)\1', re.I | re.S)
_BEADCLOSE_RE = re.compile(r'bd (?:close|update)\s+(\S+)[^\n]*?--(?:reason|notes)=(["\'])(.+?)\2', re.I | re.S)
_FIXISH = re.compile(r'\b(fix|fixed|harden|resolve|reclaim|drain|serial|mitigat|workaround|patch)\w*', re.I)
# Cheap horizon extraction: the data-window end is the most recent record of ANY
# kind, not the most recent struggle — else a lone struggle always reads "open".
_TS_RE = re.compile(r'"timestamp":\s*"(\d{4}-\d{2}-\d{2})')


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class StruggleEvent:
    name: str
    day: str          # YYYY-MM-DD
    session: str
    project: str
    snippet: str
    source: str = "claude"


@dataclass
class Remediation:
    day: str
    project: str
    kind: str         # "commit" | "bead"
    message: str


@dataclass
class StruggleRecord:
    name: str
    project: str
    source: str = "claude"
    total: int = 0
    sessions: set = field(default_factory=set)
    daily: Counter = field(default_factory=Counter)
    first: str = ""
    last: str = ""
    examples: list = field(default_factory=list)
    status: str = ""              # open | dormant | resolved
    fix_verdict: str = ""         # see correlate_fixes
    fix_evidence: str = ""


# ── scan ──────────────────────────────────────────────────────────────────────

def _project_of(cwd: str) -> str:
    m = re.search(r"/projects/([^/]+)", cwd or "")
    if not m:
        return "(unknown)"
    # collapse a worktree-suffixed clone dir (hearth-loom-main) to its repo stem only
    # when the segment itself is the project; keep as-is otherwise.
    return m.group(1)


def _blobs(rec: dict):
    """Yield (kind, text) blobs from a transcript record, skipping base64 images."""
    msg = rec.get("message", {})
    for c in (msg.get("content") or []) if isinstance(msg, dict) else []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "tool_use":
            inp = c.get("input", {}) or {}
            # RAW string values (not json.dumps) so commit/bead regexes see unescaped
            # quotes — `git commit -m "..."`, not `-m \"...\"`.
            yield ("command", "\n".join(str(v) for v in inp.values() if isinstance(v, str)))
        elif t == "tool_result":
            cc = c.get("content", "")
            s = cc if isinstance(cc, str) else json.dumps(cc)
            if not s.lstrip().startswith('[{"type": "image"') and '"base64"' not in s[:80]:
                yield ("result", s)
        elif t == "text":
            yield ("text", c.get("text", "") or "")


def _text_of(x) -> str:
    """Flatten a str / list-of-{text} / dict payload to text (no image payloads)."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "\n".join(str(i.get("text", "")) for i in x
                         if isinstance(i, dict) and isinstance(i.get("text"), str))
    if isinstance(x, dict):
        return json.dumps(x)
    return ""


def _arg_strings(args) -> str:
    """Command text from a tool-call argument blob: raw string values, so regexes
    see unescaped quotes (same contract as the Claude tool_use branch)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return args
    if isinstance(args, dict):
        parts = []
        for v in args.values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list) and all(isinstance(i, str) for i in v):
                parts.append(" ".join(v))
        return "\n".join(parts)
    return ""


def _blobs_codex(rec: dict):
    """Blobs from a Codex rollout record ``{timestamp,type,payload}``.

    Only ``response_item`` payloads carry content: function_call / custom_tool_call
    (the command), their *_output (the result), and message (user/assistant text;
    developer/system messages are injected instructions, not work, so skipped).
    """
    if rec.get("type") != "response_item":
        return
    p = rec.get("payload")
    if not isinstance(p, dict):
        return
    t = p.get("type")
    if t == "function_call":
        yield ("command", _arg_strings(p.get("arguments")))
    elif t == "custom_tool_call":
        inp = p.get("input")
        yield ("command", inp if isinstance(inp, str) else _arg_strings(inp))
    elif t in ("function_call_output", "custom_tool_call_output"):
        yield ("result", _text_of(p.get("output")))
    elif t == "message" and p.get("role") in ("user", "assistant"):
        yield ("text", _text_of(p.get("content")))


def _blobs_gemini(msg: dict):
    """Blobs from one Gemini CLI chat message (``content`` + ``toolCalls[]``)."""
    if not isinstance(msg, dict):
        return
    txt = _text_of(msg.get("content"))
    if txt:
        yield ("text", txt)
    for tc in msg.get("toolCalls") or []:
        if not isinstance(tc, dict):
            continue
        yield ("command", _arg_strings(tc.get("args")))
        for r in tc.get("result") or []:
            fr = r.get("functionResponse") if isinstance(r, dict) else None
            if not isinstance(fr, dict):
                continue
            resp = fr.get("response")
            if isinstance(resp, dict):
                for k in ("output", "error"):
                    if isinstance(resp.get(k), str):
                        yield ("result", resp[k])
            elif isinstance(resp, str):
                yield ("result", resp)


def _extract_remediation(command: str, day: str, project: str) -> list[Remediation]:
    out = []
    for m in _COMMIT_RE.finditer(command):
        msg = m.group(2)[:200]
        if _FIXISH.search(msg):
            out.append(Remediation(day, project, "commit", msg))
    for m in _BEADCLOSE_RE.finditer(command):
        msg = m.group(3)[:200]
        if _FIXISH.search(msg):
            out.append(Remediation(day, project, "bead", msg))
    return out


def _candidate_files(files: list[str], days_limit: Optional[int]) -> list[str]:
    """Bound the file set by mtime BEFORE anything opens/parses them.

    A transcript file's mtime is >= the timestamp of every record it contains
    (records are appended, and an append updates mtime), so a file whose mtime
    falls before the window's cutoff cannot hold any in-window record and is
    safe to drop without ever being opened. Only a cheap stat() per file, no
    line-by-line parsing.

    days_limit=None (the "give me everything" call) returns files unchanged —
    the unbounded read stays available, this only shrinks the bounded one.

    The cutoff is anchored on the highest mtime seen (a stat-only proxy for
    the true horizon, which normally requires parsing every line to find) with
    a +1 day buffer to absorb mtime/record-timestamp skew (TZ, a record
    written just before midnight in a file whose mtime lands just after). The
    buffer only ever makes the kept set larger, never smaller, so it cannot
    drop a file the unbounded scan would have used for the final windowed
    output — it only skips files that are provably too old to matter.
    """
    if not days_limit:
        return files
    mtimes = []
    for f in files:
        try:
            mtimes.append((f, os.path.getmtime(f)))
        except OSError:
            continue
    if not mtimes:
        return files
    newest = max(m for _, m in mtimes)
    cutoff_ts = newest - (days_limit + 1) * 86400
    return [f for f, m in mtimes if m >= cutoff_ts]


_ROLLOUT_SID_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def _default_roots(src: str) -> dict:
    return {"claude": [src], "mmx": [MMX_LIVE, MMX_MIRROR],
            "codex": list(CODEX_ROOTS), "gemini": [GEMINI_TMP]}


def _gemini_project_names(projects_file: Optional[str] = None) -> dict:
    """sha256(project root path) -> project name, from ~/.gemini/projects.json."""
    try:
        with open(projects_file or GEMINI_PROJECTS) as fh:
            projects = (json.load(fh) or {}).get("projects", {})
    except Exception:
        return {}
    return {hashlib.sha256(p.encode()).hexdigest(): n for p, n in projects.items()}


def _gather_files(sources, roots: dict) -> list[tuple[str, str]]:
    """(path, source) for every transcript file of the requested sources.

    claude: <root>/*/*.jsonl and <root>/*.jsonl.  mmx: same layout over the live dir,
    then the durable mirror for sessions mmx has since cleaned up (live wins on a
    relpath collision).  codex: rollout-*.jsonl, deduped by session id across the
    primary runtime and every Orca account root (they are hardlinked copies).
    gemini: <tmp>/*/chats/*.json[l].
    """
    out: list[tuple[str, str]] = []
    if "claude" in sources:
        for r in roots["claude"]:
            out += [(f, "claude") for f in glob.glob(os.path.join(r, "*", "*.jsonl"))
                    + glob.glob(os.path.join(r, "*.jsonl"))]
    if "mmx" in sources:
        seen: set = set()
        for r in roots["mmx"]:
            for f in glob.glob(os.path.join(r, "*", "*.jsonl")) + glob.glob(os.path.join(r, "*.jsonl")):
                rel = os.path.relpath(f, r)
                if rel not in seen:
                    seen.add(rel)
                    out.append((f, "mmx"))
    if "codex" in sources:
        seen = set()
        for pat in roots["codex"]:
            for r in sorted(glob.glob(pat)):
                for f in glob.glob(os.path.join(r, "*", "*", "*", "rollout-*.jsonl")):
                    m = _ROLLOUT_SID_RE.search(f)
                    sid = m.group(1) if m else f
                    if sid not in seen:
                        seen.add(sid)
                        out.append((f, "codex"))
    if "gemini" in sources:
        for r in roots["gemini"]:
            for ext in ("json", "jsonl"):
                out += [(f, "gemini") for f in glob.glob(os.path.join(r, "*", "chats", f"*.{ext}"))]
    return out


def _units(path: str, source: str, gem_names: dict):
    """Yield (raw, rec_or_None, ctx_or_None, blob_fn) per record of one file.

    ctx = (project, session) when the file (not each record) carries them.
    """
    if source in ("claude", "mmx"):
        with open(path, errors="replace") as fh:
            for line in fh:
                yield line, None, None, _blobs
        return
    if source == "codex":
        m = _ROLLOUT_SID_RE.search(path)
        session = (m.group(1) if m else os.path.basename(path))[:12]
        project = None
        with open(path, errors="replace") as fh:
            for i, line in enumerate(fh):
                if project is None and i < 8 and '"cwd"' in line:
                    # cwd lives in the first records (session_meta / turn_context)
                    try:
                        cwd = (json.loads(line).get("payload") or {}).get("cwd", "")
                        project = _project_of(cwd) if cwd else None
                    except Exception:
                        pass
                yield line, None, (project or "(unknown)", session), _blobs_codex
        return
    # gemini
    proj_dir = os.path.basename(os.path.dirname(os.path.dirname(path)))
    project = gem_names.get(proj_dir) or (
        "(unknown)" if re.fullmatch(r"[0-9a-f]{64}", proj_dir) else proj_dir)
    if path.endswith(".json"):
        try:
            with open(path, errors="replace") as fh:
                doc = json.load(fh)
        except Exception:
            return
        session = str(doc.get("sessionId", "") or os.path.basename(path))[:12]
        for msg in doc.get("messages") or []:
            if isinstance(msg, dict):
                yield json.dumps(msg), msg, (project, session), _blobs_gemini
        return
    session = os.path.basename(path)[:12]
    with open(path, errors="replace") as fh:
        for i, line in enumerate(fh):
            if i == 0:
                try:
                    session = str(json.loads(line).get("sessionId", "") or session)[:12]
                except Exception:
                    pass
            yield line, None, (project, session), _blobs_gemini


def scan(src: str, adapters=None, days_limit: Optional[int] = None,
         sources=("claude",), roots: Optional[dict] = None
         ) -> tuple[list[StruggleEvent], list[Remediation], str]:
    """Walk the transcripts; return (struggle_events, remediation_events, horizon).

    horizon = most recent record date across ALL records (the data-window end), used
    to decide open-vs-resolved relative to *now*, not relative to the last struggle.

    src is the Claude transcript root; ``sources`` picks which agents are read
    (default Claude only, so existing callers are unchanged) and ``roots`` overrides
    the per-source locations (tests). Every event carries its ``source``.

    days_limit, when given, is used ONLY to shrink the candidate file set via
    _candidate_files() (a stat-only mtime prefilter) before any file is opened —
    it does not change which records within the kept files are returned; the
    caller (run()) still applies the exact day-level cutoff against the real
    parsed horizon, so the final output for a given days_limit is identical to
    scanning everything and filtering afterward. See _candidate_files() for why
    the prefilter cannot drop an in-window record.
    """
    adapters = adapters if adapters is not None else load_adapters()
    classes_cache: dict[str, list[StruggleClass]] = {}
    events: list[StruggleEvent] = []
    remed: list[Remediation] = []
    horizon = ""
    roots = roots or _default_roots(src)

    tagged = dict(_gather_files(sources, roots))
    gem_names = _gemini_project_names() if "gemini" in sources else {}
    for f in _candidate_files(list(tagged), days_limit):
        source = tagged[f]
        try:
            for raw, rec, ctx, blob_fn in _units(f, source, gem_names):
                m = _TS_RE.search(raw)
                if m and m.group(1) > horizon:
                    horizon = m.group(1)
                low = raw.lower()
                has_fr = any(t in low for t in _FRICTION_TRIGGERS)
                has_fix = any(t in low for t in _FIX_TRIGGERS)
                if not (has_fr or has_fix):
                    continue
                if rec is None:
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                day = (rec.get("timestamp", "") or "")[:10]
                if not day:
                    continue
                if ctx is None:
                    project = _project_of(rec.get("cwd", ""))
                    session = (rec.get("sessionId", "") or "")[:12]
                else:
                    project, session = ctx
                classes = classes_cache.get(project)
                if classes is None:
                    classes = struggle_classes_for(project, adapters)
                    classes_cache[project] = classes
                for kind, blob in blob_fn(rec):
                    if not blob:
                        continue
                    if has_fix and kind == "command":
                        remed.extend(_extract_remediation(blob, day, project))
                    if not has_fr:
                        continue
                    bl = blob.lower()
                    if not any(t in bl for t in _FRICTION_TRIGGERS):
                        continue
                    for sc in classes:
                        if sc.pattern.search(blob):
                            events.append(StruggleEvent(
                                sc.name, day, session, project,
                                re.sub(r"\s+", " ", blob)[:140], source))
        except OSError:
            continue  # file reaped between glob and open
    return events, remed, horizon


# ── ledger ──────────────────────────────────────────────────────────────────

def _parse(d: str) -> date:
    y, m, dd = map(int, d.split("-"))
    return date(y, m, dd)


def build_ledger(events: list[StruggleEvent]) -> dict[tuple[str, str], StruggleRecord]:
    recs: dict[tuple, StruggleRecord] = {}
    for e in events:
        # Claude keeps the legacy (project, name) key; other agents get their own row
        # per source so the digest can split per agent.
        key = (e.project, e.name) if e.source == "claude" else (e.project, e.name, e.source)
        r = recs.get(key)
        if r is None:
            r = recs[key] = StruggleRecord(e.name, e.project, e.source)
        r.total += 1
        r.sessions.add(e.session)
        r.daily[e.day] += 1
        if not r.first or e.day < r.first:
            r.first = e.day
        if not r.last or e.day > r.last:
            r.last = e.day
        if len(r.examples) < 4:
            r.examples.append(f"[{e.day}] {e.snippet}")
    return recs


def _status(last: str, maxday: str) -> str:
    gap = (_parse(maxday) - _parse(last)).days
    return "open" if gap <= 2 else ("dormant" if gap <= 6 else "resolved")


def correlate_fixes(rec: StruggleRecord, remed: list[Remediation], maxday: str,
                    sc: Optional[StruggleClass] = None, window: int = 7) -> None:
    """Score whether a remediation actually dropped this struggle's frequency.

    Heuristic + honest: find remediation events (same project) whose message shares
    a keyword with the struggle, take the latest, and compare friction frequency in
    the `window` days before vs after. Verdicts:
      no_fix_found / fix_effective / fix_partial / fix_ineffective / regressed
    """
    kws = sc.keywords() if sc else tuple(t for t in rec.name.split("_") if len(t) > 2)
    cands = [r for r in remed if r.project == rec.project
             and any(k in r.message.lower() for k in kws)]
    if not cands:
        # No remediation mentioning this struggle. Honest split: resolved-on-its-own
        # vs nobody-has-touched-it.
        rec.fix_verdict = "unfixed_no_attempt" if rec.status == "open" else "resolved_no_fix_found"
        rec.fix_evidence = rec.status
        return

    # Pick the candidate that BEST EXPLAINS A DECLINE (largest before→after drop),
    # not merely the latest — a later coincidental commit must not mask the real fix.
    best = None
    for fx in cands:
        fd = _parse(fx.day)
        before = sum(n for d, n in rec.daily.items()
                     if fd - timedelta(days=window) <= _parse(d) < fd)
        after = sum(n for d, n in rec.daily.items()
                    if fd <= _parse(d) <= fd + timedelta(days=window))
        drop = before - after
        if best is None or drop > best[0]:
            best = (drop, fx, before, after)
    _, fx, before, after = best

    reduced = before > 0 and after < before * 0.4
    if rec.status in ("resolved", "dormant"):
        verdict = "fixed" if reduced else "resolved_unclear"
    elif reduced:
        verdict = "partial_still_open"        # big drop but still trickling
    else:
        verdict = "unfixed_open"              # attempted, friction did not fall
    # window truncated against the data edge → the 'after' sample is unreliable
    edge = (_parse(maxday) - _parse(fx.day)).days < window
    rec.fix_verdict = verdict
    rec.fix_evidence = (f'{fx.kind} {fx.day} "{fx.message[:46]}" '
                        f'(before={before}/after={after}{" ~edge" if edge else ""})')


# ── render ──────────────────────────────────────────────────────────────────

_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(daily: Counter, days: list[str]) -> str:
    vals = [daily.get(d, 0) for d in days]
    mx = max(vals) or 1
    return "".join(_BLOCKS[min(7, int(7 * v / mx))] if v else "·" for v in vals)


def render(recs: dict, remed: list[Remediation], maxday: str, days: list[str],
           project: Optional[str] = None) -> str:
    rows = [r for r in recs.values() if not project or r.project == project]
    rows.sort(key=lambda r: (-len(r.sessions), -r.total))
    out = [f"Struggle Ledger — window {days[0] if days else '?'}..{maxday} "
           f"({len(days)} active days)\n"]
    out.append(f"{'project':<16}{'src':<7}{'struggle':<18}{'evt':>5}{'sess':>5}  "
               f"{'last':>10} {'status':<9}{'fix verdict':<16} trend")
    for r in rows:
        out.append(f"{r.project[:15]:<16}{r.source[:6]:<7}{r.name[:17]:<18}{r.total:>5}{len(r.sessions):>5}  "
                   f"{r.last:>10} {r.status:<9}{r.fix_verdict:<16} {_sparkline(r.daily, days)}")
    return "\n".join(out)


# ── orchestration ────────────────────────────────────────────────────────────

def run(src: str, project: Optional[str] = None, days_limit: Optional[int] = None,
        sources=("claude",), roots: Optional[dict] = None):
    adapters = load_adapters()
    events, remed, horizon = scan(src, adapters, days_limit, sources, roots)
    if not events:
        return {}, [], "", []
    maxday = horizon or max(e.day for e in events)
    if days_limit:
        cutoff = (_parse(maxday) - timedelta(days=days_limit)).isoformat()
        events = [e for e in events if e.day >= cutoff]
        remed = [r for r in remed if r.day >= cutoff]
    recs = build_ledger(events)
    days = sorted({e.day for e in events})
    # resolve per-project classes once for fix keyword lookup
    cls_by_proj = {}
    for r in recs.values():
        proj, name = r.project, r.name
        r.status = _status(r.last, maxday)
        sc = cls_by_proj.setdefault(proj, {sc.name: sc for sc in struggle_classes_for(proj, adapters)}).get(name)
        correlate_fixes(r, remed, maxday, sc)
    return recs, remed, maxday, days


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="struggle_ledger.py",
                                 description="Longitudinal cross-agent friction telemetry.")
    ap.add_argument("--src", default=None, help="transcript root (default: durable archive)")
    ap.add_argument("--project", default=None, help="filter to one project")
    ap.add_argument("--days", type=int, default=None, help="limit to last N days")
    ap.add_argument("--struggle", default=None, help="drill into one struggle (timeline + examples)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--sources", default=None,
                    help="comma list of agents to read (claude,mmx,codex,gemini); default: all, "
                         "or claude only when --src is given")
    args = ap.parse_args(argv)
    if args.sources:
        sources = tuple(x.strip() for x in args.sources.split(",") if x.strip())
        bad = [x for x in sources if x not in ALL_SOURCES]
        if bad:
            ap.error(f"unknown source(s) {bad}; choose from {ALL_SOURCES}")
    else:
        sources = ("claude",) if args.src else ALL_SOURCES

    src = args.src or (DEFAULT_SRC if os.path.isdir(DEFAULT_SRC) else FALLBACK_SRC)
    recs, remed, maxday, days = run(src, args.project, args.days, sources)
    if not recs:
        print("no struggles found", file=sys.stderr)
        return 0

    if args.json:
        out = [{"project": r.project, "struggle": r.name, "source": r.source, "events": r.total,
                "sessions": len(r.sessions), "first": r.first, "last": r.last,
                "status": r.status, "fix_verdict": r.fix_verdict,
                "fix_evidence": r.fix_evidence}
               for r in recs.values() if not args.project or r.project == args.project]
        print(json.dumps(out, indent=2))
        return 0

    if args.struggle:
        hits = [r for r in recs.values() if r.name == args.struggle
                and (not args.project or r.project == args.project)]
        for r in hits:
            print(f"\n## {r.project} / {r.name} [{r.source}]  ({r.total} events, {len(r.sessions)} sessions, "
                  f"{r.first}..{r.last}, {r.status})")
            print(f"   fix: {r.fix_verdict} — {r.fix_evidence}")
            print(f"   trend: {_sparkline(r.daily, days)}")
            print("   examples:")
            for ex in r.examples:
                print(f"     {ex}")
        return 0

    print(render(recs, remed, maxday, days, args.project))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
