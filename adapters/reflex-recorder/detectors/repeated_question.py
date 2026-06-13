"""detectors/repeated_question.py — Tier-1 repeated-question detector.

Detects the same question-class asked to the user across MULTIPLE runs in the
same project.  Two signal sources are combined:

1. permission_requested events  (event_type = 'permission_requested' in run_events)
   These are CloudEvents from bus.hearth.session.permission.requested.v1 that are
   stored directly in the run_events table.  The question lives in raw_json .data
   .tool_summary (the tool being gated).

2. AskUserQuestion tool calls   (event_type = 'bus.agent.activity.v1' AND
   raw_json .data .tool_name = 'AskUserQuestion')
   The question lives in raw_json .data .tool_summary.

Algorithm
=========
1. Fetch all candidate events from run_events, extracting (run_id, text).
2. Join with runs to resolve project (run_events rows don't carry project directly).
3. Normalize each question text to a canonical class by:
   a. Stripping trailing punctuation / whitespace.
   b. Replacing any token that looks like a path, ID, ULID, or number with a
      placeholder so "allow Read /foo/bar/baz.txt" and
      "allow Read /home/eric/projects/other/file.py" map to the same class.
   c. Lower-casing.
   The result is a short, stable question class string used as the signature
   anchor — it must NOT include the run_id or any timestamp.
4. Group by (project, question_class).  Count distinct run_ids per group.
5. Only fire when a class recurs across >= 2 distinct runs (single-run questions
   are not a pattern; they could be unique context-specific asks).

Remediation ladder
==================
- permission_requested recurrence → AUTOMATE.  The recurring permission can be
  pre-authorized via a settings allow-rule (Claude Code settings.json
  `permissions.allow`) or a hookify rule.  The detector emits a stub allow-rule
  in proposed_remediation that the remediation ladder can promote to an actual
  rule automatically.
- AskUserQuestion recurrence → AUTOMATE (if the answer is stable, encode it in
  CLAUDE.md, a skill, or MEMORY.md) or INFORM (if the question is genuinely
  context-dependent but should still be flagged as a candidate for
  documentation).  The detector picks AUTOMATE for question classes that are
  fully deterministic (e.g. "which branch to push to?") and INFORM for classes
  that appear to depend on per-run context (heuristic: contains a placeholder).

Signature format (stable, no run_id, no timestamp)
===================================================
  "{project}:repeated_question:{question_class}"

where question_class is the normalized anchor string.

Usage
=====
    import sqlite3
    from detectors.repeated_question import RepeatedQuestionDetector

    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    detector = RepeatedQuestionDetector(conn)
    candidates = detector.run()
    for c in candidates:
        payload = detector.emit_candidate(c)
        # publish via nervous publish ...
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# ── Normalisation helpers ─────────────────────────────────────────────────────

# Regex patterns for tokens that vary across runs and should be replaced.
_ULID_RE = re.compile(r"\b[0-9A-Z]{26}\b")          # ULID (26 uppercase alphanums)
_UUID_RE = re.compile(                                 # UUID v4
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)  # git SHAs etc.
_NUMBER_RE = re.compile(r"\b\d+\b")                  # standalone numbers
_ABS_PATH_RE = re.compile(r"(/[^\s,;\"'()]+)")       # /absolute/path tokens
_QUOTED_PATH_RE = re.compile(r'["\']?(?:/[^\s,"\']+)["\']?')  # quoted paths
_BEAD_ID_RE = re.compile(r"\b[a-z][a-z0-9-]+-[a-z0-9]{4,}\b")  # bead IDs

# After stripping variable tokens, collapse multiple spaces to one.
_SPACE_RE = re.compile(r"\s{2,}")


def _normalize_question(text: str) -> str:
    """Strip run-specific tokens from *text* and return a stable question class.

    The resulting string:
      - is lower-case
      - has paths replaced with <path>
      - has IDs / hashes / numbers replaced with <id> / <n>
      - has leading/trailing whitespace and punctuation stripped
      - is suitable as a signature anchor (reproducible across runs)
    """
    if not text:
        return ""
    t = text.strip().rstrip("?.:!").strip()
    # Order matters: longer patterns first to avoid partial substitutions.
    t = _ABS_PATH_RE.sub("<path>", t)
    t = _ULID_RE.sub("<id>", t)
    t = _UUID_RE.sub("<id>", t)
    t = _HEX_HASH_RE.sub("<id>", t)
    t = _BEAD_ID_RE.sub("<id>", t)
    t = _NUMBER_RE.sub("<n>", t)
    t = _SPACE_RE.sub(" ", t)
    return t.lower().strip()


def _extract_question_text(event_type: str, raw_json_str: str) -> Optional[str]:
    """Extract the human-facing question/tool_summary from a raw_json event.

    Returns None if this event carries no question text.
    """
    try:
        envelope = json.loads(raw_json_str)
    except (json.JSONDecodeError, TypeError):
        return None

    # For bus.agent.activity.v1 events, the payload lives under .data
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        # Flat envelope (e.g. legacy permission_requested records stored as
        # the bus.hearth.session.permission.requested.v1 data dict directly).
        data = envelope if isinstance(envelope, dict) else {}

    if event_type == "permission_requested":
        # bus.hearth.session.permission.requested.v1 — question is tool_summary
        # or prompt_text.
        ts = data.get("tool_summary") or data.get("prompt_text")
        return str(ts).strip() if ts else None

    if event_type == "bus.agent.activity.v1":
        if data.get("tool_name") == "AskUserQuestion":
            ts = data.get("tool_summary")
            return str(ts).strip() if ts else None
        # Also treat tool_summary containing Bash description that looks like a
        # question (ends with "?").  This is a secondary heuristic: only fire if
        # the description field of the Bash summary ends with "?".
        tool_name = data.get("tool_name")
        if tool_name == "Bash":
            ts = data.get("tool_summary")
            if ts:
                try:
                    ts_dict = json.loads(ts) if isinstance(ts, str) and ts.startswith("{") else {}
                except (json.JSONDecodeError, TypeError):
                    ts_dict = {}
                desc = ts_dict.get("description", "")
                if isinstance(desc, str) and desc.strip().endswith("?"):
                    return desc.strip()
        return None

    return None


# ── Remediation helpers ───────────────────────────────────────────────────────

_PERMISSION_REMEDIATION_TEMPLATE = (
    "Automate-rung: the tool permission '{question_class}' has been requested "
    "in {occurrences} distinct runs of project '{project}'.  "
    "Add a settings allow-rule to pre-authorize it deterministically: "
    "in .claude/settings.json add an entry under \"permissions.allow\" matching "
    "the tool+pattern that produced this prompt.  "
    "Hook trigger: any run in project '{project}' that reaches a "
    "permission_requested event matching class '{question_class}'."
)

_ASK_AUTOMATE_TEMPLATE = (
    "Automate-rung: the recurring question '{question_class}' (asked in "
    "{occurrences} distinct runs of project '{project}') has a stable answer "
    "that can be encoded in CLAUDE.md or a project skill so Claude never "
    "needs to ask again.  Encode the answer in the project's CLAUDE.md under "
    "a 'Recurring decisions' section."
)

_ASK_INFORM_TEMPLATE = (
    "Inform-rung: the recurring question '{question_class}' (asked in "
    "{occurrences} distinct runs of project '{project}') appears context-dependent "
    "(contains variable tokens after normalization).  Document typical answers "
    "in CLAUDE.md or the project skill so future runs can self-answer.  "
    "If a stable default exists, escalate to Automate-rung by encoding it there."
)


def _is_deterministic_question(question_class: str) -> bool:
    """Heuristic: a question class is deterministic if it has no remaining
    placeholders after normalisation, meaning the answer doesn't vary by run.
    """
    return "<path>" not in question_class and "<id>" not in question_class


# ── Detector ──────────────────────────────────────────────────────────────────

# Minimum number of distinct runs for a class to be flagged as a pattern.
_MIN_OCCURRENCES = 2


class RepeatedQuestionDetector(BaseDetector):
    """Detect the same question-class asked to the user across >= 2 runs.

    Combines permission_requested events and AskUserQuestion tool calls.
    Normalizes question text to a class for stable cross-run deduplication.
    See module docstring for full algorithm.
    """

    DETECTOR_NAME = "repeated_question"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Scan run_events for recurring question classes across distinct runs.

        Parameters
        ----------
        conn : sqlite3.Connection
            Read-only connection to runs.db.

        Returns
        -------
        list[PatternCandidate]
            One candidate per (project, question_class) that recurs across
            >= _MIN_OCCURRENCES distinct runs.
        """
        # Fetch all candidate events: permission_requested or
        # bus.agent.activity.v1 with tool_name AskUserQuestion (or Bash w/ ?-desc).
        # We also need the project, which lives in runs.project.
        cur = conn.execute(
            """
            SELECT
                re.run_id,
                re.event_type,
                re.raw_json,
                r.project
            FROM run_events re
            JOIN runs r ON r.run_id = re.run_id
            WHERE re.event_type IN ('permission_requested', 'bus.agent.activity.v1')
            ORDER BY r.project, re.run_id, re.seq
            """
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        event_rows = [dict(zip(cols, row)) for row in rows]

        # Accumulate: (project, question_class) → set of run_ids + evidence list
        from collections import defaultdict
        class_run_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        class_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
        class_source: dict[tuple[str, str], str] = {}  # 'permission' | 'question'

        for row in event_rows:
            project = row["project"]
            run_id = row["run_id"]
            event_type = row["event_type"]
            raw_json_str = row["raw_json"]

            text = _extract_question_text(event_type, raw_json_str)
            if not text:
                continue

            qclass = _normalize_question(text)
            if not qclass:
                continue

            key = (project, qclass)
            class_run_ids[key].add(run_id)

            # Record evidence (up to 5 run_ids per class, dedup)
            ev_item = f"run_id={run_id} event_type={event_type} text={text[:80]}"
            if ev_item not in class_evidence[key]:
                class_evidence[key].append(ev_item)

            # Source: if any instance is permission_requested, mark as permission.
            if event_type == "permission_requested":
                class_source[key] = "permission"
            elif key not in class_source:
                class_source[key] = "question"

        # Build candidates for classes that exceed the minimum threshold.
        candidates: list[PatternCandidate] = []

        for (project, qclass), run_ids_set in class_run_ids.items():
            if len(run_ids_set) < _MIN_OCCURRENCES:
                continue

            run_ids = sorted(run_ids_set)
            occurrences = len(run_ids)
            source = class_source.get((project, qclass), "question")

            # Evidence: up to 8 items from recorded evidence.
            evidence = class_evidence[(project, qclass)][:8]
            evidence = [f"project={project}", f"question_class={qclass}"] + evidence

            # Choose remediation rung and text.
            if source == "permission":
                rung = "automate"
                remediation = _PERMISSION_REMEDIATION_TEMPLATE.format(
                    question_class=qclass,
                    occurrences=occurrences,
                    project=project,
                )
            elif _is_deterministic_question(qclass):
                rung = "automate"
                remediation = _ASK_AUTOMATE_TEMPLATE.format(
                    question_class=qclass,
                    occurrences=occurrences,
                    project=project,
                )
            else:
                rung = "inform"
                remediation = _ASK_INFORM_TEMPLATE.format(
                    question_class=qclass,
                    occurrences=occurrences,
                    project=project,
                )

            # Signature: stable (project, detector, anchor) — NO run_id, NO ts.
            signature = f"{project}:{self.DETECTOR_NAME}:{qclass}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="repeated_question",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=occurrences,
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=remediation,
                    extra={
                        "question_class": qclass,
                        "source": source,
                        "remediation_rung": rung,
                        "remediation_rung_justification": (
                            "permission_requested recurrences are always Automate-rung "
                            "(add allow-rule to settings.json to pre-authorize)."
                            if source == "permission"
                            else (
                                "Question has no variable placeholders after normalisation "
                                "→ stable answer → AUTOMATE (encode in CLAUDE.md/skill)."
                                if rung == "automate"
                                else
                                "Question contains variable placeholders → context-dependent "
                                "→ cannot eliminate deterministically → INFORM (document "
                                "typical answers so future runs self-answer; escalate to "
                                "Automate if a stable default is found)."
                            )
                        ),
                        "run_ids": run_ids,
                    },
                )
            )

        return candidates
