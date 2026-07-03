//! Redis Streams-native transport for nervous-bus (feature `streams`).
//!
//! The rest of this crate (`publish`, `native_publish`, `listener`) either
//! shells out to the `nervous` CLI or file-tails `debug.jsonl` — neither
//! speaks the consumer-group primitives (`XREADGROUP`, `XACK`,
//! `XAUTOCLAIM`) that every real nervous-bus consumer in the ecosystem
//! actually needs to scale past a single in-process subscriber.
//!
//! This module is a direct port of the reference implementation in
//! tengine's `tengine-dgc-hal/src/silo/nbus_redis.rs` (`RedisStreamConsumer`
//! + its `XAUTOCLAIM`-based `reap_stale()`), adapted from tengine's
//! blocking/thread-based transport to the async `redis`+`tokio` stack that
//! hearth's `crates/hearth-api/src/nbus_consumer.rs` already runs in
//! production. tachyonac-engine's Go `subscriber.go` is a third independent
//! port of the same pattern — all three converge on the same constants
//! (`REAP_MIN_IDLE_MS = 120_000`) and the same `_raw`-field envelope
//! convention documented below, so this module should be safe to swap in
//! wherever those bespoke copies currently live.
//!
//! ## Wire convention
//! Every stream entry carries the full CloudEvents-lite envelope
//! (`{specversion,id,source,type,time,datacontenttype,data}`) JSON-encoded
//! into a single field named [`RAW_FIELD`] (`"_raw"`). This is the same
//! convention `adapters/redis-mirror/mirror.py` and every existing Rust/Go
//! consumer already use — [`StreamsPublisher`] writes it, [`StreamEntry`]
//! parses it back out.
//!
//! ## Building blocks
//! - [`StreamConsumer`] — connect + bootstrap a consumer group
//!   (`XGROUP CREATE ... MKSTREAM`, idempotent), then [`StreamConsumer::read_new`]
//!   (blocking `XREADGROUP ... >`), [`StreamConsumer::ack`] (`XACK`), and
//!   [`StreamConsumer::reap_stale`] (`XAUTOCLAIM`) for orphaned PEL entries.
//! - [`StreamsPublisher`] — `XADD` with the envelope convention above.
//! - [`run_consumer_loop`] — an optional convenience that wires the three
//!   consumer primitives into the read→handle→ack(+periodic reap) loop every
//!   caller ends up hand-rolling; use the primitives directly if you need
//!   different scheduling.
//!
//! See `sdk/rust/README.md` for a full walkthrough and hearth's
//! `nbus_consumer.rs` as a real production reference.

use redis::aio::ConnectionManager;
use redis::{AsyncCommands, RedisError, RedisResult, Value as RedisValue};
use serde::Serialize;
use serde_json::Value as JsonValue;
use std::time::Duration;

/// Field name carrying the full CloudEvents-lite envelope JSON. Every
/// consumer in the ecosystem (tengine/hearth/tachyonac/redis-mirror) parses
/// this field; anything else on the entry is best-effort metadata.
pub const RAW_FIELD: &str = "_raw";

/// Minimum idle time (ms) before [`StreamConsumer::reap_stale`] is allowed
/// to reclaim a pending entry. This is the value tengine established and
/// hearth ported unchanged — kept here as the documented default; callers
/// pass their own [`Duration`] to `reap_stale` so tests can use a much
/// shorter window.
pub const REAP_MIN_IDLE_MS: u64 = 120_000;

/// Default cadence for the reaper sweep inside [`run_consumer_loop`].
pub const DEFAULT_REAP_INTERVAL: Duration = Duration::from_secs(30);

/// Default `XREADGROUP COUNT`.
pub const DEFAULT_READ_COUNT: usize = 16;

/// Default `XREADGROUP BLOCK <ms>`.
pub const DEFAULT_BLOCK_MS: u64 = 2000;

/// Returns the configured Redis URL (`NERVOUS_REDIS_URL` or localhost).
pub fn default_redis_url() -> String {
    std::env::var("NERVOUS_REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".to_string())
}

