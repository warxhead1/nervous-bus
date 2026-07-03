package nbus

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/redis/go-redis/v9"
)

// Wire format (canonical nervous-bus convention, matches internal/nbus in
// tachyonac-engine and the shell SDK's `nervous publish`):
//   - nbus:<channel>  maxlen ~10000  — typed stream for consumers filtering by channel
//   - nbus:all        maxlen ~50000  — fanout stream for consumers reading everything
//
// Each stream entry stores the full envelope JSON under field "_raw", plus
// helper fields type/source/timestamp/event_id so tooling (e.g. the
// nervous-bus CLI, dashboards) can filter/sort without decoding "_raw" first.
const (
	typedStreamMaxLen  = 10_000
	fanoutStream       = "nbus:all"
	fanoutStreamMaxLen = 50_000
)

// Publisher publishes CloudEvents-lite envelopes to the canonical nervous-bus
// Redis Streams. The zero value is not usable; construct with NewPublisher.
type Publisher struct {
	rdb    *redis.Client
	cfg    Config
	logger *slog.Logger
}

// NewPublisher creates a Publisher. The caller owns the redis client's
// lifetime (Close it themselves when done). If logger is nil, a discard
// logger is used.
func NewPublisher(rdb *redis.Client, cfg Config, logger *slog.Logger) *Publisher {
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	return &Publisher{rdb: rdb, cfg: cfg, logger: logger}
}

// Publish wraps data in a nervous-bus envelope and writes it to both
// nbus:<channel> and nbus:all, asynchronously. Never blocks the caller —
// errors are logged and swallowed, matching the bus's fire-and-forget
// contract (a slow or unavailable Redis must never block a producer's hot
// path). A nil *Publisher is a silent no-op, so callers can wire in a
// Publisher optionally without nil-checking at every call site.
func (p *Publisher) Publish(channel string, data any) {
	if p == nil {
		return
	}
	go func() {
		if err := p.publish(context.Background(), channel, data); err != nil {
			p.logger.Warn("nbus: publish failed", "channel", channel, "err", err)
		}
	}()
}

// PublishSync is like Publish but blocks until the write completes or ctx is
// cancelled, returning the first error encountered. Use this when the caller
// must confirm delivery (e.g. before shutdown, or in tests). A nil *Publisher
// is a silent no-op.
func (p *Publisher) PublishSync(ctx context.Context, channel string, data any) error {
	if p == nil {
		return nil
	}
	return p.publish(ctx, channel, data)
}

// publish is the shared implementation for Publish and PublishSync.
func (p *Publisher) publish(ctx context.Context, channel string, data any) error {
	env, err := NewEnvelope(p.cfg.Source, channel, data)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(env)
	if err != nil {
		return err
	}

	values := map[string]any{
		"_raw":      string(raw),
		"type":      env.Type,
		"source":    env.Source,
		"timestamp": env.Time,
		"event_id":  env.ID,
	}

	typedStream := "nbus:" + channel
	if err := p.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: typedStream,
		MaxLen: typedStreamMaxLen,
		Approx: true,
		Values: values,
	}).Err(); err != nil {
		// The typed stream is a convenience for narrow consumers; a failure
		// here must not stop the write to the canonical fanout stream below,
		// so it's logged rather than returned.
		p.logger.Warn("nbus: XAdd to typed stream failed", "stream", typedStream, "err", err)
	}

	if err := p.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: fanoutStream,
		MaxLen: fanoutStreamMaxLen,
		Approx: true,
		Values: values,
	}).Err(); err != nil {
		return err
	}

	return nil
}
