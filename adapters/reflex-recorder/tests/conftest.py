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
