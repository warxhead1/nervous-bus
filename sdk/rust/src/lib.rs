//! nbus — typed Rust SDK for nervous-bus.
//!
//! v0: wraps the shell SDK (`nervous publish`) so it works the moment the
//! shell tool is on PATH. Default feature — no extra deps.
//!
//! v1 (feature `native`): speaks the plugin pipe directly when in Zellij,
//! falls back to direct JSONL append to `~/.cache/nervous-bus/debug.jsonl`.
//!
//! v1 (feature `listener`): file-tails `debug.jsonl` with inode-rotation
//! awareness using the `notify` crate. Consumers can subscribe to typed
//! events without a running Zellij plugin.
//!
//! Tracking bead for v1: nervous-bus-xnn
//!
//! # Example — publish via subprocess (v0, default)
//!
//! ```no_run
//! use nbus::publish;
//! use serde_json::json;
//!
//! publish("tengine.silo.verify.v1", &json!({
//!     "silo": "racing",
//!     "session_id": "silo_racing_20260503_120000",
//!     "success": true,
//! })).unwrap();
//! ```
//!
//! # Example — native publish (v1)
//!
//! ```no_run
//! use nbus::native_publish;
//! use serde_json::json;
//!
//! native_publish("tengine.silo.verify.v1", &json!({
//!     "silo": "racing",
//! })).unwrap();
//! ```
//!
//! # Example — tail + listen (v1)
//!
//! ```no_run
//! use nbus::listener::Listener;
//! let mut l = Listener::new().unwrap();
//! l.subscribe("deer-flow.cycle.#", |event| {
//!     println!("{:#?}", event);
//! });
//! l.run();
//! ```

use serde::{Deserialize, Serialize};
use std::io::Write as IoWrite;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(feature = "native")]
use std::fs::OpenOptions;
#[cfg(feature = "listener")]
use std::io::{BufRead, BufReader};
#[cfg(feature = "listener")]
use std::path::Path;
#[cfg(feature = "subprocess")]
use std::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum PublishError {
    #[error("nervous CLI not on PATH (install sdk/shell/nervous)")]
    CliMissing,
    #[error("nervous publish exited {0}: {1}")]
    NonZeroExit(i32, String),
    #[error("payload serialization failed: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, thiserror::Error)]
pub enum NativePublishError {
    #[error("payload serialization failed: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("zellij pipe failed: {0}")]
    Zellij(String),
}

/// Why a supplied W3C `traceparent` header was rejected.
///
/// Deliberately dependency-free (hand-written `Display`/`Error` rather than a
/// `thiserror` derive): the trace-context surface is the one part of the
/// envelope a caller may have to match on from a crate that does not — and
/// should not have to — share this crate's error-derive dependency.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TraceContextError {
    /// The header was not `00-<32 hex>-<16 hex>-<2 hex>` (exactly 4
    /// hyphen-separated fields of the right widths).
    MalformedStructure,
    /// The version field was present but is not the supported `00`.
    ///
    /// Only W3C trace-context v00 is accepted; a future version's header may
    /// carry additional fields this SDK would silently truncate on replay.
    UnsupportedVersion,
    /// The trace-id field was not 32 lowercase hex characters.
    InvalidTraceId,
    /// The trace-id was well-formed but all-zero, which W3C defines as
    /// invalid.
    ZeroTraceId,
    /// The span-id (parent-id) field was not 16 lowercase hex characters.
    InvalidSpanId,
    /// The span-id was well-formed but all-zero, which W3C defines as
    /// invalid.
    ZeroSpanId,
    /// The trace-flags field was not 2 lowercase hex characters.
    ///
    /// Any two lowercase hex digits are accepted — flag *semantics*
    /// (sampling) are explicitly out of scope, only the wire shape is
    /// enforced.
    InvalidFlags,
}

impl std::fmt::Display for TraceContextError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let msg = match self {
            Self::MalformedStructure => {
                "traceparent must be 00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>"
            }
            Self::UnsupportedVersion => "traceparent version must be 00 (W3C trace-context v00)",
            Self::InvalidTraceId => "traceparent trace-id must be 32 lowercase hex characters",
            Self::ZeroTraceId => "traceparent trace-id must not be all zeroes",
            Self::InvalidSpanId => "traceparent span-id must be 16 lowercase hex characters",
            Self::ZeroSpanId => "traceparent span-id must not be all zeroes",
            Self::InvalidFlags => "traceparent flags must be 2 lowercase hex characters",
        };
        f.write_str(msg)
    }
}

impl std::error::Error for TraceContextError {}

