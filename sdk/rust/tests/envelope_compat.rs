use nbus::{make_envelope, Envelope};

const CROCKFORD_BASE32: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

#[test]
fn new_envelope_uses_a_parseable_monotonic_crockford_ulid() {
    let first = Envelope::new("/kb", "kb.entry.created.v2", &serde_json::json!({"n": 1}))
        .expect("create first envelope");
    let second = Envelope::new("/kb", "kb.entry.created.v2", &serde_json::json!({"n": 2}))
        .expect("create second envelope");

    for envelope in [&first, &second] {
        assert_eq!(envelope.id().len(), 26);
        assert!(
            envelope.id().chars().all(|c| CROCKFORD_BASE32.contains(c)),
            "ID must use the documented Crockford Base32 alphabet: {}",
            envelope.id()
        );
        ulid::Ulid::from_string(envelope.id()).expect("ID must parse as a ULID");
    }
    assert!(
        first.id() < second.id(),
        "the SDK promises monotonic IDs even within one millisecond"
    );
}

#[test]
fn serde_envelope_preserves_shape_and_escapes_fields() {
    let source = r#"/kb/producer\"quoted"#;
    let channel = r#"kb.\"entry\\created.v2"#;
    let envelope = Envelope::new(source, channel, &serde_json::json!({"quote": "a\"b\\c"}))
        .expect("create envelope");

    let json = envelope.to_json().expect("serialize envelope");
    let value: serde_json::Value = serde_json::from_str(&json).expect("valid JSON");
    assert_eq!(value["specversion"], "1.0");
    assert_eq!(value["source"], source);
    assert_eq!(value["type"], channel);
    assert_eq!(value["datacontenttype"], "application/json");
    assert_eq!(value["data"]["quote"], "a\"b\\c");
}

#[test]
fn serialized_convenience_api_is_available_to_external_callers() {
    let raw = make_envelope(
        "kb.entry.created.v2",
        &serde_json::json!({"entry_id": "e-1"}),
        "/kb",
    )
    .expect("create serialized envelope");
    let value: serde_json::Value = serde_json::from_str(&raw).expect("valid JSON");
    assert_eq!(value["source"], "/kb");
    assert_eq!(value["type"], "kb.entry.created.v2");
    assert_eq!(value["data"]["entry_id"], "e-1");
}

#[test]
fn durable_callers_can_persist_and_replay_one_envelope_unchanged() {
    let envelope = Envelope::new(
        "/kb",
        "kb.entry.created.v2",
        &serde_json::json!({"entry_id": "e-1", "title": "retry safely"}),
    )
    .expect("create envelope");
    let persisted = envelope.to_json().expect("persist envelope");

    let replay: Envelope = serde_json::from_str(&persisted).expect("read persisted envelope");
    assert_eq!(replay.id(), envelope.id());
    assert_eq!(replay, envelope);
    assert_eq!(replay.to_json().expect("replay JSON"), persisted);
}
