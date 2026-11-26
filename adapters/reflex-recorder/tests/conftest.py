"""Suite-wide isolation from the host's private overlay.

load_adapters() globs $NERVOUS_HOME/adapters/reflex-*; a real overlay's signal
ingesters scan host filesystems (build logs, repos) from inside run_synthesis,
making unit tests host-dependent and able to hang on host I/O. Tests that
exercise adapters pass an explicit `home`.
"""
import pytest


@pytest.fixture(autouse=True)
def _empty_nervous_home(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("NERVOUS_HOME", str(tmp_path_factory.mktemp("nervous_home")))


@pytest.fixture(autouse=True)
def _isolate_steering_trend_detectors(monkeypatch, tmp_path_factory):
    """The trend detectors read the host's transcripts, hits file and Redis.
    Point them at empty temp paths so a synthesis pass in a test stays hermetic."""
    from detectors.steering_hits import SteeringHitsDetector
    from detectors.stretch_opens import StretchOpensDetector

    empty = tmp_path_factory.mktemp("steering_trends")
    monkeypatch.setattr(StretchOpensDetector, "transcript_root", empty / "projects")
    monkeypatch.setattr(SteeringHitsDetector, "use_bus", False)
    monkeypatch.setattr(SteeringHitsDetector, "hits_path", empty / "hits.jsonl")