fn is_lower_hex(s: &str, width: usize) -> bool {
    s.len() == width
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Validate a W3C trace-context v00 `traceparent` header.
///
/// Strict by construction: uppercase hex, short/long fields, a non-`00`
/// version, and the two W3C-invalid all-zero ids are all rejected. This is
/// the single validation point shared by [`Envelope::with_traceparent`] and
/// the deserialization boundary, so a header can never enter an `Envelope`
/// by one path that the other would have refused.
fn validate_traceparent(traceparent: &str) -> Result<(), TraceContextError> {
    let mut fields = traceparent.split('-');
    let (version, trace_id, span_id, flags) =
        match (fields.next(), fields.next(), fields.next(), fields.next()) {
            (Some(v), Some(t), Some(s), Some(f)) => (v, t, s, f),
            _ => return Err(TraceContextError::MalformedStructure),
        };
    if fields.next().is_some() {
        return Err(TraceContextError::MalformedStructure);
    }

    if !is_lower_hex(version, 2) {
        return Err(TraceContextError::MalformedStructure);
    }
    if version != "00" {
        return Err(TraceContextError::UnsupportedVersion);
    }
    if !is_lower_hex(trace_id, 32) {
        return Err(TraceContextError::InvalidTraceId);
    }
    if trace_id.bytes().all(|b| b == b'0') {
        return Err(TraceContextError::ZeroTraceId);
    }
    if !is_lower_hex(span_id, 16) {
        return Err(TraceContextError::InvalidSpanId);
    }
    if span_id.bytes().all(|b| b == b'0') {
        return Err(TraceContextError::ZeroSpanId);
    }
    if !is_lower_hex(flags, 2) {
        return Err(TraceContextError::InvalidFlags);
    }
    Ok(())
}

/// Deserialize + validate an envelope-level `traceparent`.
///
/// Only called when the key is PRESENT (absence is handled by
/// `#[serde(default)]`), so a legacy untraced envelope round-trips
/// untouched. A present-but-invalid header — including `null` and any
/// non-string JSON type, which fail in `String::deserialize` — is a hard
/// deserialization error rather than a silently dropped field: a replayed
/// envelope whose trace context we cannot vouch for must not masquerade as
/// an untraced one.
fn deserialize_traceparent<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error as _;
    let raw = String::deserialize(deserializer)?;
    validate_traceparent(&raw).map_err(D::Error::custom)?;
    Ok(Some(raw))
}

fn default_source() -> String {
    std::env::var("NERVOUS_SOURCE").unwrap_or_else(|_| {
        // This used `Path::new(".").file_name()`, which is ALWAYS `None` —
        // the sole component of "." is `Component::CurDir`, not a name — so
        // every publish that didn't set NERVOUS_SOURCE was stamped `/unknown`
        // instead of the documented `/<cwd-basename>`. Consumers route on the
        // source URI (hearth's `derive_event_project` reads it *before* falling
        // back to `data.project`), so mis-sourced events land in the wrong
        // project bucket. Resolve the real cwd.
        let basename = std::env::current_dir()
            .ok()
            .and_then(|cwd| cwd.file_name().map(|s| s.to_string_lossy().to_string()))
            .unwrap_or_else(|| "unknown".into());
        format!("/{}", basename)
    })
}

/// CloudEvents-lite wire envelope shared by every nervous-bus transport.
///
/// Construct it with [`Envelope::new`] rather than manually formatting JSON.
/// It derives serde traits so a durable producer can persist a completed
/// envelope and publish the exact same bytes again after a retry.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Envelope {
    specversion: String,
    id: String,
    source: String,
    #[serde(rename = "type")]
    channel: String,
    time: String,
    datacontenttype: String,
    data: serde_json::Value,
    /// Optional W3C trace-context membership (CloudEvents Distributed
    /// Tracing extension). Absent — never `null` — on untraced envelopes, so
    /// the serialized bytes of every existing producer are unchanged.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_traceparent"
    )]
    traceparent: Option<String>,
}

impl Envelope {
    /// Create a new, canonical nervous-bus envelope.
    ///
    /// IDs are 26-character Crockford Base32 ULIDs. The process-wide
    /// generator is monotonic, including when several events are stamped in
    /// the same millisecond, matching the Go SDK's `oklog/ulid` contract.
    pub fn new<T: Serialize + ?Sized>(
        source: impl Into<String>,
        channel: impl Into<String>,
        data: &T,
    ) -> Result<Self, serde_json::Error> {
        Ok(Self {
            specversion: "1.0".into(),
            id: next_ulid(),
            source: source.into(),
            channel: channel.into(),
            time: iso_now(),
            datacontenttype: "application/json".into(),
            data: serde_json::to_value(data)?,
            // Trace membership is opt-in and explicit. The SDK never reads
            // the environment for an ambient traceparent and never mints one
            // on its own: a caller that wants this envelope in a chain calls
            // `with_traceparent`, and a caller minting a *fresh* span does so
            // outside the SDK (the envelope `id` above is available as
            // entropy).
            traceparent: None,
        })
    }

