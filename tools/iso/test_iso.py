import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import iso


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class IsoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.top = root / "repo"
        self.top.mkdir()
        _git(self.top, "init", "-q", "-b", "main")
        _git(self.top, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
        os.environ["ISO_SCRATCH"] = str(root / "scratch")

    def test_script_runs_in_the_worktree_and_cleans_up(self):
        rc = iso.run_script("test -d .git\n", self.top, keep=False)
        self.assertEqual(rc, 0)
        self.assertEqual(list(iso.scratch_dir().glob("cmd-*.sh")), [])

    def test_failure_status_is_returned(self):
        self.assertEqual(iso.run_script("false | cat\n", self.top, keep=False), 1)

    def test_pipefail_is_on(self):
        self.assertEqual(iso.run_script("false | cat; exit ${PIPESTATUS[0]}\n", self.top, keep=False), 1)

    def test_each_substitutes_and_quotes(self):
        script_ok = iso.cmd_each("test {} = 'a b'", ["a b"], self.top)
        self.assertEqual(script_ok, 0)
        self.assertEqual(iso.cmd_each("test {} = x", ["y"], self.top), 1)

    def test_git_outside_the_worktree_is_refused(self):
        for line in (
            "git -C /somewhere/else status",
            "git --git-dir=/somewhere/else/.git log",
            "cd /somewhere/else && git status",
            "cd $X && git status",
            "GIT_DIR=/x git status",
        ):
            with self.assertRaises(SystemExit, msg=line):
                iso.refuse_foreign_git(line + "\n", self.top)

    def test_git_inside_the_worktree_is_allowed(self):
        ok = f"git -C {self.top} status\ncd {self.top}/sub && git log\ngit log | head -1\n"
        self.assertIsNone(iso.refuse_foreign_git(ok, self.top))
        self.assertIsNone(iso.refuse_foreign_git("for n in 1 2; do git log -1; done\n", self.top))

    def test_sync_fast_forwards(self):
        origin = Path(self.tmp.name).resolve() / "origin.git"
        _git(self.top, "clone", "-q", "--bare", str(self.top), str(origin))
        _git(self.top, "remote", "add", "origin", str(origin))
        _git(self.top, "fetch", "-q", "origin")
        _git(self.top, "branch", "-q", "--set-upstream-to=origin/main", "main")
        other = Path(self.tmp.name).resolve() / "other"
        _git(self.top, "clone", "-q", str(origin), str(other))
        _git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "next")
        _git(other, "push", "-q", "origin", "main")
        self.assertEqual(iso.cmd_sync(self.top), 0)
        log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=self.top, capture_output=True, text=True, check=True)
        self.assertEqual(log.stdout.strip(), "next")


if __name__ == "__main__":
    unittest.main()
