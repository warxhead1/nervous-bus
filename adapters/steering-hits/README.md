# steering-hits

Publishes the steering plugins' hit log to the bus as `agent.steering.hit.v1`
(`schemas/agent.steering.hit.v1.json`). The plugins append one JSON line per hit
to `$AGENT_STEERING_HITS` (default `~/.local/state/agent-steering/hits.jsonl`)
and never call the bus. `tailer.py` runs `nervous publish` once per new line.

- The checkpoint (`~/.cache/nervous-bus/steering-hits/checkpoint.json`) holds the
  inode and byte offset, written after every published line. A crash re-sends at
  most one record.
- When the writer rotates the file to `hits.jsonl.1`, the tailer finishes the
  rotated file from its offset, then starts the new file at 0.
- A failed publish leaves the checkpoint in place and exits 1. A malformed line is skipped.
- redis-mirror is not used: it mirrors `debug.jsonl` envelopes, and this input is a
  plain file with no envelope. Going through `nervous publish` keeps schema
  validation, `debug.jsonl` and Redis delivery in one path.

Install (user timer, every minute): see `systemd/steering-hits.timer`.

Read side: `detectors/steering_hits.py` and `detectors/stretch_opens.py` in
`adapters/reflex-recorder/`, shown by `reflex steering-hits` and `reflex stretch-opens`.