    /// The canonical ULID assigned to this envelope.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The CloudEvents source URI for this envelope.
    pub fn source(&self) -> &str {
        &self.source
    }

    /// The nervous-bus channel stored in the CloudEvents `type` field.
    pub fn channel(&self) -> &str {
        &self.channel
    }

    /// The publish timestamp in UTC RFC3339 form.
    pub fn time(&self) -> &str {
        &self.time
    }

    /// The JSON payload retained for durable persistence and replay.
    pub fn data(&self) -> &serde_json::Value {
        &self.data
    }

    /// The envelope-level W3C `traceparent`, if this envelope is part of a
    /// traced causal chain.
    ///
    /// The value is byte-for-byte the header that was supplied, whether by
    /// [`Envelope::with_traceparent`] or by deserializing a persisted
    /// envelope — trace and span ids are never regenerated on replay.
    pub fn traceparent(&self) -> Option<&str> {
        self.traceparent.as_deref()
    }

    /// Attach a validated W3C trace-context v00 `traceparent` to this
    /// envelope.
    ///
    /// The header is preserved exactly, so re-publishing a persisted
    /// envelope keeps it in the same chain. Minting a *new* span for a new
    /// event is deliberately the caller's job — see [`Envelope::id`] for
    /// per-envelope entropy.
    ///
    /// # Errors
    ///
    /// [`TraceContextError`] if the header is not
    /// `00-<32 lowercase hex>-<16 lowercase hex>-<2 lowercase hex>` with
    /// non-zero trace- and span-ids.
    ///
    /// ```
    /// # use nbus::Envelope;
    /// let envelope = Envelope::new("/kb", "kb.entry.created.v2", &serde_json::json!({}))
    ///     .unwrap()
    ///     .with_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    ///     .unwrap();
    /// assert_eq!(
    ///     envelope.traceparent(),
    ///     Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    /// );
    /// ```
    pub fn with_traceparent(mut self, traceparent: &str) -> Result<Self, TraceContextError> {
        validate_traceparent(traceparent)?;
        self.traceparent = Some(traceparent.to_string());
        Ok(self)
    }

    /// Serialize the complete envelope for any existing publish transport.
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

fn next_ulid() -> String {
    static GENERATOR: OnceLock<Mutex<ulid::Generator>> = OnceLock::new();
    let generator = GENERATOR.get_or_init(|| Mutex::new(ulid::Generator::new()));
    let mut generator = generator
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    // The only error is an 80-bit entropy overflow inside one millisecond,
    // which is not realistically recoverable. `Ulid::new` still emits a
    // standards-compliant Crockford ULID if that pathological edge occurs.
    generator
        .generate()
        .unwrap_or_else(|_| ulid::Ulid::new())
        .to_string()
}

fn iso_now() -> String {
    use chrono::TimeZone;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs() as i64;
    chrono::Utc
        .timestamp_opt(secs, 0)
        .single()
        .map(|t| t.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_else(|| "1970-01-01T00:00:00Z".into())
}

/// Create a serialized CloudEvents-lite envelope for an immediate publish.
///
/// Use [`Envelope`] directly when a producer needs to persist one completed
/// envelope and replay it unchanged after a durable retry.
pub fn make_envelope(
    channel: &str,
    payload: &(impl Serialize + ?Sized),
    source: &str,
) -> Result<String, serde_json::Error> {
    Envelope::new(source, channel, payload)?.to_json()
}

fn debug_log_path() -> PathBuf {
    std::env::var("NERVOUS_DEBUG_LOG")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let mut p = std::env::var("HOME").map(PathBuf::from).unwrap_or_default();
            p.push(".cache/nervous-bus/debug.jsonl");
            p
        })
}

#[cfg(feature = "subprocess")]
pub fn publish<T: Serialize + ?Sized>(channel: &str, payload: &T) -> Result<(), PublishError> {
    let envelope = make_envelope(channel, payload, &default_source())?;
    let result = Command::new("nervous")
        .arg("publish")
        .arg("--json")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .and_then(|mut child| {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(envelope.as_bytes());
            }
            child.wait()
        });
    match result {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(PublishError::CliMissing),
        Err(e) => Err(PublishError::Io(e)),
        Ok(exit) if !exit.success() => {
            let code = exit.code().unwrap_or(-1);
            Err(PublishError::NonZeroExit(code, String::new()))
        }
        Ok(_) => Ok(()),
    }
}

