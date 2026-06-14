"""adapter.py — Reflexarc adapter scaffold.

Copy this directory to $NERVOUS_HOME/adapters/reflex-<project>/, rename the
PROJECT_* packages, and fill in the taxonomy / detectors / signals. The public
engine discovers it via adapter_api.load_adapters(); no engine code changes.

    cp -r nervous-bus/adapters/reflex-recorder/templates/reflex-adapter \
          ~/.config/nervous-bus/adapters/reflex-myproject
    cd ~/.config/nervous-bus/adapters/reflex-myproject
    # rename PROJECT_detectors/ PROJECT_signals/, edit the files, set name="myproject"
"""
from __future__ import annotations

from adapter_api import ProjectAdapter, SignalIngester, CommandTaxonomy
# from PROJECT_taxonomy import MyTaxonomy
# from PROJECT_detectors.my_detector import MyDetector
# from PROJECT_signals.my_signal import MySignal


class MyProjectAdapter(ProjectAdapter):
    name = "PROJECT"  # <- set to your project name

    def matches(self, project: str) -> bool:
        return project == self.name

    def taxonomy(self) -> CommandTaxonomy:
        # return MyTaxonomy()        # project build/run-verify command classes
        return CommandTaxonomy()     # default generic taxonomy

    def detectors(self) -> list:
        # return [MyDetector]        # BaseDetector subclasses
        return []

    def signals(self) -> list[SignalIngester]:
        # return [MySignal()]        # pull external cost signals into the store
        return []

    def replays(self) -> dict:
        # return {"my_detector": my_replay_fn}   # eval replays keyed by DETECTOR_NAME
        return {}
