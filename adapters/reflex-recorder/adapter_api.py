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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Pattern

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


# ── struggle classes (friction telemetry) ───────────────────────────────────

@dataclass
class StruggleClass:
    """One recurring *friction* pattern an agent fights — a struggle, not an outcome.

    The Struggle Ledger (``struggle_ledger.py``) matches each class against the text
    of transcript records (a command, a tool result, an assistant line) and tracks
    every hit longitudinally: how often, across how many sessions, and — the point —
    whether it is still happening or was fixed. A class is project-agnostic friction
    (cargo build-lock, address-in-use) when shipped by the engine, or project-specific
    (a GPU device-lost / lock-contention pattern) when contributed by a private adapter
    via :meth:`ProjectAdapter.struggle_classes` — so proprietary tool names stay private.

    ``fix_keywords`` are the terms that, appearing in a commit/bead-close message, mark
    a plausible remediation of THIS struggle (used to score whether a fix actually
    dropped the friction). Defaults to the name's tokens.
    """
    name: str
    pattern: Pattern
    description: str = ""
    kind: str = "friction"          # friction | contention | crash | retry | wait
    fix_keywords: tuple = ()

    def keywords(self) -> tuple:
        return self.fix_keywords or tuple(t for t in self.name.split("_") if len(t) > 2)


def generic_struggle_classes() -> list[StruggleClass]:
    """Cross-ecosystem friction patterns the engine ships for ANY project.

    Project-specific struggles (e.g. a bespoke GPU harness's lock/device-lost) belong
    in that project's private adapter, NOT here.
    """
    return [
        StruggleClass(
            "cargo_build_lock",
            re.compile(r"Blocking waiting for file lock on (?:package cache|build directory|artifact)", re.I),
            "parallel builds contending on the cargo cache/target lock", "contention",
            fix_keywords=("cargo", "lock", "build", "cache", "serialize", "target")),
        StruggleClass(
            "address_in_use",
            re.compile(r"address already in use|EADDRINUSE|bind(?:\(\))?:.{0,20}in use|port .{0,12}in use", re.I),
            "a port/socket is already bound (stale process / double-start)", "contention",
            fix_keywords=("port", "socket", "bind", "address", "reclaim", "stale")),
        StruggleClass(
            "resource_busy",
            re.compile(r"device or resource busy|resource temporarily unavailable", re.I),
            "an OS resource is contended (fd/device/mount)", "contention"),
        StruggleClass(
            "oom",
            re.compile(r"\bOOMKilled\b|out of memory|Cannot allocate memory|fatal runtime.*memory", re.I),
            "out-of-memory kill", "crash",
            fix_keywords=("memory", "oom", "alloc", "heap")),
    ]


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

    def struggle_classes(self) -> list["StruggleClass"]:
        """Project-specific friction patterns for the Struggle Ledger.

        E.g. a GPU project's device-lost / lock-contention / busy-wait signatures —
        which name proprietary tools and so MUST stay in the private overlay. The
        engine's generic_struggle_classes() are always included alongside these.
        """
        return []

    def project_profile(self):
        """Return this project's :class:`ProjectProfile` for the GENERIC
        structural-debt detectors (stale_fence, dual_source), or None to use the
        engine's zero-config DEFAULT_PROFILE.

        This is the thin per-project SEMANTIC layer: which dirs to walk, which
        migration-twin suffixes name a deferred path, and the dual-source
        FINGERPRINT shape (tengine: ``*_addr`` device-address tables). The
        generic detectors live in the engine and run on ANY repo; the profile
        only sharpens precision.
        """
        return None


def profile_for(project: str, adapters: Optional[list["ProjectAdapter"]] = None):
    """Resolve the ProjectProfile for *project* (adapter's, or DEFAULT_PROFILE).

    Importing DEFAULT_PROFILE lazily keeps adapter_api free of a hard dep on the
    detectors package (which imports back into adapter_api in some setups).
    """
    from detectors.profiles import DEFAULT_PROFILE  # local import: avoid cycle
    adapters = adapters if adapters is not None else load_adapters()
    a = adapter_for(project, adapters)
    if a is not None:
        try:
            prof = a.project_profile()
            if prof is not None:
                return prof
        except Exception:
            pass
    return DEFAULT_PROFILE


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


def struggle_classes_for(project: str,
                         adapters: Optional[list[ProjectAdapter]] = None) -> list[StruggleClass]:
    """Generic friction classes + any the project's private adapter contributes.

    Generic classes always apply; the adapter's project-specific ones are appended
    (a project with no overlay gets exactly the generic set).
    """
    adapters = adapters if adapters is not None else load_adapters()
    classes = list(generic_struggle_classes())
    a = adapter_for(project, adapters)
    if a is not None:
        try:
            classes.extend(a.struggle_classes())
        except Exception:
            pass
    return classes
