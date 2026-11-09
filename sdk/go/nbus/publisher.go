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
// Both caps are defaults: Config.Limits / WithMaxLen override the typed
// stream's cap per channel and Config.FanoutMaxLen overrides the fanout cap.
// MAXLEN bounds entry COUNT only, so envelopes are also bounded in bytes —
// see Limits.MaxBytes and OversizePolicy.
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

	// limits is a private copy of cfg.Limits and limitPatterns its keys in
	// resolution order, both fixed at NewPublisher so a caller mutating its
	// map afterwards cannot race with in-flight publishes.
	limits        map[string]Limits
	limitPatterns []string
}

// NewPublisher creates a Publisher. The caller owns the redis client's
// lifetime (Close it themselves when done). If logger is nil, a discard
// logger is used.
func NewPublisher(rdb *redis.Client, cfg Config, logger *slog.Logger) *Publisher {
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	limits := make(map[string]Limits, len(cfg.Limits))
	for k, v := range cfg.Limits {
		limits[k] = v
	}
	return &Publisher{
		rdb:           rdb,
		cfg:           cfg,
		logger:        logger,
		limits:        limits,
		limitPatterns: sortedLimitPatterns(limits),
	}
}

// Publish wraps data in a nervous-bus envelope and writes it to both
// nbus:<channel> and nbus:all, asynchronously. Never blocks the caller —
// errors are logged and swallowed, matching the bus's fire-and-forget
// contract (a slow or unavailable Redis must never block a producer's hot
// path). A nil *Publisher is a silent no-op, so callers can wire in a
// Publisher optionally without nil-checking at every call site.
//
// An envelope over its byte limit is refused (see OversizePolicy). Because
// Publish never returns an error, that shows up as a logged warning plus a
// bus.dead_letter event; use PublishSync to receive the error.
func (p *Publisher) Publish(channel string, data any, opts ...PublishOption) {
	if p == nil {
		return
	}
	go func() {
		if err := p.publish(context.Background(), channel, data, opts); err != nil {
			p.logger.Warn("nbus: publish failed", "channel", channel, "err", err)
		}
	}()
}

// PublishSync is like Publish but blocks until the write completes or ctx is
// cancelled, returning the first error encountered. Use this when the caller
// must confirm delivery (e.g. before shutdown, or in tests). A nil *Publisher
// is a silent no-op. An envelope refused for size returns a
// *PayloadTooLargeError (errors.Is(err, ErrPayloadTooLarge)).
func (p *Publisher) PublishSync(ctx context.Context, channel string, data any, opts ...PublishOption) error {
	if p == nil {
		return nil
	}
	return p.publish(ctx, channel, data, opts)
}

// publish is the shared implementation for Publish and PublishSync.
func (p *Publisher) publish(ctx context.Context, channel string, data any, opts []PublishOption) error {
	env, err := NewEnvelope(p.cfg.Source, channel, data)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(env)
	if err != nil {
		return err
	}

	lim := p.resolveLimits(channel, opts)
	if lim.MaxBytes > 0 && len(raw) > lim.MaxBytes {
		sizeErr := &PayloadTooLargeError{Channel: channel, Size: len(raw), Limit: lim.MaxBytes}
		if p.cfg.OversizePolicy == OversizeWarn {
			p.logger.Warn("nbus: payload over byte limit, publishing anyway",
				"channel", channel, "bytes", sizeErr.Size, "limit", sizeErr.Limit)
		} else {
			p.deadLetterOversize(ctx, env, string(raw), sizeErr)
			return sizeErr
		}
	}

	return p.write(ctx, env, string(raw), lim.MaxLen)
}

// write XADDs one envelope to nbus:<channel> (capped at typedMaxLen) and to
// the nbus:all fanout stream. It applies no size policy; callers do.
func (p *Publisher) write(ctx context.Context, env Envelope, raw string, typedMaxLen int64) error {
	values := map[string]any{
		"_raw":      raw,
		"type":      env.Type,
		"source":    env.Source,
		"timestamp": env.Time,
		"event_id":  env.ID,
	}

	typedStream := "nbus:" + env.Type
	if err := p.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: typedStream,
		MaxLen: typedMaxLen,
		Approx: true,
		Values: values,
	}).Err(); err != nil {
		// The typed stream is a convenience for narrow consumers; a failure
		// here must not stop the write to the canonical fanout stream below,
		// so it's logged rather than returned.
		p.logger.Warn("nbus: XAdd to typed stream failed", "stream", typedStream, "err", err)
	}

	fanoutMaxLen := p.cfg.FanoutMaxLen
	if fanoutMaxLen <= 0 {
		fanoutMaxLen = fanoutStreamMaxLen
	}
	if err := p.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: fanoutStream,
		MaxLen: fanoutMaxLen,
		Approx: true,
		Values: values,
	}).Err(); err != nil {
		return err
	}

	return nil
}

// deadLetterData is the data of a bus.dead_letter event
// (schemas/bus.dead_letter.v1.json). PayloadBytes and LimitBytes are extra
// fields the schema does not forbid; they make the event actionable without
// the refused payload.
type deadLetterData struct {
	FailureReason          string `json:"failure_reason"`
	OriginalType           string `json:"original_type"`
	OriginalSource         string `json:"original_source"`
	OriginalPayloadExcerpt string `json:"original_payload_excerpt"`
	PayloadBytes           int    `json:"payload_bytes"`
	LimitBytes             int    `json:"limit_bytes"`
}

// deadLetterOversize records a refused envelope on bus.dead_letter. It writes
// through write, never publish, so it cannot recurse into the size guard (the
// excerpt keeps it small regardless). Best-effort: a failure is logged and the
// caller still returns the size error.
func (p *Publisher) deadLetterOversize(ctx context.Context, orig Envelope, raw string, sizeErr *PayloadTooLargeError) {
	dl, err := NewEnvelope(p.cfg.Source, deadLetterChannel, deadLetterData{
		FailureReason:          "payload_too_large",
		OriginalType:           orig.Type,
		OriginalSource:         orig.Source,
		OriginalPayloadExcerpt: excerpt(raw, deadLetterExcerptRunes),
		PayloadBytes:           sizeErr.Size,
		LimitBytes:             sizeErr.Limit,
	})
	if err == nil {
		var dlRaw []byte
		if dlRaw, err = json.Marshal(dl); err == nil {
			err = p.write(ctx, dl, string(dlRaw), deadLetterMaxLen)
		}
	}
	if err != nil {
		p.logger.Warn("nbus: dead-letter write for oversize payload failed",
			"channel", sizeErr.Channel, "err", err)
	}
}
