import json
import os
import shutil
import subprocess
import sys

import pytest

from test_prose_guard import AFTER, BEFORE, BODY, HOOK
from test_prose_hits import NEW, OLD, records

PLUGIN = HOOK.parents[1]


@pytest.fixture
def cli(tmp_path):
    """Run prose-guard from a copy of the plugin in a bare temp dir: no repo, no sibling plugins."""
    root = tmp_path / "bare"
    shutil.copytree(PLUGIN, root / "plain-prose", ignore=shutil.ignore_patterns("tests", "__pycache__", "evals"))

    def run(*args, stdin=None, **env):
        p = subprocess.run([sys.executable, str(root / "plain-prose" / "hooks" / "prose-guard"), *args], input=stdin,
                           capture_output=True, encoding="utf-8", cwd=root, env={**os.environ, **env})
        return p.returncode, p.stdout, p.stderr
    return run


def test_check_reads_stdin_and_a_file_and_reports_a_doc_without_blocking(cli, tmp_path, hit_log):
    rc, out, err = cli("check", stdin=BEFORE)
    assert rc == 1 and "AI tell" in out and err == ""
    f = tmp_path / "bare" / "probe.md"
    f.write_text(BEFORE)
    assert cli("check", "probe.md")[:2] == (1, out.replace("text:", "probe.md:"))
    (first, second) = records(hit_log)
    assert (first["check"], first["surface"], first["action"]) == ("prose-guard", "doc", "report") and second["path"] == "probe.md"


def test_a_heavy_commit_or_pr_text_is_block_level(cli, hit_log):
    for surface in ("commit", "pr", "issue"):
        assert cli("check", "--surface", surface, "-", stdin=BODY)[0] == 2
    assert [r["action"] for r in records(hit_log)] == ["block"] * 3
    assert cli("check", "--surface", "commit", stdin="retry: raise the limit to 5\n\nThe limit is now 5.\n") == (0, "clean\n", "")


def test_exit_codes_tell_clean_report_and_block_apart_and_json(cli):
    assert cli("check", stdin=AFTER) == (0, "clean\n", "")
    assert cli("check", stdin=BEFORE)[0] == 1
    assert cli("check", "--surface", "pr", stdin=BEFORE)[0] == 2
    rc, out, _ = cli("check", "--json", stdin=BEFORE)
    data = json.loads(out)
    assert rc == 1 and data["fires"] and data["action"] == "report" and data["rules"] and data["words"] > 0
    assert data["compare"] is None and set(data["findings"]) >= {"tell", "long", "semicolon"}


def test_compare_reports_lost_facts_and_logs_a_record(cli, tmp_path, hit_log):
    (tmp_path / "bare" / "old.md").write_text(OLD)
    rc, out, _ = cli("check", "--compare", "old.md", "--json", stdin=NEW)
    data = json.loads(out)
    assert rc == 1 and data["compare"]["lost"] == {"numbers": 2, "urls": 1, "code": 1, "flags": 1, "quotes": 1, "hedges": 1}
    (rec,) = [r for r in records(hit_log) if r["check"] == "prose-compare"]
    assert rec["lost"] == data["compare"]["lost"] and "example.com" not in hit_log.read_text()
    assert "lost 7 fact" in cli("check", "--compare", "old.md", stdin=NEW)[1]


def test_score_prints_numbers_and_logs_a_reply_record(cli, hit_log):
    reply = "{{Done.|The build passed. The tests passed.}}\n\n```\nx = 1; y = 2\n```\n"
    rc, out, err = cli("score", "--json", stdin=reply)
    data = json.loads(out)
    assert (rc, err, data["words"], data["findings"]) == (0, "", 6, 0)
    (rec,) = records(hit_log)
    assert (rec["check"], rec["surface"], rec["words"]) == ("reply-score", "reply", 6)
    assert cli("score", stdin=BEFORE)[1].startswith("words ")


def test_logging_off_and_bad_input(cli, hit_log):
    assert cli("check", stdin=BEFORE, AGENT_STEERING_LOG="0")[0] == 1 and records(hit_log) == []
    assert cli("check", "missing.md")[0] == 64
    assert cli("--help")[1].startswith("usage:")
    assert cli("check", "--surface", "bogus", stdin="x")[0] == 2


def test_session_and_cwd_come_from_flags(cli, tmp_path, hit_log):
    cli("check", "--session", "sess-9", "--cwd", "/work/dir", stdin=BEFORE)
    cli("score", "--session", "sess-9", "--cwd", "/work/dir", stdin=BEFORE)
    recs = records(hit_log)
    assert [(r["session_id"], r["cwd"]) for r in recs] == [("sess-9", "/work/dir")] * 2
