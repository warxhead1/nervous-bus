"""Schema test for agent.steering.hit.v1: the contract record validates, drift fails."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "schemas" / "agent.steering.hit.v1.json").read_text())
SAMPLE = json.loads((REPO / "adapters" / "steering-hits" / "tests" / "sample_hit.json").read_text())


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_sample_validates(validator):
    validator.validate(SAMPLE)


def test_minimal_reply_score_validates(validator):
    validator.validate({"v": 1, "ts": "2026-11-26T21:00:00Z", "plugin": "plain-prose",
                        "check": "reply-score", "surface": "reply", "action": "none",
                        "findings": 0, "rules": {}, "words": 80, "avg_sentence": 12.5})


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("rules"),
    lambda r: r.pop("ts"),
    lambda r: r.update(extra="x"),
    lambda r: r.update(text="a sentence"),
    lambda r: r.update(v=2),
    lambda r: r.update(check="made-up"),
    lambda r: r.update(surface="email"),
    lambda r: r.update(action="warn"),
    lambda r: r.update(findings=-1),
    lambda r: r["rules"].update(x="two"),
    lambda r: r["lost"].update(sentences=1),
])
def test_drift_fails(validator, mutate):
    rec = copy.deepcopy(SAMPLE)
    mutate(rec)
    assert list(validator.iter_errors(rec))
