"""detectors/profiles.py — ProjectProfile + pluggable dual-source STRATEGIES.

The GENERIC structural-debt detectors (``stale_fence`` + ``dual_source``) are
project-agnostic and run zero-config on ANY repo. The *semantics* that sharpen
their precision for a specific project — which directories to walk, which file
extensions count as source, which migration-twin suffixes that project uses,
and (the crucial one) which dual-source FINGERPRINT shape that project's
parallel-representations take — live in a :class:`ProjectProfile`.

Empirically-proven boundary (probe across nervous-bus, hearth, tachyonac-engine,
kb, deer-flow):

  - ENGINE (this default profile)      : fence + dual-write comment scan — fired
    cleanly in Rust + Python + Go with NO project knowledge.
  - ENGINE + LANGUAGE PACKS            : struct/type field-overlap — needs a
    per-language parser (langpacks.py), but the *fingerprint* that decides which
    overlap counts is supplied by the profile.
  - ADAPTER (ProjectProfile override)  : the dual-source fingerprint itself.
    tengine's ``*_addr`` device-address tables are ONE shape; nervous-bus's own
    dual-source is CHANNEL-LEVEL (publish-call channel strings vs schema-file
    existence); tachyonac's is a comment "legacy stream" bridge. Every project's
    dual-source is a different shape.

Because the fingerprint shapes differ structurally (struct-field overlap vs
channel-vs-schema vs comment-regex), ``dual_source_fingerprints`` is a list of
pluggable STRATEGIES, each a :class:`DualSourceStrategy` that takes the scanned
repo context and yields candidates. ``StructOverlapStrategy`` (addr-table /
field-overlap) is just ONE built-in; ``CommentRegexStrategy`` is another, and a
``ChannelSchemaStrategy`` (publish-channel ↔ schema-file) can be added by a
sibling profile in ~30 lines WITHOUT an engine edit — the interface admits it.

Design rule: a ProjectProfile carries DATA (regex strings, suffix tuples,
thresholds) plus a list of STRATEGY objects from a small built-in palette (or a
sibling's own ``DualSourceStrategy`` subclass). It never carries project-specific
imperative glue in the engine. That keeps profiles trivially reviewable.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Pattern


# ── Strategy context ──────────────────────────────────────────────────────────


@dataclass
class ScanContext:
    """Everything a dual-source strategy needs about the scanned repo.

    Built ONCE by the dual_source scan and handed to every strategy, so each
    strategy is a pure function of (parsed repo) → candidates with no I/O of its
    own beyond what it explicitly needs (a ChannelSchemaStrategy, for instance,
    would glob schema files off ``repo_root``).

    Fields
    ------
    repo_root  : absolute repo path (for strategies that need filesystem access,
                 e.g. channel-vs-schema-file existence).
    profile    : the active ProjectProfile.
    structs    : name -> (meaningful field names, relative-file) — populated by
                 the language packs across all scanned files. Empty for languages
                 with no record types.
    texts      : list of (relative-file, full-text) for regex-style strategies.
    """
    repo_root: Path
    profile: "ProjectProfile"
    structs: dict[str, tuple[set[str], str]] = field(default_factory=dict)
    texts: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DualCandidate:
    """A dual-source candidate yielded by a strategy.

    kind   : reported DualHit.kind ("struct_overlap" | "sync_map" | "dual_write"
             | a strategy-defined kind such as "channel_schema_gap").
    anchor : stable identifier (used in the issue signature; never run-scoped).
    detail : human-readable one-liner.
    evidence : supporting lines.
    """
    kind: str
    anchor: str
    detail: str
    evidence: list[str] = field(default_factory=list)


# ── Strategy base + built-ins ─────────────────────────────────────────────────


class DualSourceStrategy(ABC):
    """A pluggable dual-source FINGERPRINT.

    Subclass this to teach the engine a new dual-source shape. The struct-overlap
    (addr-table) strategy is built in; ``CommentRegexStrategy`` is built in; a
    sibling profile can add e.g. a ``ChannelSchemaStrategy`` that scans
    ``self._publish("x.y.z")`` call strings against ``schemas/*.json`` existence
    — purely by authoring a subclass and listing it in the profile.
    """

    name: str = "strategy"

    @abstractmethod
    def find(self, ctx: ScanContext) -> list[DualCandidate]:
        """Yield zero or more dual-source candidates from the scanned repo."""


class StructOverlapStrategy(DualSourceStrategy):
    """Two record types are parallel representations when they share >= K
    identical field NAMES matching ``field_pattern`` (after noise-filtering).

    This is the tengine ``*_addr`` device-address-table fingerprint generalized:
    set ``field_pattern=r"(_addr|_address|_ptr)$"`` for tengine, or ``r"."`` with
    a higher ``min_overlap`` for a zero-config generic overlap on any repo.
    """

    def __init__(self, *, name: str, field_pattern: str, min_overlap: int = 4):
        self.name = name
        self.field_pattern = field_pattern
        self.min_overlap = min_overlap
        self._rx = re.compile(field_pattern)

    def _matching(self, shared: set[str]) -> set[str]:
        return {f for f in shared if self._rx.search(f)}

    def find(self, ctx: ScanContext) -> list[DualCandidate]:
        out: list[DualCandidate] = []
        names = list(ctx.structs)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                shared = ctx.structs[a][0] & ctx.structs[b][0]
                if not shared:
                    continue
                matched = self._matching(shared)
                if len(matched) >= self.min_overlap:
                    out.append(DualCandidate(
                        kind="struct_overlap",
                        anchor=":".join(sorted((a, b))),
                        detail=f"{a} ({ctx.structs[a][1]}) and {b} ({ctx.structs[b][1]}) "
                               f"share {len(matched)} fields matching '{self.name}' — "
                               f"parallel representations",
                        evidence=[f"shared fields ({self.name}): "
                                  f"{', '.join(sorted(matched))[:300]}"],
                    ))
        return out


class CommentRegexStrategy(DualSourceStrategy):
    """A free-text comment-based fingerprint: any source line matching
    ``pattern`` is a dual-source signal (e.g. a project-specific
    "legacy stream"/"transitional dual-write" phrasing not covered by the
    generic dual-write comment scan).

    ``anti`` (optional) is a regex that VETOES a matched line — for project
    fingerprints whose phrasing collides with a by-design pattern (e.g.
    tachyonac's "writes to both <table> and <table>" is debt, but "writes it to
    both nbus:<channel> and nbus:all" is intentional fanout). A line matching
    ``pattern`` but also ``anti`` is dropped.
    """

    def __init__(self, *, name: str, pattern: str, kind: str = "dual_write",
                 anti: Optional[str] = None):
        self.name = name
        self.kind = kind
        self._rx = re.compile(pattern, re.I)
        self._anti = re.compile(anti, re.I) if anti else None

    def find(self, ctx: ScanContext) -> list[DualCandidate]:
        out: list[DualCandidate] = []
        for rel, txt in ctx.texts:
            for i, ln in enumerate(txt.splitlines()):
                if len(ln) < 200 and self._rx.search(ln):
                    if self._anti and self._anti.search(ln):
                        continue
                    out.append(DualCandidate(
                        kind=self.kind,
                        anchor=f"{rel}:{i + 1}",
                        detail=f"{self.name} at {rel}:{i + 1}",
                        evidence=[f"{rel}:{i + 1}  {ln.strip()[:160]}"],
                    ))
        return out


class MultiLineCommentStrategy(DualSourceStrategy):
    """A dual-write fingerprint that spans CONSECUTIVE comment lines.

    Go godoc (and Rust/`//!` doc blocks) routinely split a "writes to both X and
    Y" sentence across two lines — the second table name lands on the next
    comment line. A single-line scan classifies that as NOISE. This strategy
    joins each comment line with the next ``lookahead`` comment lines (default 2)
    and matches ``pattern`` against the joined window, so::

        // computes the Brier score, and writes to both contract_settlement_scores and
        // convergence_learning_records to close the loop.

    is caught (the canonical tachyonac ``scorer.go`` case). The anchor is the
    line where the match BEGINS so it stays stable across edits below it.

    Generic + parameterized: ``lookahead`` and ``pattern`` are supplied by the
    profile; no project name is hardcoded. ``anti`` vetoes a window (e.g. the
    ``nbus:all`` fanout phrasing) exactly as in :class:`CommentRegexStrategy`.
    """

    # Default: "writes to both <a> and <b>" (allow an optional pronoun: "writes
    # IT to both"), possibly split after "and".
    DEFAULT_PATTERN = r"writes?\s+(?:\w+\s+)?to\s+both\b.{0,80}\band\b.{0,120}"
    # The lead trigger that must START on the anchor line (so the same multi-line
    # match is not re-emitted from every earlier comment line in the block).
    DEFAULT_LEAD = r"writes?\s+(?:\w+\s+)?to\s+both\b"

    def __init__(self, *, name: str, pattern: Optional[str] = None,
                 lead: Optional[str] = None, lookahead: int = 2,
                 kind: str = "dual_write", anti: Optional[str] = None):
        self.name = name
        self.kind = kind
        self.lookahead = max(1, lookahead)
        self._rx = re.compile(pattern or self.DEFAULT_PATTERN, re.I | re.S)
        self._lead = re.compile(lead or self.DEFAULT_LEAD, re.I)
        self._anti = re.compile(anti, re.I) if anti else None
        self._is_comment = re.compile(r"^\s*(//|#|\*|/\*|--|;|!)")

    def _strip_comment(self, ln: str) -> str:
        # Drop a leading comment marker so joined text reads as prose.
        return re.sub(r"^\s*(//+!?|#|\*|/\*|--|;)\s?", " ", ln).rstrip()

    def find(self, ctx: ScanContext) -> list[DualCandidate]:
        out: list[DualCandidate] = []
        for rel, txt in ctx.texts:
            lines = txt.splitlines()
            for i, ln in enumerate(lines):
                if not self._is_comment.match(ln):
                    continue
                # The lead trigger must START on THIS line — otherwise the same
                # multi-line match would be re-emitted from every earlier comment
                # line whose joined window happens to reach the trigger.
                if not self._lead.search(self._strip_comment(ln)):
                    continue
                # Join this comment line with up to `lookahead` following comment
                # lines (stop at the first non-comment line).
                window_parts = [self._strip_comment(ln)]
                for k in range(1, self.lookahead + 1):
                    j = i + k
                    if j >= len(lines) or not self._is_comment.match(lines[j]):
                        break
                    window_parts.append(self._strip_comment(lines[j]))
                if len(window_parts) < 2:
                    continue  # single-line case is the CommentRegexStrategy's job
                window = " ".join(window_parts)
                if len(window) > 400:
                    window = window[:400]
                if not self._rx.search(window):
                    continue
                if self._anti and self._anti.search(window):
                    continue
                out.append(DualCandidate(
                    kind=self.kind,
                    anchor=f"{rel}:{i + 1}",
                    detail=f"{self.name} (multi-line) at {rel}:{i + 1}",
                    evidence=[f"{rel}:{i + 1}  {window.strip()[:200]}"],
                ))
        return out


# Generic field-overlap: any two structs sharing many identical meaningful field
# names are parallel representations — regardless of project. Higher threshold
# than a project-specific fingerprint because, without semantic narrowing, more
# overlap is demanded to stay precise.
def generic_overlap_strategy() -> StructOverlapStrategy:
    return StructOverlapStrategy(
        name="identical-field overlap", field_pattern=r".", min_overlap=5)


# ── ProjectProfile ────────────────────────────────────────────────────────────


@dataclass
class ProjectProfile:
    """Per-project semantic layer for the generic structural-debt detectors.

    Every field has a sensible ZERO-CONFIG default that works on any repo. An
    adapter overrides only what sharpens its project's precision.

    Fields
    ------
    project              : project name stamped onto emitted candidates.
    scan_roots           : repo-relative dirs to walk (``["."]`` = whole repo).
    source_exts          : file extensions considered source (not docs/generated).
    skip_globs           : path substrings to skip (vendored / generated / and —
                           critically — ``.claude/worktrees`` so worktree copies
                           don't multiply a single fence into N duplicate hits).
    twin_suffixes        : migration-twin dialect (``_giga``/``_v2``/``_legacy``):
                           a symbol ``foo_v2`` near a fence names the deferred path.
    dual_source_fingerprints : ordered list of pluggable :class:`DualSourceStrategy`
                           objects. ALL run; their candidates are unioned and
                           deduped by (kind, anchor). Empty => generic overlap.
    sync_map_enabled     : whether the generic ``X_TO_Y`` sync-map scan runs at
                           all. DEFAULT **OFF**: the cross-project probe proved
                           sync-map detection is nearly all false-positive outside
                           tengine (color-space / pose / dispatch / lookup tables
                           dominate). tengine flips it ON; every other profile
                           leaves it off. A profile that turns it on SHOULD also
                           supply ``sync_map_excludes`` for its domain anti-maps.
    sync_map_excludes    : extra regexes excluding domain ``X_TO_Y`` constants that
                           are NOT hand-sync bridges (hearth: color-space maps).
                           Only consulted when ``sync_map_enabled`` is True.
    extra_anti_fence     : extra regexes marking lines that look like a fence but
                           describe correct current code / generic guidance.
    test_file_globs      : path substrings marking TEST files to exclude from the
                           dual_source scan (synthetic structs + test dual-write
                           comments are noise: ``nbus_test.go``,
                           ``test_*_dual_write.py`` inflate FP). Defaults cover the
                           common ``/tests/``, ``/test/``, ``_test.``, ``test_``
                           conventions; a profile appends project-specific names.
    require_named_replacement_file : raise stale_fence precision by only flagging a
                           fence that NAMES a replacement file/module when that file
                           actually EXISTS on disk (hearth ``DEPRECATED: Use
                           engine.rs`` → check ``engine.rs`` is present). Off by
                           default (generic repos rarely name a file); hearth turns
                           it on (25%→~90% fence precision per the probe).
    """

    project: str = "unknown"
    scan_roots: tuple[str, ...] = (".",)
    source_exts: tuple[str, ...] = (
        ".py", ".rs", ".go", ".c", ".h", ".cc", ".cpp", ".hpp",
        ".ts", ".tsx", ".js", ".glsl", ".slang", ".comp", ".vert", ".frag",
        ".java", ".kt", ".swift",
    )
    skip_globs: tuple[str, ...] = (
        "/.git/", "/node_modules/", "/target/", "/vendor/", "/__pycache__/",
        "/.claude/worktrees/", "/.worktrees/", "/generated/", "/slang_generated/",
        "/third_party/", "/dist/", "/build/", "/.venv/", "/site-packages/",
        ".bak", ".min.js",
    )
    twin_suffixes: tuple[str, ...] = (
        "_v2", "_new", "_legacy", "_old", "_safe", "_impl2", "_giga", "_ex",
    )
    dual_source_fingerprints: tuple[DualSourceStrategy, ...] = ()
    sync_map_enabled: bool = False
    sync_map_excludes: tuple[str, ...] = ()
    extra_anti_fence: tuple[str, ...] = ()
    test_file_globs: tuple[str, ...] = (
        "/tests/", "/test/", "_test.", "test_", "/testdata/", ".test.",
        "_spec.", ".spec.",
    )
    require_named_replacement_file: bool = False

    def __post_init__(self) -> None:
        # Default to a generic field-overlap strategy when none supplied, so the
        # zero-config path still produces struct-overlap candidates.
        if not self.dual_source_fingerprints:
            self.dual_source_fingerprints = (generic_overlap_strategy(),)

    # ── derived helpers ───────────────────────────────────────────────────────

    def named_alt_pattern(self) -> Pattern:
        """Regex matching a named replacement symbol near a fence, built from
        ``twin_suffixes`` (e.g. ``get_camera_addr_giga`` / ``foo_v2``)."""
        alts = "|".join(re.escape(s.lstrip("_")) for s in self.twin_suffixes)
        return re.compile(rf"\b([a-z][a-z0-9_]*_(?:{alts})\d?)\b")

    def should_skip(self, path_str: str) -> bool:
        """True if *path_str* matches any skip glob (substring match)."""
        return any(g in path_str for g in self.skip_globs)

    def is_test_file(self, path_str: str) -> bool:
        """True if *path_str* is a test file (excluded from dual_source).

        Substring match against ``test_file_globs`` (handles both directory
        conventions like ``/tests/`` and filename conventions like ``_test.go``
        / ``test_foo.py``). Uses the basename for the filename-style markers so a
        directory named ``latest/`` does not trip ``test_``.
        """
        from os.path import basename
        base = basename(path_str)
        for g in self.test_file_globs:
            if g.startswith("/") or g.endswith("/"):
                if g in path_str:
                    return True
            elif g in base:
                return True
        return False


# ── DEFAULT (zero-config) ─────────────────────────────────────────────────────

DEFAULT_PROFILE = ProjectProfile(project="generic")


# ── TENGINE reference profile ─────────────────────────────────────────────────
#
# The jewel. Reproduces the validated tengine results: parallel ``*_addr`` /
# ``_address`` / ``_ptr`` device-address tables across ExtGpuInfo / GigaAddresses
# / DgcLaneContext / GpuInfoState, ``_giga`` / ``_ex`` migration-twin dialect.
# This is exactly the ~30 lines a project authors.

TENGINE_PROFILE = ProjectProfile(
    project="tengine",
    scan_roots=("crates/tengine-dgc-hal",),
    source_exts=(".glsl", ".slang", ".comp", ".vert", ".frag", ".rs", ".c", ".h"),
    # tengine's authoritative *layout.h device-address tables live under
    # csrc/generated/ — the live detector scans them on purpose (they ARE the
    # parallel representations). So tengine overrides skip_globs to drop only
    # slang_generated/third_party/target/worktrees, NOT /generated/. (The engine
    # DEFAULT skips /generated/ because for most repos it is build output noise.)
    skip_globs=(
        "/.git/", "/node_modules/", "/target/", "/third_party/",
        "/slang_generated/", "/.claude/worktrees/", "/.worktrees/",
        "/tests/", ".bak",
    ),
    twin_suffixes=("_giga", "_v2", "_new", "_safe", "_ex"),
    dual_source_fingerprints=(
        StructOverlapStrategy(
            name="device-address table",
            field_pattern=r"(_addr|_address|_ptr)$",
            min_overlap=4,
        ),
    ),
    # tengine is the ONE project where sync maps are real signal
    # (SCHEMA_BUFFER_TO_EXTGPUINFO etc. are hand-maintained address bridges).
    # Every other profile leaves sync_map_enabled at its False default.
    sync_map_enabled=True,
    sync_map_excludes=(),
    extra_anti_fence=(),
)