/// A stable-ish default consumer name: `nbus-sdk-<pid>`. Callers with
/// multiple long-lived processes (or that want a hostname-qualified name for
/// easier `XPENDING` triage) should pass their own name to
/// [`StreamConsumer::connect`] instead.
pub fn default_consumer_name() -> String {
    format!("nbus-sdk-{}", std::process::id())
}

#[derive(Debug, thiserror::Error)]
pub enum StreamsError {
    #[error("redis error: {0}")]
    Redis(#[from] RedisError),
    #[error("payload serialization failed: {0}")]
    Serde(#[from] serde_json::Error),
}

/// One consumed stream entry: the RESP entry id (needed for `XACK`) plus the
/// raw `_raw` envelope JSON string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamEntry {
    /// Redis stream entry id, e.g. `"1717600000000-0"`.
    pub id: String,
    /// The full CloudEvents-lite envelope, JSON-encoded.
    pub raw: String,
}

impl StreamEntry {
    /// Parse [`Self::raw`] and return the envelope's `data` object.
    pub fn data(&self) -> Option<JsonValue> {
        self.envelope()?.get("data").cloned()
    }

    /// Parse [`Self::raw`] and return the envelope's `type` field.
    pub fn event_type(&self) -> Option<String> {
        self.envelope()?
            .get("type")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }

    /// Parse [`Self::raw`] as a full CloudEvents-lite envelope.
    pub fn envelope(&self) -> Option<JsonValue> {
        serde_json::from_str(&self.raw).ok()
    }
}

/// A single `(stream, group)` consumer-group reader. Reusable across every
/// stream a caller needs to join — construct one per `(stream, group)` pair.
pub struct StreamConsumer {
    conn: ConnectionManager,
    stream: String,
    group: String,
    consumer: String,
}

impl StreamConsumer {
    /// Connect and idempotently bootstrap the consumer group:
    /// `XGROUP CREATE <stream> <group> $ MKSTREAM`, swallowing the
    /// `BUSYGROUP` error that means the group already exists (matches
    /// tengine's `bootstrap_group` / hearth's `start_redis_consumer`).
    pub async fn connect(
        redis_url: &str,
        stream: &str,
        group: &str,
        consumer: &str,
    ) -> RedisResult<Self> {
        let client = redis::Client::open(redis_url)?;
        let conn = ConnectionManager::new(client).await?;
        let mut me = Self {
            conn,
            stream: stream.to_string(),
            group: group.to_string(),
            consumer: consumer.to_string(),
        };
        me.bootstrap_group().await?;
        Ok(me)
    }

    /// Convenience wrapper over [`Self::connect`] using [`default_redis_url`]
    /// and [`default_consumer_name`].
    pub async fn connect_default(stream: &str, group: &str) -> RedisResult<Self> {
        Self::connect(
            &default_redis_url(),
            stream,
            group,
            &default_consumer_name(),
        )
        .await
    }

    pub fn stream(&self) -> &str {
        &self.stream
    }

    pub fn group(&self) -> &str {
        &self.group
    }

    pub fn consumer(&self) -> &str {
        &self.consumer
    }

    async fn bootstrap_group(&mut self) -> RedisResult<()> {
        let res: RedisResult<()> = redis::cmd("XGROUP")
            .arg("CREATE")
            .arg(&self.stream)
            .arg(&self.group)
            .arg("$")
            .arg("MKSTREAM")
            .query_async(&mut self.conn)
            .await;
        match res {
            Ok(()) => Ok(()),
            Err(e) if e.code() == Some("BUSYGROUP") => Ok(()),
            Err(e) => Err(e),
        }
    }

    /// Blocking `XREADGROUP GROUP <group> <consumer> COUNT <count> BLOCK
    /// <block_ms> STREAMS <stream> >`. Returns new entries only (empty vec on
    /// block-timeout). Entries without a `_raw` field are dropped — matches
    /// tengine/hearth's convention of ignoring malformed/foreign entries
    /// rather than erroring the whole batch.
    pub async fn read_new(
        &mut self,
        count: usize,
        block: Duration,
    ) -> RedisResult<Vec<StreamEntry>> {
        let reply: RedisValue = redis::cmd("XREADGROUP")
            .arg("GROUP")
            .arg(&self.group)
            .arg(&self.consumer)
            .arg("COUNT")
            .arg(count)
            .arg("BLOCK")
            .arg(block.as_millis() as u64)
            .arg("STREAMS")
            .arg(&self.stream)
            .arg(">")
            .query_async(&mut self.conn)
            .await?;
        Ok(parse_xread_reply(&reply))
    }

