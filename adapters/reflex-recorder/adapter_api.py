"""adapter_api.py — the Reflexarc project-adapter contract.

The reflex-recorder ENGINE (segmentation, store, generic detectors, trajectory
profiler) is project-agnostic and lives in this PUBLIC repo.  Anything that
encodes knowledge about a SPECIFIC project — what its build/run commands look
like, which extra signals it emits (build reports, GPU diagnostics), and which
bespoke detectors apply — is a PRIVATE concern and lives in the overlay at::

    $NERVOUS_HOME/adapters/reflex-<project>/adapter.py      (default
    ~/.config/nervous-bus/adapters/reflex-<project>/adapter.py)

Each such file defines one or more ``ProjectAdapter`` subclasses.  The engine
discovers them at runtime via :func:`load_adapters`, with NO hard dependency on
any private code: if the overlay is absent, the engine runs generic-only.

Contract (everything optional except ``name`` + ``matches``)
============================================================
    class MyAdapter(ProjectAdapter):
        name = "tengine"
        def matches(self, project): return project == "tengine"
        def taxonomy(self):  return MyTaxonomy()           # command classes
        def detectors(self): return [MyDetector, ...]      # BaseDetector subclasses
        def signals(self):   return [MySignalIngester(), ...]  # pull external signals
        def replays(self):   return {"my_detector": my_replay_fn}  # eval replays

This is the "code plugin + registry" model: a private adapter may ship arbitrary
Python (its own detectors, signal ingesters), and integrates by subclassing the
shared base classes the engine exports.  Generic parts are extracted here; the
specific parts stay private and are templated from ``templates/reflex-adapter/``.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

# Engine directory — added to sys.path so discovered adapters can
# ``from detectors.base import BaseDetector`` / ``from adapter_api import ...``
# regardless of where their file lives on disk.
ENGINE_DIR = Path(__file__).resolve().parent


# ── command taxonomy ─────────────────────────────────────────────────────────

class CommandTaxonomy:
    """Classifies a shell command into a coarse activity class.

    The engine ships this generic default (make/ninja/cmake/test runners common
    across ecosystems).  A project adapter subclasses it to add that project's own
    build/run-verify verbs (its custom build wrapper, simulator, codegen pass),
    overriding :meth:`classify` or extending the regex tables.

    Classes: ``build`` ``run-verify`` ``explore`` ``wait`` ``act``.
    """

    BUILD = re.compile(
        r"\b(make|ninja|cmake|cargo\s+(build|check)|go\s+build|"
        r"tsc|webpack|vite\s+build|gradle|mvn\s+package|build\.sh)\b", re.I)
    TEST = re.compile(
        r"\b(cargo\s+test|pytest|go\s+test|jest|vitest|npm\s+test|ctest)\b", re.I)
    EXPLORE = re.compile(r"\b(grep|rg|find|ls|cat|head|tail|stat|sed|awk)\b", re.I)
    WAIT = re.compile(r"\bsleep\s+\d", re.I)

    def classify(self, cmd: str) -> str:
        c = cmd or ""
        if self.BUILD.search(c):
            return "build"
        if self.TEST.search(c):
            return "run-verify"
        if self.EXPLORE.search(c):
            return "explore"
        if self.WAIT.search(c):
            return "wait"
        return "act"


# ── signal ingester ──────────────────────────────────────────────────────────

class SignalIngester(ABC):
    """Pulls a non-activity signal into the run-store.

    Activity events (tool calls) arrive over the bus automatically.  Some of the
    most valuable cost signals do NOT — e.g. a build's own report of how long it
    took and how many artifacts it recompiled.  A SignalIngester reads such a
    source and writes derived features/events the detectors can key on.
    """

    name: str = ""

    @abstractmethod
    def ingest(self, conn) -> int:
        """Read the external signal, persist derived data, return rows ingested."""


# ── project adapter ──────────────────────────────────────────────────────────

class ProjectAdapter(ABC):
    """Base class for a project-specific Reflexarc adapter.

    Subclasses live PRIVATELY in ``$NERVOUS_HOME/adapters/reflex-<project>/``.
    Only ``name`` and :meth:`matches` are required.
    """

    name: str = ""

    @abstractmethod
    def matches(self, project: str) -> bool:
        """Return True if this adapter governs the given project."""

    def taxonomy(self) -> CommandTaxonomy:
        return CommandTaxonomy()

    def detectors(self) -> list:
        """Return BaseDetector subclasses contributed by this adapter."""
        return []

    def signals(self) -> list[SignalIngester]:
        return []

    def replays(self) -> dict[str, Callable]:
        """Map DETECTOR_NAME → replay(conn, signature, ...) for eval scoring."""
        return {}


# ── discovery / registry ─────────────────────────────────────────────────────

def nervous_home() -> Path:
    return Path(os.environ.get("NERVOUS_HOME", Path.home() / ".config" / "nervous-bus"))


def load_adapters(home: Optional[Path] = None) -> list[ProjectAdapter]:
    """Discover and instantiate every private ProjectAdapter.

    Globs ``<home>/adapters/reflex-*/adapter.py`` (default home = $NERVOUS_HOME).
    Imports each file in isolation and collects concrete ProjectAdapter
    subclasses.  Failures in one adapter never break the others or the engine —
    a broken/absent overlay just yields fewer (or zero) adapters.
    """
    home = home or nervous_home()
    adapters_dir = Path(home) / "adapters"
    if not adapters_dir.is_dir():
        return []

    # Ensure the engine is importable from adapter modules.
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))

    found: list[ProjectAdapter] = []
    for adapter_py in sorted(adapters_dir.glob("reflex-*/adapter.py")):
        mod_name = f"_reflex_adapter_{adapter_py.parent.name.replace('-', '_')}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, adapter_py)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # let the adapter import its sibling modules (detectors/, signals/)
            sys.path.insert(0, str(adapter_py.parent))
            try:
                spec.loader.exec_module(module)
            finally:
                sys.path.remove(str(adapter_py.parent))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[reflex] adapter {adapter_py} failed to load: {exc}",
                  file=sys.stderr)
            continue
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, ProjectAdapter)
                    and obj is not ProjectAdapter and not getattr(obj, "__abstractmethods__", None)):
                try:
                    found.append(obj())
                except Exception as exc:  # pragma: no cover
                    print(f"[reflex] adapter {obj.__name__} init failed: {exc}",
                          file=sys.stderr)
    return found


def adapter_for(project: str, adapters: list[ProjectAdapter]) -> Optional[ProjectAdapter]:
    """First adapter whose matches(project) is True, else None."""
    for a in adapters:
        try:
            if a.matches(project):
                return a
        except Exception:
            continue
    return None


def taxonomy_for(project: str, adapters: Optional[list[ProjectAdapter]] = None) -> CommandTaxonomy:
    """Resolve the taxonomy for a project (adapter's, or the generic default)."""
    adapters = adapters if adapters is not None else load_adapters()
    a = adapter_for(project, adapters)
    return a.taxonomy() if a else CommandTaxonomy()
