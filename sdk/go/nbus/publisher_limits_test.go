package nbus

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/redis/go-redis/v9"
)

// blob returns a payload whose serialized envelope is at least n bytes.
func blob(n int) map[string]string {
	return map[string]string{"snapshot": strings.Repeat("x", n)}
}

func xlen(t *testing.T, rdb *redis.Client, stream string) int64 {
	t.Helper()
	n, err := rdb.XLen(context.Background(), stream).Result()
	if err != nil {
		t.Fatalf("XLEN %s: %v", stream, err)
	}
	return n
}

// deadLetters decodes the data of every event on nbus:bus.dead_letter.
func deadLetters(t *testing.T, rdb *redis.Client) []map[string]any {
	t.Helper()
	msgs, err := rdb.XRange(context.Background(), "nbus:bus.dead_letter", "-", "+").Result()
	if err != nil {
		t.Fatalf("XRANGE dead_letter: %v", err)
	}
	var out []map[string]any
	for _, m := range msgs {
		var env Envelope
		if err := json.Unmarshal([]byte(m.Values["_raw"].(string)), &env); err != nil {
			t.Fatalf("dead-letter _raw: %v", err)
		}
		var data map[string]any
		if err := env.Decode(&data); err != nil {
			t.Fatalf("dead-letter data: %v", err)
		}
		out = append(out, data)
	}
	return out
}

func TestPublishSync_OversizeIsRejectedByDefault(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer"}, discardLogger())

	// ~60 KB, the size that took a production Valkey down.
	err := pub.PublishSync(context.Background(), "big.snapshot.v1", blob(60_000))

	if !errors.Is(err, ErrPayloadTooLarge) {
		t.Fatalf("err = %v, want ErrPayloadTooLarge", err)
	}
	var tooLarge *PayloadTooLargeError
	if !errors.As(err, &tooLarge) || tooLarge.Channel != "big.snapshot.v1" || tooLarge.Size <= tooLarge.Limit {
		t.Fatalf("PayloadTooLargeError = %+v", tooLarge)
	}
	if n := xlen(t, rdb, "nbus:big.snapshot.v1"); n != 0 {
		t.Errorf("typed stream holds %d entries; the refused payload must not be stored", n)
	}
	// nbus:all holds only the dead-letter, never the oversize original.
	msgs, _ := rdb.XRange(context.Background(), fanoutStream, "-", "+").Result()
	for _, m := range msgs {
		if raw := m.Values["_raw"].(string); len(raw) > 4096 {
			t.Errorf("nbus:all holds a %d byte entry", len(raw))
		}
	}

	dls := deadLetters(t, rdb)
	if len(dls) != 1 {
		t.Fatalf("dead letters = %d, want 1", len(dls))
	}
	dl := dls[0]
	if dl["failure_reason"] != "payload_too_large" || dl["original_type"] != "big.snapshot.v1" || dl["original_source"] != "/producer" {
		t.Errorf("dead letter = %v", dl)
	}
	if got := int(dl["payload_bytes"].(float64)); got != tooLarge.Size {
		t.Errorf("payload_bytes = %d, want %d", got, tooLarge.Size)
	}
	if ex := dl["original_payload_excerpt"].(string); utf8.RuneCountInString(ex) > 500 {
		t.Errorf("excerpt is %d runes, schema allows 500", utf8.RuneCountInString(ex))
	}
}

func TestPublishSync_DeadLetterExcerptNeverSplitsUTF8(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer"}, discardLogger())

	// Two-byte runes: a byte-wise cut at 500 would land mid-rune.
	_ = pub.PublishSync(context.Background(), "big.snapshot.v1", map[string]string{"s": strings.Repeat("é", 20_000)})

	dls := deadLetters(t, rdb)
	if len(dls) != 1 {
		t.Fatalf("dead letters = %d, want 1", len(dls))
	}
	if ex := dls[0]["original_payload_excerpt"].(string); !utf8.ValidString(ex) || utf8.RuneCountInString(ex) != 500 {
		t.Errorf("excerpt valid=%v runes=%d", utf8.ValidString(ex), utf8.RuneCountInString(ex))
	}
}

