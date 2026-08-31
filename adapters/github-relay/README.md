# github-relay

Inbound GitHub Issues ingestion + gated outbound comment relay. Closes the
estate's GitHub-Issues gap: `adapters/ci-watch/` covers Actions (CI status,
Dependabot alerts) and `tools/maintenance-train/` drains CI-red/dependabot/
chore beads into PRs, but nothing previously ingested Issues, and nothing
posted analysis back to GitHub. This adapter follows the same shape as
`ci-watch`/`system-pressure`: roster/config -> persisted diff state ->
transition-only emission -> dedup'd bead filing -> human-readable report.

## Pipeline

```
watch.py (every 30 min via github-relay.timer)
  |
  |-- inbound pass, per repo in relay-config.json with ingest=true:
  |     gh issue list --repo <r> --state all --limit 50 --json
  |       number,title,state,labels,author,updatedAt,url,body,comments
  |     -> normalize_issue() filters PRs (defensive; gh issue list never
  |        returns PR-shaped items today), truncates body to 500 chars
  |     -> classify_issue_transition() vs. persisted state
  |        (~/.cache/nervous-bus/github-relay/state.json)
  |     -> publish bus.github.issue.v1 ONLY on a transition (opened / closed /
  |        reopened / commented / labeled / updated) -- never per-poll spam
  |     -> ONE dedup'd bead per open issue, per repo with file_beads=true
  |        ("gh-issue: <owner>/<repo>#<number>: <title>"), filed once ever
  |        (tracked via bead_id in state -- never refiled even if the bead
  |        later closes as "declined")
  |
  |-- outbound pass, wired to exactly one trigger:
  |     a bead THIS adapter filed closes with `external_ref` set (mirrors
  |     tools/maintenance-train/finalize.sh's `bd update --external-ref
  |     <pr-url>` stamp after a PR opens)
  |     -> post_issue_comment(repo, number, "linked PR: <url>", mode)
  |        honoring the per-repo outbound gate, posted once (pr_comment_posted
  |        flag in state)
  |
  `-- writes ~/.cache/nervous-bus/github-relay/report.md (human) and
      snapshot.json (machine-readable, full current state every poll --
      mirrors system-pressure's snapshot pattern)
```

## Config: relay-config.json

```json
{
  "default": { "ingest": true, "file_beads": true, "outbound": "dry-run" },
  "repos": {
    "warxhead1/nervous-bus": { "outbound": "off" },
    "warxhead1/hearth": {}
  }
}
```

- **A repo not listed under `repos` is out of scope by construction** --
  github-relay never ingests or comments on it. Fail-closed, same pattern as
  `tools/maintenance-train/repo_config.py`'s allowlist.
- Per-repo entries override `default` field-by-field; anything omitted
  inherits the default.
- `ingest` — poll this repo's issues at all.
- `file_beads` — file `gh-issue:` beads for its open issues.
- `outbound` — gates `post_issue_comment`:
  - `off` — never call `gh issue comment`, not even a dry-run log line.
  - `dry-run` — compute the comment body, log it to stderr AND
    `report.md`'s "Outbound activity" section, never call `gh`.
  - `live` — actually posts via `gh issue comment`.
  - **An unrecognized value fails closed to `off`**, never to `live`
    (`get_repo_config` in `watch.py`).
  - **Flipping a repo to `live` is an explicit operator decision**, made on
    the host after reviewing what `dry-run` would have posted in
    `report.md` across a few polls. This adapter never ships a repo
    pre-armed to `live` — every entry in the shipped `relay-config.json` is
    `off` or `dry-run` (enforced by `test_watch.py::
    RelayConfigTests::test_shipped_config_never_live`, which reads the real
    shipped file, not a fixture).

## Bead -> maintenance-train integration

`gh-issue:` beads are selectable by `tools/maintenance-train/selector.py`:
`classify()` matches the `gh-issue:` title prefix (alongside `CI red:` and
dependabot/labelled/chore classes), and `resolve_repo()` extracts the target
repo from `gh-issue: <owner>/<repo>#<number>: <title>` the same way it
already extracts a repo from `CI red: <owner>/<repo>/<check>`. This means a
filed issue-bead is drained through the exact same nightly
dispatch.sh/finalize.sh pipeline as a CI-red bead: an mmx worker gets the
bead body (issue URL + body excerpt + acceptance criteria: fix via a PR
referencing `#<number>`, or post a triage comment and close with a
disposition), and `finalize.sh` stamps `--external-ref <pr-url>` on success
-- which is exactly the signal `watch.py`'s outbound pass polls for to post
the "linked PR" comment back on the issue.

See `tools/maintenance-train/tests/test_selector.py::ClassifyTests::
test_gh_issue_title` and `::ResolveRepoTests::
test_gh_issue_extracts_repo_from_title` for the integration tests.

## mmx recipe: recipes/issue-triage.md

A read-only triage prompt (classify the issue, locate relevant code, assess
reproducibility, draft a fix plan or a reply, state a disposition) modeled on
`tools/maintenance-train/brief-template.md`'s acceptance-criteria/exhaustion-
clause/output-contract shape and `~/.config/mmx-automations/recipes/
build-warnings.md`'s cluster-and-cite method. It is repo-versioned here, NOT
installed automatically (mmx-automations config lives outside this repo).
To wire it in:

```bash
ln -s /home/eric/projects/nervous-bus/adapters/github-relay/recipes/issue-triage.md \
  ~/.config/mmx-automations/recipes/issue-triage.md
```

## Schema

`schemas/bus.github.issue.v1.json` — full CloudEvents-lite envelope (see
`schemas/bus.system.pressure.v1.json` for the house style this mirrors).
`data.is_pull_request` is `const: false` -- PRs are filtered before an event
is ever built; the field exists so a future ingestion path that DOES see
PR-shaped items fails schema validation loudly instead of silently mixing PR
events into this channel.

## Install (operator, after merge -- NOT done by this PR)

```bash
mkdir -p ~/.config/systemd/user
ln -s /home/eric/projects/nervous-bus/adapters/github-relay/systemd/github-relay.service ~/.config/systemd/user/
ln -s /home/eric/projects/nervous-bus/adapters/github-relay/systemd/github-relay.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now github-relay.timer

# optional: wire the triage recipe into the mmx train
ln -s /home/eric/projects/nervous-bus/adapters/github-relay/recipes/issue-triage.md \
  ~/.config/mmx-automations/recipes/issue-triage.md
```

`gh` must be authed on the host with permission to read issues (and, once any
repo is flipped to `outbound: "live"`, permission to comment) on every repo
listed in `relay-config.json`.

## Tests

```bash
python3 -m unittest adapters.github-relay.test_watch -v
python3 -m unittest tools.maintenance-train.tests.test_selector -v   # no regression from the gh-issue: predicate
python3 -c "import json,jsonschema; s=json.load(open('schemas/bus.github.issue.v1.json')); jsonschema.Draft202012Validator.check_schema(s)"
```

All of `test_watch.py` is pure-function / stubbed-IO: no network, no `gh`,
no `bd`, no `nervous publish` calls. `fetch_issues`, `post_issue_comment`,
`publish_event`, `bd_json` are the only functions that touch a subprocess,
and every test that exercises code paths above them replaces those with
stubs or `unittest.mock.patch`.
