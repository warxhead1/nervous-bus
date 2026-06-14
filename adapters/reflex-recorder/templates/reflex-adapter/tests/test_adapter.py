"""Smoke test the scaffold loads. Replace with real adapter tests."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get(
    "REFLEX_ENGINE_HOME",
    Path.home() / "projects" / "nervous-bus" / "adapters" / "reflex-recorder"))))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_adapter_instantiates():
    from adapter import MyProjectAdapter
    a = MyProjectAdapter()
    assert a.matches("PROJECT")
    assert a.taxonomy().classify("ls") == "explore"
