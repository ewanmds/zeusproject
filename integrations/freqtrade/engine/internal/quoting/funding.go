package quoting

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"time"
)

// FundingInfo abstracts the SDK info client for funding rate queries.
type FundingInfo interface {
	MetaAndAssetCtxs() (map[string]any, error)
	PredictedFundings() ([]any, error)
}

type fundingState struct {
	rate            float64   // predicted funding rate (e.g., 0.0001 = 1 bps)
	intervalHours   float64   // funding interval in hours
	nextFundingTime time.Time // next reset time
	lastPoll        time.Time
}

// FundingTracker polls the Hyperliquid exchange for per-asset funding rates and
// computes an urgency multiplier for the inventory skew factor near funding resets.
// When paying funding near a reset, the skew is amplified (faster exit).
// When receiving, it's reduced (hold position).
type FundingTracker struct {
	pollInterval     time.Duration
	urgencyScale     float64
	horizonMinutes   float64
	assets           []string
	intervalOverride map[string]float64 // per-asset funding interval override in hours
	info             FundingInfo
	rates            map[string]fundingState
	mu               sync.RWMutex
}

// NewFundingTracker creates a new FundingTracker.
func NewFundingTracker(
	info FundingInfo,
	assets []string,
	pollInterval time.Duration,
	urgencyScale float64,
	horizonMinutes float64,
	intervalOverrides map[string]float64,
) *FundingTracker {
	return &FundingTracker{
		pollInterval:     pollInterval,
		urgencyScale:     urgencyScale,
		horizonMinutes:   horizonMinutes,
		assets:           assets,
		intervalOverride: intervalOverrides,
		info:             info,
		rates:            make(map[string]fundingState),
	}
}

// Start begins the background polling goroutine. Blocks until ctx is cancelled.
func (f *FundingTracker) Start(ctx context.Context) {
	// Initial poll immediately.
	f.poll()

	ticker := time.NewTicker(f.pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			f.poll()
		}
	}
}

// poll fetches funding rates from the SDK. Tries PredictedFundings first,
// falls back to MetaAndAssetCtxs.
func (f *FundingTracker) poll() {
	if f.info == nil {
		return
	}

	// Recover from SDK type assertion panics (e.g., MetaAndAssetCtxs returns
	// []any but SDK casts to map[string]any).
	defer func() {
		if r := recover(); r != nil {
			slog.Warn("funding: poll recovered from panic",
				"error", fmt.Sprint(r), "stack", string(debug.Stack()))
		}
	}()

	now := time.Now()

	// Try PredictedFundings first (more direct).
	if predicted, err := f.info.PredictedFundings(); err == nil {
		f.parsePredictedFundings(predicted, now)
		f.logRates("predictedFundings")
		return
	}
	// Fallback to MetaAndAssetCtxs.
	if meta, err := f.info.MetaAndAssetCtxs(); err == nil {
		f.parseMetaAndAssetCtxs(meta, now)
		f.logRates("metaAndAssetCtxs")
		return
	}
	slog.Warn("funding: failed to poll funding rates from both endpoints")
}

// logRates logs the current funding rates after a successful poll.
func (f *FundingTracker) logRates(source string) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	for asset, state := range f.rates {
		slog.Debug("funding: polled rate",
			"source", source,
			"asset", asset,
			"rate_bps", state.rate*10000,
			"interval_h", state.intervalHours,
			"next_reset", state.nextFundingTime.Format(time.RFC3339),
		)
	}
}

// parsePredictedFundings parses the SDK PredictedFundings response.
// The response is an array of items. Each item may be:
// - [assetName, {predictedFunding: rate}]
// - [assetName, predictedFunding, rate] or similar nested structures
func (f *FundingTracker) parsePredictedFundings(data []any, now time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()

	assetSet := make(map[string]bool, len(f.assets))
	for _, a := range f.assets {
		assetSet[a] = true
		// Also match stripped name (e.g., "CL" for "xyz:CL")
		stripped := stripDexPrefix(a)
		if stripped != a {
			assetSet[stripped] = true
		}
	}

	for _, item := range data {
		// Each item is typically a 2-element array: [assetName, fundingData]
		arr, ok := item.([]any)
		if !ok || len(arr) < 2 {
			continue
		}
		name, ok := arr[0].(string)
		if !ok {
			continue
		}

		// Check if this asset is one we care about.
		matchedAsset := ""
		if assetSet[name] {
			matchedAsset = name
		} else {
			// Check full names (e.g., "xyz:CL")
			for _, a := range f.assets {
				if stripDexPrefix(a) == name {
					matchedAsset = a
					break
				}
			}
		}
		if matchedAsset == "" {
			continue
		}

		// Parse funding data from the second element.
		var rate float64
		switch fd := arr[1].(type) {
		case map[string]any:
			// {predictedFunding: rate} or {funding: rate}
			if v, ok := fd["predictedFunding"]; ok {
				rate, _ = toFloat64(v)
			} else if v, ok := fd["funding"]; ok {
				rate, _ = toFloat64(v)
			}
		default:
			rate, _ = toFloat64(fd)
		}

		interval := f.assetInterval(matchedAsset)
		existing := f.rates[matchedAsset]
		f.rates[matchedAsset] = fundingState{
			rate:            rate,
			intervalHours:   interval,
			nextFundingTime: computeNextFunding(now, interval, existing.nextFundingTime),
			lastPoll:        now,
		}
	}
}

