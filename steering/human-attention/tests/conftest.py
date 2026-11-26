import pytest


@pytest.fixture(autouse=True)
def hit_log(tmp_path, monkeypatch):
    """Every test writes its hit log under tmp_path, never the real state dir."""
    path = tmp_path / "state" / "hits.jsonl"
    monkeypatch.setenv("AGENT_STEERING_HITS", str(path))
    monkeypatch.delenv("AGENT_STEERING_LOG", raising=False)
    return path
