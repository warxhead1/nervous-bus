import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import isoshape


class IsoshapeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.wt = root / "wt"
        self.wt.mkdir()
        (self.wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        self.plain = root / "plain"
        (self.plain / ".git").mkdir(parents=True)
        os.environ["ISO_SCRATCH"] = str(root / "scratch")

    def test_denied_shapes_become_a_script_file(self):
        for cmd, why in (
            ("for b in a b; do git log -1; done", "loop"),
            ("git log --oneline -3 | while read h m; do echo $h; done", "loop"),
            ("git commit -m \"$(echo hi)\"", "substitution"),
            ("claude plugin eval --help", "eval"),
        ):
            out = isoshape.rewrite(cmd, str(self.wt))
            self.assertIsNotNone(out, cmd)
            new, note = out
            self.assertRegex(new, r"^bash /\S+/cmd-[0-9a-f]{12}\.sh$")
            self.assertIn(why, note)
            body = Path(new.split(" ", 1)[1]).read_text()
            self.assertIn(cmd, body)
            self.assertIn(str(self.wt), body)

    def test_script_runs_the_original_command(self):
        new, _ = isoshape.rewrite("for n in 1 2; do git --version >/dev/null; echo $n; done", str(self.wt))
        res = subprocess.run(new, shell=True, capture_output=True, text=True, check=False)
        self.assertEqual(res.stdout.split(), ["1", "2"])

    def test_plain_commands_are_left_alone(self):
        for cmd in ("git status", "git log -1 | head -1", "ls adapters", "echo evaluate", "git fetch && git merge --ff-only origin/main"):
            self.assertIsNone(isoshape.rewrite(cmd, str(self.wt)), cmd)

    def test_foreign_git_is_never_rewritten(self):
        cmd = "for d in a b; do git -C /somewhere/else status; done"
        self.assertIsNone(isoshape.rewrite(cmd, str(self.wt)))

    def test_only_inside_a_linked_worktree(self):
        cmd = "for n in 1 2; do git log -1; done"
        self.assertIsNone(isoshape.rewrite(cmd, str(self.plain)))

    def test_off_by_default(self):
        self.assertFalse(isoshape.enabled({}))
        self.assertTrue(isoshape.enabled({"isoshape": "correct"}))


if __name__ == "__main__":
    unittest.main()
