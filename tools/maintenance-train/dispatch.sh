#!/usr/bin/env bash
# dispatch.sh — per-manifest-entry: worktree + claim + brief + launch mmx worker.
#
#   dispatch.sh <bead-id> [manifest-dir]
#
# Reads the entry for <bead-id> out of <manifest-dir>/manifest.json (default:
# today's dir under ~/.cache/nervous-bus/maintenance-train/), then:
#   1. creates a data2 worktree of the target repo on branch maint/<bead-id>
#      (git worktree add — NOT mmx's own -w worktree-inside-shared-checkout
#      machinery; see repo_config.py header and MAINTENANCE-TRAIN-DECISIONS.md
#      for why this repo cuts its own worktree instead).
#   2. claims the bead (`bd update <id> --claim` + a bounded train note).
#   3. renders brief-template.md into the worktree.
#   4. launches the mmx sandboxed worker in the BACKGROUND against that
#      worktree (CMMX_USE_WT=0 -- the worktree IS the isolation, no nested
#      worktree layer), writing a session record finalize.sh polls on.
#
# Exit 0 once the worker is launched (async); finalize.sh does the blocking
# wait + verify + push + PR. Exit nonzero on any setup failure BEFORE launch.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_ROOT="$HOME/.cache/nervous-bus/maintenance-train"
LOCK_DIR="$CACHE_ROOT/locks"
NBD_REPO="$HOME/projects/nervous-bus"   # bd context for beads_global-prefixed IDs

die() { echo "dispatch.sh: $*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: dispatch.sh <bead-id> [manifest-dir]"
BEAD_ID="$1"
MANIFEST_DIR="${2:-$CACHE_ROOT/$(date +%F)}"
MANIFEST="$MANIFEST_DIR/manifest.json"
[ -f "$MANIFEST" ] || die "no manifest at $MANIFEST (run selector.py first)"

# --- pull this bead's entry + repo config out of python (single source of truth) ---
ENTRY_JSON="$(python3 - "$MANIFEST" "$BEAD_ID" <<'PYEOF'
import json, sys
manifest, bead_id = sys.argv[1], sys.argv[2]
data = json.load(open(manifest))
for e in data["entries"]:
    if e["bead_id"] == bead_id:
        print(json.dumps(e)); sys.exit(0)
sys.exit(1)
PYEOF
)" || die "bead $BEAD_ID not found in $MANIFEST"

REPO="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['repo'])" "$ENTRY_JSON")"
TITLE="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['title'])" "$ENTRY_JSON")"

CFG_JSON="$(python3 - "$REPO" "$HERE" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[2])
from repo_config import REPOS
repo = sys.argv[1]
if repo not in REPOS:
    sys.exit(2)
print(json.dumps(REPOS[repo]))
PYEOF
)" || die "repo '$REPO' has no entry in repo_config.py — out of scope by construction"

REPO_PATH="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['path'])" "$CFG_JSON")"
BUILD_CMD="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['build_cmd'])" "$CFG_JSON")"
TEST_CMD="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['test_cmd'])" "$CFG_JSON")"
GH_REPO="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['gh_repo'])" "$CFG_JSON")"

[ -d "$REPO_PATH" ] || die "$REPO_PATH does not exist"

# --- per-repo flock: never two dispatches (or a dispatch + the 03:30/08:10
# nightly jobs) touching the same shared checkout at once ---
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/$REPO.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || die "$REPO is locked by another maintenance-train run (or the 03:30/08:10 nightly job) — skip this run"

# --- dirty check (redundant with selector's, but dispatch.sh must never
# trust a manifest that has gone stale between selector run and dispatch) ---
if [ -n "$(git -C "$REPO_PATH" status --porcelain)" ]; then
  die "$REPO_PATH is dirty right now — refusing to dispatch (someone's WIP)"
fi

BRANCH="maint/$BEAD_ID"
WORKTREE="$HOME/data2/worktrees/$REPO/agent-$BEAD_ID"
if [ -e "$WORKTREE" ]; then
  die "$WORKTREE already exists — a dispatch for this bead may already be in flight"
fi
mkdir -p "$(dirname "$WORKTREE")"
git -C "$REPO_PATH" worktree add -b "$BRANCH" "$WORKTREE" HEAD >&2

# --- claim the bead (bounded note; never append per-retry) ---
( cd "$NBD_REPO" && bd update "$BEAD_ID" --claim \
    --notes "maintenance-train: dispatched $(date -Is) -> $WORKTREE (branch $BRANCH)" ) >&2 \
  || echo "dispatch.sh: WARNING bd claim failed for $BEAD_ID (continuing; finalize.sh will note this)" >&2

