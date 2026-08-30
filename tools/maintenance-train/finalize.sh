#!/usr/bin/env bash
# finalize.sh — verify a dispatched worker's output ON HOST, push, open the
# PR, update the bead, publish the bus event. Never trusts the sandbox's own
# claim of success; always re-runs build/test itself in the worktree.
#
#   finalize.sh <bead-id> [--timeout-secs N]
#
# Exit 0 on a successful PR (or a clean, bounded no-op release when the
# worker made no commits / failed its own gate). Exit nonzero only on a
# finalize.sh-internal failure (missing session, host command errors).
set -euo pipefail

CACHE_ROOT="$HOME/.cache/nervous-bus/maintenance-train"
NBD_REPO="$HOME/projects/nervous-bus"
NERVOUS_BIN="$HOME/projects/nervous-bus/sdk/shell/nervous"

die() { echo "finalize.sh: $*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: finalize.sh <bead-id> [--timeout-secs N]"
BEAD_ID="$1"; shift || true
TIMEOUT_SECS=1800
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout-secs) TIMEOUT_SECS="$2"; shift 2;;
    *) die "unknown arg $1";;
  esac
done

SESSION_DIR="$CACHE_ROOT/sessions/$BEAD_ID"
SESSION_JSON="$SESSION_DIR/session.json"
[ -f "$SESSION_JSON" ] || die "no session for $BEAD_ID at $SESSION_JSON (dispatch.sh not run?)"

jget() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$SESSION_JSON" "$1"; }
REPO="$(jget repo)"
REPO_PATH="$(jget repo_path)"
GH_REPO="$(jget gh_repo)"
WORKTREE="$(jget worktree)"
BRANCH="$(jget branch)"
BUILD_CMD="$(jget build_cmd)"
TEST_CMD="$(jget test_cmd)"
TITLE="$(jget title)"
REPORT_PATH="$(jget report_path)"

[ -d "$WORKTREE" ] || die "worktree $WORKTREE gone"

release_bead() { # release_bead <one-line-note>
  # Both -a "" (clear assignee) AND --status open are required: --claim sets
  # status=in_progress, and clearing only the assignee leaves the bead stuck
  # in_progress with no owner -- unclaimable by a future run (MEASURED
  # 2026-08-30 smoke test: bd update --claim failed with "issue not
  # claimable: status in_progress" on the retry after a bare assignee clear).
  ( cd "$NBD_REPO" && bd update "$BEAD_ID" -a "" --status open --notes "maintenance-train: $1" ) >&2 || true
}

# --- block until report file OR timeout OR worker process exited ---
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
while :; do
  [ -s "$REPORT_PATH" ] && break
  if [ -f "$SESSION_DIR/worker.exit" ]; then
    sleep 5  # grace period for a slow final flush
    [ -s "$REPORT_PATH" ] && break
    echo "finalize.sh: worker exited but $REPORT_PATH is empty — treating as failure" >&2
    release_bead "worker exited without a report ($(date -Is)); see $SESSION_DIR/worker.log"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "finalize.sh: TIMEOUT after ${TIMEOUT_SECS}s waiting for $BEAD_ID" >&2
    release_bead "worker timed out after ${TIMEOUT_SECS}s ($(date -Is)); worktree left at $WORKTREE for manual inspection"
    exit 0
  fi
  sleep 15
done
echo "finalize.sh: report present -> $REPORT_PATH" >&2

# --- did the worker actually commit anything? ---
BASE_BRANCH="$(git -C "$REPO_PATH" symbolic-ref --short HEAD)"
COMMIT_COUNT="$(git -C "$WORKTREE" rev-list --count "$BASE_BRANCH..$BRANCH" 2>/dev/null || echo 0)"
if [ "${COMMIT_COUNT:-0}" -eq 0 ]; then
  echo "finalize.sh: no commits on $BRANCH — worker found nothing to fix" >&2
  release_bead "worker made no commits ($(date -Is)); report at $REPORT_PATH — see exhaustion-clause section"
  exit 0
fi

# --- HOST re-verify: never trust the sandbox's own claim ---
BUILD_LOG="$SESSION_DIR/host-build.log"
TEST_LOG="$SESSION_DIR/host-test.log"
if ! ( cd "$WORKTREE" && bash -lc "$BUILD_CMD" ) >"$BUILD_LOG" 2>&1; then
  echo "finalize.sh: HOST build FAILED for $BEAD_ID — tail:" >&2
  tail -n 40 "$BUILD_LOG" >&2
  release_bead "worker committed but host build failed ($(date -Is)); see $BUILD_LOG, worktree left at $WORKTREE"
  exit 0
fi
if ! ( cd "$WORKTREE" && bash -lc "$TEST_CMD" ) >"$TEST_LOG" 2>&1; then
  echo "finalize.sh: HOST test FAILED for $BEAD_ID — tail:" >&2
  tail -n 40 "$TEST_LOG" >&2
  release_bead "worker committed but host test gate failed ($(date -Is)); see $TEST_LOG, worktree left at $WORKTREE"
  exit 0
fi
echo "finalize.sh: host build+test PASSED for $BEAD_ID" >&2

# --- push + PR ---
git -C "$WORKTREE" push -u origin "$BRANCH" >&2

PR_BODY_FILE="$(mktemp)"
{
  echo "Bead: $BEAD_ID"
  echo ""
  echo "## Worker report"
  echo ""
  cat "$REPORT_PATH"
  echo ""
  echo "---"
  echo ""
  echo "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
} > "$PR_BODY_FILE"

PR_URL="$(gh pr create --repo "$GH_REPO" --base "$BASE_BRANCH" --head "$BRANCH" \
  --title "[maint] $BEAD_ID: $TITLE" --body-file "$PR_BODY_FILE")"
rm -f "$PR_BODY_FILE"
echo "finalize.sh: opened $PR_URL" >&2

( cd "$NBD_REPO" && bd update "$BEAD_ID" --external-ref "$PR_URL" \
    --notes "maintenance-train: PR opened $(date -Is) -> $PR_URL" ) >&2 || true

# --- bus event (schema-first: schemas/bus.maintenance.pr.v1.json) ---
if [ -x "$NERVOUS_BIN" ]; then
  "$NERVOUS_BIN" publish bus.maintenance.pr.v1 "$(python3 -c '
import json, sys
print(json.dumps({
    "bead_id": sys.argv[1], "repo": sys.argv[2], "pr_url": sys.argv[3],
    "title": sys.argv[4],
}))
' "$BEAD_ID" "$REPO" "$PR_URL" "$TITLE")" >&2 || \
    echo "finalize.sh: WARNING bus publish failed (non-fatal, PR already open)" >&2
fi

echo "finalize.sh: $BEAD_ID DONE -> $PR_URL"
