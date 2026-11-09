package nbus

import (
	"errors"
	"fmt"
	"sort"
)

// Redis streams are capped by entry COUNT only, and an entry's cost is its
// size. 10,000 entries of 60 KB is 600 MB for ONE channel, and a handful of
// such channels is enough to push a Redis-backed bus over (a production
// Valkey died at 2.5 GB of streams this way). Limits therefore bound both
// axes: MaxLen bounds how many entries a typed stream keeps, MaxBytes bounds
// how large one entry may be before it is refused at publish time.
//
// Payloads on the bus are NOTIFICATIONS (ids plus the minimal facts a
// consumer needs to decide whether to fetch), not documents.
const (
	// defaultMaxPayloadBytes bounds the serialized envelope. It is a
	// notification-sized ceiling: it leaves room for normal events, and trips
	// on a producer that starts publishing full snapshots.
	defaultMaxPayloadBytes = 16 * 1024

	deadLetterChannel = "bus.dead_letter"
	// deadLetterMaxLen matches the cap the shell SDK and redis-mirror apply
	// to nbus:bus.dead_letter.
	deadLetterMaxLen = 10_000
	// deadLetterExcerptRunes matches the schema's original_payload_excerpt
	// maxLength.
	deadLetterExcerptRunes = 500
)

// Limits bounds what one channel may put into Redis. The zero value of each
// field means "inherit": from a less specific entry in Config.Limits, then
// from the SDK default.
type Limits struct {
	// MaxLen is the approximate MAXLEN of the typed stream nbus:<channel>.
	// 0 inherits (default 10,000). Values below 0 are ignored.
	MaxLen int64

	// MaxBytes is the largest serialized envelope, in bytes, the channel may
	// publish. 0 inherits (default 16 KiB). A negative value disables the
	// check for the channel.
	MaxBytes int
}

// OversizePolicy selects what the Publisher does with an envelope larger than
// its byte limit.
type OversizePolicy int

const (
	// OversizeReject (the zero value) refuses the publish: nothing is written
	// to nbus:<channel> or nbus:all, a bus.dead_letter event with
	// failure_reason "payload_too_large" is written instead, and the publish
	// returns a *PayloadTooLargeError.
	OversizeReject OversizePolicy = iota

	// OversizeWarn logs a warning and publishes the envelope anyway.
	OversizeWarn
)

// ErrPayloadTooLarge is matched (errors.Is) by the error a publish returns
// when an envelope exceeds its byte limit under OversizeReject.
var ErrPayloadTooLarge = errors.New("nbus: payload exceeds byte limit")

// PayloadTooLargeError reports an envelope refused for exceeding its byte
// limit. It satisfies errors.Is(err, ErrPayloadTooLarge).
type PayloadTooLargeError struct {
	Channel string
	// Size is the serialized envelope size in bytes.
	Size int
	// Limit is the byte limit that applied to Channel.
	Limit int
}

func (e *PayloadTooLargeError) Error() string {
	return fmt.Sprintf("nbus: payload for %q is %d bytes, over the %d byte limit", e.Channel, e.Size, e.Limit)
}

func (e *PayloadTooLargeError) Unwrap() error { return ErrPayloadTooLarge }

// PublishOption overrides the resolved Limits for a single publish call.
type PublishOption func(*Limits)

// WithMaxLen overrides the typed stream's approximate MAXLEN for one publish.
// n <= 0 leaves the configured value in place.
func WithMaxLen(n int64) PublishOption {
	return func(l *Limits) {
		if n > 0 {
			l.MaxLen = n
		}
	}
}

// WithMaxBytes overrides the byte limit for one publish. 0 leaves the
// configured value in place; a negative n disables the check for this call.
func WithMaxBytes(n int) PublishOption {
	return func(l *Limits) {
		if n != 0 {
			l.MaxBytes = n
		}
	}
}

// sortedLimitPatterns returns the keys of limits, longest first (ties broken
// lexicographically) so resolution is deterministic: among several matching
// globs the longest, i.e. most specific, pattern wins.
func sortedLimitPatterns(limits map[string]Limits) []string {
	patterns := make([]string, 0, len(limits))
	for k := range limits {
		patterns = append(patterns, k)
	}
	sort.Slice(patterns, func(i, j int) bool {
		if len(patterns[i]) != len(patterns[j]) {
			return len(patterns[i]) > len(patterns[j])
		}
		return patterns[i] < patterns[j]
	})
	return patterns
}

// resolveLimits returns the effective Limits for channel. Each field is
// resolved independently: per-call options first, then the exact channel
// entry, then matching glob entries from most to least specific, then the SDK
// defaults. A field an entry leaves at zero falls through to the next.
func (p *Publisher) resolveLimits(channel string, opts []PublishOption) Limits {
	var out Limits
	for _, opt := range opts {
		if opt != nil {
			opt(&out)
		}
	}
	fill := func(l Limits) {
		if out.MaxLen == 0 && l.MaxLen > 0 {
			out.MaxLen = l.MaxLen
		}
		if out.MaxBytes == 0 {
			out.MaxBytes = l.MaxBytes
		}
	}
	if l, ok := p.limits[channel]; ok {
		fill(l)
	}
	for _, pat := range p.limitPatterns {
		if pat != channel && matchesPattern(pat, channel) {
			fill(p.limits[pat])
		}
	}
	if out.MaxLen == 0 {
		out.MaxLen = typedStreamMaxLen
	}
	if out.MaxBytes == 0 {
		out.MaxBytes = defaultMaxPayloadBytes
	}
	return out
}

// excerpt returns the first n runes of s, never splitting a UTF-8 sequence.
func excerpt(s string, n int) string {
	count := 0
	for i := range s {
		if count == n {
			return s[:i]
		}
		count++
	}
	return s
}
