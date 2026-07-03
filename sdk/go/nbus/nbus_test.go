package nbus

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func newMiniredis(t *testing.T) (*redis.Client, *miniredis.Miniredis) {
	t.Helper()
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mr.Close)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return rdb, mr
}

func TestMatchesPattern(t *testing.T) {
	cases := []struct {
		pattern, channel string
		want             bool
	}{
		{"bus.notify.v1", "bus.notify.v1", true},
		{"loom.lifecycle.*", "loom.lifecycle.started", true},
		{"loom.lifecycle.*", "loom.lifecycle.started.v1", false}, // * is one segment
		{"loom.lifecycle.#", "loom.lifecycle.started.v1", true},  // # is any suffix
		{"loom.#", "loom.lifecycle.finished.v1", true},
		{"agent.session.*.v1", "agent.session.heartbeat.v1", true},
		{"bus.notify.v1", "bus.notify.v2", false},
		{"bus.notify.v1", "bus.notify", false}, // channel too short
		{"#", "anything.at.all.v3", true},
	}
	for _, tc := range cases {
		if got := matchesPattern(tc.pattern, tc.channel); got != tc.want {
			t.Errorf("matchesPattern(%q,%q)=%v want %v", tc.pattern, tc.channel, got, tc.want)
		}
	}
}

func TestNewEnvelope_RoundTrips(t *testing.T) {
	type body struct {
		Ticker string `json:"ticker"`
	}
	env, err := NewEnvelope("/my-service", "bus.notify.v1", body{Ticker: "SPX"})
	if err != nil {
		t.Fatalf("NewEnvelope: %v", err)
	}
	if env.SpecVersion != "1.0" {
		t.Errorf("SpecVersion = %q, want 1.0", env.SpecVersion)
	}
	if env.ID == "" {
		t.Error("ID must not be empty")
	}
	if env.DataContentType != "application/json" {
		t.Errorf("DataContentType = %q", env.DataContentType)
	}
	if _, err := time.Parse(time.RFC3339, env.Time); err != nil {
		t.Errorf("Time %q not RFC3339: %v", env.Time, err)
	}
	var decoded body
	if err := env.Decode(&decoded); err != nil {
		t.Fatalf("Decode: %v", err)
	}
	if decoded.Ticker != "SPX" {
		t.Errorf("decoded.Ticker = %q, want SPX", decoded.Ticker)
	}
}

func TestNewEnvelope_IDsAreUniqueAndSortable(t *testing.T) {
	e1, _ := NewEnvelope("/x", "bus.notify.v1", nil)
	e2, _ := NewEnvelope("/x", "bus.notify.v1", nil)
	if e1.ID == e2.ID {
		t.Fatal("expected distinct ULIDs across two calls")
	}
	if len(e1.ID) != 26 || len(e2.ID) != 26 {
		t.Errorf("expected 26-char ULIDs, got %d and %d", len(e1.ID), len(e2.ID))
	}
}

func TestPublishSync_WritesBothStreams(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/my-service"}, discardLogger())

	type body struct {
		Ticker string `json:"ticker"`
	}
	if err := pub.PublishSync(context.Background(), "bus.notify.v1", body{Ticker: "SPX"}); err != nil {
		t.Fatalf("PublishSync: %v", err)
	}

	for _, stream := range []string{"nbus:bus.notify.v1", "nbus:all"} {
		n, err := rdb.XLen(context.Background(), stream).Result()
		if err != nil || n != 1 {
			t.Fatalf("stream %s len=%d err=%v, want 1", stream, n, err)
		}
	}

	msgs, err := rdb.XRange(context.Background(), "nbus:all", "-", "+").Result()
	if err != nil || len(msgs) != 1 {
		t.Fatalf("XRange nbus:all: %v (n=%d)", err, len(msgs))
	}
	raw, ok := msgs[0].Values["_raw"].(string)
	if !ok {
		t.Fatalf("entry missing _raw field; values=%v", msgs[0].Values)
	}
	var env Envelope
	if err := json.Unmarshal([]byte(raw), &env); err != nil {
		t.Fatalf("decode _raw: %v", err)
	}
	if env.Type != "bus.notify.v1" || env.Source != "/my-service" {
		t.Errorf("envelope type=%q source=%q", env.Type, env.Source)
	}
	// Helper fields for tooling must mirror the envelope.
	if msgs[0].Values["type"] != "bus.notify.v1" {
		t.Errorf("helper type field = %v", msgs[0].Values["type"])
	}
	if msgs[0].Values["event_id"] != env.ID {
		t.Errorf("helper event_id field = %v, want %v", msgs[0].Values["event_id"], env.ID)
	}
}

