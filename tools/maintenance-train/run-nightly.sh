#!/usr/bin/env bash
# run-nightly.sh — the systemd oneshot entrypoint: select tonight's queue,
# dispatch each entry, then block (sequentially, one at a time — mmx sessions
# are heavy) on finalize.sh for each before moving to the next. A stuck
# finalize on entry 1 delays entry 2's dispatch; that is intentional for v1
# (bounded blast radius over throughput — see README.md decision log).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_ROOT="$HOME/.cache/nervous-bus/maintenance-train"
DATE="$(date +%F)"
MANIFEST_DIR="$CACHE_ROOT/$DATE"

echo "run-nightly: selecting queue for $DATE" >&2
python3 "$HERE/selector.py" --out-dir "$MANIFEST_DIR"

MANIFEST="$MANIFEST_DIR/manifest.json"
[ -f "$MANIFEST" ] || { echo "run-nightly: no manifest written, nothing to do" >&2; exit 0; }

mapfile -t BEAD_IDS < <(python3 -c "
import json
data = json.load(open('$MANIFEST'))
for e in data['entries']:
    print(e['bead_id'])
")

if [ "${#BEAD_IDS[@]}" -eq 0 ]; then
  echo "run-nightly: manifest empty, nothing to dispatch tonight" >&2
  exit 0
fi

FAIL=0
for bead_id in "${BEAD_IDS[@]}"; do
  echo "run-nightly: dispatching $bead_id" >&2
  if ! "$HERE/dispatch.sh" "$bead_id" "$MANIFEST_DIR"; then
    echo "run-nightly: dispatch FAILED for $bead_id, skipping" >&2
    FAIL=1
    continue
  fi
  if ! "$HERE/finalize.sh" "$bead_id"; then
    echo "run-nightly: finalize FAILED for $bead_id" >&2
    FAIL=1
  fi
done

exit "$FAIL"