#[cfg(not(feature = "subprocess"))]
pub fn publish<T: Serialize + ?Sized>(_channel: &str, _payload: &T) -> Result<(), PublishError> {
    Err(PublishError::CliMissing)
}

/// Publish without spawning the `nervous` CLI: append the CloudEvent envelope
/// straight to the debug log the redis-mirror adapter tails.
///
/// This is the path to use from a long-lived multi-threaded service. The
/// subprocess path costs a `bash` + a `python3` interpreter *per event*, which
/// is fine for a hook firing a handful of times and catastrophic for a server
/// fanning out thousands (hearth-api, 2026-08-16: a restart re-emitted ~4000
/// session events, 4000 interpreters exhausted RAM, and the children wedged
/// in D-state inside zram compression — load average 4041).
///
/// Schema validation is unaffected: the mirror validates every line it reads
/// off the log, so a malformed payload still dead-letters exactly as it would
/// have via the CLI.
#[cfg(feature = "native")]
pub fn native_publish<T: Serialize + ?Sized>(
    channel: &str,
    payload: &T,
) -> Result<(), NativePublishError> {
    native_publish_with_source(channel, payload, &default_source())
}

/// [`native_publish`] with an explicit CloudEvents `source` URI.
///
/// Consumers route on `source` (hearth's `derive_event_project` reads it
/// before falling back to `data.project`), and a service publishing on behalf
/// of several projects can't express that through the process-wide
/// `NERVOUS_SOURCE` env var. Pass it per-call instead.
#[cfg(feature = "native")]
pub fn native_publish_with_source<T: Serialize + ?Sized>(
    channel: &str,
    payload: &T,
    source: &str,
) -> Result<(), NativePublishError> {
    let envelope = make_envelope(channel, payload, source).map_err(NativePublishError::Serde)?;
    let log_path = debug_log_path();

    if std::env::var_os("ZELLIJ").is_some()
        && std::env::var("NERVOUS_NO_ZELLIJ").ok() != Some("1".into())
    {
        let plugin = std::env::var("NERVOUS_PLUGIN").unwrap_or_else(|_| "nervous-bus".into());
        let prog = std::process::Command::new("zellij")
            .args(["pipe", "-p", &plugin, "-n", channel])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        if let Ok(mut child) = prog {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(envelope.as_bytes());
            }
            // Drop stdin (above, by scope) THEN reap. Without this wait the
            // child stays a zombie for the lifetime of the process — one PID
            // leaked per publish, which for a long-lived publisher eventually
            // exhausts the pid_max table.
            let _ = child.wait();
        }
    }

    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(NativePublishError::Io)?;
    // One `write_all` of envelope-plus-newline, NOT `writeln!`. `writeln!` goes
    // through `write_fmt`, which may issue the body and the newline as separate
    // `write(2)` calls; with several threads (or processes) appending to the
    // same log that interleaves and yields corrupt JSONL lines the mirror drops
    // on the floor. A single write to an O_APPEND fd is atomic at this size.
    let mut line = envelope.into_bytes();
    line.push(b'\n');
    file.write_all(&line).map_err(NativePublishError::Io)
}

#[cfg(not(feature = "native"))]
pub fn native_publish<T: Serialize + ?Sized>(
    _channel: &str,
    _payload: &T,
) -> Result<(), NativePublishError> {
    Err(NativePublishError::Io(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "native feature not enabled — rebuild with --features native",
    )))
}

#[cfg(not(feature = "native"))]
pub fn native_publish_with_source<T: Serialize + ?Sized>(
    _channel: &str,
    _payload: &T,
    _source: &str,
) -> Result<(), NativePublishError> {
    Err(NativePublishError::Io(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "native feature not enabled — rebuild with --features native",
    )))
}

#[cfg(feature = "listener")]
pub mod listener {
    use super::*;
    use notify::{Config, RecommendedWatcher, RecursiveMode, Watcher};
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    pub type EventCallback = Box<dyn Fn(CloudEvent) + Send + Sync>;

    #[derive(Clone, Debug)]
    pub struct CloudEvent {
        pub id: String,
        pub source: String,
        pub channel: String,
        pub time: String,
        pub data: serde_json::Value,
    }

    pub struct Listener {
        log_path: PathBuf,
        watchers: std::sync::Mutex<HashMap<String, EventCallback>>,
        stop: Arc<Mutex<bool>>,
    }

    impl Listener {
        pub fn new() -> std::io::Result<Self> {
            Ok(Self {
                log_path: debug_log_path(),
                watchers: std::sync::Mutex::new(HashMap::new()),
                stop: Arc::new(Mutex::new(false)),
            })
        }

