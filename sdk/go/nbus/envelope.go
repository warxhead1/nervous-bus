package nbus

import (
	"encoding/json"
	"time"

	"github.com/oklog/ulid/v2"
)

// Envelope is the CloudEvents-lite wire format used across the nervous-bus
// ecosystem (see schemas/*.json in the repo root — every field here matches
// the `required` set every schema enforces). Rust, Python, and shell
// consumers deserialize the exact same shape, so this struct must never grow
// fields the shared schema doesn't also define without a corresponding
// schema update.
type Envelope struct {
	// SpecVersion is always "1.0" — see NewEnvelope.
	SpecVersion string `json:"specversion"`
	// ID is a ULID (26-char Crockford Base32, lexically sortable by
	// millisecond timestamp) uniquely identifying this event.
	ID string `json:"id"`
	// Source is a URI-path identifying the producer, e.g. "/hearth-loom/worker".
	Source string `json:"source"`
	// Type is the dot-delimited, versioned channel name, e.g. "loom.lifecycle.started.v1".
	Type string `json:"type"`
	// Subject optionally scopes the event to an entity within Type (e.g. a bead ID).
	Subject string `json:"subject,omitempty"`
	// Time is the publish timestamp, RFC3339 in UTC.
	Time string `json:"time"`
	// DataContentType is always "application/json".
	DataContentType string          `json:"datacontenttype"`
	Data            json.RawMessage `json:"data"`
}

// NewEnvelope wraps data in a nervous-bus envelope, stamping a fresh ULID id
// and the current UTC time. source should be a URI-path with a leading slash
// (e.g. "/my-service"); channel is the dot-delimited, versioned type name
// (e.g. "bus.notify.v1").
func NewEnvelope(source, channel string, data any) (Envelope, error) {
	raw, err := json.Marshal(data)
	if err != nil {
		return Envelope{}, err
	}
	return Envelope{
		SpecVersion: "1.0",
		// ulid.Make() draws from a process-wide monotonic entropy source that
		// is safe for concurrent use and guarantees uniqueness plus
		// monotonic ordering for IDs minted within the same millisecond.
		ID:              ulid.Make().String(),
		Source:          source,
		Type:            channel,
		Time:            time.Now().UTC().Format(time.RFC3339),
		DataContentType: "application/json",
		Data:            raw,
	}, nil
}

// Decode unmarshals the envelope's Data field into dst.
func (e Envelope) Decode(dst any) error {
	return json.Unmarshal(e.Data, dst)
}
