import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("sweep", Path(__file__).with_name("sweep.py"))
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

REPO = "o/r"


def run(i, branch, sha, owner="o", status="queued"):
    return {"id": i, "name": "Unit Tests", "head_branch": branch, "head_sha": sha, "status": status,
            "head_repository": {"owner": {"login": owner}}}


class Api:
    """Stands in for gh. runs: by status. prs: {"owner:branch": [head sha, ...]} or None for a failed lookup."""

    def __init__(self, runs, prs, cancel_ok=True):
        self.runs, self.prs, self.cancel_ok, self.cancelled, self.calls = runs, prs, cancel_ok, [], []

    def __call__(self, path, method="GET"):
        self.calls.append((method, path))
        if method == "POST":
            self.cancelled.append(int(path.split("/")[-2]))
            return {} if self.cancel_ok else None
        if "/actions/runs?" in path:
            assert "event=pull_request" in path
            status = path.split("status=")[1].split("&")[0]
            return {"workflow_runs": self.runs.get(status, [])}
        key = path.split("head=")[1].split("&")[0]
        heads = self.prs.get(key, [])
        return None if heads is None else [{"head": {"sha": s}} for s in heads]


def test_runs_of_a_closed_pr_are_cancelled_and_an_open_pr_is_left_alone():
    api = Api({"queued": [run(1, "merged", "aaa"), run(2, "open", "bbb")],
               "in_progress": [run(3, "merged", "aaa", status="in_progress")]},
              {"o:merged": [], "o:open": ["bbb"]})
    got = sweep.sweep_repo(REPO, api=api)
    assert sorted(api.cancelled) == [1, 3]
    assert {g[0] for g in got} == {1, 3} and all(g[3] == "pull request closed" for g in got)


def test_a_run_of_a_commit_the_pr_moved_past_is_cancelled():
    api = Api({"queued": [run(1, "feat", "old"), run(2, "feat", "new")]}, {"o:feat": ["new"]})
    got = sweep.sweep_repo(REPO, api=api)
    assert api.cancelled == [1] and got[0][3] == "pull request moved to a newer commit"


def test_a_failed_pr_lookup_cancels_nothing():
    api = Api({"queued": [run(1, "feat", "aaa")]}, {"o:feat": None})
    assert sweep.sweep_repo(REPO, api=api) == [] and api.cancelled == []


def test_a_fork_branch_is_looked_up_under_the_fork_owner():
    api = Api({"queued": [run(1, "main", "aaa", owner="stranger")]}, {"stranger:main": ["aaa"], "o:main": []})
    assert sweep.sweep_repo(REPO, api=api) == []


def test_one_pr_lookup_per_branch_and_dry_run_cancels_nothing():
    api = Api({"queued": [run(i, "merged", "aaa") for i in range(1, 6)]}, {"o:merged": []})
    got = sweep.sweep_repo(REPO, dry_run=True, api=api)
    assert len(got) == 5 and api.cancelled == []
    assert sum(1 for m, p in api.calls if "/pulls?" in p) == 1


def test_a_cancel_that_fails_is_not_reported_as_cancelled():
    api = Api({"queued": [run(1, "merged", "aaa")]}, {"o:merged": []}, cancel_ok=False)
    assert sweep.sweep_repo(REPO, api=api) == []


def test_only_pull_request_runs_are_listed():
    api = Api({}, {})
    sweep.sweep_repo(REPO, api=api)
    assert all("event=pull_request" in p for m, p in api.calls if "/actions/runs?" in p)
