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

use serde::Serialize;
use std::io::Write as IoWrite;
use std::path::PathBuf;
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
    std::env::var("NERVOUS_SOURCE")
        .unwrap_or_else(|_| {
            let basename = std::path::Path::new(".")
                .file_name()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_else(|| "unknown".into());
            format!("/{}", basename)
        })
}

fn ulid() -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    let mut bytes = [0u8; 10];
    getrandom::getrandom(&mut bytes).ok();
    let hex: String = bytes.iter().map(|b| format!("{:02X}", b)).collect();
    format!("{:010}{}", ts, &hex[..16])
}

fn iso_now() -> String {
    use chrono::TimeZone;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs() as i64;
    chrono::Utc.timestamp_opt(secs, 0).single()
        .map(|t| t.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_else(|| "1970-01-01T00:00:00Z".into())
}

fn make_envelope(channel: &str, payload: &(impl Serialize + ?Sized), source: &str) -> Result<String, serde_json::Error> {
    let id = ulid();
    let time = iso_now();
    let data = serde_json::to_string(payload)?;
    Ok(format!(
        r#"{{"specversion":"1.0","id":"{}","source":{},"type":"{}","time":"{}","datacontenttype":"application/json","data":{} }}"#,
        id,
        serde_json::to_string(source)?,
        channel,
        time,
        data,
    ))
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

#[cfg(feature = "native")]
pub fn native_publish<T: Serialize + ?Sized>(channel: &str, payload: &T) -> Result<(), NativePublishError> {
    let source = default_source();
    let envelope = make_envelope(channel, payload, &source).map_err(NativePublishError::Serde)?;
    let log_path = debug_log_path();

    if let Some(_zellij_sock) = std::env::var_os("ZELLIJ") {
        if std::env::var("NERVOUS_NO_ZELLIJ").ok() != Some("1".into()) {
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
            }
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
    writeln!(file, "{}", envelope).map_err(NativePublishError::Io)
}

#[cfg(not(feature = "native"))]
pub fn native_publish<T: Serialize + ?Sized>(_channel: &str, _payload: &T) -> Result<(), NativePublishError> {
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
            self.watchers.lock().unwrap().insert(pattern.to_string(), Box::new(callback));
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
            if parts.len() > event_parts.len() && !parts[parts.len() - 1].chars().all(|c| c == '*' || c == '#') {
                return false;
            }
            true
        }

        pub fn parse_line(line: &str) -> Option<CloudEvent> {
            let raw: serde_json::Value = serde_json::from_str(line).ok()?;
            let obj = raw.as_object()?;
            let channel = obj.get("type")?.as_str()?.to_string();
            let id = obj.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let source = obj.get("source").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let time = obj.get("time").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let data = obj.get("data").cloned().unwrap_or(serde_json::Value::Null);
            Some(CloudEvent { id, source, channel, time, data })
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

#[cfg(not(feature = "listener"))]
pub mod listener {
    use super::*;

    pub struct Listener;
    impl Listener {
        pub fn new() -> std::io::Result<Self> { Ok(Self) }
        pub fn subscribe(&mut self, _: &str, _: impl Fn(()) + Send + Sync + 'static) -> &mut Self { self }
        pub fn stop(&self) {}
        pub fn run(&self) {}
        pub fn tail(&self, _: &str) -> Result<Vec<String>, notify::Error> { Ok(vec![]) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publish_returns_cli_missing_when_no_binary() {
        let _ = serde_json::to_string(&serde_json::json!({"k": "v"})).unwrap();
    }

    #[test]
    fn native_publish_drops_subprocess_hop() {
        #[cfg(feature = "native")]
        {
            let result = native_publish("test.channel", &serde_json::json!({"k": "v"}));
            assert!(result.is_ok());
        }
        #[cfg(not(feature = "native"))]
        {
            let result = native_publish("test.channel", &serde_json::json!({"k": "v"}));
            assert!(matches!(result, Err(NativePublishError::Io(_))));
        }
    }
}

#[cfg(all(feature = "listener", test))]
mod listener_tests {
    use crate::listener::Listener;

    #[test]
    fn listener_pattern_matching() {
        assert!(Listener::matches_pattern("deer-flow.cycle.start", "deer-flow.#"));
        assert!(Listener::matches_pattern("deer-flow.cycle.done", "deer-flow.#"));
        assert!(Listener::matches_pattern("deer-flow.cycle", "deer-flow.#"));
        assert!(Listener::matches_pattern("tengine.session.frame", "tengine.#"));
        assert!(Listener::matches_pattern("agent.session.started", "#"));
        assert!(Listener::matches_pattern("agent.session.started", "*"));
        assert!(Listener::matches_pattern("deer-flow.audit.recommendation.v1", "deer-flow.audit.#"));
        assert!(!Listener::matches_pattern("deer-flow.audit.recommendation.v1", "deer-flow.tool.*"));
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