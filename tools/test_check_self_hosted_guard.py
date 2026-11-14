import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("guard", Path(__file__).with_name("check_self_hosted_guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

WF = """name: x
on:
  {trigger}
jobs:
  hosted:
    runs-on: ubuntu-latest
  ours:
{cond}    runs-on: [self-hosted, linux]
    steps:
      - run: make
"""
COND = "    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository\n"


class Guard(unittest.TestCase):
    def test_unguarded_self_hosted_job_on_pull_request_is_reported(self):
        self.assertEqual(len(guard.problems(WF.format(trigger="pull_request:", cond=""))), 1)

    def test_guarded_job_passes(self):
        self.assertEqual(guard.problems(WF.format(trigger="pull_request:", cond=COND)), [])

    def test_no_pull_request_trigger_passes(self):
        self.assertEqual(guard.problems(WF.format(trigger="workflow_dispatch:", cond="")), [])

    def test_pull_request_target_fails_even_with_the_condition(self):
        self.assertEqual(len(guard.problems(WF.format(trigger="pull_request_target:", cond=COND))), 1)

    def test_this_repo_is_clean(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        for p in root.glob("*.y*ml"):
            self.assertEqual(guard.problems(p.read_text(encoding="utf-8")), [], p.name)


if __name__ == "__main__":
    unittest.main()
