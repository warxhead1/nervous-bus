import json
import os
import shutil
import subprocess
import sys

import pytest

from test_copy_audit import GAME, HOOK, QUIET_GAME, TSX

PLUGIN = HOOK.parents[3]


@pytest.fixture
def cli(tmp_path):
    """Run the scripts from a copy of the plugin in a bare temp dir: no repo, no sibling plugins."""
    root = tmp_path / "bare"
    shutil.copytree(PLUGIN, root / "human-attention", ignore=shutil.ignore_patterns("tests", "__pycache__", "evals"))
    scripts = root / "human-attention" / "skills" / "human-attention" / "scripts"

    def run(script, *args, stdin=None, **env):
        p = subprocess.run([sys.executable, str(scripts / script), *args], input=stdin, capture_output=True,
                           encoding="utf-8", cwd=root, env={**os.environ, **env})
        return p.returncode, p.stdout, p.stderr
    return run


def hits(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_check_scores_stdin_and_logs(cli, hit_log):
    rc, out, err = cli("copy-audit-hook", "check", "-", stdin=GAME)
    assert (rc, err) == (1, "") and "copy-audit stdin" in out and "Stand on a blue tile" in out
    data = json.loads(cli("copy-audit-hook", "check", "--json", stdin=GAME)[1])
    assert data["action"] == "report" and data["rules"] and data["lines"]
    first, second = hits(hit_log)
    assert (first["check"], first["surface"], first["plugin"], first["path"]) == ("copy-audit", "ui-file", "human-attention", "")
    assert "blue tile" not in hit_log.read_text()


def test_quiet_copy_and_exit_codes(cli, hit_log):
    assert cli("copy-audit-hook", "check", stdin=QUIET_GAME) == (0, "clean\n", "")
    assert hits(hit_log) == []
    assert cli("copy-audit-hook", "check", stdin=GAME)[0] == 1
    cli("copy-audit-hook", "check", "--session", "s9", "--cwd", "/work", stdin=GAME)
    assert [(r["session_id"], r["cwd"]) for r in hits(hit_log)][-1] == ("s9", "/work")


def test_before_file_blames_only_what_was_added(cli, tmp_path):
    root = tmp_path / "bare"
    (root / "p.tsx").write_text(TSX)
    (root / "old.tsx").write_text(TSX)
    assert cli("copy-audit-hook", "check", "--before", "old.tsx", "p.tsx")[1] == "clean\n"
    assert "copy-audit p.tsx" in cli("copy-audit-hook", "check", "p.tsx")[1]


def test_logging_off_and_missing_file(cli, hit_log):
    assert cli("copy-audit-hook", "check", stdin=GAME, AGENT_STEERING_LOG="0")[0] == 1 and hits(hit_log) == []
    assert cli("copy-audit-hook", "check", "missing.tsx")[0] == 64


def test_copy_audit_reads_stdin(cli):
    rc, out, _ = cli("copy-audit", "-", "--ext", ".tsx", "--json", stdin=TSX)
    data = json.loads(out)
    assert rc == 0 and data["files"][0]["file"] == "<stdin>" and data["total"]["heading_subtitle_pairs"] == 2
