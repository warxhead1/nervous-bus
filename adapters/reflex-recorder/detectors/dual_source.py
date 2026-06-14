"""detectors/dual_source.py — GENERIC dual-source-of-truth / hand-sync detector,
parameterized by a ProjectProfile + language packs.

The structural GENERATOR of elongated refactors: the same logical value lives in
two places, kept coherent by hand. It manufactures a heisenbug class (one copy
updated, the other stale, or the two racing) AND forces every migration to touch
both sides. Canonical case (tengine): ExtGpuInfo and GigaHeader/GigaAddresses both
hold camera_state/terrain/entity/orch addresses, CPU-dual-written (schema mask to
both; ``SCHEMA_BUFFER_TO_EXTGPUINFO`` address sync).

Three deterministic signals, no agent reasoning required:
  1. STRUCT/TYPE FIELD OVERLAP — two record definitions sharing >= K identical
     field names matching the project's dual-source FINGERPRINT. The language
     packs (langpacks.py) parse Rust/C ``struct{}``, Go ``type X struct{}``, and
     Python ``@dataclass``/``class`` so the overlap works across all three.
  2. SYNC MAP — a ``<A>_TO_<B>`` table (e.g. ``SCHEMA_BUFFER_TO_EXTGPUINFO``): an
     explicit hand-maintained bridge between two representations.
  3. DUAL-WRITE comment — "writes to both X and Y", "keep ... in sync".

The comment + sync-map signals are project-AGNOSTIC (fired in Rust+Python+Go).
The struct-overlap FINGERPRINT is the one semantic an adapter supplies via its
ProjectProfile (tengine: ``*_addr`` device-address tables). With no profile, a
high-overlap generic fingerprint fires zero-config on any repo.

Rung: ELIMINATE (collapse to one source of truth) — feeds the deletion backlog.

──────────────────────────────────────────────────────────────────────────────
Authoring a sibling profile (e.g. tachyonac-engine, Go) — ~30 lines:

    from detectors.profiles import (
        ProjectProfile, StructOverlapStrategy, CommentRegexStrategy)

    TACHYONAC_PROFILE = ProjectProfile(
        project="tachyonac-engine",
        scan_roots=("internal", "cmd"),
        source_exts=(".go",),
        twin_suffixes=("_legacy", "_v2", "_new", "_old"),
        dual_source_fingerprints=(
            # ALL strategies run; their candidates are unioned + deduped.
            # (a) two structs both carrying the settlement-score columns:
            StructOverlapStrategy(name="settlement-score columns",
                                  field_pattern=r"(_score|_settlement|_stream)$",
                                  min_overlap=3),
            # (b) tachyonac's "Transitional dual-write" / "legacy stream" phrasing:
            CommentRegexStrategy(name="legacy-stream bridge",
                                 pattern=r"transitional dual-write|legacy stream|NBUS_STREAM"),
        ),
        # color-space / enum X_TO_Y constants are never sync maps; tachyonac has
        # none, but a Rust vision project would add e.g. r"LINEAR_SRGB_TO_" here.
        sync_map_excludes=(),
        extra_anti_fence=(),
    )

The fingerprint is a list of PLUGGABLE STRATEGIES — addr-table/struct-overlap is
just one built-in. A project whose dual-source is CHANNEL-LEVEL (e.g. nervous-bus:
``self._publish("x.y.z")`` call strings vs ``schemas/*.json`` file existence)
authors its own ``DualSourceStrategy`` subclass whose ``find(ctx)`` globs schema
files off ``ctx.repo_root`` and diffs them against publish-call channels — and
lists it here, NO engine edit. An adapter returns this profile from its
``project_profile()`` method (or the engine falls back to DEFAULT_PROFILE).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Iterable, Optional

from detectors.base import BaseDetector, PatternCandidate
from detectors.langpacks import extract_all
from detectors.profiles import DEFAULT_PROFILE, ProjectProfile, ScanContext

# A hand-sync mapping table identifier: NAME_TO_OTHER (>=1 underscore each side).
_SYNC_MAP = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_TO_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\b")
# ...but X_TO_Y also names unit conversions, VK sampler enums, color-space / pose
# maps, and field-offset consts — those are NOT hand-sync bridges. Exclude them.
# (hearth ANTI: LINEAR_SRGB_TO_*, BLAZEPOSE_TO_COCO, ALPHA_TO_COVERAGE.)
_NOT_SYNC_MAP = re.compile(
    r"(?ix)(_OFFSET$|^VK_|CLAMP_TO|MIRROR_|_TO_(EDGE|BORDER|GENERAL|COLOR|DEG|RAD|MB|KB|GB|"
    r"METERS|MS|M|COVERAGE|COCO|RGB|SRGB|LINEAR|HSV|YUV|XYZ|LAB)$"
    r"|^(RAD|BYTES|DEG|LINEAR_SRGB|SRGB|BLAZEPOSE|ALPHA)_TO_"
    r"|^EQUAL_TO|^HASH_|^EC_|^SPILL_TO|_TO_TRACK)")

# Dual-write comments.
_DUAL_WRITE = re.compile(
    r"(?ix)(write\w*\s+to\s+both|keep\w*\s+.{0,40}\s+in\s+sync|sync\w*\s+.{0,40}\s+to\s+both"
    r"|writes?\s+both\s+\w+\s+AND\s+\w+|dual[\s-]?write|auto[\s-]?sync\w*\s+.{0,30}\s+to)")

# Field-name noise to ignore in overlap (padding/reserved/common scalars don't
# signal parallel data).
_NOISE_FIELDS = {"reserved", "_reserved", "padding", "pad", "_pad", "valid", "count",
                 "flags", "size", "magic", "version", "id", "type", "data", "header",
                 "x", "y", "z", "w", "width", "height", "name", "len", "length",
                 "delta_time", "frame_index"}
_MIN_FIELDS = 3  # a struct needs at least this many meaningful fields to be considered


@dataclass
class DualHit:
    kind: str            # "struct_overlap" | "sync_map" | "dual_write"
    anchor: str          # stable identifier
    detail: str
    evidence: list = dc_field(default_factory=list)


def _iter_source(repo: Path, profile: ProjectProfile) -> Iterable[Path]:
    roots = [repo / r for r in profile.scan_roots] or [repo]
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in profile.source_exts:
                continue
            # dual_source never scans test files (synthetic structs are noise).
            if profile.should_skip(str(p)) or "/tests/" in str(p) or "/test/" in str(p):
                continue
            yield p


def _meaningful_fields(raw: set[str]) -> set[str]:
    return {n for n in raw if n.lower() not in _NOISE_FIELDS and len(n) > 2}


def scan(repo_root: str, *, profile: ProjectProfile = DEFAULT_PROFILE) -> list[DualHit]:
    """Scan *repo_root* for dual-source signals, parameterized by *profile*.

    Findings are deduplicated by content hash (kind:anchor) so worktree-copy
    duplication cannot inflate one structural fact into N hits.
    """
    repo = Path(repo_root)
    structs: dict[str, tuple[set[str], str]] = {}   # name -> (fields, file)
    texts: list[tuple[str, str]] = []                # (rel, full-text)
    sync_maps: dict[str, str] = {}                   # MAP_NAME -> file
    sync_excludes = [re.compile(rx) for rx in profile.sync_map_excludes]

    for path in _iter_source(repo, profile):
        try:
            txt = path.read_text(errors="replace")
        except Exception:
            continue
        try:
            rel = str(path.relative_to(repo))
        except ValueError:
            rel = str(path)
        texts.append((rel, txt))
        # LANGUAGE PACK: parse record types for this file's language.
        for name, raw_fields in extract_all(txt, path.suffix):
            fields = _meaningful_fields(raw_fields)
            if len(fields) >= _MIN_FIELDS:
                # keep the definition with the most fields if declared twice
                if name not in structs or len(fields) > len(structs[name][0]):
                    structs[name] = (fields, rel)
        for m in _SYNC_MAP.finditer(txt):
            sync_maps.setdefault(m.group(1), rel)

    hits: list[DualHit] = []
    seen: set[str] = set()

    def _add(kind: str, anchor: str, detail: str, evidence: list) -> None:
        key = f"{kind}:{anchor}"
        if key not in seen:
            seen.add(key)
            hits.append(DualHit(kind=kind, anchor=anchor, detail=detail,
                                evidence=evidence))

    # 1) PLUGGABLE FINGERPRINT STRATEGIES — built once, every strategy runs over
    #    the shared ScanContext; candidates are unioned + deduped. addr-table /
    #    struct-overlap is one built-in strategy; comment-regex is another; a
    #    sibling's channel-vs-schema strategy slots in here with no engine edit.
    ctx = ScanContext(repo_root=repo, profile=profile, structs=structs, texts=texts)
    strategies = profile.dual_source_fingerprints or DEFAULT_PROFILE.dual_source_fingerprints
    for strat in strategies:
        try:
            for c in strat.find(ctx):
                _add(c.kind, c.anchor, c.detail, list(c.evidence))
        except Exception:
            continue  # one broken strategy never sinks the scan

    # 2) sync maps (excluding unit conversions / VK enums / color-space / offsets)
    for mp, rel in sync_maps.items():
        if _NOT_SYNC_MAP.search(mp) or any(rx.search(mp) for rx in sync_excludes):
            continue
        _add("sync_map", mp,
             f"hand-maintained sync map {mp} ({rel})",
             [f"{rel}: {mp} bridges two representations — collapse to one source"])

    # 3) dual-write comments (generic, project-agnostic phrasing)
    for rel, txt in texts:
        for i, ln in enumerate(txt.splitlines()):
            if _DUAL_WRITE.search(ln) and len(ln) < 200:
                _add("dual_write", f"{rel}:{i + 1}",
                     f"dual-write at {rel}:{i + 1}",
                     [f"{rel}:{i + 1}  {ln.strip()[:160]}"])

    return hits


class GenericDualSourceDetector(BaseDetector):
    """Project-agnostic dual-source detector. Pass a ProjectProfile to supply the
    struct-overlap fingerprint; omit it to run zero-config on any repo."""

    DETECTOR_NAME = "dual_source"

    def __init__(
        self,
        conn=None,
        repo_root: Optional[str] = None,
        profile: Optional[ProjectProfile] = None,
    ):
        self._profile = profile or DEFAULT_PROFILE
        self._repo = repo_root or os.environ.get(
            "REFLEX_SCAN_REPO", str(Path.cwd()))
        if conn is not None:
            super().__init__(conn)

    def detect(self, conn) -> list[PatternCandidate]:
        import subprocess
        project = self._profile.project
        try:
            head = subprocess.run(["git", "-C", self._repo, "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            head = "WORKTREE"
        out: list[PatternCandidate] = []
        for h in scan(self._repo, profile=self._profile):
            out.append(PatternCandidate(
                project=project, pattern_name=f"dual_source:{h.kind}",
                detector=self.DETECTOR_NAME,
                signature=f"{project}:dual_source:{h.kind}:{h.anchor}", occurrences=1,
                evidence=[h.detail] + h.evidence, run_ids=[head or "WORKTREE"],
                proposed_remediation="Collapse to one source of truth; delete the redundant "
                                     "representation and any hand-sync between them.",
                extra={"kind": h.kind, "anchor": h.anchor},
            ))
        return out
