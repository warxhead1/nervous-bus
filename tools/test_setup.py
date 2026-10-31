"""Unit tests for tools/setup.py (install/uninstall, shims, suite discovery, doctor)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup as S  # noqa: E402

needs_symlink = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@needs_symlink
def test_install_symlinks_and_is_idempotent(tmp_path):
    r = S.install_one("argshape", tmp_path, windows=False)
    assert r["action"] == "linked"
    link = tmp_path / "argshape"
    assert link.is_symlink() and Path(os.path.realpath(link)) == S.script_path("argshape").resolve()
    assert S.install_one("argshape", tmp_path, windows=False)["action"] == "unchanged"


@needs_symlink
def test_refuses_to_overwrite_foreign_file(tmp_path):
    (tmp_path / "argshape").write_text("#!/bin/sh\necho mine\n")
    r = S.install_one("argshape", tmp_path, windows=False)
    assert r["action"].startswith("refused")
    assert (tmp_path / "argshape").read_text().endswith("mine\n")
    assert S.uninstall_one("argshape", tmp_path, windows=False)["action"].startswith("kept")
    assert (tmp_path / "argshape").exists()


@needs_symlink
def test_foreign_symlink_is_kept(tmp_path):
    other = tmp_path / "elsewhere.py"
    other.write_text("x")
    (tmp_path / "argshape").symlink_to(other)
    assert S.install_one("argshape", tmp_path, windows=False)["state"] == "foreign"
    assert other.exists() and (tmp_path / "argshape").is_symlink()


@needs_symlink
def test_link_from_other_checkout_is_relinked_and_not_uninstalled(tmp_path):
    fake = tmp_path / "co" / "adapters" / "argshape"
    fake.mkdir(parents=True)
    (fake / "argshape.py").write_text("x")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "argshape").symlink_to(fake / "argshape.py")
    assert S.uninstall_one("argshape", bin_dir, windows=False)["action"].startswith("kept")
    assert S.install_one("argshape", bin_dir, windows=False)["action"] == "relinked"
    assert S.classify(bin_dir / "argshape", "argshape")[0] == "ours"


@needs_symlink
def test_uninstall_removes_only_ours_and_dry_run_is_inert(tmp_path):
    S.install_one("needs-you", tmp_path, windows=False)
    assert S.uninstall_one("needs-you", tmp_path, windows=False, dry_run=True)["action"] == "would remove"
    assert (tmp_path / "needs-you").is_symlink()
    assert S.uninstall_one("needs-you", tmp_path, windows=False)["action"] == "removed"
    assert not (tmp_path / "needs-you").exists()
    assert S.uninstall_one("needs-you", tmp_path, windows=False)["action"] == "absent"


def test_dry_run_writes_nothing(tmp_path):
    bin_dir = tmp_path / "bin"
    r = S.install_one("argshape", bin_dir, windows=False, dry_run=True)
    assert r["action"] == "would link" and not bin_dir.exists()


def test_windows_shim_path_works_on_any_platform(tmp_path):
    r = S.install_one("transcripts", tmp_path, windows=True)
    shim = tmp_path / "transcripts.cmd"
    assert r["action"] == "linked" and shim.is_file()
    raw = shim.read_bytes().decode()
    assert S.SHIM_MARK in raw and "py -3" in raw and str(S.script_path("transcripts")) in raw
    assert "\r\n" in raw and "\r\r" not in raw
    assert S.classify(shim, "transcripts")[0] == "ours"
    assert S.install_one("transcripts", tmp_path, windows=True)["action"] == "unchanged"
    assert S.uninstall_one("transcripts", tmp_path, windows=True)["action"] == "removed"
    assert not shim.exists()


def test_windows_shim_for_bash_cli_uses_bash(tmp_path):
    S.install_one("nervous", tmp_path, windows=True, bash=r"C:\Git\bin\bash.exe")
    assert r'"C:\Git\bin\bash.exe"' in (tmp_path / "nervous.cmd").read_text()


def test_windows_refuses_foreign_cmd(tmp_path):
    (tmp_path / "argshape.cmd").write_text("@echo hi\r\n")
    assert S.install_one("argshape", tmp_path, windows=True)["action"].startswith("refused")


def test_cli_names_skips_bash_clis_without_bash():
    names = S.cli_names(False)
    assert {"argshape", "needs-you", "transcripts"} <= set(names)
    assert not {"nervous", "nbd", "probe"} & set(names)
    assert {"nervous", "nbd", "probe"} <= set(S.cli_names(True))


def test_default_bin_dir_per_platform(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    win = S.default_bin_dir("win32")
    assert win.name == "bin" and win.parent.name == "nervous-bus"
    assert S.default_bin_dir("linux") == Path.home() / ".local" / "bin"


def test_suite_discovery_on_fake_tree(tmp_path):
    (tmp_path / "adapters" / "a").mkdir(parents=True)
    (tmp_path / "adapters" / "a" / "test_x.py").write_text("")
    (tmp_path / "adapters" / "b" / "sub").mkdir(parents=True)
    (tmp_path / "adapters" / "b" / "sub" / "test_y.py").write_text("")
    (tmp_path / "adapters" / "c").mkdir()
    (tmp_path / "adapters" / "c" / "readme.md").write_text("")
    (tmp_path / "adapters" / "vendor").mkdir()
    (tmp_path / "adapters" / "vendor" / "test_z.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t.py").write_text("")
    assert list(S.discover_suites(tmp_path)) == ["a", "b", "tests"]


def test_real_repo_suites_include_core():
    s = S.discover_suites()
    assert {"transcripts", "tools", "tests"} <= set(s)
    assert not any(n in ("vendor", "autobench") for n in s)


def _doctor(tmp_path, *extra):
    return subprocess.run([sys.executable, str(Path(S.__file__)), "doctor", "--json",
                           "--bin-dir", str(tmp_path), *extra], capture_output=True, text=True)


def test_doctor_json_shape(tmp_path):
    p = _doctor(tmp_path)
    assert p.returncode == 0
    d = json.loads(p.stdout)
    for k in ("platform", "python", "bash", "redis", "deps", "agents", "bin_dir", "clis",
              "problems", "warnings", "ok"):
        assert k in d
    assert d["python"]["ok"] is True
    assert set(d["clis"]) == set(S.CLIS)
    assert all(c["state"] == "missing" for c in d["clis"].values())
    assert {"claude", "codex"} <= set(d["agents"])
    assert isinstance(d["bin_dir"]["on_path"], bool)


def test_doctor_strict_exit_code_follows_ok(tmp_path):
    p = _doctor(tmp_path, "--strict")
    assert p.returncode == (0 if json.loads(p.stdout)["ok"] else 1)


def test_hooks_unsupported_agent_and_dry_run(capsys):
    assert S.main(["hooks", "--agent", "codex"]) == 0
    assert "not supported yet" in capsys.readouterr().out
    assert S.main(["hooks", "--dry-run"]) == 0
    assert "argshape" in capsys.readouterr().out


def test_test_list_and_unknown_suite(capsys):
    assert S.main(["test", "--list"]) == 0
    assert "transcripts" in capsys.readouterr().out
    assert S.main(["test", "nope-suite"]) == 2