func TestPublish_NilPublisherIsNoOp(t *testing.T) {
	var pub *Publisher
	// Must not panic.
	pub.Publish("bus.notify.v1", map[string]string{"x": "y"})
	if err := pub.PublishSync(context.Background(), "bus.notify.v1", nil); err != nil {
		t.Fatalf("nil Publisher.PublishSync returned error: %v", err)
	}
}

func TestDispatch_DecodesAndRoutesByPattern(t *testing.T) {
	sub := NewSubscriber(nil, Config{}, discardLogger())

	var got Envelope
	hits := 0
	sub.On("loom.lifecycle.#", func(_ context.Context, env Envelope) error {
		got = env
		hits++
		return nil
	})
	otherHits := 0
	sub.On("agent.session.*.v1", func(_ context.Context, _ Envelope) error {
		otherHits++
		return nil
	})

	env, _ := NewEnvelope("/op", "loom.lifecycle.started.v1", map[string]string{"reason": "test"})
	raw, _ := json.Marshal(env)
	sub.dispatch(context.Background(), redis.XMessage{ID: "1-0", Values: map[string]any{"_raw": string(raw)}})

	if hits != 1 || got.Type != "loom.lifecycle.started.v1" {
		t.Fatalf("matching handler hits=%d type=%q", hits, got.Type)
	}
	if otherHits != 0 {
		t.Fatalf("non-matching handler fired %d times", otherHits)
	}
}

func TestDispatch_MalformedRawSkipped(t *testing.T) {
	sub := NewSubscriber(nil, Config{}, discardLogger())
	hits := 0
	sub.On("#", func(_ context.Context, _ Envelope) error { hits++; return nil })

	// Must not panic and must not invoke handlers on undecodable payload.
	sub.dispatch(context.Background(), redis.XMessage{ID: "1-0", Values: map[string]any{"_raw": "{not-json"}})
	sub.dispatch(context.Background(), redis.XMessage{ID: "2-0", Values: map[string]any{"nope": "x"}})
	if hits != 0 {
		t.Fatalf("handler fired %d times on malformed/missing payload", hits)
	}
}

func TestDispatch_PayloadKeyFallback(t *testing.T) {
	sub := NewSubscriber(nil, Config{}, discardLogger())
	hits := 0
	sub.On("#", func(_ context.Context, _ Envelope) error { hits++; return nil })

	env, _ := NewEnvelope("/x", "bus.notify.v1", nil)
	raw, _ := json.Marshal(env)
	// Legacy "payload" key must still decode for mixed-deploy safety.
	sub.dispatch(context.Background(), redis.XMessage{ID: "1-0", Values: map[string]any{"payload": string(raw)}})
	if hits != 1 {
		t.Fatalf("payload fallback did not dispatch; hits=%d", hits)
	}
}

// A crashed consumer leaves an entry in the PEL forever (XReadGroup delivered
// it, but XAck never ran). reapStale must reclaim it via XAUTOCLAIM once it
// has been idle past ReapMinIdle, and run it through the exact same
// dispatch+XAck path as a normal read — proving the recovery path is
// additive and doesn't require any change to how live entries are handled.
func TestReapStale_ReclaimsOrphanedPELEntry(t *testing.T) {
	rdb, mr := newMiniredis(t)

	cfg := Config{Stream: "nbus:all", Group: "my-service", Consumer: "host-a"}
	ctx := context.Background()

	// Simulate the crashed consumer: create the group, publish one entry, and
	// read it via XReadGroup as "host-a" so it lands in the PEL — but never ACK it.
	if err := rdb.XGroupCreateMkStream(ctx, cfg.Stream, cfg.Group, "$").Err(); err != nil {
		t.Fatalf("XGroupCreateMkStream: %v", err)
	}
	env, _ := NewEnvelope("/op", "loom.lifecycle.started.v1", map[string]string{"reason": "orphan"})
	raw, _ := json.Marshal(env)
	if _, err := rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: cfg.Stream,
		Values: map[string]any{"_raw": string(raw)},
	}).Result(); err != nil {
		t.Fatalf("XAdd: %v", err)
	}
	if _, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: cfg.Group, Consumer: "host-a", Streams: []string{cfg.Stream, ">"}, Count: 10,
	}).Result(); err != nil {
		t.Fatalf("XReadGroup (crashed consumer's read): %v", err)
	}
	// host-a "crashes" here — no XAck. Confirm it's stranded in the PEL.
	pending, err := rdb.XPending(ctx, cfg.Stream, cfg.Group).Result()
	if err != nil || pending.Count != 1 {
		t.Fatalf("expected 1 pending entry after unacked read, got %+v err=%v", pending, err)
	}

	// Age the entry past ReapMinIdle so it becomes eligible for reclaim.
	// (miniredis's FastForward only decays TTLs; XAUTOCLAIM idle-time is
	// measured against the server's now-clock, so we must advance that.)
	mr.SetTime(time.Now().Add(ReapMinIdle + time.Second))

	// A redeployed replica comes up under a new consumer identity (hostname
	// changed) and runs the reaper.
	newSub := NewSubscriber(rdb, Config{Stream: cfg.Stream, Group: cfg.Group, Consumer: "host-b"}, discardLogger())
	hits := 0
	var gotType string
	newSub.On("#", func(_ context.Context, e Envelope) error {
		hits++
		gotType = e.Type
		return nil
	})
	newSub.reapStale(ctx, cfg.Stream)

	if hits != 1 {
		t.Fatalf("handler fired %d times, want 1 (reclaimed entry must dispatch)", hits)
	}
	if gotType != "loom.lifecycle.started.v1" {
		t.Errorf("dispatched envelope type = %q, want loom.lifecycle.started.v1", gotType)
	}
	// The reclaimed entry must be ACKed exactly like a normal read — PEL empty afterward.
	pendingAfter, err := rdb.XPending(ctx, cfg.Stream, cfg.Group).Result()
	if err != nil || pendingAfter.Count != 0 {
		t.Fatalf("expected PEL empty after reap, got %+v err=%v", pendingAfter, err)
	}
}

