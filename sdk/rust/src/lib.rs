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

pub(crate) fn make_envelope(
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
