"""Keep every steering test's hit records out of the user's real hit log.

Hooks append to $AGENT_STEERING_HITS. Subprocess tests inherit os.environ,
so pointing it at a per-test file covers in-process and spawned hooks alike.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_hit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STEERING_HITS", str(tmp_path / "hits.jsonl"))
