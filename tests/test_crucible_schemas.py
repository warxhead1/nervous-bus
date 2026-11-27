"""Crucible bus schemas: positive samples pass, any added text field fails,
and the gamelab. private prefix trips the leak guard."""
import importlib.util
import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

REPO = Path(__file__).resolve().parents[1]
SCHEMAS = REPO / "schemas"
U = "0" * 26
SAMPLES = {
    "crucible.question.asked": {"game": "demo", "question_id": f"q-{U}", "sitting_id": f"sit-{U}", "mode": "batch",
        "class": "owner", "door": "two-way", "weight": "divergent", "parts": 3},
    "crucible.question.settled": {"game": "demo", "question_id": f"q-{U}", "outcome": "answered",
        "agreed_with_rec": True, "via": "kiln-tap", "latency_ms": 5, "typed_chars": 0, "probe": False},
    "crucible.decision.settled": {"game": "demo", "decision_id": f"d-{U}", "authority": "owner", "door": "two-way",
        "class": "owner", "supersedes": None},
    "crucible.decision.flipped": {"game": "demo", "decision_id": f"d-{U}", "via": "kiln-tap"},
    "crucible.label.edited": {"game": "demo", "label_id": f"lbl-{U}", "kind": "k1", "edit": "rename",
        "via": "kiln-edit", "chars_from": 5, "chars_to": 7},
    "crucible.investigation.finished": {"game": "demo", "investigation_id": f"x-{U}", "reading": "formal",
        "verdict": "pass", "n": 10, "controls_ok": True, "wall_s": 1.5, "model_requests": 2},
    "crucible.audit.finished": {"game": "demo", "audit_id": f"au-{U}", "domain": "ux", "verdict": "pass",
        "seats": 3, "agreed": 5, "disputed": 1, "mechanical_pass": True},
    "crucible.art.check": {"game": "demo", "bible_rev": 3, "layer": "L2", "metric_id": "palette.role",
        "pass": True, "value": 4.2, "tolerance": 6.0},
    "crucible.bet.resolved": {"game": "demo", "bet_id": f"b-{U}", "bettor_kind": "owner", "abs_error": 0.4},
    "crucible.sitting.closed": {"game": "demo", "sitting_id": f"sit-{U}", "cards": 5, "taps": 6, "seconds": 9.5, "pushes": 1},
    "crucible.lane.state": {"game": "demo", "lane_id": "lane-1", "state": "running", "tier": "t1", "wall_s": 0},
    "crucible.gate.result": {"game": "demo", "lane_id": "lane-1", "gate_id": "fmt", "tier": "t1", "pass": True,
        "wall_s": 3.1, "cached": False},
}
HEARTH = {
    "bus.hearth.decision.requested": {"decision_id": "dec-1", "correlation_id": "dec-1", "project": "p", "title": "t",
        "options": [{"id": "a", "label": "A"}], "recommended": "a"},
    "bus.hearth.decision.responded": {"decision_id": "dec-1", "correlation_id": "dec-1", "project": "p",
        "session_id": None, "kind": "choice", "status": "answered", "option_id": "a", "answers": None,
        "text": None, "via": "kiln", "answered_by": "owner", "responded_at": "2026-11-26T00:00:00Z"},
}


def validator(name):
    schema = json.loads((SCHEMAS / f"{name}.v1.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_crucible_accepts_sample_rejects_text(name):
    v = validator(name)
    assert not list(v.iter_errors(SAMPLES[name]))
    assert list(v.iter_errors({**SAMPLES[name], "text": "x"}))


@pytest.mark.parametrize("name", sorted(HEARTH))
def test_hearth_decision_sample(name):
    assert not list(validator(name).iter_errors(HEARTH[name]))


def test_hearth_requested_options_and_parts_exclusive():
    v = validator("bus.hearth.decision.requested")
    both = {**HEARTH["bus.hearth.decision.requested"],
            "parts": [{"id": "p1", "label": "P", "options": [{"id": "a", "label": "A"}]}]}
    assert list(v.iter_errors(both))


def test_gamelab_prefix_trips_leak_guard(tmp_path):
    spec = importlib.util.spec_from_file_location("leak", REPO / "tools" / "check_private_schema_leak.py")
    leak = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(leak)
    assert "gamelab." in leak.load_prefixes()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "gamelab.x.v1.json").write_text("{}")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    leak.REPO = tmp_path
    assert leak.main() == 1
