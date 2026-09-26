"""detectors/user_correction.py — Tier-1 user_correction detector.

Ground truth for ``query.py remediation`` (query_remediation_effect):
that function reads detector_hits rows with detector='user_correction' and
signature=<theme>, and that contract is FIXED (query.py already ships it,
issue #39 only had to make user_correction fire). Themes are exactly the
theme names in ~/.claude/memory-global/rules.toml's [rule.*] tables:

    idle_dispatch, band_aid_fix, worktree_git_mistake, stale_claim,
    destructive_op, secrets, sudo, monitor_accuracy, unfiled_finding,
    model_tiering, ask_operator_for_automatable

Source of "genuine human prompt" (never an injected envelope)
===============================================================
Two harnesses, two archives, joined onto `runs` by (session_id, timestamp
inside [started, ended]):

1. Claude Code — durable transcript mirror at
   ~/.cache/nervous-bus/reflex/transcripts/<munged-cwd>/<session_id>.jsonl
   (transcript_snapshot.py keeps this alive past worktree reaping). A
   transcript line is a genuine user turn iff:
     - type == "user"
     - isMeta is not true (isMeta=True lines are hook-injected context,
       e.g. tool-result stand-ins — NOT something the human typed)
     - isCompactSummary is not true (auto-generated context-carry text)
     - message.content is a *string* (list content is a tool_result blob
       the harness attached to a synthetic "user" turn, not a human message)
   Measured against a live transcript 2026-09-26: 1228 user-typed rows,
   1214 non-meta, of which 58 have string content — the rest are tool_result
   lists. That is the population this detector scans.

2. Codex CLI — ~/.codex/history.jsonl, one JSON object per line:
   {"session_id": ..., "ts": <unix seconds>, "text": ...}. Every line here
   is already a genuine typed turn (codex has no separate synthetic-user
   channel the way Claude Code's hook system does) — verified 2026-09-26
   that ~1026 of the ~1357 codex-cli run session_ids in the live DB have a
   matching history.jsonl session_id (join key).

Noise filtering (_strip_envelope)
==================================
Even a genuine `type == "user" and not isMeta` Claude Code line can carry
harness-injected wrapper tags glued onto (or instead of) what the human
typed: <command-name>, <command-message>, <command-args>,
<local-command-caveat>, <local-command-stdout>, <bash-input>, <bash-stdout>,
<task-notification>, <system-reminder>, and the auto-continuation preamble
"This session is being continued from a previous conversation...". These are
stripped; a line that is ENTIRELY one of these envelopes (nothing left after
stripping) is dropped. This mirrors, in scope though not in code (that file
was not found in the sanctioned claude-hook-fast worktree as of 2026-09-26 —
searched tools/claude-hooks/ and grepped the whole repo for
"GenuineUserBoundary" with no match; this docstring's rule set was derived
fresh from a live transcript sample instead), the intent of
idleStopIsGenuineUserBoundary referenced in nervous-bus's own CLAUDE.md.

Theme detection
===============
A hit requires BOTH a correction cue (the user pushing back: "why did you",
"stop", "don't", "you keep", all-caps frustration, "that's wrong", "revert
this", etc.) AND at least one theme-specific keyword, in the SAME (stripped,
length-capped) turn. Requiring both cuts obvious false positives (a theme
keyword mentioned neutrally, e.g. planning a sudo migration, is not a
correction). Matching is capped to the first 2000 chars of a turn — genuine
corrections are short reactive messages; without a cap, keyword regexes can
false-positive inside a long pasted log/backtrace the user quoted verbatim.

Signature = the theme name alone (NO project prefix, no run_id) — this is
query.py's fixed join key, not a free choice. One PatternCandidate is emitted
per (project, theme) aggregating every distinct run_id in that project whose
turns matched; BaseDetector.record_hit then dedups to one detector_hits row
per (run_id, 'user_correction', theme).

Evidence is COUNTS AND SESSION/RUN IDS ONLY — never raw user text (this repo
is public; policy in the module header of query.py / CLAUDE.md).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from detectors.base import BaseDetector, PatternCandidate

DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".cache" / "nervous-bus" / "reflex" / "transcripts"
DEFAULT_CODEX_HISTORY = Path.home() / ".codex" / "history.jsonl"

# Turns longer than this are matched only on their first N chars (see module
# docstring: avoids a keyword false-positiving inside a pasted log).
_MATCH_CHAR_CAP = 2000

# ── Envelope stripping ──────────────────────────────────────────────────────

_ENVELOPE_TAG_RE = re.compile(
    r"<(command-name|command-message|command-args|local-command-caveat|"
    r"local-command-stdout|local-command-stderr|bash-input|bash-stdout|"
    r"bash-stderr|task-notification|system-reminder)\b[^>]*>.*?"
    r"(?:</\1>|$)",
    re.DOTALL,
)
_COMPACT_CONTINUATION_RE = re.compile(
    r"^\s*This session is being continued from a previous conversation",
)

# Cross-agent relay: multi-agent orchestration (Orca teammate messaging, a
# workflow coordinator's steering nudge) is injected into the transcript as a
# synthetic `type == "user"` turn with plain string content — it passes every
# other genuineness check (not isMeta, not a list, not a compact summary) but
# was never typed by Eric. Measured against the live DB 2026-09-26: of a
# 46-item hand-labelled stratified sample of pre-fix hits, 28+ (~60%) were
# exactly this class (a "correction" cue/keyword landing inside an agent's own
# report text, e.g. "reverted", "rm -rf", "sudo -n", quoted verbatim from a
# teammate's summary) — the single largest precision loss, worse than any
# individual theme regex's false-positive rate. These turns are ENTIRELY
# synthetic in every sample observed (no genuine trailing human commentary),
# so the whole turn is dropped rather than partially stripped.
# Iteration 2 (same 2026-09-26 measurement pass): re-labelling a FRESH 40-item
# sample after the fix above still found this class, just in phrasings the
# first regex didn't cover — "Coordinator update/review" (not just
# "steering"), "Heads-up from the <X> session", "From the audit session
# (<id>)", and a "[<project>] N agent(s) finished:" dispatch banner. All four
# are the identical failure mode (an orchestrator/teammate's text landing in
# the transcript as a synthetic `type=="user"` turn) — 7 of 40 hand-labelled
# hits in the second pass were exactly these variants.
_RELAY_MESSAGE_RE = re.compile(
    r"^\s*Another\s+\S+\s+session\s+sent\s+a\s+message\s*:"
    r"|^\s*Coordinator\s+(?:update|review|steering)\b"
    r"|^\s*Heads-up\s+from\s+the\b.{0,60}?\bsession\b"
    r"|^\s*From\s+the\s+audit\s+session\b"
    r"|^\s*\[[^\]]+\]\s+\d+\s+agents?\s+finished\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_synthetic_relay(text: str) -> bool:
    return bool(_RELAY_MESSAGE_RE.match(text)) or "<teammate-message" in text


def _strip_envelope(text: str) -> str:
    """Strip harness-injected wrapper tags. Returns '' if nothing genuine remains."""
    if _COMPACT_CONTINUATION_RE.search(text):
        return ""
    if _is_synthetic_relay(text):
        return ""
    stripped = _ENVELOPE_TAG_RE.sub("", text).strip()
    return stripped


# ── Themes: correction cue (required) + theme keywords (>=1 required) ──────

# All-caps frustration burst (>=4 consecutive caps letters) — kept as its own
# case-SENSITIVE pattern; folding it into a case-insensitive alternation would
# match any 4-letter lowercase word too.
#
# Iteration 3 (2026-09-26, 33-item hand-labelled sample, 16/33 = 48% overall
# precision — below the 80% bar): the dominant remaining false-positive class
# was a genuine correction-cue word AND a genuine theme keyword each present
# *somewhere* in a long turn, but unrelated to each other — a command-output
# paste (env var dump, git-push rejection, `make deploy` log, a continuation
# directive quoting a report) that happens to contain one all-caps env var
# token (matched as a "shouting burst") and, 800 characters away, an
# unrelated theme keyword ("worktree", "still", "idle"). A single bare
# all-caps word is also frequently just an identifier/acronym
# (TACHYONAC_ALLOW_OPEN_SESSION_DEPLOY, PID, CPU) rather than shouting.
# Two structural fixes: (1) require a *burst* of >=2 distinct all-caps words
# to count as a correction cue, not one; (2) require the cue and the theme
# keyword to be within PROXIMITY_WINDOW characters of each other in the same
# turn (see _theme_hits below) rather than merely co-present in the whole
# (capped) turn.
PROXIMITY_WINDOW = 220

_ALLCAPS_WORD_RE = re.compile(r"\b[A-Z]{4,}\b")
_CORRECTION_CUE_CI_RE = re.compile(
    r"\bwhy (?:did|do|does|are|were|is)\s+you\b"
    r"|\bstop\s+(?:doing|using|running|trying)\b"
    r"|\bdon'?t\s+(?:do|use|run|touch)\b"
    r"|\byou\s+keep\b"
    r"|\bthat'?s\s+(?:wrong|not right|incorrect)\b"
    r"|\bnot\s+what\s+i\s+(?:asked|said|wanted|meant)\b"
    r"|\bnever\s+(?:do|use|run)\s+that\s+again\b"
    r"|\brevert\s+(?:that|this)\b"
    r"|\bundo\s+(?:that|this)\b"
    r"|\bstill\s+(?:broken|failing|wrong|not working)\b",
    re.IGNORECASE,
)


# Iteration 4 (2026-09-26, 24-item hand-labelled sample after iteration 3's
# proximity fix, 15/24 = 62.5% overall — better than iteration 3's 48% but
# still short of 80%). The remaining false-positive class: an all-caps
# "burst" of >=2 words is not, on its own, evidence of shouting — a directive
# banner ("SCHEMA RULING", "CONTINUATION MANDATE"), a technical acronym list
# ("DGC+YAML+SLANG"), or an AWS/URL param dump (ALGORITHM, CREDENTIAL,
# UNSIGNED, PAYLOAD) also clusters multiple all-caps words within
# PROXIMITY_WINDOW without being a human correction at all. Every GENUINE
# shouting burst in the hand-labelled sample had one of two independent
# tells: (a) it contained a word from a small frustration vocabulary ("WHY",
# "STILL", "BROKEN", "HELL", "NEVER", "DAMN", "WTF", "SERIOUSLY", "ENOUGH",
# "ALREADY", "WRONG", "WORST", "ANYTHING", "EVERYONE", "EVERY", "REALLY"), or
# (b) it sat immediately next to a "!" or "?" run (a directive banner never
# does — it ends in a period or a colon-led list). Gate the burst on EITHER
# tell so a real all-caps sentence with different wording ("ANYTHING
# hardcoded to EIGHT!?!?!") still counts via its punctuation, while a
# technical banner (no punctuation burst, no frustration word) does not.
_SHOUT_VOCAB = {
    # Iteration 4b: dropped "WHAT" and "AGAIN" — both false-positived on
    # normal capitalized emphasis in structured design prose ("identifying
    # HOW they interact... WHAT they are trying to deal with"), which is not
    # shouting. Every remaining word only shows up capitalized when a human
    # is actually annoyed, in the hand-labelled sample.
    "WHY", "STILL", "BROKEN", "HELL", "NEVER", "DAMN", "WTF", "SERIOUSLY",
    "ENOUGH", "ALREADY", "WRONG", "WORST", "ANYTHING", "EVERYONE", "EVERY",
    "REALLY", "STOP", "DONT", "ACTUALLY", "JESUS", "GOD",
    "DUDE", "SICK", "TIRED", "ALWAYS", "KEEP", "KEEPS",
}
_NEARBY_SHOUT_PUNCT_RE = re.compile(r"[!?]")
_SHOUT_PUNCT_WINDOW = 20


def _allcaps_burst_spans(text: str) -> list[tuple[int, int]]:
    """Spans of all-caps words that are part of a >=2-word GENUINE shouting
    burst (each word within PROXIMITY_WINDOW of another all-caps word, AND
    the burst carries a frustration-vocabulary word or sits next to "!"/"?")
    — an isolated technical banner/acronym cluster does not count."""
    words = list(_ALLCAPS_WORD_RE.finditer(text))
    clusters: list[list[re.Match]] = []
    for i, m in enumerate(words):
        neighbors = [
            other for j, other in enumerate(words)
            if i != j and abs(m.start() - other.start()) <= PROXIMITY_WINDOW
        ]
        if neighbors:
            clusters.append([m] + neighbors)
    spans: list[tuple[int, int]] = []
    for cluster in clusters:
        has_vocab = any(m.group(0) in _SHOUT_VOCAB for m in cluster)
        has_punct = any(
            _NEARBY_SHOUT_PUNCT_RE.search(
                text[max(0, m.start() - _SHOUT_PUNCT_WINDOW): m.end() + _SHOUT_PUNCT_WINDOW]
            )
            for m in cluster
        )
        if has_vocab or has_punct:
            spans.extend(m.span() for m in cluster)
    return spans


def _correction_cue_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _CORRECTION_CUE_CI_RE.finditer(text)]
    spans.extend(_allcaps_burst_spans(text))
    return spans


def _has_correction_cue(text: str) -> bool:
    return bool(_CORRECTION_CUE_CI_RE.search(text)) or bool(_allcaps_burst_spans(text))


THEME_KEYWORDS: dict[str, re.Pattern] = {
    "idle_dispatch": re.compile(
        # Iteration 4: bare \bidle\b false-positived on unrelated technical
        # uses of the word ("idle" postgres connections, "weeks idle: data
        # sources") that have nothing to do with the AGENT sitting idle
        # between dispatches. Require it paired with wait/dispatch/cron
        # context instead of the bare word.
        r"\bidle\s+(?:wait\w*|dispatch\w*)\b|\b(?:wait\w*|dispatch\w*)\s+idle\b|"
        r"\bsat\s+(?:there|for)\b|\bwaiting\s+for\s+nothing\b|"
        r"\bnothing\s+(?:is\s+)?happening\b|\bno\s+progress\b|\bstill\s+waiting\b|"
        r"\bidle\s+waiting\b|\bforce\s+a\s+cron\s+tick\b",
        re.IGNORECASE,
    ),
    "band_aid_fix": re.compile(
        r"\bband[\s-]?aid\b|\bhack\b|\bworkaround\b|\broot\s+cause\b|"
        r"\bhardcod\w*\b|\bquick\s+fix\b|\bcheap\s+fix\b|\bpatch(?:ing)?\s+over\b",
        re.IGNORECASE,
    ),
    "worktree_git_mistake": re.compile(
        r"\bworktree\b|\bwrong\s+branch\b|\bwrong\s+repo\b|\bstash(?:ed|ing)?\b|"
        r"\bforce\s+push\b|\breset\s+--hard\b|\bchecked\s+out\b",
        re.IGNORECASE,
    ),
    "stale_claim": re.compile(
        r"\byou\s+said\s+(?:it\s+was\s+)?done\b|\bnot\s+actually\s+done\b|"
        r"\bstill\s+(?:broken|failing)\b|\bdidn'?t\s+verify\b|\byou\s+claimed\b|"
        r"\bdoesn'?t\s+work\b|\bnot\s+true\b",
        re.IGNORECASE,
    ),
    "destructive_op": re.compile(
        r"\brm\s+-rf\b|\bdeleted\b|\bdestroyed\b|\bwiped\b|\byou\s+(?:just\s+)?deleted\b|"
        r"\bforce\s+delete\b|\breset\s+--hard\b",
        re.IGNORECASE,
    ),
    "secrets": re.compile(
        # Iteration 2: bare \btoken\b was the single noisiest keyword in this
        # set (a real hit sampled 2026-09-26 matched on "token" inside an
        # unrelated financial-data column-name dump) — require a security-
        # specific compound instead of the bare word.
        r"\bsecret\b|\bapi\s+key\b|\b(?:api|auth|access|bearer)\s+token\b|"
        r"\bcredential\b|\bpassword\b|\bleaked\b|\bexposed\s+(?:secret|key|credential)\b",
        re.IGNORECASE,
    ),
    "sudo": re.compile(
        r"\bsudo\b|\broot\s+access\b|\belevat\w*\b|\bwhy\s+(?:do|does)\s+you\s+need\s+root\b",
        re.IGNORECASE,
    ),
    "monitor_accuracy": re.compile(
        # Iteration 2: bare \bmonitor\b false-positived on a PHYSICAL display
        # ("don't use the main monitor... find the other wayland display").
        # Every genuine hit in the hand-labelled sample had "monitor" within
        # a short span of a trigger/event/setup word ("monitor that didn't
        # TRIGGER", "so many monitor EVENTS", "SETTING a monitor for a
        # command") — require that proximity instead of the bare word.
        r"\bmonitor\w*\b.{0,60}?\b(?:trigger\w*|fire\w*|fired|notif\w*|armed|"
        r"event\w*|waiter|set\w*|status)\b"
        r"|\b(?:trigger\w*|fire\w*|fired|notif\w*|armed|event\w*|waiter|set\w*)\b"
        r".{0,60}?\bmonitor\w*\b"
        r"|\bwaiter\b|\bfalse\s+positive\b|\bstale\s+(?:log|status|monitor)\b|"
        r"\bnever\s+fired\b|\barmed\s+on\b",
        re.IGNORECASE | re.DOTALL,
    ),
    "unfiled_finding": re.compile(
        r"\bfile\s+(?:an?\s+|the\s+)?issue\b|\bdidn'?t\s+file\b|\bno\s+bead\b|"
        r"\bnot\s+filed\b|\bfile\s+it\b",
        re.IGNORECASE,
    ),
    "model_tiering": re.compile(
        # Iteration 2: bare \bfable\b false-positived on legitimate forward-
        # looking instructions to dispatch a fable agent ("launch a fable 5
        # agent to evaluate...") — that's normal dispatch-tiering usage, not
        # a correction about a WRONG past tiering choice. Require the
        # correction-flavored phrasing instead.
        r"\bwrong\s+model\b|\bshould(?:'ve| have)?\s+used\s+(?:sonnet|haiku|opus|fable)\b|"
        r"\btoo\s+expensive\b|\bmodel\s+tier\w*\b|\bcheaper\s+model\b",
        re.IGNORECASE,
    ),
    "ask_operator_for_automatable": re.compile(
        r"\bwhy\s+are\s+you\s+asking\s+me\b|\bjust\s+do\s+it\b|\bautomate\s+this\b|"
        r"\bdon'?t\s+ask\s+me\b|\bstop\s+asking\b",
        re.IGNORECASE,
    ),
}

THEMES: tuple[str, ...] = tuple(THEME_KEYWORDS.keys())


def match_themes(text: str) -> list[str]:
    """Return the list of themes whose keyword regex matches *text* AND has a
    correction cue within PROXIMITY_WINDOW characters (iteration 3: cue and
    keyword merely co-present anywhere in a long pasted turn was the dominant
    remaining false-positive class — see PROXIMITY_WINDOW's comment above).
    Empty list if no correction cue at all in the turn.
    """
    snippet = text[:_MATCH_CHAR_CAP]
    cue_spans = _correction_cue_spans(snippet)
    if not cue_spans:
        return []
    themes = []
    for theme, rx in THEME_KEYWORDS.items():
        for kw_match in rx.finditer(snippet):
            kstart, kend = kw_match.span()
            if any(
                kstart - cend <= PROXIMITY_WINDOW and cstart - kend <= PROXIMITY_WINDOW
                for cstart, cend in cue_spans
            ):
                themes.append(theme)
                break
    return themes


# ── Claude Code transcript source ───────────────────────────────────────────

def _index_claude_transcripts(root: Path) -> dict[str, Path]:
    """Map session_id -> transcript path, one pass over the archive tree.

    Cheap: only lists filenames (os.walk), never opens/reads a file here.
    """
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                index[fn[: -len(".jsonl")]] = Path(dirpath) / fn
    return index


def _iter_claude_turns(path: Path) -> Iterator[tuple[float, str]]:
    """Yield (unix_ts, genuine_stripped_text) for each real human turn in a
    Claude Code transcript file. Skips isMeta / isCompactSummary / list-content
    rows and rows that strip down to nothing. Never raises — a corrupt line is
    skipped, not fatal to the whole file.
    """
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            if obj.get("isMeta") or obj.get("isCompactSummary"):
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            stripped = _strip_envelope(content)
            if not stripped:
                continue
            ts_raw = obj.get("timestamp")
            ts = _parse_iso_ts(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                continue
            yield ts, stripped


def _parse_iso_ts(value: str) -> Optional[float]:
    """Parse an RFC3339 timestamp (with or without fractional seconds) to a
    unix epoch float. Returns None on any parse failure."""
    from datetime import datetime

    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


# ── Codex history source ────────────────────────────────────────────────────

def _iter_codex_turns(path: Path, session_ids: set[str]) -> Iterator[tuple[str, float, str]]:
    """Yield (session_id, unix_ts, genuine_stripped_text) for lines in
    ~/.codex/history.jsonl whose session_id is one we care about (bounds the
    scan to sessions that actually joined to an in-window run).
    """
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            sid = obj.get("session_id")
            if sid not in session_ids:
                continue
            text = obj.get("text")
            if not isinstance(text, str):
                continue
            stripped = _strip_envelope(text)
            if not stripped:
                continue
            ts = obj.get("ts")
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            yield sid, ts, stripped


def _iso_to_epoch(value: str) -> Optional[float]:
    return _parse_iso_ts(value)


class UserCorrectionDetector(BaseDetector):
    """See module docstring for the full algorithm."""

    DETECTOR_NAME = "user_correction"

    # Overridable in tests so a fixture never touches the real filesystem.
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT
    codex_history_path: Path = DEFAULT_CODEX_HISTORY

    def detect(
        self, conn: sqlite3.Connection, since_ts: Optional[str] = None
    ) -> list[PatternCandidate]:
        since_clause = "AND started >= ?" if since_ts else ""
        params: list = [since_ts] if since_ts else []
        rows = conn.execute(
            f"""
            SELECT run_id, project, agent_kind, session_id, started, ended
            FROM runs
            WHERE session_id IS NOT NULL AND session_id != ''
              {since_clause}
            """,
            params,
        ).fetchall()
        if not rows:
            return []

        # session_id -> list of (run_id, project, started_epoch, ended_epoch)
        by_session: dict[str, list[tuple[str, str, float, float]]] = {}
        codex_sessions: set[str] = set()
        claude_sessions: set[str] = set()
        for run_id, project, agent_kind, session_id, started, ended in rows:
            st = _iso_to_epoch(started)
            en = _iso_to_epoch(ended)
            if st is None or en is None:
                continue
            by_session.setdefault(session_id, []).append((run_id, project, st, en))
            if agent_kind == "codex-cli":
                codex_sessions.add(session_id)
            else:
                claude_sessions.add(session_id)

        # project -> theme -> set(run_id)
        hits: dict[str, dict[str, set[str]]] = {}

        def _record(session_id: str, ts: float, text: str) -> None:
            candidates_for_session = by_session.get(session_id)
            if not candidates_for_session:
                return
            run_id, project = _resolve_run(candidates_for_session, ts)
            if run_id is None:
                return
            themes = match_themes(text)
            for theme in themes:
                hits.setdefault(project, {}).setdefault(theme, set()).add(run_id)

        if claude_sessions:
            index = _index_claude_transcripts(self.transcript_root)
            for session_id in claude_sessions:
                path = index.get(session_id)
                if path is None:
                    continue
                for ts, text in _iter_claude_turns(path):
                    _record(session_id, ts, text)

        if codex_sessions and self.codex_history_path.is_file():
            for session_id, ts, text in _iter_codex_turns(
                self.codex_history_path, codex_sessions
            ):
                _record(session_id, ts, text)

        candidates: list[PatternCandidate] = []
        for project, by_theme in hits.items():
            for theme, run_ids in by_theme.items():
                run_ids_sorted = sorted(run_ids)
                candidates.append(
                    PatternCandidate(
                        project=project,
                        pattern_name="user_correction",
                        signature=theme,
                        detector=self.DETECTOR_NAME,
                        occurrences=len(run_ids_sorted),
                        evidence=[
                            f"theme={theme}",
                            f"project={project}",
                            f"run_count={len(run_ids_sorted)}",
                            f"run_ids={','.join(run_ids_sorted[:5])}",
                        ],
                        run_ids=run_ids_sorted,
                        extra={
                            "theme": theme,
                            "remediation_rung": "inform",
                            "remediation_rung_justification": (
                                "user_correction is ground-truth data collection for "
                                "query.py remediation (before/after hit rate around a "
                                "rule change), not itself a fix — Inform is the "
                                "detector's own rung, never a fix proposal."
                            ),
                        },
                    )
                )
        return candidates


def _resolve_run(
    candidates: list[tuple[str, str, float, float]], ts: float
) -> tuple[Optional[str], Optional[str]]:
    """Pick the run whose [started, ended] window contains ts.

    Falls back to the run with the latest started <= ts (a turn can land a
    hair after `ended` is recorded, since ended is stamped at close time and
    the turn timestamp precedes the close event that reads it). Returns
    (None, None) if no run qualifies at all.
    """
    for run_id, project, st, en in candidates:
        if st <= ts <= en:
            return run_id, project
    before = [(run_id, project, st) for run_id, project, st, _en in candidates if st <= ts]
    if before:
        run_id, project, _st = max(before, key=lambda t: t[2])
        return run_id, project
    return None, None