    /// `XACK <stream> <group> <entry_id>`.
    pub async fn ack(&mut self, entry_id: &str) -> RedisResult<()> {
        let _: i64 = self
            .conn
            .xack(&self.stream, &self.group, &[entry_id])
            .await?;
        Ok(())
    }

    /// `XACK <stream> <group> <id1> <id2> ...` in one round trip.
    pub async fn ack_many(&mut self, entry_ids: &[String]) -> RedisResult<()> {
        if entry_ids.is_empty() {
            return Ok(());
        }
        let _: i64 = self.conn.xack(&self.stream, &self.group, entry_ids).await?;
        Ok(())
    }

    /// Best-effort pending-entry count for this group (`XPENDING` summary
    /// form). Returns `0` on any error — callers use this for
    /// heartbeat/queue-depth metrics that must never fail the caller.
    pub async fn pending_count(&mut self) -> u64 {
        let reply: RedisResult<RedisValue> = redis::cmd("XPENDING")
            .arg(&self.stream)
            .arg(&self.group)
            .query_async(&mut self.conn)
            .await;
        match reply {
            Ok(RedisValue::Array(items)) => match items.first() {
                Some(RedisValue::Int(n)) => (*n).max(0) as u64,
                _ => 0,
            },
            _ => 0,
        }
    }

    /// `XAUTOCLAIM <stream> <group> <consumer> <min_idle_ms> 0` — reclaim PEL
    /// entries idle longer than `min_idle` under this consumer's name. This
    /// is the orphan-recovery path: entries a crashed consumer read but
    /// never acked flow back through here, get reclaimed under the caller's
    /// consumer name, and should be pushed back through the normal
    /// handle→ack path. Pass a short `min_idle` in tests; production callers
    /// should use [`REAP_MIN_IDLE_MS`].
    pub async fn reap_stale(&mut self, min_idle: Duration) -> RedisResult<Vec<StreamEntry>> {
        let reply: RedisValue = redis::cmd("XAUTOCLAIM")
            .arg(&self.stream)
            .arg(&self.group)
            .arg(&self.consumer)
            .arg(min_idle.as_millis() as u64)
            .arg("0")
            .query_async(&mut self.conn)
            .await?;
        Ok(parse_xautoclaim_reply(&reply))
    }

    /// Raw access to the underlying connection for commands this module
    /// doesn't wrap yet (e.g. `XLEN`, `XINFO`).
    pub fn connection(&mut self) -> &mut ConnectionManager {
        &mut self.conn
    }
}

/// Options for [`run_consumer_loop`].
#[derive(Debug, Clone)]
pub struct RunLoopOptions {
    pub read_count: usize,
    pub block: Duration,
    pub reap_interval: Duration,
    pub reap_min_idle: Duration,
}

impl Default for RunLoopOptions {
    fn default() -> Self {
        Self {
            read_count: DEFAULT_READ_COUNT,
            block: Duration::from_millis(DEFAULT_BLOCK_MS),
            reap_interval: DEFAULT_REAP_INTERVAL,
            reap_min_idle: Duration::from_millis(REAP_MIN_IDLE_MS),
        }
    }
}

/// Convenience read→handle→ack loop wired on top of the [`StreamConsumer`]
/// primitives: periodic [`StreamConsumer::reap_stale`] sweep, blocking
/// [`StreamConsumer::read_new`], and per-entry `handler` dispatch. `handler`
/// returns `true` to ack the entry immediately, `false` to leave it pending
/// (e.g. on a transient failure — a later reap sweep will redeliver it).
/// Runs until `should_stop` returns `true`, checked once per read-block
/// cycle (so `block` bounds shutdown latency).
///
/// This is a convenience, not a requirement — call the primitives directly
/// if you need different scheduling (e.g. hearth's actual consumer reaps
/// once at startup before joining the live loop).
pub async fn run_consumer_loop<F, Fut>(
    consumer: &mut StreamConsumer,
    opts: RunLoopOptions,
    mut handler: F,
    mut should_stop: impl FnMut() -> bool,
) -> RedisResult<()>
where
    F: FnMut(StreamEntry) -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let mut last_reap = std::time::Instant::now();
    while !should_stop() {
        if last_reap.elapsed() >= opts.reap_interval {
            last_reap = std::time::Instant::now();
            let reclaimed = consumer.reap_stale(opts.reap_min_idle).await?;
            for entry in reclaimed {
                let id = entry.id.clone();
                if handler(entry).await {
                    consumer.ack(&id).await?;
                }
            }
        }

        let entries = consumer.read_new(opts.read_count, opts.block).await?;
        for entry in entries {
            let id = entry.id.clone();
            if handler(entry).await {
                consumer.ack(&id).await?;
            }
        }
    }
    Ok(())
}

