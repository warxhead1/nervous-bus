"""project — the one cwd -> project resolver (stdlib only).

Six modules used to normalise a working directory into a project name on their own and
disagreed on the same path, which splits one project into many signatures and parks the
rest in "(unknown)". Everything that needs "which project is this cwd" calls this.

    from project import project_of, project_of_dirname
    project_of("/home/me/data2/worktrees/kb/agent-ab12cd34")   # 'kb'
    project_of_dirname("-home-me-projects-hearth-loom")         # 'hearth-loom'

Order of resolution for project_of(cwd):
  1. cron family   .../auto-<family>-run-<N>-<ts>          -> 'auto-<family>'
  2. scratch/home  $HOME itself, dot-dirs under it, /tmp, claude-tmp  -> 'misc'
  3. under a configured root (NBUS_PROJECT_ROOTS) the first directory below it stands
     for the project: `git rev-parse --git-common-dir` on it (or, for a plain directory
     of worktrees, on the worktrees still inside it) names the repo; the directory
     name is the answer when git cannot say (deleted, not a repo)
  4. anywhere else, git on the cwd itself when it exists (cached, bounded timeout)
  5. generic layout: .claude/worktrees, */worktrees/<project>/<name>,
     <project>.worktrees/..., agent-<id> / wt-* basename -> parent directory
None means "cannot tell". Callers keep their own sentinel for it; the basename of an
unrecognised path is never invented.

Configuration (environment, read on every call):
  NBUS_PROJECT_ROOTS    os.pathsep-separated directories whose children are projects.
                        Default: ~/projects, ~/data2/worktrees, ~/data2/orca,
                        ~/orca/projects, ~/orca/workspaces
  NBUS_PROJECT_ALIASES  'old=new,old2=new2' folds renamed projects onto one name.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import time
from typing import Optional

MISC = "misc"
GIT_TIMEOUT_S = 1.5

# cron-style sessions run in a fresh dir per run; the family is the stable identity.
_CRON_RE = re.compile(r"(?:^|-)(auto-.+?)-run-\d+-\d{8}T\d{4,6}Z?$")
# per-lane directory names that are never a project: agent-<hex id>, wt-*, wf_<hex>-*
_LANE_RE = re.compile(r"^(?:agent-[0-9a-f]{6,}|wt-.+|wf_[0-9a-f]+-.*)$")
# a date-stamped directory is a run or batch container (codex-2026-08-16), never a project
_DATED_RE = re.compile(r"(?:^|[-_.])(?:\d{4}-\d{2}-\d{2}|\d{8})(?:$|[-_.T])")
# directory names that hold projects or sandboxes but are not projects themselves
_NOT_PROJECT = frozenset({
    "work", "tmp", "home", "root", "var", "usr", "opt", "srv", "mnt", "src", "data",
    "projects", "worktrees", "workspaces", "workspace", "repos", "code", "misc",
})
_GIT_ENV_DROP = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
})


def _home() -> str:
    return os.path.normpath(os.path.expanduser("~"))


def roots() -> tuple[str, ...]:
    env = os.environ.get("NBUS_PROJECT_ROOTS")
    if env is not None:
        raw = [p for p in env.split(os.pathsep) if p.strip()]
    else:
        h = _home()
        raw = [os.path.join(h, *p.split("/")) for p in (
            "projects", "data2/worktrees", "data2/orca", "orca/projects", "orca/workspaces")]
    return tuple(dict.fromkeys(os.path.normpath(os.path.expanduser(p)) for p in raw))


def aliases() -> tuple[tuple[str, str], ...]:
    out = []
    for pair in os.environ.get("NBUS_PROJECT_ALIASES", "").split(","):
        old, sep, new = pair.partition("=")
        if sep and old.strip() and new.strip():
            out.append((old.strip(), new.strip()))
    return tuple(out)


def cache_clear() -> None:
    _resolve.cache_clear()
    _container_repo.cache_clear()
    _git_repo.cache_clear()
    _children.cache_clear()


def _finish(name: Optional[str], alias: tuple) -> Optional[str]:
    if name is None:
        return None
    return dict(alias).get(name, name)


def _usable(seg: str, declared: bool = False) -> Optional[str]:
    """A directory name as a project name, or None when it cannot be one.

    declared=True is a child of a configured root: the operator said these are
    projects, so only dot-dirs and per-lane names are refused, not generic names.
    """
    for suffix in (".worktrees", ".git"):          # foo.worktrees/, bare foo.git
        if seg.endswith(suffix) and len(seg) > len(suffix):
            seg = seg[: -len(suffix)]
    if not seg or seg.startswith(".") or _LANE_RE.match(seg):
        return None
    if not declared and (seg in _NOT_PROJECT or _DATED_RE.search(seg)):
        return None
    return seg


def _under(path: str, root: str) -> Optional[list[str]]:
    """Path segments below root, or None when path is not strictly below it."""
    if path == root or not path.startswith(root.rstrip("/") + "/"):
        return None
    return [s for s in path[len(root):].split("/") if s]


def _is_misc(path: str, home: str, root_list: tuple) -> bool:
    if path in root_list:
        return True                     # a session started in the projects dir itself
    if any(_under(path, r) is not None for r in root_list):
        return False
    parts = [s for s in path.split("/") if s]
    if "claude-tmp" in parts or parts[:1] == ["tmp"] or parts[:2] == ["var", "tmp"]:
        return True
    if path == home:
        return True
    rest = _under(path, home)
    return bool(rest) and rest[0].startswith(".")


def _root_child(path: str, root_list: tuple) -> Optional[tuple[str, str]]:
    """(directory, name) of the first path segment below whichever root holds path."""
    for r in root_list:
        rest = _under(path, r)
        if rest:
            return os.path.join(r, rest[0]), rest[0]
    return None


def _generic_layout(path: str) -> Optional[str]:
    parts = [s for s in path.split("/") if s]
    for seg in parts:
        if seg.endswith(".worktrees"):
            name = _usable(seg)
            if name:
                return name
    for i in range(len(parts) - 2):
        if parts[i + 1] == ".claude" and i + 2 < len(parts) and parts[i + 2] == "worktrees":
            name = _usable(parts[i])
            if name:
                return name
    for i, seg in enumerate(parts[:-1]):
        if seg == "worktrees" and i > 0 and parts[i - 1] != ".claude":
            name = _usable(parts[i + 1])
            if name:
                return name
    if len(parts) >= 2 and _LANE_RE.match(parts[-1]):
        return _usable(parts[-2])
    return None


@functools.lru_cache(maxsize=1 << 16)
def _git_repo(path: str, home: str) -> Optional[tuple[str, str]]:
    """(name, repo directory) of the repository path belongs to, when path exists."""
    if not os.path.isdir(path):
        return None
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    common = os.path.normpath(out if os.path.isabs(out) else os.path.join(path, out))
    base = os.path.basename(common)
    if base == ".git":
        repo = os.path.dirname(common)
        name = os.path.basename(repo)
    elif os.path.basename(os.path.dirname(common)) == "modules" and \
            os.path.basename(os.path.dirname(os.path.dirname(common))) == ".git":
        repo, name = common, base            # submodule: its own name
    else:
        repo = common
        name = base[:-4] if base.endswith(".git") and len(base) > 4 else base
    if repo in (home, "/", ""):               # a dotfiles repo at $HOME is not a project
        return None
    return name, repo


def _git_project(path: str, home: str) -> Optional[str]:
    hit = _git_repo(path, home)
    return _usable(hit[0]) if hit else None


_PROBE_KIDS = 40       # directories examined inside a container
_PROBE_BUDGET_S = 3.0  # wall-clock cap on one container's vote


@functools.lru_cache(maxsize=1 << 14)
def _container_repo(container: str, home: str) -> Optional[str]:
    """The repo a root's child directory stands for.

    The child is either a checkout itself (git answers for it) or a plain directory of
    worktrees named after a lane rather than the repo (data2/worktrees/<lane>/agent-*).
    For the latter the worktrees still inside it vote and must all agree; a nested
    clone of some other repo says nothing about what the container is and is skipped.
    Judging by the survivors is what lets a deleted worktree resolve at all.
    """
    own = _git_project(container, home)
    if own:
        return own
    try:
        kids = sorted(e.path for e in os.scandir(container)
                      if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return None
    seen: set = set()
    deadline = time.monotonic() + _PROBE_BUDGET_S
    for kid in kids[:_PROBE_KIDS]:
        if time.monotonic() > deadline:        # a hung mount must not stall a caller
            return None
        hit = _git_repo(kid, home)
        if hit is None or hit[1] == kid:      # not a repo, or a standalone clone
            continue
        name = _usable(hit[0])
        if name:
            seen.add(name)
        if len(seen) > 1:
            return None
    return next(iter(seen), None)


def _cron_family(cwd: str, alias: tuple) -> Optional[str]:
    for seg in reversed(cwd.split("/")):
        m = _CRON_RE.match(seg)
        if m:
            return _finish(m.group(1), alias)
    return None


@functools.lru_cache(maxsize=1 << 16)
def _resolve(cwd: str, root_list: tuple, home: str, alias: tuple, cron: bool = True) -> Optional[str]:
    if cron:
        family = _cron_family(cwd, alias)
        if family:
            return family
    if _is_misc(cwd, home, root_list):
        return MISC
    name = None
    child = _root_child(cwd, root_list)
    if child and _usable(child[1], declared=True):
        name = _container_repo(child[0], home) or _usable(child[1], declared=True)
    return _finish(name or _git_project(cwd, home) or _generic_layout(cwd), alias)


def _clean(cwd) -> Optional[str]:
    if not isinstance(cwd, str):
        return None
    cwd = cwd.strip()
    return os.path.normpath(cwd) if cwd.startswith("/") else None


def _is_checkout(name: str, root_list: tuple, home: str) -> bool:
    """True when name is itself a checkout under some root (not just a lane's name)."""
    for r in root_list:
        path = os.path.join(r, name)
        hit = _git_repo(path, home)
        if hit and hit[1] == path:
            return True
    return False


def project_of_transcript(path) -> Optional[str]:
    """Project a Claude transcript's session was launched in, from the project-dir
    component of its path ('.../-home-me-data2-orca-app-to-market-wardr/<session>/...').
    None when the path has no such component or the launch dir was scratch/home."""
    for part in str(path).split("/"):
        if len(part) > 1 and part.startswith("-"):
            name = project_of_dirname(part)
            return None if name == MISC else name
    return None


def project_of(cwd, transcript=None) -> Optional[str]:
    """Project name for a working directory, or None when it cannot be told.

    transcript, when given, is the path of the Claude transcript the cwd came from. A
    subagent's own cwd is often a worktree under a lane-named directory
    (data2/worktrees/<lane>/agent-<id>) whose repo is only recoverable from the session
    that launched it, so the launch directory answers whenever the cwd's own answer is
    not itself a checkout. A cwd that names a real checkout (a cross-repo dispatch) and
    a cron family keep their own answer.
    """
    path = _clean(cwd)
    home, root_list, alias = _home(), roots(), aliases()
    own = _resolve(path, root_list, home, alias) if path else None
    if transcript is None:
        return own
    launch = project_of_transcript(transcript)
    if not launch or own == launch:
        return own
    if own is None or own == MISC:
        return launch
    if _cron_family(path, alias) or _is_checkout(own, root_list, home):
        return own
    return launch


def repo_of(cwd) -> Optional[str]:
    """The repository directory name for a cwd: project_of without the cron-family rule,
    for callers that go looking for the checkout on disk."""
    path = _clean(cwd)
    return _resolve(path, roots(), _home(), aliases(), False) if path else None


def _munge(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", s)


@functools.lru_cache(maxsize=256)
def _children(root: str) -> tuple[tuple[str, str], ...]:
    """(munged name, name) of each directory under root, longest munged name first."""
    try:
        names = [e.name for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return ()
    return tuple(sorted(((_munge(n), n) for n in names), key=lambda t: (-len(t[0]), t[0])))


def project_of_dirname(name) -> Optional[str]:
    """Project for a Claude Code project-dir name ('-home-me-projects-foo-bar').

    The munging (every non-alphanumeric character becomes '-') is lossy, so the name is
    decoded against the real children of each root: the longest child whose munged form
    is a dash-bounded prefix wins. Unknown children resolve to None rather than a guess.
    """
    if not isinstance(name, str) or not name.startswith("-"):
        return None
    home, root_list, alias = _home(), roots(), aliases()
    m = _CRON_RE.search(name)
    if m:
        return _finish(m.group(1), alias)
    for r in root_list:
        pre = _munge(r) + "-"
        if name.startswith(pre):
            rest = name[len(pre):]
            for munged, child in _children(r):
                if rest == munged or rest.startswith(munged + "-"):
                    return _finish(_usable(child, declared=True), alias)
            # child no longer on disk: a '--' is a '/.' boundary (.claude, .worktrees),
            # so everything before it is the project directory's own name
            head, sep, _tail = rest.partition("--")
            return _finish(_usable(head, declared=True), alias) if sep else None
    if name in {_munge(r) for r in root_list}:
        return MISC
    hm = _munge(home)
    if name == hm or name.startswith(hm + "--") or "-claude-tmp" in name or \
            name.startswith(("-tmp-", "-var-tmp-")) or name == "-tmp":
        return MISC
    return None
