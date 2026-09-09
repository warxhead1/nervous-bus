# Hearth artifact promotion

`hearth.artifact.promoted.v1` carries immutable revision metadata. The producer
and KB adapter are under development; their presence is not evidence of deployed
delivery or consumption. The draft schema is
[`hearth.artifact.promoted.v1.json`](../schemas/hearth.artifact.promoted.v1.json).

## Identity and bytes

`artifact_id` plus positive `revision` identifies one retained revision.
`artifact_uri` must equal `hearth-artifact://<artifact_id>/revisions/<revision>`.
The producer and consumer enforce this cross-field equality; JSON Schema does
not. `project` plus `slug` identifies the artifact family.

`sha256` hashes the exact UTF-8 content bytes; `bytes` is their length, limited
to 2 MiB. Content is retained in Hearth and is never included in this event.
Hearth's separate request digest includes content and authored metadata and
excludes optimistic concurrency input. Replaying the same normalized request
returns its original revision, including after a newer revision exists.

To fetch content, resolve the immutable URI through the authenticated Hearth
API: `/api/project-artifacts/<id>/revisions/<revision>/content`. Verify its
digest and byte count. Never resolve `provenance.path` on another host. The URI
is an identifier, not a public link or an access credential.

## Durable transport

The producer creates a completed `nbus::Envelope` once, then stores its exact
serialized bytes in the same database transaction as the revision and outbox.
The envelope ID and time are producer-created. Retry delivery reuses those
bytes and the same ID. Delivery is at least once: a crash after acknowledgement
but before the database update can cause a duplicate event.

Bus and KB delivery have independent states and expiring ownership leases.
Only a matching lease token can record completion. A failed bus delivery must
not undo durable content storage or claim KB success. Local event logging alone
does not prove Redis delivery. A publisher acknowledgement does not prove that
any consumer ingested the event. Inspect each target's receipt separately.

The existing CLI's completed-envelope publication path does not guarantee
initial schema validation. The mirror's validation and dead-letter behavior
are a separate boundary. Deploy the schema before enabling the producer; test
actual mirror behavior during rollout. Never infer exactly-once delivery,
ordering, schema acceptance, or consumer acknowledgement from an exit code alone.

## KB linkage

`kb-artifact ingest --manifest - --json` accepts the immutable event `data`,
without its envelope or content. It validates revision identity and creates a
metadata pointer using KB's normal atomic entry writer. Its receipt includes
`entry_id`, `uri`, `path`, `created`, `artifact_uri`, and `sha256`.

The adapter deduplicates by canonical revision URI and full manifest identity.
A replay returns the existing entry; conflicting metadata for the same URI is
an error. A receipt proves the pointer was persisted. It does not prove content
was fetched, rendered, indexed, or judged correct. Normal KB event emission is
distinct from successful pointer persistence. No live stream consumer is claimed.

`kb_refs` contains at most 32 unique `kb://` references. Each has at least two
nonempty path segments consisting of lowercase letters, digits, `.`, `_`, or
`-`; `.` and `..` segments are forbidden. Total length is at most 512 bytes.
No query, fragment, percent encoding, or arbitrary URL is accepted.

## Provenance and limits

The provenance object permits only the fields declared in the schema. These
are observational claims, not authorization or signed attestations. Omitted
`dirty` means unknown; do not infer a clean working tree. Do not include secrets,
signed URLs, cookies, or bearer tokens in authored fields. The schema rejects
unknown fields but cannot recognize every secret embedded in allowed strings.

Title must contain non-whitespace text. Title and summary limits count
characters; producer and KB additionally cap each provenance string at 2048
UTF-8 bytes and the immutable manifest/envelope transport at 64 KiB. JSON
Schema's string length constraint alone does not enforce those byte limits.

This is the first, unpublished schema candidate. Future breaking changes after
publication require a new major-version file and an explicit migration window.
