package feed

import (
	"log/slog"
	"math"
	"strconv"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/types"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

const (
	maxMidMovePct           = 0.10
	outlierRecoveryRejects  = 3
	outlierRecoveryCooldown = 2 * time.Second
)

type outlierState struct {
	consecutiveRejects int
	lastAcceptedAt     time.Time
}

// GRPCFeed wraps the SDK gRPC stream to produce BookUpdate values.
type GRPCFeed struct {
	stream       *hyperliquid.GRPCStream
	channels     []chan types.BookUpdate
	mu           sync.Mutex
	prevMid      map[string]float64
	outlierState map[string]outlierState
}

// NewGRPCFeed creates a gRPC feed that fans out BookUpdates to all provided channels.
// endpoint is the QuickNode URL.
// coins is the list of assets to subscribe to.
func NewGRPCFeed(endpoint string, coins []string, channels ...chan types.BookUpdate) *GRPCFeed {
	// Create f first so the OnReconnect closure can capture it.
	f := &GRPCFeed{
		channels:     channels,
		prevMid:      make(map[string]float64),
		outlierState: make(map[string]outlierState),
	}

	grpcConfig := &hyperliquid.GRPCStreamConfig{
		Secure:       true,
		Reconnect:    true,
		MaxReconnect: 0, // unlimited
		OnError: func(err error) {
			slog.Error("gRPC stream error", "error", err)
		},
		OnClose: func() {
			slog.Warn("gRPC stream closed")
		},
		OnConnect: func() {
			slog.Info("gRPC stream connected")
		},
		OnReconnect: func(attempt int) {
			slog.Warn("gRPC stream reconnecting", "attempt", attempt)
			// Clear circuit breaker state so post-reconnect prices are not
			// rejected due to large gaps during the disconnect window.
			f.mu.Lock()
			f.prevMid = make(map[string]float64)
			f.outlierState = make(map[string]outlierState)
			f.mu.Unlock()
		},
	}

	stream := hyperliquid.NewGRPCStream(endpoint, grpcConfig)
	f.stream = stream

	for _, coin := range coins {
		c := coin // capture for closure
		stream.L2Book(c, func(data map[string]any) {
			f.handleL2Book(c, data)
		}, hyperliquid.L2BookNLevels(10))
	}

	return f
}

// Start begins the gRPC stream (non-blocking).
func (f *GRPCFeed) Start() error {
	return f.stream.Start()
}

// Stop shuts down the gRPC stream.
func (f *GRPCFeed) Stop() {
	f.stream.Stop()
}

func (f *GRPCFeed) handleL2Book(coin string, data map[string]any) {
	update, err := parseL2Book(coin, data)
	if err != nil {
		slog.Error("failed to parse L2Book", "coin", coin, "error", err)
		return
	}

	if update.BestBid == 0 || update.BestAsk == 0 {
		slog.Debug("skipping L2Book with zero bid/ask", "coin", coin)
		return
	}

	// Crossed book: bid >= ask means the book is invalid.
	if update.BestBid >= update.BestAsk {
		slog.Warn("skipping crossed book", "coin", coin, "bid", update.BestBid, "ask", update.BestAsk)
		return
	}

	// Circuit breaker: reject updates where mid moves >10% from the last
	// accepted mid, but recover automatically after repeated confirmations or
	// a short cooldown without an accepted baseline refresh.
	now := update.Timestamp
	var changePct float64
	var recoverAfterGap bool
	f.mu.Lock()
	prev, hasPrev := f.prevMid[coin]
	if hasPrev && prev > 0 {
		changePct = math.Abs(update.Mid/prev - 1.0)
		if changePct > maxMidMovePct {
			state := f.outlierState[coin]
			state.consecutiveRejects++
			age := now.Sub(state.lastAcceptedAt)
			if state.lastAcceptedAt.IsZero() {
				age = 0
			}
			if state.consecutiveRejects < outlierRecoveryRejects && age < outlierRecoveryCooldown {
				f.outlierState[coin] = state
				f.mu.Unlock()
				slog.Warn("skipping outlier book update (>10% mid change)",
					"coin", coin,
					"prev_mid", prev,
					"new_mid", update.Mid,
					"change_pct", changePct*100,
					"reject_count", state.consecutiveRejects,
				)
				return
			}
			recoverAfterGap = true
		}
	}
	f.prevMid[coin] = update.Mid
	f.outlierState[coin] = outlierState{
		lastAcceptedAt: now,
	}
	f.mu.Unlock()

	if recoverAfterGap {
		slog.Warn("accepting large-gap book update after recovery threshold",
			"coin", coin,
			"prev_mid", prev,
			"new_mid", update.Mid,
			"change_pct", changePct*100,
		)
	}

	slog.Debug("L2Book update",
		"coin", coin,
		"bid", update.BestBid,
		"ask", update.BestAsk,
		"mid", update.Mid,
	)

	for _, ch := range f.channels {
		select {
		case ch <- update:
		default:
			// Channel full: drain oldest then retry non-blocking.
			// Non-blocking on both sides avoids deadlock when multiple
			// gRPC callbacks (one per coin) race on the same channel.
			select {
			case <-ch:
			default:
			}
			select {
			case ch <- update:
			default:
				slog.Warn("book update channel full, dropping update", "coin", coin)
			}
		}
	}
}

// parseL2Book extracts full L2 depth (bids, asks) from the SDK callback data.
//
// The gRPC SDK streams L2Book data in several formats with cascading fallbacks:
//
//  1. Primary: "bids"/"asks" as [][]any — each level is []any{px_string, sz_string, count}
//  2. Fallback: "bids"/"asks" as []any wrapping []any levels
//  3. Fallback: "levels" as [[bids...], [asks...]] with map levels {px, sz, n}
//  4. Fallback: "data" wrapper with nested "bids"/"asks" as map levels
//
// BestBid, BestAsk, and Mid are derived from the parsed depth.
func parseL2Book(coin string, data map[string]any) (types.BookUpdate, error) {
	update := types.BookUpdate{
		Coin:      coin,
		Timestamp: time.Now(),
	}

	// Primary format: "bids"/"asks" as [][]any (gRPC SDK native format).
	if bids, ok := data["bids"].([][]any); ok {
		asks, _ := data["asks"].([][]any)
		update.Bids = parseSliceSliceLevels(bids)
		update.Asks = parseSliceSliceLevels(asks)
	}

	// Fallback: "bids"/"asks" as []any (in case SDK version differs).
	if len(update.Bids) == 0 && len(update.Asks) == 0 {
		if bidsRaw, ok := data["bids"].([]any); ok {
			asksRaw, _ := data["asks"].([]any)
			update.Bids = parseAnySliceLevels(bidsRaw)
			update.Asks = parseAnySliceLevels(asksRaw)
		}
	}

	// Fallback: "levels" format [[bids...], [asks...]] with map levels.
	if len(update.Bids) == 0 && len(update.Asks) == 0 {
		if levels, ok := data["levels"].([]any); ok && len(levels) >= 2 {
			bidMaps, _ := levels[0].([]any)
			askMaps, _ := levels[1].([]any)
			update.Bids = parseMapLevels(bidMaps)
			update.Asks = parseMapLevels(askMaps)
		}
	}

	// Fallback: "data" wrapper format.
	if len(update.Bids) == 0 && len(update.Asks) == 0 {
		if bookData, ok := data["data"].(map[string]any); ok {
			bidMaps, _ := bookData["bids"].([]any)
			askMaps, _ := bookData["asks"].([]any)
			update.Bids = parseMapLevels(bidMaps)
			update.Asks = parseMapLevels(askMaps)
		}
	}

	// Derive BestBid/BestAsk/Mid from parsed depth.
	if len(update.Bids) > 0 {
		update.BestBid = update.Bids[0].Price
	}
	if len(update.Asks) > 0 {
		update.BestAsk = update.Asks[0].Price
	}
	if update.BestBid > 0 && update.BestAsk > 0 {
		update.Mid = (update.BestBid + update.BestAsk) / 2.0
	}

	if len(update.Bids) > 0 && len(update.Asks) > 0 {
		bidSize, askSize := update.Bids[0].Size, update.Asks[0].Size
		if bidSize+askSize > 0 {
			update.Microprice = (update.BestBid*askSize + update.BestAsk*bidSize) / (bidSize + askSize)
		} else {
			update.Microprice = update.Mid
		}
	} else {
		update.Microprice = update.Mid
	}

	return update, nil
}

// parseSliceSliceLevels parses levels from [][]any where each level is
// []any{px_string, sz_string, count}.
func parseSliceSliceLevels(levels [][]any) []types.BookLevel {
	out := make([]types.BookLevel, 0, len(levels))
	for _, lev := range levels {
		if len(lev) < 1 {
			continue
		}
		px, ok := parseFloat(lev[0])
		if !ok || px <= 0 {
			continue
		}
		var sz float64
		if len(lev) >= 2 {
			sz, _ = parseFloat(lev[1])
		}
		out = append(out, types.BookLevel{Price: px, Size: sz})
	}
	return out
}

// parseAnySliceLevels parses levels from []any where each element is []any{px, sz, count}.
func parseAnySliceLevels(levels []any) []types.BookLevel {
	out := make([]types.BookLevel, 0, len(levels))
	for _, raw := range levels {
		lev, ok := raw.([]any)
		if !ok || len(lev) < 1 {
			continue
		}
		px, ok := parseFloat(lev[0])
		if !ok || px <= 0 {
			continue
		}
		var sz float64
		if len(lev) >= 2 {
			sz, _ = parseFloat(lev[1])
		}
		out = append(out, types.BookLevel{Price: px, Size: sz})
	}
	return out
}

// parseMapLevels parses levels from []any where each element is
// map[string]any{"px": "...", "sz": "...", "n": ...}.
func parseMapLevels(levels []any) []types.BookLevel {
	out := make([]types.BookLevel, 0, len(levels))
	for _, raw := range levels {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		px, ok := parseFloat(m["px"])
		if !ok || px <= 0 {
			continue
		}
		sz, _ := parseFloat(m["sz"])
		out = append(out, types.BookLevel{Price: px, Size: sz})
	}
	return out
}

// parseFloat extracts a float64 from string or numeric types.
func parseFloat(v any) (float64, bool) {
	switch val := v.(type) {
	case string:
		f, err := strconv.ParseFloat(val, 64)
		return f, err == nil
	case float64:
		return val, true
	case float32:
		return float64(val), true
	case int:
		return float64(val), true
	case int64:
		return float64(val), true
	case uint32:
		return float64(val), true
	default:
		return 0, false
	}
}