/// `XADD`-based publisher. Uses the same CloudEvents-lite envelope shape as
/// [`crate::publish`]/[`crate::native_publish`] (see [`crate::make_envelope`])
/// so consumers don't need to distinguish how an event reached the stream.
pub struct StreamsPublisher {
    conn: ConnectionManager,
    source: String,
}

/// Trim behaviour for [`StreamsPublisher::publish_with`].
#[derive(Debug, Clone, Default)]
pub struct PublishOptions {
    /// Approximate `MAXLEN` trim applied on every `XADD`. `None` disables
    /// trimming (the stream grows unbounded — fine for tests/short-lived
    /// streams, not recommended for long-running production streams).
    pub maxlen: Option<usize>,
}

impl StreamsPublisher {
    pub async fn connect(redis_url: &str) -> RedisResult<Self> {
        Self::connect_as(redis_url, &crate::default_source()).await
    }

    /// Connect with an explicit `source` for the envelope (overrides
    /// `NERVOUS_SOURCE`/cwd-basename autodetection).
    pub async fn connect_as(redis_url: &str, source: &str) -> RedisResult<Self> {
        let client = redis::Client::open(redis_url)?;
        let conn = ConnectionManager::new(client).await?;
        Ok(Self {
            conn,
            source: source.to_string(),
        })
    }

    /// `XADD <stream> * _raw <envelope> type <event_type> event_id <id>
    /// timestamp <time> source <source>` — no trim. Returns the generated
    /// entry id.
    pub async fn publish<T: Serialize + ?Sized>(
        &mut self,
        stream: &str,
        event_type: &str,
        payload: &T,
    ) -> Result<String, StreamsError> {
        self.publish_with(stream, event_type, payload, &PublishOptions::default())
            .await
    }

    /// Same as [`Self::publish`] with explicit [`PublishOptions`] (e.g. a
    /// `MAXLEN` trim, matching `adapters/redis-mirror/mirror.py`'s
    /// approximate-trim convention).
    pub async fn publish_with<T: Serialize + ?Sized>(
        &mut self,
        stream: &str,
        event_type: &str,
        payload: &T,
        opts: &PublishOptions,
    ) -> Result<String, StreamsError> {
        let envelope = crate::make_envelope(event_type, payload, &self.source)?;
        let envelope_json: JsonValue = serde_json::from_str(&envelope)?;
        let event_id = envelope_json
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        let time = envelope_json
            .get("time")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();

        let fields: Vec<(&str, String)> = vec![
            (RAW_FIELD, envelope),
            ("type", event_type.to_string()),
            ("event_id", event_id.clone()),
            ("timestamp", time),
            ("source", self.source.clone()),
        ];

        let mut cmd = redis::cmd("XADD");
        cmd.arg(stream);
        if let Some(maxlen) = opts.maxlen {
            cmd.arg("MAXLEN").arg("~").arg(maxlen);
        }
        cmd.arg("*");
        for (k, v) in &fields {
            cmd.arg(k).arg(v);
        }
        let entry_id: String = cmd
            .query_async(&mut self.conn)
            .await
            .map_err(StreamsError::Redis)?;
        Ok(entry_id)
    }

