// Stub the WASM host function so zellij_tile's shim links in native test builds.
// pipe_message_to_plugin and other shim fns call this; making it a no-op is safe
// because tests verify State field mutations, not actual zellij IPC.
#[no_mangle]
extern "C" fn host_run_plugin_command() {}

use nervous_bus::State;
use std::collections::BTreeMap;
use zellij_tile::prelude::{PipeMessage, PipeSource, ZellijPlugin};

fn pub_msg(channel: &str, payload: &str) -> PipeMessage {
    PipeMessage {
        source: PipeSource::Cli("cli-0".into()),
        name: channel.into(),
        payload: Some(payload.into()),
        args: BTreeMap::new(),
        is_private: false,
    }
}

fn sub_msg(channel: &str, plugin_id: u32) -> PipeMessage {
    PipeMessage {
        source: PipeSource::Plugin(plugin_id),
        name: format!("subscribe:{}", channel),
        payload: Some("{}".into()),
        args: BTreeMap::new(),
        is_private: false,
    }
}

fn cloud_event(channel: &str) -> String {
    format!(
        r#"{{"specversion":"1.0","id":"01HTEST000000000000000001","source":"/test/unit","type":"{channel}","time":"2026-05-02T00:00:00Z","datacontenttype":"application/json","data":{{"n":1}}}}"#
    )
}

#[test]
fn publish_appends_to_ring_buffer() {
    let mut state = State::default();
    let payload = cloud_event("tengine.session.frame");
    state.pipe(pub_msg("tengine.session.frame", &payload));

    let ring = state
        .ring
        .get("tengine.session.frame")
        .expect("ring missing");
    assert_eq!(ring.len(), 1);
    assert!(ring[0].contains("tengine.session.frame"));
}

#[test]
fn subscribe_registers_plugin_id() {
    let mut state = State::default();
    state.pipe(sub_msg("tengine.session.frame", 42));

    let subs = state
        .subscribers
        .get("tengine.session.frame")
        .expect("subscriber list missing");
    assert!(subs.contains(&42));
}

#[test]
fn subscribe_is_idempotent() {
    let mut state = State::default();
    state.pipe(sub_msg("bus.bead.created", 7));
    state.pipe(sub_msg("bus.bead.created", 7));

    let subs = state.subscribers.get("bus.bead.created").unwrap();
    assert_eq!(subs.len(), 1, "duplicate subscriber registered");
}

#[test]
fn ring_buffer_caps_at_capacity() {
    let mut state = State::default();
    // Fill ring beyond RING_CAPACITY (256) with distinct events
    for i in 0..300u32 {
        let payload = format!(
            r#"{{"specversion":"1.0","id":"{:026}","source":"/test","type":"bus.test","time":"2026-05-02T00:00:00Z","datacontenttype":"application/json","data":{{"i":{i}}}}}"#,
            i
        );
        state.pipe(pub_msg("bus.test", &payload));
    }

    let ring = state.ring.get("bus.test").unwrap();
    assert_eq!(ring.len(), 256, "ring should cap at RING_CAPACITY");
    // Oldest 44 events should be evicted; ring[0] should contain i=44
    assert!(ring[0].contains(r#""i":44"#));
}

#[test]
fn malformed_payload_is_dropped() {
    let mut state = State::default();
    state.pipe(pub_msg("bus.test", "not-json-at-all"));
    assert!(
        !state.ring.contains_key("bus.test"),
        "malformed event must not enter ring"
    );
}

#[test]
fn malformed_goes_to_dead_letter() {
    let mut state = State::default();
    state.pipe(pub_msg("bus.test", "not-json-at-all"));

    let dl_ring = state
        .ring
        .get("bus.dead_letter")
        .expect("dead_letter ring must exist");
    assert_eq!(dl_ring.len(), 1, "exactly one dead-letter entry expected");

    let dl: serde_json::Value =
        serde_json::from_str(&dl_ring[0]).expect("dead-letter must be valid JSON");
    assert_eq!(dl["type"], "bus.dead_letter");
    assert_eq!(dl["data"]["failure_reason"], "malformed_json");
    assert_eq!(dl["data"]["original_type"], "UNKNOWN");
    assert!(dl["data"]["original_payload_excerpt"]
        .as_str()
        .unwrap()
        .contains("not-json"));
    assert_eq!(dl["data"]["retry_count"], 0);
}

#[test]
fn missing_type_field_goes_to_dead_letter() {
    let mut state = State::default();
    // Valid JSON but missing the required "type" field
    state.pipe(pub_msg("bus.test", r#"{"specversion":"1.0","id":"01HTEST","source":"/x","time":"2026-05-02T00:00:00Z","data":{}}"#));

    assert!(
        !state.ring.contains_key("bus.test"),
        "event without type must not route to bus.test"
    );
    let dl_ring = state
        .ring
        .get("bus.dead_letter")
        .expect("dead_letter ring must exist");
    let dl: serde_json::Value = serde_json::from_str(&dl_ring[0]).unwrap();
    assert_eq!(dl["data"]["failure_reason"], "missing_required_field");
}

#[test]
fn cli_subscribe_is_ignored() {
    let mut state = State::default();
    // CLI source can't receive fan-out; subscribe request should be a no-op
    let msg = PipeMessage {
        source: PipeSource::Cli("cli-0".into()),
        name: "subscribe:bus.bead.created".into(),
        payload: Some("{}".into()),
        args: BTreeMap::new(),
        is_private: false,
    };
    state.pipe(msg);
    assert!(
        !state.subscribers.contains_key("bus.bead.created"),
        "CLI source must not be registered as subscriber"
    );
}

#[test]
fn replay_on_subscribe_exposes_prior_events() {
    let mut state = State::default();

    // Publish two events before subscriber joins
    for i in 1u32..=2 {
        let payload = format!(
            r#"{{"specversion":"1.0","id":"{:026}","source":"/test","type":"hearth.ember.tick","time":"2026-05-02T00:00:00Z","datacontenttype":"application/json","data":{{"seq":{i}}}}}"#,
            i
        );
        state.pipe(pub_msg("hearth.ember.tick", &payload));
    }

    // Subscriber joins; ring should have 2 events (fan-out itself is a no-op in test builds)
    state.pipe(sub_msg("hearth.ember.tick", 99));

    let ring = state.ring.get("hearth.ember.tick").unwrap();
    assert_eq!(ring.len(), 2, "ring should have 2 events for replay");
    assert!(ring[0].contains(r#""seq":1"#));
    assert!(ring[1].contains(r#""seq":2"#));
}