        pub fn subscribe<F>(&mut self, pattern: &str, callback: F) -> &mut Self
        where
            F: Fn(CloudEvent) + Send + Sync + 'static,
        {
            self.watchers
                .lock()
                .unwrap()
                .insert(pattern.to_string(), Box::new(callback));
            self
        }

        pub fn stop(&self) {
            let mut s = self.stop.lock().unwrap();
            *s = true;
        }

        pub fn matches_pattern(event: &str, pattern: &str) -> bool {
            if pattern == "#" || pattern == "*" {
                return true;
            }
            let parts: Vec<&str> = pattern.split('.').collect();
            let event_parts: Vec<&str> = event.split('.').collect();
            for (i, part) in parts.iter().enumerate() {
                if i >= event_parts.len() {
                    return false;
                }
                if *part != "#" && *part != "*" && *part != event_parts[i] {
                    return false;
                }
            }
            if parts.len() > event_parts.len()
                && !parts[parts.len() - 1].chars().all(|c| c == '*' || c == '#')
            {
                return false;
            }
            true
        }

        pub fn parse_line(line: &str) -> Option<CloudEvent> {
            let raw: serde_json::Value = serde_json::from_str(line).ok()?;
            let obj = raw.as_object()?;
            let channel = obj.get("type")?.as_str()?.to_string();
            let id = obj
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let source = obj
                .get("source")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let time = obj
                .get("time")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let data = obj.get("data").cloned().unwrap_or(serde_json::Value::Null);
            Some(CloudEvent {
                id,
                source,
                channel,
                time,
                data,
            })
        }

        fn read_events(path: &Path) -> Vec<CloudEvent> {
            let mut events = Vec::new();
            let file = match std::fs::File::open(path) {
                Ok(f) => f,
                Err(_) => return events,
            };
            let mut fp = BufReader::new(file);
            let mut buf = String::new();
            loop {
                match fp.read_line(&mut buf) {
                    Ok(0) => break,
                    Ok(_) => {
                        let line = buf.trim().to_string();
                        buf.clear();
                        if line.is_empty() {
                            continue;
                        }
                        if let Some(event) = Self::parse_line(&line) {
                            events.push(event);
                        }
                    }
                    Err(_) => break,
                }
            }
            events
        }

        pub fn tail(&self, pattern: &str) -> Result<Vec<String>, notify::Error> {
            let (tx, rx) = std::sync::mpsc::channel();
            let log_path = self.log_path.clone();

            let mut watcher: RecommendedWatcher = Watcher::new(
                move |res: notify::Result<notify::Event>| {
                    if let Ok(event) = res {
                        if event.kind.is_modify() {
                            let _ = tx.send(());
                        }
                    }
                },
                Config::default().with_poll_interval(Duration::from_millis(100)),
            )?;
            watcher.watch(&log_path, RecursiveMode::NonRecursive)?;

            let mut events = Vec::new();
            let start = std::time::Instant::now();
            let window = Duration::from_millis(500);

            loop {
                if start.elapsed() >= window {
                    break;
                }

                let new_events: Vec<String> = Self::read_events(&log_path)
                    .into_iter()
                    .filter(|e| Self::matches_pattern(&e.channel, pattern))
                    .filter(|e| !events.iter().any(|seen: &String| seen == &e.id))
                    .map(|e| e.id)
                    .collect();

                if !new_events.is_empty() {
                    events.extend(new_events);
                    break;
                }

                match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(_) => continue,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }

            drop(watcher);
            Ok(events)
        }

        pub fn run(&self) {
            let (tx, rx) = std::sync::mpsc::channel();
            let log_path = self.log_path.clone();
            let stop = self.stop.clone();

            let mut watcher: RecommendedWatcher = Watcher::new(
                move |res: Result<notify::Event, notify::Error>| {
                    if let Ok(event) = res {
                        if event.kind.is_modify() {
                            let _ = tx.send(());
                        }
                    }
                },
                Config::default().with_poll_interval(Duration::from_secs(1)),
            )
            .expect("notify watcher creation failed");
            watcher.watch(&log_path, RecursiveMode::NonRecursive).ok();

            loop {
                {
                    let s = stop.lock().unwrap();
                    if *s {
                        break;
                    }
                }
                let events = Self::read_events(&log_path);
                let watchers = self.watchers.lock().unwrap();
                for event in events {
                    for (pattern, callback) in watchers.iter() {
                        if Self::matches_pattern(&event.channel, pattern) {
                            callback(event.clone());
                        }
                    }
                }

                match rx.recv_timeout(Duration::from_secs(1)) {
                    Ok(_) => continue,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }
        }
    }