    /// Raw access to the underlying connection.
    pub fn connection(&mut self) -> &mut ConnectionManager {
        &mut self.conn
    }
}

// ─── Redis reply parsing (pure, unit-testable without a live connection) ──────

/// Extract the `_raw` field value from a flat `[field, val, field, val, ...]`
/// RESP field array.
fn extract_raw_field(fields: &[RedisValue]) -> Option<String> {
    let mut i = 0;
    while i + 1 < fields.len() {
        if redis_value_to_string(&fields[i]).as_deref() == Some(RAW_FIELD) {
            return redis_value_to_string(&fields[i + 1]);
        }
        i += 2;
    }
    None
}

/// Stringify a RESP bulk/simple/int value (UTF-8 lossy for binary bulk).
fn redis_value_to_string(v: &RedisValue) -> Option<String> {
    match v {
        RedisValue::BulkString(bytes) => Some(String::from_utf8_lossy(bytes).into_owned()),
        RedisValue::SimpleString(s) => Some(s.clone()),
        RedisValue::Int(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Parse an `XREADGROUP` reply into [`StreamEntry`] values.
/// Shape: `[ [stream_name, [ [id, [f,v,...]], ... ] ], ... ]`, or Nil on
/// block-timeout.
fn parse_xread_reply(reply: &RedisValue) -> Vec<StreamEntry> {
    let mut out = Vec::new();
    let RedisValue::Array(streams) = reply else {
        return out;
    };
    for stream in streams {
        let RedisValue::Array(pair) = stream else {
            continue;
        };
        let Some(RedisValue::Array(entries)) = pair.get(1) else {
            continue;
        };
        collect_entries(entries, &mut out);
    }
    out
}

/// Parse an `XAUTOCLAIM` reply into [`StreamEntry`] values.
/// Shape: `[next_cursor, [ [id, [f,v,...]], ... ], [deleted_ids]]`.
fn parse_xautoclaim_reply(reply: &RedisValue) -> Vec<StreamEntry> {
    let mut out = Vec::new();
    let RedisValue::Array(parts) = reply else {
        return out;
    };
    if let Some(RedisValue::Array(entries)) = parts.get(1) {
        collect_entries(entries, &mut out);
    }
    out
}

/// Shared helper: walk `[ [id, [f,v,...]], ... ]` collecting entries with a
/// `_raw` field. Tombstones (deleted-from-stream entries `XAUTOCLAIM` can
/// hand back with `Nil` fields) are skipped.
fn collect_entries(entries: &[RedisValue], out: &mut Vec<StreamEntry>) {
    for entry in entries {
        let RedisValue::Array(idfields) = entry else {
            continue;
        };
        let Some(id) = idfields.first().and_then(redis_value_to_string) else {
            continue;
        };
        let Some(RedisValue::Array(fields)) = idfields.get(1) else {
            continue;
        };
        if let Some(raw) = extract_raw_field(fields) {
            out.push(StreamEntry { id, raw });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data_field(v: &str) -> RedisValue {
        RedisValue::BulkString(v.as_bytes().to_vec())
    }

    #[test]
    fn extract_raw_field_finds_value() {
        let fields = vec![
            data_field("type"),
            data_field("autobench.gpu_job.v1"),
            data_field("_raw"),
            data_field("{\"hello\":1}"),
        ];
        assert_eq!(extract_raw_field(&fields).as_deref(), Some("{\"hello\":1}"));
    }

    #[test]
    fn extract_raw_field_absent() {
        let fields = vec![data_field("type"), data_field("x")];
        assert!(extract_raw_field(&fields).is_none());
    }

    #[test]
    fn parse_xread_reply_extracts_entries() {
        // [ [stream, [ [id, [_raw, envelope]] ]] ]
        let reply = RedisValue::Array(vec![RedisValue::Array(vec![
            data_field("nbus:test.v1"),
            RedisValue::Array(vec![RedisValue::Array(vec![
                data_field("1717-0"),
                RedisValue::Array(vec![data_field("_raw"), data_field("{\"a\":1}")]),
            ])]),
        ])]);
        let entries = parse_xread_reply(&reply);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].id, "1717-0");
        assert_eq!(entries[0].raw, "{\"a\":1}");
    }

    #[test]
    fn parse_xread_reply_nil_is_empty() {
        assert!(parse_xread_reply(&RedisValue::Nil).is_empty());
    }

    #[test]
    fn parse_xread_reply_drops_entries_without_raw() {
        let reply = RedisValue::Array(vec![RedisValue::Array(vec![
            data_field("nbus:test.v1"),
            RedisValue::Array(vec![RedisValue::Array(vec![
                data_field("1-0"),
                RedisValue::Array(vec![data_field("other"), data_field("x")]),
            ])]),
        ])]);
        assert!(parse_xread_reply(&reply).is_empty());
    }

    #[test]
    fn parse_xautoclaim_reply_extracts_entries() {
        let reply = RedisValue::Array(vec![
            data_field("0-0"),
            RedisValue::Array(vec![RedisValue::Array(vec![
                data_field("1717-5"),
                RedisValue::Array(vec![data_field("_raw"), data_field("{\"b\":2}")]),
            ])]),
            RedisValue::Array(vec![]),
        ]);
        let entries = parse_xautoclaim_reply(&reply);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].id, "1717-5");
        assert_eq!(entries[0].raw, "{\"b\":2}");
    }

    #[test]
    fn parse_xautoclaim_skips_tombstone_nil_fields() {
        let reply = RedisValue::Array(vec![
            data_field("0-0"),
            RedisValue::Array(vec![RedisValue::Array(vec![
                data_field("1717-6"),
                RedisValue::Nil,
            ])]),
            RedisValue::Array(vec![]),
        ]);
        assert!(parse_xautoclaim_reply(&reply).is_empty());
    }

    #[test]
    fn stream_entry_parses_data_and_type() {
        let entry = StreamEntry {
            id: "1-0".into(),
            raw: r#"{"specversion":"1.0","id":"x","source":"/t","type":"tengine.cmd.step.v1","time":"2026-01-01T00:00:00Z","data":{"k":"v"}}"#.into(),
        };
        assert_eq!(entry.event_type().as_deref(), Some("tengine.cmd.step.v1"));
        assert_eq!(entry.data().unwrap()["k"], "v");
    }

    #[test]
    fn stream_entry_malformed_raw_returns_none() {
        let entry = StreamEntry {
            id: "1-0".into(),
            raw: "not json".into(),
        };
        assert!(entry.data().is_none());
        assert!(entry.event_type().is_none());
    }

    #[test]
    fn default_consumer_name_includes_pid() {
        let name = default_consumer_name();
        assert!(name.starts_with("nbus-sdk-"));
        assert!(name.contains(&std::process::id().to_string()));
    }
}

// ─── Live integration tests against a real Redis/Valkey instance ──────────────
//
// These exercise the actual XGROUP/XREADGROUP/XACK/XAUTOCLAIM round trip —
// the reply-shape unit tests above only prove the parser is correct, not
// that the commands we send produce that shape against a real server.
//
// Point `NERVOUS_REDIS_TEST_URL` at a scratch Redis/Valkey instance to
// override the default (`redis://127.0.0.1:6379/15` — db 15, kept separate
// from the default db 0 that a locally-running nervous-bus/hearth stack
// mirrors production streams into). Every test uses a randomly-suffixed
// stream/group name so concurrent runs don't collide, and best-effort
// cleans up its stream key afterwards.
#[cfg(all(test, feature = "streams-live-tests"))]
mod live_tests {
    use super::*;
    use serde_json::json;

    fn test_redis_url() -> String {
        std::env::var("NERVOUS_REDIS_TEST_URL")
            .unwrap_or_else(|_| "redis://127.0.0.1:6379/15".to_string())
    }

    fn unique_name(prefix: &str) -> String {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!("{prefix}-{nanos}")
    }

    async fn cleanup(stream: &str) {
        if let Ok(client) = redis::Client::open(test_redis_url()) {
            if let Ok(mut conn) = ConnectionManager::new(client).await {
                let _: RedisResult<()> = redis::cmd("DEL").arg(stream).query_async(&mut conn).await;
            }
        }
    }

    #[tokio::test]
    async fn live_group_bootstrap_is_idempotent() {
        let stream = unique_name("nbus:test.bootstrap");
        let group = "g1";
        let c1 = StreamConsumer::connect(&test_redis_url(), &stream, group, "c1").await;
        assert!(
            c1.is_ok(),
            "first connect should create the group: {:?}",
            c1.err().map(|e| e.to_string())
        );
        // Second connect to the SAME group must swallow BUSYGROUP, not error.
        let c2 = StreamConsumer::connect(&test_redis_url(), &stream, group, "c2").await;
        assert!(
            c2.is_ok(),
            "second connect must be idempotent: {:?}",
            c2.err().map(|e| e.to_string())
        );
        cleanup(&stream).await;
    }

    #[tokio::test]
    async fn live_publish_then_consume_then_ack() {
        let stream = unique_name("nbus:test.roundtrip");
        let group = "g1";
        let mut publisher = StreamsPublisher::connect_as(&test_redis_url(), "/sdk-test")
            .await
            .expect("connect publisher");
        let mut consumer = StreamConsumer::connect(&test_redis_url(), &stream, group, "c1")
            .await
            .expect("connect consumer");

        publisher
            .publish(&stream, "nbus.sdk_test.roundtrip.v1", &json!({"n": 42}))
            .await
            .expect("publish");

        let entries = consumer
            .read_new(10, Duration::from_secs(2))
            .await
            .expect("read_new");
        assert_eq!(entries.len(), 1, "expected exactly one delivered entry");
        let entry = &entries[0];
        assert_eq!(
            entry.event_type().as_deref(),
            Some("nbus.sdk_test.roundtrip.v1")
        );
        assert_eq!(entry.data().unwrap()["n"], 42);

        // Unacked entry shows up in the PEL.
        assert_eq!(consumer.pending_count().await, 1);

        consumer.ack(&entry.id).await.expect("ack");
        assert_eq!(consumer.pending_count().await, 0, "ack must clear the PEL");

        cleanup(&stream).await;
    }

    #[tokio::test]
    async fn live_ack_many_acks_multiple_entries() {
        let stream = unique_name("nbus:test.ackmany");
        let group = "g1";
        let mut publisher = StreamsPublisher::connect(&test_redis_url()).await.unwrap();
        let mut consumer = StreamConsumer::connect(&test_redis_url(), &stream, group, "c1")
            .await
            .unwrap();

        for i in 0..3 {
            publisher
                .publish(&stream, "nbus.sdk_test.batch.v1", &json!({"i": i}))
                .await
                .unwrap();
        }

        let entries = consumer.read_new(10, Duration::from_secs(2)).await.unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(consumer.pending_count().await, 3);

        let ids: Vec<String> = entries.iter().map(|e| e.id.clone()).collect();
        consumer.ack_many(&ids).await.unwrap();
        assert_eq!(consumer.pending_count().await, 0);

        cleanup(&stream).await;
    }

    #[tokio::test]
    async fn live_xautoclaim_reaps_orphaned_pending_entry() {
        // The money test: consumer A reads an entry and crashes without
        // acking. Consumer B, joining the SAME group, uses reap_stale to
        // reclaim A's orphaned PEL entry — the exact recovery path
        // REAP_MIN_IDLE_MS exists for.
        let stream = unique_name("nbus:test.reap");
        let group = "g1";
        let mut publisher = StreamsPublisher::connect(&test_redis_url()).await.unwrap();

        let mut consumer_a =
            StreamConsumer::connect(&test_redis_url(), &stream, group, "consumer-a")
                .await
                .unwrap();
        publisher
            .publish(&stream, "nbus.sdk_test.reap.v1", &json!({"orphan": true}))
            .await
            .unwrap();
        let read = consumer_a
            .read_new(10, Duration::from_secs(2))
            .await
            .unwrap();
        assert_eq!(read.len(), 1, "consumer A must read the entry into its PEL");
        // consumer A "crashes" here — never acks.

        // Give the entry a moment to age past a (tiny, test-only) idle
        // threshold, then reap under a second consumer identity.
        tokio::time::sleep(Duration::from_millis(50)).await;
        let mut consumer_b =
            StreamConsumer::connect(&test_redis_url(), &stream, group, "consumer-b")
                .await
                .unwrap();
        let reclaimed = consumer_b
            .reap_stale(Duration::from_millis(10))
            .await
            .expect("reap_stale");
        assert_eq!(
            reclaimed.len(),
            1,
            "consumer B must reclaim A's orphaned entry"
        );
        assert_eq!(reclaimed[0].data().unwrap()["orphan"], true);

        // The entry is now owned by consumer B — B acking it must clear the
        // PEL entirely (proves ownership actually transferred, not just that
        // the entry was echoed back).
        consumer_b.ack(&reclaimed[0].id).await.unwrap();
        assert_eq!(consumer_b.pending_count().await, 0);

        cleanup(&stream).await;
    }

    #[tokio::test]
    async fn live_reap_stale_ignores_recently_delivered_entries() {
        // Negative case: an entry delivered moments ago must NOT be
        // reclaimed by a reap sweep using the production-sized idle window —
        // otherwise a live (not crashed) consumer would get its in-flight
        // work stolen out from under it.
        let stream = unique_name("nbus:test.reap-negative");
        let group = "g1";
        let mut publisher = StreamsPublisher::connect(&test_redis_url()).await.unwrap();
        let mut consumer_a =
            StreamConsumer::connect(&test_redis_url(), &stream, group, "consumer-a")
                .await
                .unwrap();
        publisher
            .publish(&stream, "nbus.sdk_test.reap_negative.v1", &json!({}))
            .await
            .unwrap();
        consumer_a
            .read_new(10, Duration::from_secs(2))
            .await
            .unwrap();

        let mut consumer_b =
            StreamConsumer::connect(&test_redis_url(), &stream, group, "consumer-b")
                .await
                .unwrap();
        let reclaimed = consumer_b
            .reap_stale(Duration::from_millis(REAP_MIN_IDLE_MS))
            .await
            .unwrap();
        assert!(
            reclaimed.is_empty(),
            "a freshly-delivered entry must not be reclaimed by the production idle window"
        );

        cleanup(&stream).await;
    }

    #[tokio::test]
    async fn live_run_consumer_loop_acks_via_handler() {
        let stream = unique_name("nbus:test.runloop");
        let group = "g1";
        let mut publisher = StreamsPublisher::connect(&test_redis_url()).await.unwrap();

        // Bootstrap the group BEFORE publishing: XGROUP CREATE ... $ sets the
        // group's last-delivered-id to whatever is currently the newest
        // stream entry, so anything published before the group exists is
        // invisible to `>` reads (by design — matches real consumer-group
        // semantics, not an SDK bug).
        let mut consumer = StreamConsumer::connect(&test_redis_url(), &stream, group, "c1")
            .await
            .unwrap();

        publisher
            .publish(&stream, "nbus.sdk_test.runloop.v1", &json!({"x": 1}))
            .await
            .unwrap();

        let seen = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let seen_clone = seen.clone();
        let opts = RunLoopOptions {
            read_count: 10,
            block: Duration::from_millis(200),
            reap_interval: Duration::from_secs(3600),
            reap_min_idle: Duration::from_millis(REAP_MIN_IDLE_MS),
        };
        let mut iterations = 0u32;
        run_consumer_loop(
            &mut consumer,
            opts,
            |entry| {
                let seen_clone = seen_clone.clone();
                async move {
                    seen_clone.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    assert_eq!(entry.data().unwrap()["x"], 1);
                    true // ack
                }
            },
            || {
                iterations += 1;
                iterations > 1 // stop after one read cycle
            },
        )
        .await
        .unwrap();

        assert_eq!(seen.load(std::sync::atomic::Ordering::SeqCst), 1);
        assert_eq!(
            consumer.pending_count().await,
            0,
            "handler ack must clear the PEL"
        );

        cleanup(&stream).await;
    }
}