// parseMetaAndAssetCtxs parses the SDK MetaAndAssetCtxs response.
// Returns {universe: [{name: "BTC", ...}], assetCtxs: [{funding: "0.0001", ...}]}.
// The universe and assetCtxs arrays are aligned by index.
func (f *FundingTracker) parseMetaAndAssetCtxs(data map[string]any, now time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()

	// Extract the two parallel arrays.
	var universe []any
	var assetCtxs []any

	// The response may have universe directly or nested under a "meta" key.
	if meta, ok := data["meta"].(map[string]any); ok {
		if u, ok := meta["universe"].([]any); ok {
			universe = u
		}
	}
	if u, ok := data["universe"].([]any); ok {
		universe = u
	}
	if ctx, ok := data["assetCtxs"].([]any); ok {
		assetCtxs = ctx
	}

	if universe == nil || assetCtxs == nil || len(universe) != len(assetCtxs) {
		return
	}

	assetSet := make(map[string]bool, len(f.assets))
	for _, a := range f.assets {
		assetSet[a] = true
		stripped := stripDexPrefix(a)
		if stripped != a {
			assetSet[stripped] = true
		}
	}

	for i, u := range universe {
		um, ok := u.(map[string]any)
		if !ok {
			continue
		}
		name, _ := um["name"].(string)
		if name == "" {
			continue
		}

		// Check if this asset is one we care about.
		matchedAsset := ""
		if assetSet[name] {
			matchedAsset = name
		} else {
			for _, a := range f.assets {
				if stripDexPrefix(a) == name {
					matchedAsset = a
					break
				}
			}
		}
		if matchedAsset == "" {
			continue
		}

		if i >= len(assetCtxs) {
			continue
		}
		ctx, ok := assetCtxs[i].(map[string]any)
		if !ok {
			continue
		}

		var rate float64
		if v, ok := ctx["funding"]; ok {
			rate, _ = toFloat64(v)
		}

		interval := f.assetInterval(matchedAsset)
		existing := f.rates[matchedAsset]
		f.rates[matchedAsset] = fundingState{
			rate:            rate,
			intervalHours:   interval,
			nextFundingTime: computeNextFunding(now, interval, existing.nextFundingTime),
			lastPoll:        now,
		}
	}
}

// UrgencyMultiplier returns the funding urgency multiplier for the given asset
// and position side. The multiplier is:
//
//	1.0 + urgencyScale * fundingCost * timeDecay
//
// where fundingCost = rate * sideSign (positive = paying, negative = receiving),
// and timeDecay ramps from 0 (far from funding) to 1 (at funding time).
//
// Returns 1.0 when: no position (""), no data, far from funding.
func (f *FundingTracker) UrgencyMultiplier(asset, positionSide string) float64 {
	if positionSide == "" {
		return 1.0
	}

	f.mu.RLock()
	state, ok := f.rates[asset]
	if !ok {
		// Try stripped name — API returns "CL" but config uses "xyz:CL".
		state, ok = f.rates[stripDexPrefix(asset)]
	}
	f.mu.RUnlock()
	if !ok {
		return 1.0
	}

	// sideSign: +1 for long (long pays positive funding), -1 for short
	var sideSign float64
	switch positionSide {
	case "long":
		sideSign = 1.0
	case "short":
		sideSign = -1.0
	default:
		return 1.0
	}

	fundingCost := state.rate * sideSign // positive = paying, negative = receiving

	timeToNext := time.Until(state.nextFundingTime).Minutes()
	if timeToNext < 0 {
		timeToNext = 0
	}
	timeDecay := math.Max(0, math.Min(1.0, 1.0-timeToNext/f.horizonMinutes))

	return 1.0 + f.urgencyScale*fundingCost*timeDecay
}

// assetInterval returns the funding interval in hours for the given asset.
// Checks per-asset override first, strips dex prefix ("xyz:CL" -> "CL"),
// defaults to 1.0 hour.
func (f *FundingTracker) assetInterval(asset string) float64 {
	if f.intervalOverride != nil {
		if v, ok := f.intervalOverride[asset]; ok && v > 0 {
			return v
		}
		// Try stripped name.
		stripped := stripDexPrefix(asset)
		if stripped != asset {
			if v, ok := f.intervalOverride[stripped]; ok && v > 0 {
				return v
			}
		}
	}
	return 1.0 // default: 1 hour (HIP-3 assets)
}

// computeNextFunding computes the next funding boundary from epoch.
// If existing is in the future, keeps it (avoids jitter from rounding).
func computeNextFunding(now time.Time, intervalHours float64, existing time.Time) time.Time {
	if !existing.IsZero() && existing.After(now) {
		return existing
	}
	intervalDur := time.Duration(intervalHours * float64(time.Hour))
	if intervalDur <= 0 {
		intervalDur = time.Hour
	}
	// Compute next boundary aligned to epoch.
	elapsed := now.Unix()
	intervalSec := int64(intervalDur.Seconds())
	if intervalSec <= 0 {
		intervalSec = 3600
	}
	nextSec := ((elapsed / intervalSec) + 1) * intervalSec
	return time.Unix(nextSec, 0).UTC()
}

// stripDexPrefix removes the "xyz:" prefix from HIP-3 asset names.
func stripDexPrefix(asset string) string {
	if idx := strings.Index(asset, ":"); idx >= 0 {
		return asset[idx+1:]
	}
	return asset
}

// toFloat64 converts various numeric types and strings to float64.
func toFloat64(v any) (float64, bool) {
	switch val := v.(type) {
	case float64:
		return val, true
	case float32:
		return float64(val), true
	case int:
		return float64(val), true
	case int64:
		return float64(val), true
	case string:
		f, err := strconv.ParseFloat(val, 64)
		return f, err == nil
	default:
		// Try fmt.Sprint as last resort for any numeric type.
		s := fmt.Sprint(v)
		f, err := strconv.ParseFloat(s, 64)
		return f, err == nil
	}
}
