import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "fork_guard.py"
OURS = {"id": 1, "full_name": "warxhead1/nervous-bus"}
FORK = {"id": 2, "full_name": "stranger/nervous-bus"}


def run(event=None, raw=None, path=None):
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_EVENT_PATH"}
    with tempfile.TemporaryDirectory() as tmp:
        if path is None and (event is not None or raw is not None):
            path = os.path.join(tmp, "event.json")
            Path(path).write_text(raw if raw is not None else json.dumps(event), encoding="utf-8")
        if path:
            env["GITHUB_EVENT_PATH"] = path
        p = subprocess.run([str(HOOK)], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip()


def pull(head, base=OURS, key="pull_request"):
    return {"repository": base, key: {"head": {"repo": head}, "base": {"repo": base}}}


class ForkGuard(unittest.TestCase):
    def test_a_fork_pull_request_is_refused(self):
        rc, out = run(pull(FORK))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "fork-guard: REFUSED (pull request from another repository)")

    def test_a_pull_request_from_this_repository_runs(self):
        self.assertEqual(run(pull(OURS)), (0, "fork-guard: allowed (pull request from this repository)"))

    def test_a_deleted_fork_is_refused(self):
        self.assertEqual(run(pull(None))[0], 1)

    def test_a_fork_with_our_name_and_another_id_is_refused(self):
        self.assertEqual(run(pull({"id": 9, "full_name": OURS["full_name"]}))[0], 1)

    def test_names_decide_when_the_payload_has_no_ids(self):
        ours = {"full_name": OURS["full_name"]}
        self.assertEqual(run(pull(ours, ours))[0], 0)
        self.assertEqual(run(pull({"full_name": FORK["full_name"]}, ours))[0], 1)
        self.assertEqual(run(pull({}, {}))[0], 1)

    def test_workflow_run_from_a_fork_is_refused(self):
        self.assertEqual(run({"repository": OURS, "workflow_run": {"head_repository": FORK}})[0], 1)
        self.assertEqual(run({"repository": OURS, "workflow_run": {"head_repository": OURS}})[0], 0)

    def test_push_schedule_and_manual_events_run(self):
        for event in ({"repository": OURS, "ref": "refs/heads/main"}, {"schedule": "0 3 * * *"}, {"inputs": {}}):
            self.assertEqual(run(event), (0, "fork-guard: allowed (not a pull request event)"))

    def test_a_payload_that_cannot_be_read_is_refused(self):
        self.assertEqual(run()[0], 1)
        self.assertEqual(run(raw="{nope")[0], 1)
        self.assertEqual(run(raw="[]")[0], 1)
        self.assertEqual(run(path="/nonexistent/event.json")[0], 1)

    def test_the_payload_is_never_printed(self):
        event = pull(FORK)
        event["pull_request"]["title"] = "SECRET-TITLE"
        self.assertNotIn("SECRET", run(event)[1])


if __name__ == "__main__":
    unittest.main()
