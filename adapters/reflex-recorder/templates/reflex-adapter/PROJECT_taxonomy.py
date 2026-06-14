"""PROJECT_taxonomy.py — your project's command taxonomy (rename + edit)."""
from __future__ import annotations
import re
from adapter_api import CommandTaxonomy


class MyTaxonomy(CommandTaxonomy):
    # Override the regex tables with YOUR project's build / run-verify verbs.
    BUILD = re.compile(r"\b(make|ninja|cargo\s+(build|check)|YOUR_BUILD_CMD)\b", re.I)
    RUN_VERIFY = re.compile(r"\b(cargo\s+test|pytest|YOUR_VERIFY_CMD)\b", re.I)

    def classify(self, cmd: str) -> str:
        c = cmd or ""
        if self.BUILD.search(c):
            return "build"
        if self.RUN_VERIFY.search(c):
            return "run-verify"
        if self.EXPLORE.search(c):
            return "explore"
        if self.WAIT.search(c):
            return "wait"
        return "act"