func TestPublishSync_NotificationSizedPayloadIsUnaffected(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer"}, discardLogger())

	if err := pub.PublishSync(context.Background(), "bus.notify.v1", map[string]string{"id": "01J", "kind": "changed"}); err != nil {
		t.Fatal(err)
	}
	if n := xlen(t, rdb, "nbus:bus.notify.v1"); n != 1 {
		t.Errorf("typed stream = %d entries, want 1", n)
	}
	if n := xlen(t, rdb, fanoutStream); n != 1 {
		t.Errorf("fanout stream = %d entries, want 1", n)
	}
	if n := xlen(t, rdb, "nbus:bus.dead_letter"); n != 0 {
		t.Errorf("dead_letter = %d entries, want 0", n)
	}
}

func TestPublish_AsyncOversizeIsDeadLettered(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer"}, discardLogger())

	pub.Publish("big.snapshot.v1", blob(60_000))

	deadline := time.Now().Add(2 * time.Second)
	for xlen(t, rdb, "nbus:bus.dead_letter") == 0 {
		if time.Now().After(deadline) {
			t.Fatal("no dead letter after Publish of an oversize payload")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if n := xlen(t, rdb, "nbus:big.snapshot.v1"); n != 0 {
		t.Errorf("typed stream = %d entries, want 0", n)
	}
}

func TestPublishSync_ByteLimitOverrides(t *testing.T) {
	ctx := context.Background()
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{
		Source: "/producer",
		Limits: map[string]Limits{
			"tight.v1":     {MaxBytes: 300},     // stricter than the default
			"roomy.*":      {MaxBytes: 100_000}, // looser than the default
			"unbounded.v1": {MaxBytes: -1},
		},
	}, discardLogger())

	if err := pub.PublishSync(ctx, "tight.v1", blob(500)); !errors.Is(err, ErrPayloadTooLarge) {
		t.Errorf("tight.v1 500B: err = %v, want ErrPayloadTooLarge", err)
	}
	if err := pub.PublishSync(ctx, "tight.v1", blob(50)); err != nil {
		t.Errorf("tight.v1 50B: %v", err)
	}
	if err := pub.PublishSync(ctx, "roomy.snapshot", blob(40_000)); err != nil {
		t.Errorf("roomy.snapshot 40KB: %v", err)
	}
	if err := pub.PublishSync(ctx, "unbounded.v1", blob(200_000)); err != nil {
		t.Errorf("unbounded.v1 200KB: %v", err)
	}
	// Without an entry the default still applies.
	if err := pub.PublishSync(ctx, "other.v1", blob(40_000)); !errors.Is(err, ErrPayloadTooLarge) {
		t.Errorf("other.v1 40KB: err = %v, want ErrPayloadTooLarge", err)
	}
	// Per-call options win over Config.
	if err := pub.PublishSync(ctx, "other.v1", blob(40_000), WithMaxBytes(-1)); err != nil {
		t.Errorf("other.v1 with WithMaxBytes(-1): %v", err)
	}
	if err := pub.PublishSync(ctx, "roomy.snapshot", blob(500), WithMaxBytes(300)); !errors.Is(err, ErrPayloadTooLarge) {
		t.Errorf("roomy.snapshot with WithMaxBytes(300): err = %v, want ErrPayloadTooLarge", err)
	}
}

func TestPublishSync_WarnPolicyPublishesAnyway(t *testing.T) {
	var logs bytes.Buffer
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer", OversizePolicy: OversizeWarn},
		slog.New(slog.NewTextHandler(&logs, nil)))

	if err := pub.PublishSync(context.Background(), "big.snapshot.v1", blob(60_000)); err != nil {
		t.Fatalf("OversizeWarn must not fail the publish: %v", err)
	}
	if n := xlen(t, rdb, "nbus:big.snapshot.v1"); n != 1 {
		t.Errorf("typed stream = %d entries, want 1", n)
	}
	if n := xlen(t, rdb, "nbus:bus.dead_letter"); n != 0 {
		t.Errorf("dead_letter = %d entries, want 0 under OversizeWarn", n)
	}
	if !strings.Contains(logs.String(), "big.snapshot.v1") {
		t.Errorf("no warning naming the channel in log: %q", logs.String())
	}
}

