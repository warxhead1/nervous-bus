# StopFailure context provenance audit

## Scope and evidence boundary

This audit covers `nervous-bus-xo8j` only: the StopFailure producer path and
its emitted `bus.hearth.session.error.v1` context. It does not infer a failure
class from a session label, does not modify the v1 schema, and does not make a
runtime claim for a rebuilt-but-uninstalled hook.

## Measurement

- The available hook-trace records show the sampled StopFailure payload shape
  contains a top-level `error` string. The source parser instead read
  `error_type` and `error_message`; both parsed fields were empty in the
  sampled records.
- The source producer (`tools/claude-hooks/cmd_nbus.go`) copied those empty
  fields into the event map. `publishNbusEnvelope` removes empty strings, so
  the producer is the loss boundary before the event reaches Hearth.
- `event.HookInput` had no field for the observed `error` key. The redacted
  fixture records only field names and placeholders, not raw trace payloads,
  message text, session IDs, paths, or transcripts.

## Result and remediation

The narrow sibling implementation bead is `ox-synthesis-0da` in
`claude-hook-fast`. Its isolated-checkout change maps the observed `error`
string to the existing v1 `error_message` field and emits `error_type` as
`unknown` when the upstream payload contains no typed cause; it does not
invent a cause or change `schemas/v1`.

The source/test result establishes the producer contract, not installed-hook,
Redis, Hearth-consumer, or phone/UI delivery. Root owns rebuilding/installing
the hook and the separate Hearth integration/runtime validation.

## SDK migration note

For `nervous-bus-dgwm`, the SDK API changes `read_new` and `reap_stale` to
return an explicit valid-or-invalid result. Consumers must durably
dead-letter an invalid result before ACKing its exact Redis ID; a callback
returning `false` intentionally leaves it pending for recovery.