// reapStale must be a no-op (no false reclaims, no crash) when nothing is
// stale — the common case on every periodic tick during normal operation.
func TestReapStale_NoOpWhenNothingStale(t *testing.T) {
	rdb, _ := newMiniredis(t)
	cfg := Config{Stream: "nbus:all", Group: "my-service", Consumer: "host-a"}
	ctx := context.Background()
	if err := rdb.XGroupCreateMkStream(ctx, cfg.Stream, cfg.Group, "$").Err(); err != nil {
		t.Fatalf("XGroupCreateMkStream: %v", err)
	}

	sub := NewSubscriber(rdb, cfg, discardLogger())
	hits := 0
	sub.On("#", func(_ context.Context, _ Envelope) error { hits++; return nil })
	sub.reapStale(ctx, cfg.Stream) // empty PEL, empty group — must not panic or dispatch anything.
	if hits != 0 {
		t.Fatalf("handler fired %d times on empty PEL, want 0", hits)
	}
}

// A handler still mid-flight (delivered recently, well under ReapMinIdle)
// must NOT be reclaimed by a concurrent sweep — only genuinely stale entries
// are fair game. This guards the "purely additive" property: reapStale must
// never race a live in-flight message.
func TestReapStale_DoesNotReclaimFreshlyDeliveredEntry(t *testing.T) {
	rdb, _ := newMiniredis(t)
	cfg := Config{Stream: "nbus:all", Group: "my-service", Consumer: "host-a"}
	ctx := context.Background()
	if err := rdb.XGroupCreateMkStream(ctx, cfg.Stream, cfg.Group, "$").Err(); err != nil {
		t.Fatalf("XGroupCreateMkStream: %v", err)
	}
	env, _ := NewEnvelope("/op", "loom.lifecycle.started.v1", nil)
	raw, _ := json.Marshal(env)
	if err := rdb.XAdd(ctx, &redis.XAddArgs{Stream: cfg.Stream, Values: map[string]any{"_raw": string(raw)}}).Err(); err != nil {
		t.Fatalf("XAdd: %v", err)
	}
	if _, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group: cfg.Group, Consumer: "host-a", Streams: []string{cfg.Stream, ">"}, Count: 10,
	}).Result(); err != nil {
		t.Fatalf("XReadGroup: %v", err)
	}
	// No FastForward — entry is "freshly delivered", still well within an
	// in-flight handler's normal processing window.

	other := NewSubscriber(rdb, Config{Stream: cfg.Stream, Group: cfg.Group, Consumer: "host-b"}, discardLogger())
	hits := 0
	other.On("#", func(_ context.Context, _ Envelope) error { hits++; return nil })
	other.reapStale(ctx, cfg.Stream)

	if hits != 0 {
		t.Fatalf("handler fired %d times on a freshly-delivered (non-stale) entry, want 0", hits)
	}
	pending, err := rdb.XPending(ctx, cfg.Stream, cfg.Group).Result()
	if err != nil || pending.Count != 1 {
		t.Fatalf("expected the fresh entry to remain pending (not reclaimed), got %+v err=%v", pending, err)
	}
}

// TestSubscriber_ConsumerDefaultsToHostname verifies NewSubscriber fills in
// Consumer from the OS hostname when the caller leaves it blank, so replicas
// don't collide on an empty consumer identity by accident.
func TestSubscriber_ConsumerDefaultsToHostname(t *testing.T) {
	sub := NewSubscriber(nil, Config{Group: "g"}, discardLogger())
	if sub.cfg.Consumer == "" {
		t.Fatal("expected Consumer to default to hostname, got empty string")
	}
}
