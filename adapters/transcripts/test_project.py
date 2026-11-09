"""project.project_of / project_of_dirname: one cwd -> project answer for every caller.

Hermetic: every path lives under a tmp HOME, so nothing depends on the host layout.
Classes mirror the differential run over the real runs.db / tokens.db / transcript mirror.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import project  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAVE_GIT, reason="git not installed")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    for sub in ("projects", "data2/worktrees", "data2/orca"):
        (h / sub).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    for var in ("NBUS_PROJECT_ROOTS", "NBUS_PROJECT_ALIASES", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    project.cache_clear()
    yield h
    project.cache_clear()


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=path)
    return path


# ── layout, no filesystem needed (deleted worktrees are the common case) ─────────────

@pytest.mark.parametrize("rel, expected", [
    ("projects/kb", "kb"),
    ("projects/kb/src/deep/dir", "kb"),
    ("data2/worktrees/kb/wf_3998f149-2b7-2", "kb"),
    ("data2/worktrees/kb/agent-ab12cd34ef56", "kb"),
    ("data2/orca/app-to-market/wardr", "app-to-market"),
    ("data2/orca/app-to-market", "app-to-market"),
    ("data2/orca/nervous-bus/_evidence/2026-11-08/lane/scratch", "nervous-bus"),
    ("projects/tengine/.claude/worktrees/agent-a3f67d389e54ce3f8", "tengine"),
    ("projects/nervous-bus/.mmx-worktrees/mmxd-20260909-172811-p4168869-x", "nervous-bus"),
    ("projects/hearth.worktrees/feature-x", "hearth"),
    ("projects/orca", "orca"),               # a project literally named like a container
    ("projects/unreal-battlebots-gamedev.git", "unreal-battlebots-gamedev"),
])
def test_layout_under_default_roots(home, rel, expected):
    assert project.project_of(str(home / rel)) == expected


def test_cron_family_replaces_the_per_run_directory(home):
    cwd = home / "data2/worktrees/tachyonac-engine/auto-oxsynth-mmx-sweep-run-5-20260919T1000"
    assert project.project_of(str(cwd)) == "auto-oxsynth-mmx-sweep"
    cwd = home / "data2/orca/sweepers-adventures/auto-sweepers-nightly-gate-run-57-20261025T0730"
    assert project.project_of(str(cwd)) == "auto-sweepers-nightly-gate"


@pytest.mark.parametrize("rel", [
    "", ".codex/memories", ".config/orca/codex-accounts/abc/home/memories", ".gemini/config",
    "data2/claude-tmp/claude-1000/x/scratchpad", "projects", "data2/orca",
])
def test_scratch_and_home_are_misc(home, rel):
    assert project.project_of(str(home / rel) if rel else str(home)) == project.MISC


def test_tmp_is_misc():
    assert project.project_of("/tmp/hearth-codex-proof.S72mfd") == project.MISC
    assert project.project_of("/var/tmp/anything") == project.MISC


@pytest.mark.parametrize("cwd", [
    "", None, "relative/path", "unknown", "/work", "/work/apps/thermoflow",
    "/work/.claude/worktrees/agent-af710ac8d4ef5414c",
])
def test_unresolvable_is_none_never_a_basename(cwd):
    assert project.project_of(cwd) is None


def test_lane_directories_are_never_projects(home):
    assert project.project_of(str(home / "data2/worktrees/agent-ac5d5249d1e2ec589")) is None
    assert project.project_of(
        str(home / "data2/worktrees/agent-ac5d5249d1e2ec589/agent-ace5145d18f82d00b")) is None


def test_wt_basename_resolves_to_its_parent_outside_the_roots(tmp_path):
    assert project.project_of(str(tmp_path / "elsewhere/foo/wt-L1-split")) == "foo"
    assert project.project_of(str(tmp_path / "elsewhere/foo/agent-ab12cd34ef56")) == "foo"


def test_dated_batch_directory_is_not_a_project(home):
    assert project.project_of(str(home / "data/codex-2026-08-16/wt-L1-split-backtest")) is None


def test_trailing_slash_and_dotdot_do_not_split_a_project(home):
    assert project.project_of(str(home / "projects/kb") + "/") == "kb"
    assert project.project_of(str(home / "projects/kb/src/../lib")) == "kb"


# ── configuration ────────────────────────────────────────────────────────────────

def test_custom_roots_replace_the_defaults(home, tmp_path, monkeypatch):
    monkeypatch.setenv("NBUS_PROJECT_ROOTS", os.pathsep.join([str(tmp_path / "code"), str(tmp_path / "wt")]))
    assert project.project_of(str(tmp_path / "code/app/src")) == "app"
    assert project.project_of(str(tmp_path / "wt/app2/lane")) == "app2"
    assert project.project_of(str(home / "projects/kb")) is None


def test_aliases_fold_renamed_projects(home, monkeypatch):
    monkeypatch.setenv("NBUS_PROJECT_ALIASES", "home-automation=hearth, old=new")
    assert project.project_of(str(home / "projects/home-automation.worktrees/x")) == "hearth"
    assert project.project_of(str(home / "projects/hearth")) == "hearth"
    assert project.project_of(str(home / "projects/kb")) == "kb"


# ── git ────────────────────────────────────────────────────────────────────────

@needs_git
def test_worktree_lane_resolves_to_the_repo_not_the_lane_name(home):
    repo = _repo(home / "projects/realrepo")
    lane = home / "data2/worktrees/capitalsim"
    lane.mkdir()
    _git("worktree", "add", "-q", "-b", "b1", str(lane / "agent-aef2500b540413696"), cwd=repo)
    assert project.project_of(str(lane / "agent-aef2500b540413696")) == "realrepo"
    # a sibling worktree that has since been deleted still resolves through the survivor
    assert project.project_of(str(lane / "agent-deadbeef0123")) == "realrepo"


@needs_git
def test_container_with_no_surviving_worktree_falls_back_to_its_name(home):
    (home / "data2/worktrees/gone-lane").mkdir()
    assert project.project_of(str(home / "data2/worktrees/gone-lane/agent-deadbeef0123")) == "gone-lane"


@needs_git
def test_nested_standalone_clone_does_not_rename_the_container(home):
    base = home / "data2/orca/hearth"
    _repo(base / "hearth-start-publish")
    (base / "_evidence").mkdir()
    assert project.project_of(str(base / "hearth-start-publish/proj")) == "hearth"
    assert project.project_of(str(base / "_evidence/2026-09-02/e2e")) == "hearth"


@needs_git
def test_conflicting_survivors_do_not_vote(home):
    a, b = _repo(home / "projects/repo-a"), _repo(home / "projects/repo-b")
    lane = home / "data2/worktrees/mixed"
    lane.mkdir()
    _git("worktree", "add", "-q", "-b", "x", str(lane / "wa"), cwd=a)
    _git("worktree", "add", "-q", "-b", "y", str(lane / "wb"), cwd=b)
    assert project.project_of(str(lane / "agent-deadbeef0123")) == "mixed"


@needs_git
def test_repo_outside_every_root_is_named_by_git(tmp_path):
    repo = _repo(tmp_path / "elsewhere" / "myrepo")
    (repo / "sub").mkdir()
    assert project.project_of(str(repo / "sub")) == "myrepo"


@needs_git
def test_dotfiles_repo_at_home_does_not_name_every_directory(home):
    _repo(home)
    (home / "Documents/notes").mkdir(parents=True)
    assert project.project_of(str(home / "Documents/notes")) is None


@needs_git
def test_inherited_git_dir_from_a_hook_does_not_hijack(home, tmp_path, monkeypatch):
    other = _repo(tmp_path / "other-repo")
    repo = _repo(home / "projects/realrepo")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    project.cache_clear()
    assert project.project_of(str(repo)) == "realrepo"


# ── Claude project-dir names ──────────────────────────────────────────────────────

def _munged(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def test_dirname_decodes_against_the_real_children(home):
    (home / "projects/hearth").mkdir()
    (home / "projects/hearth-loom").mkdir()
    (home / "data2/orca/app-to-market").mkdir()
    pre = _munged(home)
    assert project.project_of_dirname(f"{pre}-projects-hearth-loom") == "hearth-loom"
    assert project.project_of_dirname(f"{pre}-projects-hearth") == "hearth"
    assert project.project_of_dirname(f"{pre}-projects-hearth-loom--claude-worktrees-x") == "hearth-loom"
    assert project.project_of_dirname(f"{pre}-data2-orca-app-to-market-wardr") == "app-to-market"


def test_dirname_of_a_deleted_project_uses_the_dot_directory_boundary(home):
    pre = _munged(home)
    assert project.project_of_dirname(f"{pre}-projects-home-automation--claude-worktrees-x") == "home-automation"
    assert project.project_of_dirname(f"{pre}-projects-home-automation") is None


def test_dirname_scratch_cron_and_garbage(home):
    pre = _munged(home)
    assert project.project_of_dirname(pre) == project.MISC
    assert project.project_of_dirname(f"{pre}--codex-memories") == project.MISC
    assert project.project_of_dirname(f"{pre}-data2-claude-tmp-ttl-probe") == project.MISC
    assert project.project_of_dirname(
        f"{pre}-data2-orca-sweepers-adventures-auto-sweepers-nightly-gate-run-57-20261025T0730"
    ) == "auto-sweepers-nightly-gate"
    for bad in ("mmx", "", None, "projects/kb"):
        assert project.project_of_dirname(bad) is None


def test_dirname_and_cwd_agree_for_the_same_directory(home):
    (home / "projects/kb").mkdir()
    (home / "data2/orca/biz-worthy").mkdir()
    for rel in ("projects/kb", "data2/orca/biz-worthy"):
        d = home / rel
        assert project.project_of_dirname(_munged(d)) == project.project_of(str(d))