    impl Default for Listener {
        fn default() -> Self {
            Self::new().expect("Listener::default failed")
        }
    }
}

/// Real Redis Streams consumer-group transport (`XREADGROUP`/`XACK`/
/// `XAUTOCLAIM`) — see [`streams`] module docs. Enabled via the `streams`
/// feature; pulls in `redis` + `tokio` (not part of the default feature set,
/// which stays dependency-light for callers that only need `publish`).
#[cfg(feature = "streams")]
pub mod streams;

#[cfg(not(feature = "listener"))]
pub mod listener {
    // Pre-existing bug fix (unrelated to the `streams` feature added here):
    // this stub previously returned `Result<_, notify::Error>`, but `notify`
    // is only a dependency when the `listener` feature is enabled — so
    // `cargo build`/`cargo build --features streams` with default features
    // (`listener` off) failed to compile at all. `std::io::Error` keeps the
    // stub's `Err` variant meaningful without depending on `notify`.
    pub struct Listener;
    impl Listener {
        pub fn new() -> std::io::Result<Self> {
            Ok(Self)
        }
        pub fn subscribe(&mut self, _: &str, _: impl Fn(()) + Send + Sync + 'static) -> &mut Self {
            self
        }
        pub fn stop(&self) {}
        pub fn run(&self) {}
        pub fn tail(&self, _: &str) -> std::io::Result<Vec<String>> {
            Ok(vec![])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The envelope must be parseable JSON, not just plausible-looking text.
    /// Fields are serialized through serde so hostile values cannot break out
    /// of their JSON positions.
    #[test]
    fn envelope_is_valid_json() {
        let raw = make_envelope(
            "tengine.silo.verify.v1",
            &serde_json::json!({"ok": true}),
            "/x",
        )
        .expect("serialize");
        let parsed: serde_json::Value = serde_json::from_str(&raw).expect("envelope must be JSON");
        assert_eq!(parsed["type"], "tengine.silo.verify.v1");
        assert_eq!(parsed["source"], "/x");
        assert_eq!(parsed["specversion"], "1.0");
        assert_eq!(parsed["data"]["ok"], true);
    }

    #[test]
    fn envelope_escapes_channel_and_source() {
        let nasty = r#"a"b\c"#;
        let raw = make_envelope(nasty, &serde_json::json!({}), nasty).expect("serialize");
        let parsed: serde_json::Value =
            serde_json::from_str(&raw).expect("quotes in channel must not break the envelope");
        assert_eq!(parsed["type"], nasty);
        assert_eq!(parsed["source"], nasty);
    }

    /// Regression: `Path::new(".").file_name()` is `None`, so this used to
    /// return `/unknown` for every caller that hadn't set NERVOUS_SOURCE.
    #[test]
    fn default_source_is_the_cwd_basename_not_unknown() {
        if std::env::var_os("NERVOUS_SOURCE").is_some() {
            return; // caller pinned it; nothing to assert
        }
        let expected = std::env::current_dir()
            .unwrap()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .to_string();
        assert_eq!(default_source(), format!("/{expected}"));
        assert_ne!(default_source(), "/unknown");
    }

    const VALID: &str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

    /// An untraced envelope must serialize to the SAME bytes as before this
    /// field existed: absent, never `null`. Every legacy consumer (including
    /// `additionalProperties: false` envelope schemas) depends on this.
    #[test]
    fn untraced_envelope_omits_the_field_entirely() {
        let envelope =
            Envelope::new("/kb", "kb.entry.created.v2", &serde_json::json!({"n": 1})).unwrap();
        assert_eq!(envelope.traceparent(), None);

        let raw = envelope.to_json().unwrap();
        assert!(
            !raw.contains("traceparent"),
            "untraced envelope must not emit the key at all: {raw}"
        );
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert!(parsed.as_object().unwrap().get("traceparent").is_none());
    }

    /// Legacy envelopes persisted before this field existed must still
    /// deserialize — absence is normal, not an error.
    #[test]
    fn legacy_envelope_without_the_field_still_deserializes() {
        let legacy = r#"{"specversion":"1.0","id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","source":"/tengine","type":"tengine.silo.verify.v1","time":"2026-05-11T10:00:00Z","datacontenttype":"application/json","data":{"silo":"racing"}}"#;
        let envelope: Envelope = serde_json::from_str(legacy).expect("legacy envelope must parse");
        assert_eq!(envelope.traceparent(), None);
        assert_eq!(envelope.channel(), "tengine.silo.verify.v1");
    }

    /// The exact supplied header survives serialize → deserialize → replay.
    /// Nothing regenerates the trace- or span-id.
    #[test]
    fn traceparent_survives_a_serde_roundtrip_byte_for_byte() {
        let envelope = Envelope::new("/kb", "kb.entry.created.v2", &serde_json::json!({"n": 1}))
            .unwrap()
            .with_traceparent(VALID)
            .expect("valid header");

        let raw = envelope.to_json().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(parsed["traceparent"], VALID);

        let replayed: Envelope = serde_json::from_str(&raw).expect("replay");
        assert_eq!(replayed.traceparent(), Some(VALID));
        assert_eq!(
            replayed, envelope,
            "replay must be identical, not merely similar"
        );

        // Re-serializing the replayed envelope reproduces the same bytes, so
        // a durable retry publishes the same chain membership.
        assert_eq!(replayed.to_json().unwrap(), raw);
    }

    #[test]
    fn with_traceparent_accepts_any_two_lowercase_hex_flags() {
        for flags in ["00", "01", "ff", "3a"] {
            let header = format!("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-{flags}");
            let envelope = Envelope::new("/x", "c.v1", &serde_json::json!({}))
                .unwrap()
                .with_traceparent(&header)
                .unwrap_or_else(|e| panic!("flags {flags} must be accepted: {e}"));
            assert_eq!(envelope.traceparent(), Some(header.as_str()));
        }
    }

    #[test]
    fn with_traceparent_rejects_malformed_headers() {
        let cases: &[(&str, TraceContextError)] = &[
            // Wrong field count / structure.
            ("", TraceContextError::MalformedStructure),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",
                TraceContextError::MalformedStructure,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
                TraceContextError::MalformedStructure,
            ),
            (
                "004bf92f3577b34da6a3ce929d0e0e473600f067aa0ba902b701",
                TraceContextError::MalformedStructure,
            ),
            // Version.
            (
                "0-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                TraceContextError::MalformedStructure,
            ),
            (
                "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                TraceContextError::UnsupportedVersion,
            ),
            (
                "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                TraceContextError::UnsupportedVersion,
            ),
            // Trace-id: short, long, uppercase, non-hex, all-zero.
            (
                "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",
                TraceContextError::InvalidTraceId,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e47360-00f067aa0ba902b7-01",
                TraceContextError::InvalidTraceId,
            ),
            (
                "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
                TraceContextError::InvalidTraceId,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e473g-00f067aa0ba902b7-01",
                TraceContextError::InvalidTraceId,
            ),
            (
                "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
                TraceContextError::ZeroTraceId,
            ),
            // Span-id: short, long, uppercase, all-zero.
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b-01",
                TraceContextError::InvalidSpanId,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b70-01",
                TraceContextError::InvalidSpanId,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00F067AA0BA902B7-01",
                TraceContextError::InvalidSpanId,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
                TraceContextError::ZeroSpanId,
            ),
            // Flags: short, long, uppercase.
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
                TraceContextError::InvalidFlags,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-011",
                TraceContextError::InvalidFlags,
            ),
            (
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-FF",
                TraceContextError::InvalidFlags,
            ),
        ];
        for (header, expected) in cases {
            let envelope = Envelope::new("/x", "c.v1", &serde_json::json!({})).unwrap();
            match envelope.with_traceparent(header) {
                Ok(_) => panic!("must reject {header:?}"),
                Err(e) => assert_eq!(&e, expected, "wrong error for {header:?}"),
            }
        }
    }

