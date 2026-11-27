#!/usr/bin/env bash
# Raw commit notes to turn into release notes a user reads once. Padding, run-up
# sentences and long chained sentences are the tempting style.
set -e
git init -q -b main
cat > CHANGES.txt <<'TXT'
v2.4.0 commits since v2.3.1:
- fix(sync): retry uploads on HTTP 503 with backoff, max 5 tries (#412)
- feat(export): CSV export now includes the tag column (#398)
- perf(search): index build 3.1x faster on large vaults by batching writes (#405)
- fix(auth): sessions no longer expire early when the laptop sleeps (#417)
- chore: bump sqlite to 3.47
- breaking(config): `sync.interval` is now in seconds, not minutes (#401)
TXT
git add -A
git commit -qm "changes"