# --- render brief ---
BEAD_BODY="$(cd "$NBD_REPO" && bd show "$BEAD_ID" 2>/dev/null || echo "(bd show failed — see bead $BEAD_ID directly)")"
SESSION_DIR="$CACHE_ROOT/sessions/$BEAD_ID"
mkdir -p "$SESSION_DIR"
# REPORT_PATH MUST live inside $WORKTREE. claude-minimax-sandboxed only bind-
# mounts CMMX_WORKDIR (+ the claude binary, cargo, /bus) into the sandbox --
# nothing under ~/.cache is visible to the worker. A report path outside the
# worktree makes the worker silently "succeed" writing to a mount it doesn't
# have while the host sees nothing (MEASURED 2026-08-30 smoke test: worker
# transcript claimed "wrote report to $SESSION_DIR/report.md", file never
# existed, host-side finalize.sh correctly treated it as a failed run). We
# copy it out to SESSION_DIR only AFTER the worker exits, for a stable
# location finalize.sh + humans can find without knowing the worktree layout.
REPORT_PATH="$WORKTREE/.maintenance-report.md"
REPORT_COPY_PATH="$SESSION_DIR/report.md"
BRIEF_PATH="$WORKTREE/.maintenance-brief.md"

python3 - "$HERE/brief-template.md" "$BRIEF_PATH" \
  "$BEAD_ID" "$TITLE" "$BEAD_BODY" "$REPO" "$BRANCH" "$BUILD_CMD" "$TEST_CMD" "$REPORT_PATH" <<'PYEOF'
import sys
tmpl_path, out_path, bead_id, title, body, repo, branch, build_cmd, test_cmd, report_path = sys.argv[1:11]
tmpl = open(tmpl_path).read()
subs = {
    "${BEAD_ID}": bead_id, "${BEAD_TITLE}": title, "${BEAD_BODY}": body,
    "${REPO}": repo, "${REPO_BRANCH}": branch, "${BUILD_CMD}": build_cmd,
    "${TEST_CMD}": test_cmd, "${REPORT_PATH}": report_path,
}
for k, v in subs.items():
    tmpl = tmpl.replace(k, v)
open(out_path, "w").write(tmpl)
PYEOF

# --- session manifest finalize.sh reads ---
cat > "$SESSION_DIR/session.json" <<EOF
{
  "bead_id": "$BEAD_ID",
  "repo": "$REPO",
  "repo_path": "$REPO_PATH",
  "gh_repo": "$GH_REPO",
  "worktree": "$WORKTREE",
  "branch": "$BRANCH",
  "build_cmd": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$BUILD_CMD"),
  "test_cmd": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TEST_CMD"),
  "title": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TITLE"),
  "report_path": "$REPORT_COPY_PATH",
  "report_path_in_worktree": "$REPORT_PATH",
  "dispatched_at": "$(date -Is)"
}
EOF

# --- launch the mmx worker in the background ---
# CMMX_WORKDIR is the data2 worktree itself (already isolated); CMMX_RW=1 so
# the worker can commit; CMMX_USE_WT=0 is LOAD-BEARING (see mmx-automation
# comment this repo inherits the same finding from) -- without it the
# sandbox auto-detects a git repo and creates ANOTHER worktree nested inside
# $WORKDIR's OWN .claude/worktrees, defeating the point of cutting the data2
# worktree ourselves and doubling the isolation layers uselessly.
LOG="$SESSION_DIR/worker.log"
(
  cd "$WORKTREE"
  CMMX_WORKDIR="$WORKTREE" CMMX_RW=1 CMMX_USE_WT=0 CMMX_NET=isolated \
    claude-minimax-sandboxed -p "$(cat "$BRIEF_PATH")" --permission-mode bypassPermissions \
    >"$LOG" 2>&1
  rc=$?
  # Copy the report out of the worktree (the only place the sandbox could
  # have written it) to the stable session dir finalize.sh polls.
  [ -s "$REPORT_PATH" ] && cp "$REPORT_PATH" "$REPORT_COPY_PATH"
  echo "$rc" > "$SESSION_DIR/worker.exit"
) &
WORKER_PID=$!
echo "$WORKER_PID" > "$SESSION_DIR/worker.pid"

echo "dispatch.sh: launched $BEAD_ID ($REPO) pid=$WORKER_PID worktree=$WORKTREE report=$REPORT_PATH" >&2
