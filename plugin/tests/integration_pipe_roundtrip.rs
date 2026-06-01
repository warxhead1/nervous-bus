// integration_pipe_roundtrip — end-to-end behavior of the pipe boundary
// (publish → ring → subscriber fan-out). route_smoke.rs covers many primitive
// state mutations; this file focuses on the multi-actor and lifecycle paths
// that a regression in fan-out would otherwise sneak past CI on (per
// nervous-bus-3jy).
//
// Test build: zellij_tile's WASM imports are gated behind #[cfg(not(test))]
// in plugin/src/lib.rs, so calling state.pipe() exercises the real routing
// code without spawning a WASM host. fan-out side-effects (pipe_message_to_plugin)
// are no-ops in tests; behavior is verified through observable State mutations.

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

fn ev(channel: &str, seq: u32) -> String {
    format!(
        r#"{{"specversion":"1.0","id":"01HTEST{:019}","source":"/test","type":"{channel}","time":"2026-05-03T00:00:00Z","datacontenttype":"application/json","data":{{"seq":{seq}}}}}"#,
        seq
    )
}

#[test]
fn two_subscribers_on_same_channel_both_register() {
    // Bead acceptance #3: two subscribers on same channel → both receive.
    // Test verifies registration; the actual receive is a no-op in test
    // builds (pipe_message_to_plugin is gated to non-test).
    let mut state = State::default();
    state.pipe(sub_msg("bus.bead.created", 11));
    state.pipe(sub_msg("bus.bead.created", 22));

    let subs = state
        .subscribers
        .get("bus.bead.created")
        .expect("subscriber list missing");
    assert!(subs.contains(&11), "plugin 11 must be registered");
    assert!(subs.contains(&22), "plugin 22 must be registered");
    assert_eq!(subs.len(), 2, "exactly two distinct subscribers expected");
}

#[test]
fn subscribers_are_per_channel_not_shared() {
    // Subscribing to channel A must not enroll the plugin in channel B.
    // (regression guard: a previous refactor briefly used a single Vec.)
    let mut state = State::default();
    state.pipe(sub_msg("bus.bead.created", 1));
    state.pipe(sub_msg("loom.lifecycle.v1", 2));

    let a = state.subscribers.get("bus.bead.created").unwrap();
    let b = state.subscribers.get("loom.lifecycle.v1").unwrap();
    assert_eq!(a, &vec![1]);
    assert_eq!(b, &vec![2]);
}

#[test]
fn replay_on_late_subscribe_yields_all_three_events() {
    // Bead acceptance #4: subscribe AFTER 3 events → all 3 are in the
    // ring (ready for fan-out replay). Tightens replay_on_subscribe_exposes_prior_events
    // (which used 2) to the bead's exact spec.
    let mut state = State::default();
    for i in 1u32..=3 {
        state.pipe(pub_msg("bus.bead.scored", &ev("bus.bead.scored", i)));
    }
    state.pipe(sub_msg("bus.bead.scored", 50));

    let ring = state.ring.get("bus.bead.scored").expect("ring missing");
    assert_eq!(ring.len(), 3, "all 3 events must remain in ring for replay");
    assert!(ring[0].contains(r#""seq":1"#));
    assert!(ring[1].contains(r#""seq":2"#));
    assert!(ring[2].contains(r#""seq":3"#));

    let subs = state.subscribers.get("bus.bead.scored").unwrap();
    assert!(subs.contains(&50));
}

#[test]
fn channel_name_preserved_verbatim_through_pipe() {
    // A regression in routing that munged the channel string (case fold,
    // dot-collapse, prefix strip) would silently misroute. This guards by
    // round-tripping a deliberately-tricky channel name.
    let chan = "deer-flow.audit.recommendation";
    let mut state = State::default();
    state.pipe(pub_msg(chan, &ev(chan, 1)));

    assert!(
        state.ring.contains_key(chan),
        "channel name must be preserved exactly through pipe()"
    );
    let ring = state.ring.get(chan).unwrap();
    assert!(ring[0].contains(r#"deer-flow.audit.recommendation"#));
}

#[test]
fn malformed_does_not_create_subscriber_state() {
    // A malformed publish must NOT mint a subscriber list as a side effect
    // (defense against a routing bug where dead_letter accidentally registers
    // the publish CLI source as a subscriber).
    let mut state = State::default();
    state.pipe(pub_msg("bus.test", "definitely-not-json"));

    assert!(
        !state.subscribers.contains_key("bus.test"),
        "malformed publish must not register subscribers"
    );
    // dead_letter ring exists, but no subscriber list for it either
    assert!(
        !state.subscribers.contains_key("bus.dead_letter"),
        "dead_letter must not auto-register subscribers"
    );
}
