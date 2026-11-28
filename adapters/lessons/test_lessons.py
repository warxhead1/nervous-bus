"""lessons CLI tests with a fake `nervous` that records each publish."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE.parents[1] / "schemas" / "agent.lesson.v1.json").read_text())
V = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "nervous"
    fake.write_text(f'#!/bin/sh\n[ -f "{tmp_path}/fail" ] && exit 7\nprintf "%s %s\\n" "$2" "$3" >> "{tmp_path}/sent"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return {"tmp": tmp_path, "env": dict(os.environ, NERVOUS_BIN=str(fake), NERVOUS_LESSONS_FILE=str(tmp_path / "l.jsonl"))}


def run(env, *args):
    return subprocess.run([sys.executable, str(HERE / "lessons.py"), *args], capture_output=True, text=True, env=env["env"])


def record(env, project="alpha", area="ci", lesson="Gates that poll hide their cost.", **kw):
    args = ["record", "--project", project, "--area", area, "--lesson", lesson, "--evidence", "3 h of polling in one day"]
    for k, v in kw.items():
        args += [f"--{k}", v]
    return run(env, *args)


def test_record_validates_stores_and_publishes(env):
    r = record(env, rule="Use a wait with a deadline.")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    V.validate(rec)
    sent = (env["tmp"] / "sent").read_text().splitlines()
    assert sent == [f"agent.lesson.v1 {json.dumps(rec, separators=(',', ':'))}"]
    assert json.loads((env["tmp"] / "l.jsonl").read_text()) == rec


def test_publish_failure_keeps_the_lesson(env):
    (env["tmp"] / "fail").write_text("")
    r = record(env)
    assert r.returncode == 1 and "stored" in r.stderr
    assert len((env["tmp"] / "l.jsonl").read_text().splitlines()) == 1


def test_query_filters_and_hides_superseded(env):
    old = json.loads(record(env, lesson="Old claim.").stdout)
    record(env, project="beta", area="build", lesson="Other project.")
    new = json.loads(record(env, lesson="Corrected claim.", supersedes=old["lesson_id"]).stdout)
    V.validate(new)

    def q(*a):
        return [d["lesson"] for d in json.loads(run(env, "query", *a).stdout)]

    assert q("--project", "alpha") == ["Corrected claim."]
    assert sorted(q("--project", "alpha", "--all")) == ["Corrected claim.", "Old claim."]
    assert q("--project", "beta", "--area", "build") == ["Other project."]
    assert q("--area", "nothing") == []
    assert old["lesson_id"] in run(env, "query", "--all", "--text").stdout


@pytest.mark.parametrize("override", [
    {"--area": "Bad Area"},
    {"--lesson": "x" * 301},
    {"--lesson": "two\nlines"},
    {"--supersedes": "les_000000000000"},
    {"--project": " "},
])
def test_bad_input_is_refused_and_not_stored(env, override):
    base = {"--project": "alpha", "--area": "ci", "--lesson": "ok.", "--evidence": "e"}
    base.update(override)
    r = run(env, "record", *[x for kv in base.items() for x in kv])
    assert r.returncode == 2
    assert not (env["tmp"] / "l.jsonl").exists()
