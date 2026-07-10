package nbus

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// Handler processes a decoded nervous-bus envelope. Return an error to have
// it logged (delivery is still ACKed either way — handlers own their own
// retry/dead-letter strategy; nbus provides at-least-once delivery, not
// automatic redelivery-on-error).
type Handler func(ctx context.Context, env Envelope) error

// Subscriber consumes events from a nervous-bus Redis Stream using a
// consumer group, so multiple replicas can share the work while each event
// is still delivered at-least-once. Construct with NewSubscriber, register
// handlers with On, then call Run.
type Subscriber struct {
	rdb      *redis.Client
	cfg      Config
	handlers map[string][]Handler // channel pattern → handlers
	logger   *slog.Logger
}

// PEL (pending-entries-list) recovery tuning. This mirrors the XAUTOCLAIM
// reaper pattern proven in tachyonac-engine's internal/nbus (reapStale): a
// message is only eligible for reclaim once it has sat unacked for
// ReapMinIdle — long enough that a normal in-flight handler wouldn't trigger
// a false reclaim — and the sweep runs on a fixed cadence so a crashed
// consumer's PEL entries drain on their own instead of leaking forever.
const (
	// ReapMinIdle is how long an entry must sit unacked in the PEL before a
	// different consumer is allowed to reclaim it via XAUTOCLAIM.
	ReapMinIdle = 2 * time.Minute
	// ReapInterval is how often the periodic sweep runs inside Run's loop.
	ReapInterval = 30 * time.Second
	// ReapBatchSize bounds how many stale entries a single XAUTOCLAIM call
	// reclaims, so one sweep can't monopolize the loop if the PEL is large.
	// 100 matches Redis's own XAUTOCLAIM default COUNT.
	ReapBatchSize = 100
)

// NewSubscriber creates a Subscriber. The caller owns the redis client's
// lifetime. Consumer defaults to os.Hostname() when cfg.Consumer is empty.
// If logger is nil, a discard logger is used. The caller must call Run to
// start consuming.
func NewSubscriber(rdb *redis.Client, cfg Config, logger *slog.Logger) *Subscriber {
	if cfg.Consumer == "" {
		if host, err := os.Hostname(); err == nil {
			cfg.Consumer = host
		}
	}
	if logger == nil {
		logger = slog.New(slog.DiscardHandler)
	}
	return &Subscriber{
		rdb:      rdb,
		cfg:      cfg,
		handlers: make(map[string][]Handler),
		logger:   logger,
	}
}

// On registers a handler for events matching the channel pattern. Patterns
// support "*" (exactly one dot-delimited segment) and "#" (any suffix,
// including zero segments), matching the nervous-bus routing convention used
// across every SDK in this repo.
func (s *Subscriber) On(pattern string, h Handler) {
	s.handlers[pattern] = append(s.handlers[pattern], h)
}

// Run starts the consumer-group loop. It blocks until ctx is cancelled.
// It creates the consumer group on the configured stream (default nbus:all)
// if it doesn't already exist, reclaims anything left over in the PEL from a
// prior crash, then reads new entries via XREADGROUP, dispatching each to
// matching handlers and XACKing it. A periodic XAUTOCLAIM sweep (every
// ReapInterval) recovers entries orphaned by consumers that die mid-flight.
func (s *Subscriber) Run(ctx context.Context) {
	stream := s.cfg.streamOrDefault()

	if err := s.ensureGroup(ctx, stream); err != nil {
		s.logger.Error("nbus: failed to ensure consumer group",
			"stream", stream, "group", s.cfg.Group, "err", err)
	}

	s.logger.Info("nbus: subscriber running",
		"stream", stream,
		"group", s.cfg.Group,
		"consumer", s.cfg.Consumer,
	)

	// Reclaim anything orphaned by a prior crash (process died between
	// XReadGroup and XAck, or the consumer identity changed across a
	// redeploy) before reading fresh entries. This is strictly additive
	// recovery: reclaimed entries flow through the exact same dispatch+XAck
	// path as normally-read ones, so at-least-once semantics for the live
	// read path are unchanged.
	s.reapStale(ctx, stream)
	lastReap := time.Now()

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// Periodic sweep for entries stranded by consumers that crashed (or
		// were rescheduled under a new identity) since this loop started.
		if time.Since(lastReap) >= ReapInterval {
			lastReap = time.Now()
			s.reapStale(ctx, stream)
		}

		entries, err := s.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    s.cfg.Group,
			Consumer: s.cfg.Consumer,
			Streams:  []string{stream, ">"},
			Count:    10,
			Block:    time.Second,
		}).Result()
		if err != nil {
			if errors.Is(err, redis.Nil) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				continue
			}
			s.logger.Warn("nbus: XReadGroup error", "err", err)
			time.Sleep(time.Second)
			continue
		}

		for _, str := range entries {
			for _, msg := range str.Messages {
				s.processMessage(ctx, stream, msg)
			}
		}
	}
}