    /// A rejected header leaves nothing behind: the error carries no
    /// half-traced envelope, so there is no way to publish an unvalidated
    /// trace context.
    #[test]
    fn trace_context_error_is_a_std_error_with_a_message() {
        let err = Envelope::new("/x", "c.v1", &serde_json::json!({}))
            .unwrap()
            .with_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01")
            .unwrap_err();
        let as_dyn: &dyn std::error::Error = &err;
        assert!(as_dyn.to_string().contains("all zeroes"), "{as_dyn}");
    }

    /// Validation binds at the DESERIALIZATION boundary too — a hand-edited
    /// or third-party-written line cannot smuggle an invalid header in.
    #[test]
    fn deserialization_rejects_an_invalid_traceparent() {
        let bad_values = [
            "\"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7\"", // too few fields
            "\"01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\"", // bad version
            "\"00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01\"", // uppercase
            "\"00-00000000000000000000000000000000-00f067aa0ba902b7-01\"", // zero trace
            "\"00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01\"", // zero span
            "\"\"",                                                     // empty string
            "null", // explicitly null — absence is allowed, null is not
            "42",   // non-string
            "true", // non-string
            "[\"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\"]", // non-string
        ];
        for value in bad_values {
            let raw = format!(
                r#"{{"specversion":"1.0","id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","source":"/x","type":"c.v1","time":"2026-05-11T10:00:00Z","datacontenttype":"application/json","data":{{}},"traceparent":{value}}}"#
            );
            assert!(
                serde_json::from_str::<Envelope>(&raw).is_err(),
                "traceparent {value} must be rejected at the deserialization boundary"
            );
        }
    }

