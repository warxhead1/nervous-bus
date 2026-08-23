"""detectors/kb_recall_gap.py — Tier-1 kb-recall-gap detector.

Detects a run that hit repeated_question (the same question class re-asked
across runs — see repeated_question.py's own docstring for that algorithm)
with NO kb-recall activity recorded in that SAME run: no
kb.guidance.provided.v1 (kb check), kb.session.context.v1 (kb prime), or
kb.entry.created.v1 (kb landmark / kb ingest / kb watch) event in the run's
run_events.

MAPPING PRINCIPLE — no new capture
====================================
Reuses two signals already captured elsewhere in this engine, exactly like
failure_taxonomy.py's pattern:
  1. repeated_question's detector_hits rows for a run (this detector MUST be
     registered in synthesis.py's DETECTOR_CLASSES list AFTER
     RepeatedQuestionDetector — see that module's docstring for the same
     ordering requirement; BaseDetector.run() commits each detector's hits
     to detector_hits before the next detector in the list runs, so this
     detector's own detect() call sees them in the same synthesis pass).
  2. run_events rows whose event_type is one of the three kb-recall channels
     below — the same table and query idiom repeated_question.py already
     uses for its own 'permission_requested' event_type lookup.

Emission matrix that justifies the three channel names chosen here
(SOURCE-VERIFIED 2026-08-23, kb-recall-loop emission re-audit — grep of
`publish_to_bus(...)` call sites in kb's src/ tree, cross-checked against a
live capture: `kb prime --project kb --json` and `kb check <query> --project
kb` were run read-only against the real nervous-bus debug.jsonl and each
appended exactly the envelope named below within the same second):
  kb.guidance.provided.v1  — kb check <query>            (src/commands/check.rs:220)
  kb.session.context.v1    — kb prime                     (src/commands/prime.rs:177,344)
  kb.entry.created.v1      — kb landmark / kb ingest / kb watch
                              (src/commands/landmark.rs:136, ingest.rs:390,
                               watch.rs:658,1321)
These three are kb's live read-and-answer surface (check/prime) plus its
durable-write surface (entry creation) — the events a session emits when it
actually consulted or grew the vault. kb.knowledge.gap.v1 and
kb.session.indexed.v1 are DELIBERATELY excluded from the recall-activity set:
a knowledge-gap event is itself evidence recall did NOT happen (kb.enrich,
kb check --emit-gap, kb overlap, kb watch's scope_divergence/loom.coord
handlers — see schemas/kb.knowledge.gap.v1.json, corrected in this same
audit), and session-indexed is a batch/backfill signal (kb sync, kb
ingest-sessions, kb watch's session-started/bead-complete handlers — see
schemas/kb.session.indexed.v1.json, also corrected in this audit), not a
live recall action taken during the run in question.

Signal
======
A run with a repeated_question detector_hits row (repeated_question fired
for this run_id — the run asked the user something already asked in a prior
run of the same project) AND zero run_events rows matching
event_type IN (kb.guidance.provided.v1, kb.session.context.v1,
kb.entry.created.v1) is flagged context_failure:kb_recall_missing.

Output
======
One PatternCandidate per project, aggregating every matching run_id, mirroring
repeated_question.py / failure_taxonomy.py's aggregation shape.

Remediation ladder
===================
INFORM only. There is no deterministic single fix for "didn't check the
vault" — worth surfacing so a human decides whether a `kb check` step
belongs earlier in the project's own CLAUDE.md/skill workflow. Not
AUTOMATE: unlike repeated_question's own settings.json allow-rule
automation, adding a kb-check step to a workflow is a human workflow
decision, not a fixed deterministic toggle.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from detectors.base import BaseDetector, PatternCandidate

# Channels source-verified as kb's live-consultation/growth surface (see
# module docstring emission matrix). Excludes kb.knowledge.gap.v1 (itself a
# recall-MISS signal) and kb.session.indexed.v1 (batch/backfill, not a
# live-recall action taken during the run being scored).
KB_RECALL_CHANNELS = frozenset({
    "kb.guidance.provided.v1",
    "kb.session.context.v1",
    "kb.entry.created.v1",
})


def has_kb_recall_gap(kb_recall_event_count: int) -> bool:
    """Pure predicate: True iff zero kb-recall events were seen for this run.

    Pure function (no DB access) so it is directly unit-testable, mirroring
    failure_taxonomy.classify_run's pattern. Callers are expected to only
    invoke this for runs already known to have a repeated_question hit.
    """
    return kb_recall_event_count <= 0


def _repeated_question_run_ids(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (run_id, project) for every run with a repeated_question hit."""
    rows = conn.execute(
        "SELECT DISTINCT run_id, project FROM detector_hits WHERE detector = 'repeated_question'"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _kb_recall_event_count(conn: sqlite3.Connection, run_id: str) -> int:
    """Count run_events rows for this run whose event_type is a kb-recall channel."""
    placeholders = ",".join("?" for _ in KB_RECALL_CHANNELS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM run_events WHERE run_id = ? AND event_type IN ({placeholders})",
        (run_id, *KB_RECALL_CHANNELS),
    ).fetchone()
    return row[0] if row else 0


class KbRecallGapDetector(BaseDetector):
    """Flag runs that repeated a question with no kb-recall activity in the same run."""

    DETECTOR_NAME = "kb_recall_gap"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        hits_by_project: dict[str, list[dict]] = defaultdict(list)

        for run_id, project in _repeated_question_run_ids(conn):
            kb_count = _kb_recall_event_count(conn, run_id)
            if not has_kb_recall_gap(kb_count):
                continue
            hits_by_project[project or ""].append({"run_id": run_id, "kb_count": kb_count})

        candidates: list[PatternCandidate] = []
        for project, hits in hits_by_project.items():
            run_ids = [h["run_id"] for h in hits]

            evidence = [f"project={project}", f"runs_tagged={len(hits)}"]
            for h in hits[:8]:
                evidence.append(
                    f"run={h['run_id']}: repeated_question hit, 0 kb-recall events "
                    f"({'/'.join(sorted(KB_RECALL_CHANNELS))})"
                )

            signature = f"{project}:{self.DETECTOR_NAME}:context_failure_kb_recall_missing"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="kb_recall_gap",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(hits),
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=(
                        "Inform-rung: these runs re-asked a question already asked in a "
                        "prior run of this project (see repeated_question) with no "
                        "kb.guidance.provided.v1 / kb.session.context.v1 / "
                        "kb.entry.created.v1 activity in between — the vault was never "
                        "consulted or grown in the run that repeated the question. "
                        "Consider adding a `kb check <question>` step to this project's "
                        "session-start workflow (CLAUDE.md or a skill) so recurring "
                        "questions get answered from the vault instead of the user."
                    ),
                    extra={
                        "bucket": "context_failure",
                        "reason": "kb_recall_missing",
                        "remediation_rung": "inform",
                        "remediation_rung_justification": (
                            "No deterministic single fix exists for 'didn't check the "
                            "vault' — unlike repeated_question's own settings.json "
                            "allow-rule automation, adding a kb-check step is a workflow "
                            "change a human should decide on."
                        ),
                    },
                )
            )

        return candidates
