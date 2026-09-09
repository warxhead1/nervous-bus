# Hearth artifact promotion event

**Channel:** `hearth.artifact.promoted.v1`
**Schema:** [`schemas/hearth.artifact.promoted.v1.json`](../schemas/hearth.artifact.promoted.v1.json)
**Producer:** Hearth `project-artifacts` transactional outbox (Hearth repo)
**Consumer:** KB `kb-artifact` ingestion adapter — **under development; not yet live/verified consumption.** Do not treat the existence of this event as evidence that KB is currently ingesting Hearth artifacts.

This document specifies the contract every implementer (producer, mirror, consumer)
MUST respect. It exists to keep the identity, transport, idempotency, and authorization
semantics of Hearth artifact promotion crisp across the bus, the Hearth producer, and
the KB ingest binary. Read it before wiring up either side.

## 1. Identity

The event identifies **one retained immutable revision** of one logical artifact.

- `artifact_id` (UUID) + `revision` (positive int) is the durable identity.
- `artifact_uri` is the canonical immutable handle:
  `hearth-artifact://<lowercase-uuid>/revisions/<positive-int>`.
- `sha256` + `bytes` + `kind` describe the retained payload.
- `project` + `slug` describe the artifact family (the retained revision sits inside it).

Identity refers to the **retained exact revision and the producer's authored claims
about it**, not to a semantic correctness proof. A consumer that resolves the bytes and
finds the digest does not check; consumers that need meaning-validity must do their own
model/tooling pass — the bus does not promise that.

### 1.1 Cross-field identity (not schema-enforceable)

The schema cannot enforce that the UUID inside `artifact_uri` matches `artifact_id`, or
that the trailing path segment of `artifact_uri` matches `revision`. Both **MUST** be
validated by the producer (before publish) and the consumer (before persist):

- **Producer**: refuse to publish if `parse_uri(artifact_uri)` does not equal
  `(artifact_id_lower, revision)`.
- **Consumer**: same check on ingest. A mismatch is a producer bug; do not silently
  rewrite.

## 2. Request digest vs. content digest

`sha256` is the **content digest** of the retained bytes. It is **not** the request
digest (i.e. it is NOT a hash of the producing request, the producer's working tree, or
the raw fetched HTML). Conflating the two is a category error:

- Request digest answers "did the producer's request produce the same input?". It is
  unstable across re-runs.
- Content digest answers "is the retained revision byte-for-byte identical?". It is
  stable across retention; this is what consumers use.

The `provenance` object is where request-side context goes (`commit`, `path`,
`session_id`, `bead_id`, etc.). It is metadata; it does not appear in `sha256`.

## 3. Replay and idempotency

At-least-once transport is assumed (redis-mirror / XREADGROUP). Consumers MUST be
idempotent. The event's exact-replay identity is:

- CloudEvents envelope `id` (broker-assigned)
- CloudEvents envelope `time` (broker-assigned)
- `data.created_at` (producer-assigned)

A duplicate delivery (same `artifact_id` + `revision` + `created_at`) MUST NOT create a
second derived record on the consumer side. Recommended approach: key consumer-side
writes on `(artifact_id, revision)`; treat `created_at` as informational only. If a
consumer must preserve the broker-assigned envelope identifiers, store them as
provenance-style fields but never as a key.

The producer is responsible for emitting exactly once per logical promotion. The schema
cannot prevent double-emission at the producer layer; the consumer's idempotency is the
defense.

## 4. Transport guarantees (and what they don't get you)

The Hearth producer publishes via the nervous-bus shell SDK or the Rust SDK
(`sdk/rust/`). That guarantees:

- Schema validation at publish time (validation failure → DLQ, never main stream).
- The CloudEvents-lite envelope: `specversion`, `id`, `source`, `type`,
  `datacontenttype`, `time`, `data`.

It does **not** guarantee:

- Ordering between revisions of the same artifact. If a producer publishes revisions
  N and N+1 out of order, the consumer sees them out of order. Consumers that need
  strictly monotonic revisions MUST sort on `revision` after dedup.
- Delivery on a per-artifact partition. `kb_refs` and `project` are hints, not
  partitioning keys.

## 5. Authorization

There is **no authorization** carried in this event. Concretely, the following MUST
NOT appear anywhere in `data`:

- Auth tokens (bearer, cookie, API key, signed URL, etc.)
- Internal service accounts or impersonation claims
- Tenant identifiers beyond the public `project` slug
- Signed/presigned URLs that grant bearer access to artifact bytes
- Cookie jars, session cookies, or `Authorization:` headers

If a producer needs to gate access, gate it at the Hearth API. Consumers that resolve
bytes do so via the **authenticated Hearth API**, using their own credentials. The bus
event is identity; the API call is authorization.

Trace metadata (`session_id`, `bead_id`, `run_id`, `task_id`, `dispatch_id`, `agent`,
`commit`, `repository`, `branch`, `path`) is correlation context — useful for
debugging, but it is NOT an authorization signal and MUST NOT be treated as one. A
trace id from a producer that is no longer trusted is still just a trace id.

## 6. What this event does NOT carry

- **Fetched content, raw HTML, or raw response bodies.** The bus carries metadata; the
  bytes live behind the Hearth API. Payload cap on `bytes` is 2 MiB for the size
  *declaration*, not for inline content.
- **Authorization** of any kind (see §5).
- **Provenance-derived signed attestations** (e.g. Sigstore, SLSA, in-toto). The
  `provenance` object is observational metadata for correlation, not a signed chain of
  custody.
- **Semantic correctness proof.** Identity refers to the retained revision and the
  producer's authored claims, not to whether the content is true, accurate, or safe.

## 7. Resolution

To resolve a referenced revision to bytes, a consumer MUST:

1. Parse `artifact_uri` to `(artifact_id, revision)`.
2. Call the authenticated Hearth API to fetch the bytes for that `(artifact_id,
   revision)` pair. Authentication uses the consumer's own credentials, not anything
   from the bus event.
3. Verify that the bytes match `sha256` and `bytes` from the event. Mismatch is a
   producer/retention inconsistency; surface it and refuse the bytes.

Consumers MUST NOT:

- Dereference the producer's local source path (anything in `provenance.path`). The
  path is observational; the producer's filesystem layout is not part of the contract.
- Treat `provenance.commit` / `provenance.repository` / `provenance.branch` as
  resolvable locations. They are recorded for human correlation, not for
  reproducible byte retrieval. Bytes come from the Hearth API.

## 8. `provenance.dirty`

`provenance.dirty = true` means the working tree was not clean at the moment of
promotion. Consumers SHOULD surface this (e.g. as a flag in derived records) — it does
not invalidate the event, but it does change how a careful operator reasons about the
revision's reproducibility. `dirty = false` is the expected default for CI-driven
promotion; manual promotion may legitimately set `true`.

## 9. Migration from a prior version

There is no prior version. This is v1 of the channel; no migration window applies. If a
breaking change is needed in the future, the publisher will bump to
`hearth.artifact.promoted.v2` and a new schema file will land alongside this one
(deprecation-in-place per the project's major-version policy).

## 10. KB ingest specifically

The KB consumer (under development) MUST:

- Reject events whose `schema_version != 1`.
- Validate cross-field identity per §1.1 before doing anything else.
- Key writes on `(artifact_id, revision)`; tolerate redelivery per §3.
- Resolve bytes via the authenticated Hearth API per §7; never via `provenance.path`.
- Strip any value that looks like an auth token from any incoming payload before
  persistence — defense in depth, even though the producer contract forbids them.

KB is NOT yet live consuming this channel. Do not assume that any event observed on the
bus has been ingested; verify against KB's own state if it matters.
