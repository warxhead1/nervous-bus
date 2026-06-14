"""detectors/stale_fence.py — GENERIC stale-workaround ("Chesterton's fence whose
reason expired") detector, parameterized by a ProjectProfile.

The single most expensive elongated-refactor seed observed: a workaround/revert is
left in place ("X was causing issues — stick with Y", "DEPRECATED: use Z instead"),
the bug that justified it gets fixed LATER, but nobody removes the fence. The dead
path coexists with the live one, the type system can't tell them apart, and every
fresh agent has to relearn which side is the corpse. The canonical case (tengine):
``world_state.glsl`` kept ``ExtGpuInfo.camera_state_addr`` because "Giga registry
lookup was causing issues", months after the bug was fixed → a months-long
dual-ABI migration.

This is a CODE+GIT scanner promoted from the tengine reflex adapter into the
engine. The fence-language regexes, FIX anti-filter, named-alt detection, and
git-blame recurrence are project-AGNOSTIC and fired cleanly in Rust + Python + Go
during the cross-project probe. The only project-specific bits — which dirs to
walk, which extensions count, which migration-twin suffixes name the deferred path
— come from a :class:`ProjectProfile` (``DEFAULT_PROFILE`` works zero-config on any
repo).

Recurrence: code-scanners use git HEAD short-sha as ``run_id`` so a fence that
survives N synthesis scans accrues recurrence — turning "ignored debt" into a
climbing number, not a one-off alarm.

Rung: ELIMINATE (remove the fence + the dead path), feeding the deletion backlog.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from detectors.base import BaseDetector, PatternCandidate
from detectors.profiles import DEFAULT_PROFILE, ProjectProfile

# Fence language: a deferred/abandoned-better-path marker. Two tiers.
# STRONG = explicitly names a replacement or a documented failure-of-the-better-path.
_STRONG_FENCE = re.compile(
    r"(?ix)"
    r"( was \s+ causing \s+ issues"
    r"| stick \s+ with \s+ what \s+ works"
    r"| DEPRECATED \s*:? \s* (use|see|prefer)"
    r"| use \s+ \w+ \s+ instead"
    r"| reverted? \b .{0,40} (because|due \s+ to|was \s+ causing|broke|broken)"
    r"| fall(ing)? \s* back \s+ to .{0,40} (because|until|since)"
    r"| temporar(y|ily) \b .{0,40} (until|because|workaround)"
    r")"
)
# WEAK = generic debt markers; lower precision, kept for recall but down-weighted.
_WEAK_FENCE = re.compile(r"(?ix)\b(WORKAROUND|HACK|FIXME|XXX|do \s* not \s+ use|don'?t \s+ use)\b")

# Anti-patterns: lines that LOOK like a fence but describe CORRECT current code or are
# generic guidance — not a deprecated path. "🔥 FIX: use X instead of Y" annotates the
# chosen-correct code; "use this/these instead of raw access" is API guidance.
_ANTI_FENCE = re.compile(
    r"(?ix)"
    r"( \b FIX \b \s* (\# \d+ \s*)? :?"           # FIX / FIX 2 annotations
    r"| use \s+ (this|these|those|that|it|them) \s+ instead"  # guidance, no named symbol
    r")"
)

# Comment-line heuristic (we only want fences in COMMENTS, not string literals).
_COMMENT = re.compile(r"^\s*(//|#|\*|/\*|--|;)")

# A replacement FILE/MODULE named in a fence: "DEPRECATED: Use engine.rs",
# "use source/kernel.rs instead", "see foo/bar.py". We capture path-like tokens
# with a source extension so we can check the file exists on disk (a fence that
# names a still-present replacement is a high-confidence stale path).
_NAMED_FILE = re.compile(
    r"(?ix)\b(?:use|see|prefer|moved\s+to|replaced\s+by)\b[^\n]{0,60}?"
    r"\b([\w./\-]+\.(?:rs|go|py|c|h|cc|cpp|hpp|ts|tsx|js|glsl|slang|comp))\b")

# Symbol-at-or-below-fence: the code def the fence guards. Generic across langs.
_SYMBOL = re.compile(
    r"(?x)\b(?:"
    r"struct\s+(\w+)"                          # struct Name (C/Rust/GLSL)
    r"|type\s+(\w+)\s+struct"                  # Go: type Name struct
    r"|(?:class|def)\s+(\w+)"                  # Python class/def
    r"|fn\s+(\w+)"                             # Rust fn Name
    r"|func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("     # Go func [recv] Name(
    r"|(?:[\w:<>*&]+\s+)+(\w+)\s*\("           # C/GLSL fn:  ret name(
    r"|(\w+)\s*:\s*[\w:<>]+\s*[,;=]"           # field: Type
    r")"
)


@dataclass
class FenceHit:
    file: str
    line: int
    text: str
    symbol: str          # the code symbol the fence guards (next non-comment def)
    named_alt: Optional[str]
    introduced: str      # git commit sha that introduced the fence line
    introduced_date: str
    later_fixes: int     # # of fix/perf commits to this file AFTER the fence landed
    strong: bool
    named_file: Optional[str] = None        # replacement file/module named in the fence
    named_file_exists: Optional[bool] = None  # whether that file is present on disk


def _iter_source(repo: Path, profile: ProjectProfile) -> Iterable[Path]:
    roots = [repo / r for r in profile.scan_roots] or [repo]
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in profile.source_exts:
                continue
            if profile.should_skip(str(p)):
                continue
            yield p


def _resolve_named_file(repo: Path, fence_file_rel: str, named: str) -> bool:
    """True if *named* (a file/module mentioned in a fence) exists on disk.

    Tries, in order: relative to the fence file's own directory (``Use engine.rs``
    almost always means the sibling), relative to the repo root, and finally a
    basename match anywhere in the tree (bounded). Conservative: a bare basename
    that matches SOMEWHERE counts, because the fence asserting "use engine.rs"
    only makes sense if some engine.rs exists.
    """
    named = named.strip().lstrip("./")
    fence_dir = (repo / fence_file_rel).parent
    # 1) sibling / relative-to-fence-dir
    if (fence_dir / named).exists():
        return True
    # 2) relative to repo root
    if (repo / named).exists():
        return True
    # 3) basename match anywhere (bounded scan)
    base = os.path.basename(named)
    if base and base != named:
        # path with dirs given but not found above -> treat as absent
        return False
    found = 0
    for p in repo.rglob(base):
        if p.is_file():
            return True
        found += 1
        if found > 50:
            break
    return False


def _next_symbol(lines: list[str], idx: int) -> str:
    """The first function/struct/type/field def at or below the fence comment."""
    for j in range(idx, min(idx + 6, len(lines))):
        m = _SYMBOL.search(lines[j])
        if m:
            g = next((x for x in m.groups() if x), None)
            if g:
                return g
    return ""


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return ""


def _blame_line(repo: Path, file: Path, line: int) -> tuple[str, str]:
    """(sha, iso-date) that introduced *line*, via git blame --porcelain."""
    try:
        rel = str(file.relative_to(repo))
    except ValueError:
        rel = str(file)
    out = _git(repo, "blame", "-L", f"{line},{line}", "--porcelain", "--", rel)
    if not out:
        return ("", "")
    sha = out.split(" ", 1)[0][:12] if out else ""
    date = ""
    for ln in out.splitlines():
        if ln.startswith("author-time "):
            import datetime as dt
            try:
                date = dt.datetime.utcfromtimestamp(int(ln.split()[1])).strftime("%Y-%m-%d")
            except Exception:
                pass
            break
    return (sha, date)


def _later_fix_commits(repo: Path, file: Path, since_date: str) -> int:
    """# of fix(/perf( commits touching this file AFTER the fence landed —
    a proxy for 'the bug that justified the fence may since have been fixed'."""
    if not since_date:
        return 0
    try:
        rel = str(file.relative_to(repo))
    except ValueError:
        rel = str(file)
    out = _git(repo, "log", f"--since={since_date}", "--pretty=%s", "--", rel)
    n = 0
    for s in out.splitlines():
        if re.match(r"(?i)^(fix|perf)\b", s.strip()):
            n += 1
    return n


def scan(
    repo_root: str,
    *,
    profile: ProjectProfile = DEFAULT_PROFILE,
    blame: bool = True,
    max_files: int = 8000,
) -> list[FenceHit]:
    """Scan *repo_root* for stale-fence comments, parameterized by *profile*.

    Findings are deduplicated by content hash (file:line:text) so worktree-copy
    duplication (the same fence appearing under ``.claude/worktrees/...``) cannot
    inflate a single fence into N hits. (The skip_globs already drop worktree
    copies; the dedup is belt-and-suspenders for symlinked/nested trees.)
    """
    repo = Path(repo_root)
    anti = [_ANTI_FENCE] + [re.compile(rx, re.I) for rx in profile.extra_anti_fence]
    named_alt_re = profile.named_alt_pattern()
    hits: list[FenceHit] = []
    seen_hashes: set[str] = set()
    seen = 0
    for path in _iter_source(repo, profile):
        seen += 1
        if seen > max_files:
            break
        try:
            lines = path.read_text(errors="replace").splitlines()
        except Exception:
            continue
        try:
            rel = str(path.relative_to(repo))
        except ValueError:
            rel = str(path)
        for i, ln in enumerate(lines):
            if not _COMMENT.match(ln):
                continue
            if any(a.search(ln) for a in anti):
                continue  # fix-annotation or generic guidance, not a deprecated path
            strong = bool(_STRONG_FENCE.search(ln))
            if not strong and not _WEAK_FENCE.search(ln):
                continue
            # Dedup by content hash (file:line:text) — Engine hygiene fix.
            chash = f"{rel}:{i + 1}:{ln.strip()}"
            if chash in seen_hashes:
                continue
            seen_hashes.add(chash)
            # Look a couple lines around for a named alternative.
            window = " ".join(lines[max(0, i - 1): i + 3])
            alt_m = named_alt_re.search(window)
            alt = alt_m.group(0) if alt_m else None
            # A replacement FILE named in the fence (e.g. "Use engine.rs").
            named_file: Optional[str] = None
            file_exists: Optional[bool] = None
            fm = _NAMED_FILE.search(window)
            if fm:
                named_file = fm.group(1)
                file_exists = _resolve_named_file(repo, rel, named_file)
            symbol = _next_symbol(lines, i + 1)
            sha, date, later = "", "", 0
            if blame:
                sha, date = _blame_line(repo, path, i + 1)
                later = _later_fix_commits(repo, path, date)
            hits.append(FenceHit(
                file=rel, line=i + 1, text=ln.strip()[:200],
                symbol=symbol, named_alt=alt, introduced=sha, introduced_date=date,
                later_fixes=later, strong=strong,
                named_file=named_file, named_file_exists=file_exists,
            ))
    return hits


def is_stale_candidate(h: FenceHit, *, require_named_replacement_file: bool = False) -> bool:
    """A fence worth surfacing: strong language AND a corroborating signal.

    Base signal (high precision): strong fence language AND (a named alternative
    symbol exists OR the justifying bug likely got fixed later).

    When *require_named_replacement_file* is True (a profile flag), the fence MUST
    additionally name a replacement file that EXISTS on disk. The probe showed
    this lifts hearth fence precision 25%→~90%: ``DEPRECATED: Use engine.rs`` is
    real debt only because ``engine.rs`` is present; a fence naming a file that no
    longer exists is already-resolved and should not fire.
    """
    if not h.strong:
        return False
    if require_named_replacement_file:
        # A present named replacement file IS the corroboration (it proves the
        # deprecated path's successor still exists); the alt-symbol/later-fix
        # signals are not required in this mode.
        return h.named_file is not None and h.named_file_exists is True
    return h.named_alt is not None or h.later_fixes >= 1


class GenericStaleFenceDetector(BaseDetector):
    """Project-agnostic stale-fence detector. Pass a ProjectProfile to sharpen
    precision; omit it to run zero-config on any repo."""

    DETECTOR_NAME = "stale_fence"

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
        project = self._profile.project
        head = _git(Path(self._repo), "rev-parse", "--short", "HEAD").strip() or "WORKTREE"
        out: list[PatternCandidate] = []
        require_file = self._profile.require_named_replacement_file
        for h in scan(self._repo, profile=self._profile):
            if not is_stale_candidate(h, require_named_replacement_file=require_file):
                continue
            anchor = f"{h.file}:{h.symbol or h.line}"
            ev = [
                f"{h.file}:{h.line}  {h.text}",
                f"introduced {h.introduced or '?'} ({h.introduced_date or '?'})",
            ]
            if h.named_alt:
                ev.append(f"named replacement present: {h.named_alt}")
            if h.named_file:
                ev.append(f"names replacement file {h.named_file} "
                          f"({'present' if h.named_file_exists else 'absent'} on disk)")
            if h.later_fixes:
                ev.append(f"{h.later_fixes} fix/perf commit(s) to this file since the "
                          f"fence landed — its reason may be stale")
            out.append(PatternCandidate(
                project=project, pattern_name="stale_fence", detector=self.DETECTOR_NAME,
                signature=f"{project}:stale_fence:{anchor}", occurrences=1, evidence=ev,
                run_ids=[head],
                proposed_remediation=(
                    f"Verify the fence's justifying bug is fixed, then ELIMINATE: repoint "
                    f"callers to {h.named_alt or 'the live path'} and delete the "
                    f"deprecated path."),
                extra={"file": h.file, "line": h.line, "symbol": h.symbol,
                       "named_alt": h.named_alt, "later_fixes": h.later_fixes,
                       "named_file": h.named_file,
                       "named_file_exists": h.named_file_exists},
            ))
        return out
