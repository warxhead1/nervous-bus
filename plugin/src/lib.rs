// nervous-bus — zellij WASM plugin
//
// Routes pipe messages between named channels. The plugin is intentionally
// dumb: parse the event type, fan out to subscribers, retain a small ring
// buffer for late-joining subscribers. No business logic, no schema validation.
//
// Wire contract: see ../README.md (CloudEvents-lite JSONL)
//
// All zellij shim calls (register_plugin!, request_permission, subscribe,
// pipe_message_to_plugin) are gated with #[cfg(not(test))] so the crate can
// be compiled as rlib for unit/integration tests without a WASM host.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, VecDeque};
use zellij_tile::prelude::*;

const RING_CAPACITY: usize = 256;

#[cfg(not(test))]
register_plugin!(State);

#[derive(Default)]
pub struct State {
    /// channel name -> plugin IDs that have subscribed for fan-out
    pub subscribers: HashMap<String, Vec<u32>>,
    /// channel name -> ring buffer of recent CloudEvent JSONL strings
    pub ring: HashMap<String, VecDeque<String>>,
    /// monotonic counter for dead-letter IDs within this session
    pub dl_counter: u64,
    /// epoch seconds of last subscriber snapshot publish (rate-limited)
    pub last_subscriber_snapshot: u64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CloudEvent {
    pub specversion: String,
    pub id: String,
    pub source: String,
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(default)]
    pub subject: Option<String>,
    pub time: String,
    #[serde(default)]
    pub datacontenttype: Option<String>,
    pub data: serde_json::Value,
}

impl ZellijPlugin for State {
    fn load(&mut self, _configuration: BTreeMap<String, String>) {
        #[cfg(not(test))]
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::MessageAndLaunchOtherPlugins,
        ]);
    }

    fn pipe(&mut self, pipe_message: PipeMessage) -> bool {
        let payload = match pipe_message.payload.as_deref() {
            Some(p) => p,
            None => return false,
        };

        if let Some(channel) = pipe_message.name.strip_prefix("subscribe:") {
            // Only plugin sources can receive pipe-back fan-out.
            // CLI and keybind sources use the debug log file instead.
            let plugin_id = match pipe_message.source {
                PipeSource::Plugin(id) => id,
                _ => return false,
            };

            let subs = self.subscribers.entry(channel.to_string()).or_default();
            if !subs.contains(&plugin_id) {
                subs.push(plugin_id);
            }

            // Replay the ring buffer so late-joining subscribers catch up.
            if let Some(events) = self.ring.get(channel) {
                for _event_line in events.iter() {
                    #[cfg(not(test))]
                    pipe_message_to_plugin(
                        MessageToPlugin::new("nervous-bus-event")
                            .with_destination_plugin_id(plugin_id)
                            .with_payload(_event_line),
                    );
                }
            }
            return false;
        }

        // Subscriber query path: 'nervous subscribers' sends to "subscribers:" channel.
        // Returns JSON map of channel → [plugin_ids] for all channels.
        if pipe_message.name == "subscribers" || pipe_message.name == "subscribers:" {
            #[cfg(not(test))]
            {
                let reply: serde_json::Value = self
                    .subscribers
                    .iter()
                    .map(|(ch, ids)| (ch.clone(), serde_json::json!(ids)))
                    .collect();
                let payload = serde_json::to_string(&reply).unwrap_or_default();
                pipe_message_to_plugin(
                    MessageToPlugin::new("nervous-bus-reply")
                        .with_destination_plugin_id(match pipe_message.source {
                            PipeSource::Plugin(id) => id,
                            _ => 0,
                        })
                        .with_payload(&payload),
                );
            }
            return false;
        }

        // Publish path: parse envelope, append to ring, fan out.
        let source_str = format_pipe_source(&pipe_message.source);
        let event: CloudEvent = match serde_json::from_str(payload) {
            Ok(e) => e,
            Err(_) => {
                let failure_reason =
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(payload) {
                        if v.get("type")
                            .and_then(|t| t.as_str())
                            .unwrap_or("")
                            .is_empty()
                        {
                            "missing_required_field"
                        } else {
                            "malformed_json"
                        }
                    } else {
                        "malformed_json"
                    };
                self.push_dead_letter(payload, failure_reason, &source_str);
                return false;
            }
        };

        // Missing required field: event_type must be non-empty.
        if event.event_type.is_empty() {
            self.push_dead_letter(payload, "missing_required_field", &source_str);
            return false;
        }

        let channel = event.event_type.clone();
        self.append_and_fanout(&channel, payload);

        false
    }

    fn update(&mut self, _event: Event) -> bool {
        // Rate-limited subscriber snapshot publish — every 30s, push channel→[plugin_ids] to bus.
        let now_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        if now_secs >= self.last_subscriber_snapshot + 30 {
            self.last_subscriber_snapshot = now_secs;
            let subs: serde_json::Value = self
                .subscribers
                .iter()
                .map(|(ch, ids)| (ch.clone(), serde_json::json!(ids)))
                .collect();
            let envelope = serde_json::json!({
                "specversion": "1.0",
                "id": format!("sub-snap-{}", now_secs),
                "source": "/nervous-bus/plugin",
                "type": "bus.subscribers.snapshot",
                "time": epoch_secs_to_rfc3339(now_secs),
                "datacontenttype": "application/json",
                "data": subs,
            });
            if let Ok(line) = serde_json::to_string(&envelope) {
                self.append_and_fanout("bus.subscribers.snapshot", &line);
            }
        }
        false
    }

    fn render(&mut self, _rows: usize, _cols: usize) {}
}

