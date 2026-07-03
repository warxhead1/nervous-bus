// Package nbus is a typed Go client for nervous-bus: Redis Streams pub/sub
// using CloudEvents-lite envelopes, with consumer-group delivery, XAUTOCLAIM-based
// pending-entry recovery, and glob-style channel routing.
//
// It is the canonical, standalone port of the Redis Streams client pioneered
// in tachyonac-engine's internal/nbus package — see
// https://github.com/warxhead1/tachyonac-engine (internal/nbus/subscriber.go,
// internal/nbus/publisher.go) for the reference implementation this SDK
// generalizes. Any Go service that wants to publish to or consume from the
// public nervous-bus ecosystem (see the repo root README) should import this
// package rather than re-implementing the Redis wire protocol.
package nbus

// Config configures a Publisher and/or Subscriber.
//
// Zero-value Config is usable for a Publisher (Source defaults to "" — callers
// should set it) but a Subscriber additionally requires Group; Consumer
// defaults to the local hostname if empty (see NewSubscriber).
type Config struct {
	// Stream is the Redis Stream name a Subscriber reads from via XREADGROUP.
	// The Publisher does not use this field directly — it always writes to the
	// canonical pair of streams: nbus:<channel> and nbus:all. Default when
	// empty: "nbus:all" (the canonical fanout stream).
	Stream string

	// Source is the CloudEvents `source` URI-path stamped onto every envelope
	// this client publishes (e.g. "/tachyonac-engine", "/hearth-loom/worker").
	Source string

	// Group is the Redis consumer-group name used for XREADGROUP/XACK/XAUTOCLAIM.
	// Required for Subscriber.Run.
	Group string

	// Consumer is this replica's unique consumer identity within Group.
	// Defaults to os.Hostname() if empty.
	Consumer string
}

// streamOrDefault returns cfg.Stream, falling back to the canonical fanout
// stream "nbus:all" when unset.
func (c Config) streamOrDefault() string {
	if c.Stream == "" {
		return "nbus:all"
	}
	return c.Stream
}