// ensureGroup makes sure the configured consumer group exists on stream,
// creating both the stream and group (via XGROUP CREATE MKSTREAM) only when
// needed. It first checks for the group with XINFO GROUPS so the common
// steady-state case (group already exists, e.g. every process restart) never
// issues a create call at all. If the group turns out to be missing, it
// creates it; a BUSYGROUP error from that create (another replica won the
// race between our existence check and our create) is expected under
// concurrent startup and is swallowed. Any other error — including a
// genuinely missing permission or a broken connection — is returned so the
// caller can log or act on it instead of it being silently discarded.
func (s *Subscriber) ensureGroup(ctx context.Context, stream string) error {
	groups, err := s.rdb.XInfoGroups(ctx, stream).Result()
	if err == nil {
		for _, g := range groups {
			if g.Name == s.cfg.Group {
				return nil
			}
		}
	}
	// err != nil here typically means the stream itself doesn't exist yet
	// ("ERR no such key"); either way (stream missing, or stream present but
	// our group isn't in it) we fall through and attempt to create it.
	if err := s.rdb.XGroupCreateMkStream(ctx, stream, s.cfg.Group, "$").Err(); err != nil {
		if strings.Contains(err.Error(), "BUSYGROUP") {
			return nil
		}
		return err
	}
	return nil
}

// processMessage dispatches one entry and acknowledges it. This is the
// single path used both for freshly-read entries (XReadGroup) and for
// entries reclaimed from a stale PEL (XAUTOCLAIM) — identical dispatch+XAck
// semantics either way, unconditional on handler error.
func (s *Subscriber) processMessage(ctx context.Context, stream string, msg redis.XMessage) {
	s.dispatch(ctx, msg)
	// Acknowledge after dispatch regardless of handler errors. Handlers are
	// responsible for their own error recovery; nbus's delivery contract is
	// at-least-once, not automatic retry.
	_ = s.rdb.XAck(ctx, stream, s.cfg.Group, msg.ID).Err()
}

// reapStale reclaims PEL entries that have been idle longer than ReapMinIdle
// via XAUTOCLAIM and runs them through the normal dispatch+XAck path. Idle
// time is measured from each entry's last delivery, so entries genuinely
// in-flight in a live handler are never touched — only entries whose
// original consumer never came back to ACK them (crash, redeploy with a new
// identity, etc). Errors are logged and swallowed; this is best-effort
// recovery and must never block or crash the read loop.
func (s *Subscriber) reapStale(ctx context.Context, stream string) {
	messages, _, err := s.rdb.XAutoClaim(ctx, &redis.XAutoClaimArgs{
		Stream:   stream,
		Group:    s.cfg.Group,
		Consumer: s.cfg.Consumer,
		MinIdle:  ReapMinIdle,
		Start:    "0",
		Count:    ReapBatchSize,
	}).Result()
	if err != nil {
		if errors.Is(err, redis.Nil) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return
		}
		s.logger.Warn("nbus: XAUTOCLAIM reap failed", "stream", stream, "err", err)
		return
	}
	if len(messages) == 0 {
		return
	}
	s.logger.Info("nbus: reclaimed stale PEL entries", "stream", stream, "count", len(messages))
	for _, msg := range messages {
		s.processMessage(ctx, stream, msg)
	}
}

// dispatch decodes one raw stream message into an Envelope and invokes every
// handler whose registered pattern matches the envelope's Type. Malformed or
// undecodable entries are logged (or silently skipped, for the legacy
// fallback case) and never panic the read loop.
func (s *Subscriber) dispatch(ctx context.Context, msg redis.XMessage) {
	// The canonical bus stores the full envelope JSON under the "_raw" field.
	payloadRaw, ok := msg.Values["_raw"].(string)
	if !ok {
		// Graceful fallback: some legacy producers wrote a bare "payload"
		// key instead of "_raw". Still attempt to decode so a mixed
		// deployment doesn't silently drop messages.
		payloadRaw, ok = msg.Values["payload"].(string)
		if !ok {
			return
		}
	}
	var env Envelope
	if err := json.Unmarshal([]byte(payloadRaw), &env); err != nil {
		s.logger.Warn("nbus: envelope unmarshal failed", "id", msg.ID, "err", err)
		return
	}

	for pattern, handlers := range s.handlers {
		if !matchesPattern(pattern, env.Type) {
			continue
		}
		for _, h := range handlers {
			if err := h(ctx, env); err != nil {
				s.logger.Warn("nbus: handler error",
					"channel", env.Type,
					"pattern", pattern,
					"err", err,
				)
			}
		}
	}
}

// matchesPattern matches a channel name against a glob pattern. "*" matches
// exactly one dot-delimited segment; "#" matches any suffix (including zero
// segments). This is the same routing grammar used by the shell and Rust
// SDKs in this repo.
func matchesPattern(pattern, channel string) bool {
	if pattern == channel {
		return true
	}
	pp := strings.Split(pattern, ".")
	cp := strings.Split(channel, ".")
	return matchSegments(pp, cp)
}

func matchSegments(pp, cp []string) bool {
	for len(pp) > 0 {
		switch pp[0] {
		case "#":
			return true
		case "*":
			if len(cp) == 0 {
				return false
			}
			pp, cp = pp[1:], cp[1:]
		default:
			if len(cp) == 0 || pp[0] != cp[0] {
				return false
			}
			pp, cp = pp[1:], cp[1:]
		}
	}
	return len(cp) == 0
}