impl State {
    /// Append a line to a channel's ring buffer and fan out to subscribers.
    /// Stamps data._bus.received_at (epoch microseconds) for latency observability.
    fn append_and_fanout(&mut self, channel: &str, line: &str) {
        let received_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_micros() as u64;

        // Inject _bus.received_at into the payload for latency tracking.
        // Path: data._bus.received_at (epoch microseconds).
        let stamped_line = if let Ok(mut v) = serde_json::from_str::<serde_json::Value>(line) {
            if let Some(data) = v.get_mut("data").and_then(|d| d.as_object_mut()) {
                let bus = data
                    .entry("_bus".to_string())
                    .or_insert_with(|| serde_json::Value::Object(Default::default()));
                if let Some(bus_obj) = bus.as_object_mut() {
                    bus_obj.insert(
                        "received_at".to_string(),
                        serde_json::Value::Number(received_at.into()),
                    );
                }
            }
            serde_json::to_string(&v).unwrap_or_else(|_| line.to_string())
        } else {
            line.to_string()
        };

        let buf = self.ring.entry(channel.to_string()).or_default();
        if buf.len() >= RING_CAPACITY {
            buf.pop_front();
        }
        buf.push_back(stamped_line.clone());

        let plugin_ids: Vec<u32> = self.subscribers.get(channel).cloned().unwrap_or_default();

        for _id in plugin_ids {
            #[cfg(not(test))]
            pipe_message_to_plugin(
                MessageToPlugin::new("nervous-bus-event")
                    .with_destination_plugin_id(_id)
                    .with_payload(&stamped_line),
            );
        }
    }

    /// Construct and enqueue a bus.dead_letter CloudEvent.
    pub fn push_dead_letter(&mut self, payload: &str, failure_reason: &str, source_str: &str) {
        self.dl_counter += 1;
        let ts_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let id = format!("dl-{ts_secs}-{}", self.dl_counter);

        let original_type = serde_json::from_str::<serde_json::Value>(payload)
            .ok()
            .and_then(|v| {
                v.get("type")
                    .and_then(|t| t.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_else(|| "UNKNOWN".to_string());

        let excerpt: String = payload.chars().take(500).collect();

        let envelope = serde_json::json!({
            "specversion": "1.0",
            "id": id,
            "source": "/nervous-bus/plugin",
            "type": "bus.dead_letter",
            "time": epoch_secs_to_rfc3339(ts_secs),
            "datacontenttype": "application/json",
            "data": {
                "original_type": original_type,
                "failure_reason": failure_reason,
                "original_payload_excerpt": excerpt,
                "original_source": source_str,
                "retry_count": 0,
            }
        });

        if let Ok(line) = serde_json::to_string(&envelope) {
            self.append_and_fanout("bus.dead_letter", &line);
        }
    }
}

fn format_pipe_source(src: &PipeSource) -> String {
    match src {
        PipeSource::Cli(id) => format!("cli:{id}"),
        PipeSource::Plugin(id) => format!("plugin:{id}"),
        PipeSource::Keybind => "keybind".to_string(),
    }
}

/// Minimal Gregorian calendar formatter — epoch seconds → RFC3339 UTC string.
/// Algorithm: Howard Hinnant's civil_from_days (public domain).
fn epoch_secs_to_rfc3339(secs: u64) -> String {
    let time_of_day = secs % 86400;
    let h = time_of_day / 3600;
    let min = (time_of_day % 3600) / 60;
    let s = time_of_day % 60;

    let z = (secs / 86400) as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mo <= 2 { y + 1 } else { y };

    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{min:02}:{s:02}Z")
}