    #[test]
    fn deserialization_accepts_a_valid_traceparent() {
        let raw = format!(
            r#"{{"specversion":"1.0","id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","source":"/x","type":"c.v1","time":"2026-05-11T10:00:00Z","datacontenttype":"application/json","data":{{}},"traceparent":"{VALID}"}}"#
        );
        let envelope: Envelope = serde_json::from_str(&raw).expect("valid header must parse");
        assert_eq!(envelope.traceparent(), Some(VALID));
    }

    /// `make_envelope` and `Envelope::new` keep their pre-existing
    /// signatures and untraced output — no implicit trace issuance, no
    /// environment reads.
    #[test]
    fn constructors_never_mint_a_traceparent_implicitly() {
        std::env::set_var("TRACEPARENT", VALID);
        let raw = make_envelope("c.v1", &serde_json::json!({}), "/x").unwrap();
        std::env::remove_var("TRACEPARENT");
        assert!(
            !raw.contains("traceparent"),
            "the SDK must not read an ambient traceparent: {raw}"
        );
    }

    #[cfg(feature = "native")]
    #[test]
    fn native_publish_appends_one_parseable_line_per_event() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("nested/debug.jsonl");
        // Scoped to this test only — the default target is the LIVE bus log.
        std::env::set_var("NERVOUS_DEBUG_LOG", &log);
        std::env::set_var("NERVOUS_NO_ZELLIJ", "1");

        native_publish_with_source("test.channel.v1", &serde_json::json!({"n": 1}), "/probe")
            .expect("first publish");
        native_publish_with_source("test.channel.v1", &serde_json::json!({"n": 2}), "/probe")
            .expect("second publish");

        let body = std::fs::read_to_string(&log).unwrap();
        let lines: Vec<&str> = body.lines().filter(|l| !l.trim().is_empty()).collect();
        assert_eq!(lines.len(), 2, "one line per publish, log={body:?}");
        for (i, line) in lines.iter().enumerate() {
            let ev: serde_json::Value =
                serde_json::from_str(line).expect("each line must parse standalone");
            assert_eq!(ev["type"], "test.channel.v1");
            assert_eq!(ev["source"], "/probe");
            assert_eq!(ev["data"]["n"], (i + 1) as i64);
        }
        std::env::remove_var("NERVOUS_DEBUG_LOG");
        std::env::remove_var("NERVOUS_NO_ZELLIJ");
    }

    #[cfg(not(feature = "native"))]
    #[test]
    fn native_publish_is_unsupported_without_the_feature() {
        let result = native_publish("test.channel", &serde_json::json!({"k": "v"}));
        assert!(matches!(result, Err(NativePublishError::Io(_))));
    }
}

#[cfg(all(feature = "listener", test))]
mod listener_tests {
    use crate::listener::Listener;

    #[test]
    fn listener_pattern_matching() {
        assert!(Listener::matches_pattern(
            "deer-flow.cycle.start",
            "deer-flow.#"
        ));
        assert!(Listener::matches_pattern(
            "deer-flow.cycle.done",
            "deer-flow.#"
        ));
        assert!(Listener::matches_pattern("deer-flow.cycle", "deer-flow.#"));
        assert!(Listener::matches_pattern(
            "tengine.session.frame",
            "tengine.#"
        ));
        assert!(Listener::matches_pattern("agent.session.started", "#"));
        assert!(Listener::matches_pattern("agent.session.started", "*"));
        assert!(Listener::matches_pattern(
            "deer-flow.audit.recommendation.v1",
            "deer-flow.audit.#"
        ));
        assert!(!Listener::matches_pattern(
            "deer-flow.audit.recommendation.v1",
            "deer-flow.tool.*"
        ));
    }

    #[test]
    fn cloud_event_parse() {
        let line = r#"{"specversion":"1.0","id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","source":"/tengine","type":"tengine.silo.verify.v1","time":"2026-05-11T10:00:00Z","datacontenttype":"application/json","data":{"silo":"racing","success":true}}"#;
        let event = Listener::parse_line(line).expect("failed to parse");
        assert_eq!(event.channel, "tengine.silo.verify.v1");
        assert_eq!(event.source, "/tengine");
        assert_eq!(event.data["silo"], "racing");
    }
}
