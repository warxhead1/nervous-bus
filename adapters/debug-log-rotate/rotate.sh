#!/usr/bin/env bash
# rotate.sh — size-triggered rotation for ~/.cache/nervous-bus/debug.jsonl.
#
# Why rename+recreate ("logrotate create" semantics) instead of copytruncate:
# every writer (kb's atomic_append, the `nervous` shell SDK, claude-hook-fast's
# Go publisher) opens debug.jsonl with O_APPEND, and the sole tailer —
# adapters/redis-mirror/mirror.py — tracks position by (inode, byte offset)
# and explicitly reopens at offset 0 when the path's inode changes
# (TailReader.check_rotation). A rename swaps the inode at the path atomically
# and mirror.py picks up the new (empty) file on its next 100ms poll; a
# copytruncate would instead truncate the inode mirror.py already has open,
# racing its next read against our truncate. Rename is the only safe choice
# for this tailer's design — do not switch to copytruncate.
#
# Keeps 3 compressed generations (.1.gz newest .. .3.gz oldest); rotates when
# the live file exceeds ROTATE_MAX_BYTES. Meant to run from a periodic
# systemd user timer (see systemd/debug-log-rotate.timer), not continuously.

set -euo pipefail

LOG="${NERVOUS_DEBUG_LOG:-$HOME/.cache/nervous-bus/debug.jsonl}"
ROTATE_MAX_BYTES="${ROTATE_MAX_BYTES:-104857600}"  # 100 MiB
KEEP_GENERATIONS=3

if [[ ! -f "$LOG" ]]; then
    exit 0
fi

size=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
if (( size <= ROTATE_MAX_BYTES )); then
    exit 0
fi

echo "[debug-log-rotate] $LOG is ${size} bytes (> ${ROTATE_MAX_BYTES}), rotating" >&2

# Shift existing compressed generations up: .2.gz -> .3.gz, .1.gz -> .2.gz.
# Oldest generation beyond KEEP_GENERATIONS is dropped.
for ((i = KEEP_GENERATIONS - 1; i >= 1; i--)); do
    src="${LOG}.${i}.gz"
    dst="${LOG}.$((i + 1)).gz"
    if [[ -f "$src" ]]; then
        mv -f "$src" "$dst"
    fi
done
rm -f "${LOG}.$((KEEP_GENERATIONS + 1)).gz"

# Atomic rename — same filesystem, so this is a single rename() syscall.
# mirror.py's next poll (<=100ms later) sees a new inode at $LOG and reopens
# fresh at offset 0; any bytes it hadn't yet read from the pre-rotation file
# are still fully present in "${LOG}.1" below, just no longer live-tailed.
mv -f "$LOG" "${LOG}.1"

# Recreate an empty live file immediately so writers never see a missing
# path (they'd still auto-create it on next append, but this closes the gap).
: > "$LOG"
chmod --reference="${LOG}.1" "$LOG" 2>/dev/null || true

# Compress the rotated-out generation after the swap, off the hot path.
gzip -f "${LOG}.1"

echo "[debug-log-rotate] rotated to ${LOG}.1.gz" >&2