// publishN publishes n small events on channel and returns the typed stream
// length afterwards.
func publishN(t *testing.T, rdb *redis.Client, pub *Publisher, channel string, n int, opts ...PublishOption) int64 {
	t.Helper()
	for i := 0; i < n; i++ {
		if err := pub.PublishSync(context.Background(), channel, map[string]int{"i": i}, opts...); err != nil {
			t.Fatal(err)
		}
	}
	return xlen(t, rdb, "nbus:"+channel)
}

func TestPublish_PerCallMaxLenTrimsTypedStream(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer"}, discardLogger())

	if got := publishN(t, rdb, pub, "capped.v1", 12, WithMaxLen(3)); got != 3 {
		t.Errorf("typed stream with WithMaxLen(3) = %d, want 3", got)
	}
	if got := publishN(t, rdb, pub, "uncapped.v1", 12); got != 12 {
		t.Errorf("typed stream with default cap = %d, want 12", got)
	}
	// The override is for the typed stream only.
	if got := xlen(t, rdb, fanoutStream); got != 24 {
		t.Errorf("fanout stream = %d, want 24", got)
	}
}

func TestLimits_MaxLenResolutionOrder(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{
		Source: "/producer",
		Limits: map[string]Limits{
			"#":                      {MaxLen: 25},
			"loom.#":                 {MaxLen: 20},
			"loom.lifecycle.*":       {MaxLen: 5},
			"loom.lifecycle.started": {MaxLen: 2},
			// MaxBytes only: MaxLen must fall through to the glob above.
			"loom.lifecycle.big": {MaxBytes: 100_000},
		},
	}, discardLogger())

	cases := []struct {
		channel string
		want    int64
	}{
		{"loom.lifecycle.started", 2}, // exact beats every glob
		{"loom.lifecycle.stopped", 5}, // longest matching glob beats loom.# and #
		{"loom.lifecycle.big", 5},     // zero MaxLen inherits from the next match
		{"loom.other.v1", 20},         // loom.# beats #
		{"misc.v1", 25},               // catch-all
	}
	for _, tc := range cases {
		if got := publishN(t, rdb, pub, tc.channel, 30); got != tc.want {
			t.Errorf("%s: typed stream = %d, want %d", tc.channel, got, tc.want)
		}
	}
	// A per-call option beats the configured entry.
	if got := publishN(t, rdb, pub, "loom.lifecycle.started", 30, WithMaxLen(7)); got != 7 {
		t.Errorf("WithMaxLen(7) over exact entry: %d, want 7", got)
	}
}

func TestConfig_FanoutMaxLen(t *testing.T) {
	rdb, _ := newMiniredis(t)
	pub := NewPublisher(rdb, Config{Source: "/producer", FanoutMaxLen: 4}, discardLogger())

	publishN(t, rdb, pub, "a.v1", 10)
	if got := xlen(t, rdb, fanoutStream); got != 4 {
		t.Errorf("fanout stream = %d, want 4", got)
	}
}

func TestNewPublisher_SnapshotsLimits(t *testing.T) {
	rdb, _ := newMiniredis(t)
	limits := map[string]Limits{"a.v1": {MaxLen: 3}}
	pub := NewPublisher(rdb, Config{Source: "/producer", Limits: limits}, discardLogger())

	limits["a.v1"] = Limits{MaxLen: 9}
	limits["b.v1"] = Limits{MaxLen: 1}

	if got := publishN(t, rdb, pub, "a.v1", 12); got != 3 {
		t.Errorf("a.v1 = %d, want 3 (later mutation of the caller's map must not apply)", got)
	}
	if got := publishN(t, rdb, pub, "b.v1", 12); got != 12 {
		t.Errorf("b.v1 = %d, want 12", got)
	}
}

func TestDeadLetterIsNotItselfGuardedAgainstRecursion(t *testing.T) {
	rdb, _ := newMiniredis(t)
	// A limit so low even the dead letter would exceed it if it went through
	// the guard; it must still be written, exactly once.
	pub := NewPublisher(rdb, Config{
		Source: "/producer",
		Limits: map[string]Limits{"#": {MaxBytes: 10}},
	}, discardLogger())

	err := pub.PublishSync(context.Background(), "x.v1", blob(100))
	if !errors.Is(err, ErrPayloadTooLarge) {
		t.Fatalf("err = %v", err)
	}
	if got := len(deadLetters(t, rdb)); got != 1 {
		t.Errorf("dead letters = %d, want exactly 1", got)
	}
}
