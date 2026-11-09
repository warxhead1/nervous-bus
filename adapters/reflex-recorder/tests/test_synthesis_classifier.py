"""tests/test_synthesis_classifier.py — synthesis treatment of classifier-kind
signatures (failure_taxonomy).

A classifier labels every run, so its prevalence and issue recurrence say
nothing. Synthesis scores it with prevalence 0 and a windowed run count, and a
propose_fix needs a CLASSIFIED bucket clearing the severity/recurrence floors.
Real run_synthesis + real detectors against in-memory fixtures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REC_DIR = Path(__file__).parent.parent
_TESTS_DIR = Path(__file__).parent
for _p in (_REC_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import synthesis as syn
from detectors.base import KIND_CLASSIFIER, detector_kind
from detectors.failure_taxonomy import PLANNING_FAILURE, UNCLASSIFIED

from test_synthesis import _insert_issue, _insert_run, _make_conn, _relative_pair  # noqa: E402

PROJECT = "cls-proj"


@pytest.fixture(autouse=True)
def _pin_builtin_detectors(monkeypatch):
    monkeypatch.setattr(syn, "DETECTOR_CLASSES", list(syn._BUILTIN_DETECTOR_CLASSES))


@pytest.fixture(autouse=True)
def _forbid_default_db_path(monkeypatch):
    monkeypatch.setattr(
        syn, "DEFAULT_DB_PATH",
        Path("/nonexistent-test-guard/must-not-use-default-db/runs.db"),
    )


def _seed(conn, prefix, n, *, outcome=None, labeled=False, days_ago=1):
    for i in range(n):
        started, ended = _relative_pair(days_ago=days_ago, minutes=5)
        _insert_run(
            conn, f"{prefix}-{i}", project=PROJECT, started=started, ended=ended,
            close_reason="idle_timeout", outcome=outcome,
            labeled_at=ended if labeled else None,
        )


def _evals(result, bucket):
    return [
        e for e in result.evals
        if e["detector"] == "failure_taxonomy" and e["issue_signature"].endswith(f":{bucket}")
    ]


def _one(result, bucket):
    found = _evals(result, bucket)
    assert len(found) == 1, found
    return found[0]


def test_unclassified_mass_is_never_a_propose_fix_and_has_no_prevalence():
    conn = _make_conn()
    _seed(conn, "u", 12)  # no signal at all: every run is `unclassified`
    result = syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    ev = _one(result, UNCLASSIFIED)
    assert ev["decision"] != "propose_fix"
    assert ev["detector_kind"] == KIND_CLASSIFIER
    assert ev["score_components"]["prevalence"] == 0.0
    assert ev["score_components"]["recurrence_count"] == 12  # windowed runs
    assert not [e for e in result.evals if e["decision"] == "propose_fix"]


def test_classifier_signatures_never_create_issue_rows():
    conn = _make_conn()
    _seed(conn, "u", 4)
    _seed(conn, "t", 4, outcome="thrashed", labeled=True)
    syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    n = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE detector = 'failure_taxonomy'"
    ).fetchone()[0]
    assert n == 0


def test_classified_bucket_below_the_floors_stays_monitor():
    conn = _make_conn()
    n = syn.CLASSIFIER_MIN_CONFIRMED_FAILURES - 1  # confirmed failures just short
    _seed(conn, "t", n, outcome="thrashed", labeled=True)
    ev = _one(syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT), PLANNING_FAILURE)
    assert ev["decision"] == "monitor"
    assert "below the classifier floors" in ev["decision_rationale"]


def test_classified_bucket_clearing_both_floors_proposes_a_fix():
    conn = _make_conn()
    n = max(syn.CLASSIFIER_MIN_RUNS, syn.CLASSIFIER_MIN_CONFIRMED_FAILURES)
    _seed(conn, "t", n, outcome="thrashed", labeled=True)
    result = syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    ev = _one(result, PLANNING_FAILURE)
    assert ev["decision"] == "propose_fix"
    assert ev["score_components"]["prevalence"] == 0.0
    assert ev["detector_kind"] == KIND_CLASSIFIER
    assert [p for p in result.pattern_discovered_payloads
            if p["payload"]["signature"] == ev["issue_signature"]]


def test_runs_floor_is_separate_from_the_failure_floor():
    conn = _make_conn()
    # Plenty of confirmed failures but too few runs overall is impossible for
    # planning_failure (failures ARE runs), so raise the runs floor instead.
    n = syn.CLASSIFIER_MIN_CONFIRMED_FAILURES
    _seed(conn, "t", n, outcome="thrashed", labeled=True)
    result = syn.run_synthesis(
        conn, dry_run=True, project_filter=PROJECT,
        classifier_min_runs=n + 1, classifier_min_confirmed_failures=n,
    )
    assert _one(result, PLANNING_FAILURE)["decision"] == "monitor"
    result = syn.run_synthesis(
        conn, dry_run=True, project_filter=PROJECT,
        classifier_min_runs=n, classifier_min_confirmed_failures=n,
    )
    assert _one(result, PLANNING_FAILURE)["decision"] == "propose_fix"


def test_floors_are_not_met_by_confirmed_clean_mass():
    conn = _make_conn()
    big = max(syn.CLASSIFIER_MIN_RUNS, syn.CLASSIFIER_MIN_CONFIRMED_FAILURES)
    # planning_failure via the cadence+thrashed route, but cleaner runs in the
    # same hit set dominate: build with an explicit clean-outcome run set that
    # also carries a planning hit through red_baseline_dispatch is not
    # available here, so assert via the decision function the loop delegates to.
    decision, why = syn.make_classifier_decision(
        classified=True, window_runs=big * 4,
        labeled_support={"confirmed_failures": big, "confirmed_clean": big * 2, "unlabeled": 0},
        logical_run_count=big * 4,
    )
    assert decision == "suppressed", why


def test_legacy_classifier_issue_row_is_superseded_not_trusted():
    conn = _make_conn()
    sig = f"{PROJECT}:failure_taxonomy:{UNCLASSIFIED}"
    # A legacy row with the inflated counter and a standing propose_fix verdict,
    # but no run in the window carries the label any more.
    _insert_issue(conn, sig, project=PROJECT, detector="failure_taxonomy",
                  recurrence_count=182767, last_seen=_relative_pair(0)[1])
    _seed(conn, "other", 3, days_ago=1)  # keeps the project over MIN_PROJECT_RUNS
    conn.execute(
        "INSERT INTO run_evals (eval_id, issue_signature, project, detector, score, decision, "
        "rung, synthesis_pass_at) VALUES ('old-eval', ?, ?, 'failure_taxonomy', 0.76, "
        "'propose_fix', 'inform', '2026-10-01T00:00:00Z')",
        (sig, PROJECT),
    )
    result = syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    ev = [e for e in result.evals if e["issue_signature"] == sig][0]
    assert ev["decision"] != "propose_fix"
    assert ev["score_components"]["recurrence_count"] != 182767
    assert ev["supersedes_eval_id"] == "old-eval"
    # ...and the legacy counter itself is left alone (nothing writes it).
    assert conn.execute(
        "SELECT recurrence_count FROM issues WHERE signature = ?", (sig,)
    ).fetchone()[0] == 182767


def test_non_classifier_detectors_still_score_on_prevalence():
    conn = _make_conn()
    _seed(conn, "r", 6)
    sig = f"{PROJECT}:worktree_leak:x"
    for i in range(6):
        conn.execute(
            "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
            (f"r-{i}", "worktree_leak", sig, PROJECT, _relative_pair(0)[1]),
        )
    _insert_issue(conn, sig, project=PROJECT, detector="worktree_leak", recurrence_count=6,
                  last_seen=_relative_pair(0)[1])
    result = syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    ev = [e for e in result.evals if e["issue_signature"] == sig][0]
    assert detector_kind("worktree_leak") != KIND_CLASSIFIER
    assert ev["detector_kind"] == "detector"
    assert ev["score_components"]["prevalence"] > 0.0


def test_eval_payload_still_validates_against_the_published_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = _REC_DIR.parent.parent / "schemas" / "bus.agent.run.eval.v1.json"
    schema = json.loads(schema_path.read_text())
    conn = _make_conn()
    _seed(conn, "t", syn.CLASSIFIER_MIN_RUNS, outcome="thrashed", labeled=True)
    result = syn.run_synthesis(conn, dry_run=True, project_filter=PROJECT)
    for e in _evals(result, PLANNING_FAILURE):
        jsonschema.Draft202012Validator(schema).validate({k: v for k, v in e.items() if k != "issue"})
