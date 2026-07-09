# debug-log-rotate

Size-triggered rotation for `~/.cache/nervous-bus/debug.jsonl`, the append-only
JSONL log every producer in this ecosystem writes to (and that
`adapters/redis-mirror/mirror.py` tails).

## Why this exists

`debug.jsonl` has no built-in rotation and grows unbounded (it hit 200MiB+ in
practice). `rotate.sh` checks its size on a timer and, once it crosses 100MiB,
rotates via rename+recreate and keeps 3 gzip-compressed generations
(`debug.jsonl.1.gz` newest .. `debug.jsonl.3.gz` oldest).

## Why rename, not copytruncate

`mirror.py`'s `TailReader` tracks tail position as `(inode, byte offset)` and
explicitly reopens at offset 0 when the path's inode changes
(`check_rotation`). A rename swaps the inode atomically; mirror.py picks up
the new empty file on its next ~100ms poll. copytruncate would instead
truncate the inode mirror.py already has an open fd on, racing its next read
against the truncate — not safe for this tailer. See the comment block at
the top of `rotate.sh`.

## Install

```
cp adapters/debug-log-rotate/systemd/debug-log-rotate.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now debug-log-rotate.timer
```

## Verifying after a rotation

- `systemctl --user status redis-mirror` should stay active, no restart.
- `redis-cli XLEN nbus:all` should keep increasing after a rotation (a
  test-safe event published post-rotation should show up).
- `journalctl --user -u debug-log-rotate` shows the rotate/skip decision each
  run.
