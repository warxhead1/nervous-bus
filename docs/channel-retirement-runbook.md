# Channel retirement runbook

How to formally retire a nervous-bus channel: confirm it's actually dead,
remove its schema, update the generated docs, and leave a trail so nobody
mistakes "removed" for "never existed" or "still planned."

Six retirements precede this runbook (`git log --oneline -- 'schemas/*.json'
| grep -iE 'retir|remove.*schema'`): 11 zombie `kb.*` events +
`hearth.device.state`/`hearth.presence` (`fadea6f`), private `tengine.diag.*`
schemas (`e1de1a3`), misnamed `kernel.*` schemas (`36f0770`). This document
narrates the procedure those commits followed ad hoc.

## 1. Confirm zero events — check BOTH surfaces

A channel can look dead in one place and not the other. Check both:

1. **`nbus:all` (last ~50k events, universal stream)** — fast, catches
   anything recent:
   ```python
   import redis, json
   r = redis.Redis.from_url("redis://localhost:6379", decode_responses=True)
   entries = r.xrange("nbus:all", count=100000)
   hits = [e for e in entries if json.loads(e[1].get("_raw","{}")).get("type") == "<channel>"]
   ```
   **Gotcha**: a bare substring `grep`/`in` check over raw JSON is not enough
   — other events can *mention* a channel name inside unrelated payload text
   (e.g. an agent-activity event logging a tool call that referenced the
   channel name in a shell command). Always parse and compare the `type`
   field, not the raw string.

2. **The per-channel Redis-mirror stream, `nbus:<channel>`** — this is the
   durable one. `nbus:all` and `nbus:bus.dead_letter` are both `MAXLEN~`
   trimmed (10k/50k), so an old one-off event can fall out of the universal
   stream's window while still sitting in its own per-channel stream, which
   is written by `redis-mirror` for every channel with any matching prefix
   config and only trims per its own `maxlen` (see `adapters/redis-mirror/`).
   Check it directly:
   ```
   redis-cli EXISTS "nbus:<channel>"
   redis-cli XLEN "nbus:<channel>"
   redis-cli XINFO STREAM "nbus:<channel>"    # entries-added vs length
   redis-cli XRANGE "nbus:<channel>" - +      # inspect actual payloads
   redis-cli XINFO GROUPS "nbus:<channel>"    # any consumer groups ever registered?
   ```
   If `entries-added` equals `XLEN`, nothing has ever been trimmed and the
   count you see is the channel's *entire lifetime* history — trustworthy
   even if it's nonzero.

3. **Also check the durable JSONL log** (`~/.cache/nervous-bus/debug.jsonl`
   + its `.N.gz` rotations) the same way — parse each line, compare `type`,
   don't substring-grep.

**"Zero events" isn't the only retirement-eligible state.** A channel with a
handful of stale, clearly-synthetic events (test/smoke payloads, no real
production data, no consumer group ever registered, nothing in weeks) is
just as dead as one with a literal zero — say so precisely in the commit
message rather than rounding up to "zero events ever" if it isn't quite
that. Evidence over rhetoric: a reviewer should be able to reproduce your
count from the commands above.

## 2. Confirm zero consumers

`XINFO GROUPS nbus:<channel>` returning empty means no consumer group has
ever read the stream. Cross-check the likely consumer repos by grepping for
the channel's dispatch string (e.g. `handle_<channel_snake_case>` in
hearth's `nbus_consumer.rs`, or any `case`/`match` arm naming the channel).
If a consumer exists, retirement is not safe — file a removal task against
that consumer first and treat this as blocked, not done.

## 3. Remove the schema (or document the schema-less case)

- **Schema file exists**: `git rm schemas/<channel>.json`. Do not
  deprecate-in-place unless the channel might come back with a *different*
  shape soon (deprecate-in-place, via a `"status"` field, is for channels
  being superseded — see `status_of()` in `tools/gen_channels_md.py` and the
  `kb.*` precedent). A channel with no consumer and no product need is fully
  removed, not just marked.
- **Schema-less orphan** (fires on the bus but was never registered — a
  standing schema-first violation on its own): there's no file to `git rm`.
  Add it to `RETIRED_NO_SCHEMA` in `tools/gen_channels_md.py` anyway, with a
  note that it never had a schema (distinguish this from the
  had-a-schema-then-removed case so a future reader doesn't go looking for
  a deleted file that never existed).

## 4. Update the source docs (NOT the generated file directly)

- `tools/gen_channels_md.py`'s `RETIRED_NO_SCHEMA` list is the hand-maintained
  input for the "Retired channels (no schema file)" section — add an entry
  for every channel retired in this pass (including ones that DID have a
  schema file you just `git rm`'d, since after removal they're in the same
  "nothing on disk to classify()" bucket).
- `schemas/CHANNELS.md` is **generated** — do not hand-edit it. Regenerate
  with `python3 tools/gen_channels_md.py` as a separate step (or let whoever
  owns the merge do it, if multiple retirements are landing in the same
  window — regenerating mid-flight can produce a diff that fights a sibling
  PR's in-flight schema additions).
- `schemas/_README.md`'s hand-maintained "Channels" table only needs an edit
  if the retired channel had a row there in the first place.

## 5. Notify consumers / grace period

None of the six precedents needed a grace period — all had zero evidenced
consumers, so there was nothing to notify. If a retirement candidate *does*
have a consumer (caught in step 2), this runbook doesn't apply as-is: file
the consumer-removal task, let it land, re-run step 1-2 to reconfirm zero
events post-removal, then proceed. Don't remove a schema out from under a
live (even if soon-to-be-dead) consumer.

## 6. Verify

- `python3 tools/gen_channels_md.py` regenerates cleanly and the CI
  schema-lint drift-gate (`.github/workflows/ci-schema-lint.yml`) would pass
  (regenerate + diff byte-for-byte).
- `tests/test_channel_taxonomy.py` (or equivalent) still passes.
- If a companion repo owned the producer (e.g. a Go/Rust CLI), confirm it
  still builds after removing the dead publish call sites and unused
  channel constants — a schema removal without a producer-side cleanup pass
  leaves a `Publish()` call that will now fail schema validation at runtime
  (fails soft by design in most publishers, but it's dead code regardless).
