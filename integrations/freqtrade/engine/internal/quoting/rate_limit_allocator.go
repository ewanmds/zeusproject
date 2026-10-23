package quoting

import (
	"math"
	"sync"
	"time"
)


// assetROI tracks per-asset ROI metrics for the rate limit allocator.
type assetROI struct {
	captureEWMA float64   // EWMA of capture bps per fill
	volumeEWMA  float64   // EWMA of volume USD per fill
	apiCalls    int       // API calls since last Evaluate
	lastFill    time.Time // timestamp of last RecordFill
	roiScore    float64   // computed by Evaluate: captureBps * volumeUSD / max(apiCalls, 1)
}

// RateLimitAllocator tracks per-asset ROI and allocates API budget (refresh
// frequency) to the most profitable assets. High-ROI assets get faster refresh
// (lower multiplier), low-ROI assets get slower refresh (higher multiplier).
type RateLimitAllocator struct {
	assets   []string
	perAsset map[string]*assetROI
	halfLife time.Duration
	minMult  float64 // fastest refresh multiplier (e.g., 0.7)
	maxMult  float64 // slowest refresh multiplier (e.g., 3.0)
	mu       sync.Mutex
}

// NewRateLimitAllocator creates a new allocator for the given assets.
func NewRateLimitAllocator(assets []string, halfLife time.Duration, minMult, maxMult float64) *RateLimitAllocator {
	pa := make(map[string]*assetROI, len(assets))
	for _, a := range assets {
		pa[a] = &assetROI{}
	}
	return &RateLimitAllocator{
		assets:   assets,
		perAsset: pa,
		halfLife: halfLife,
		minMult:  minMult,
		maxMult:  maxMult,
	}
}

// ewmaAlpha computes the EWMA smoothing factor: 1 - 2^(-elapsed/halfLife).
// Wraps the package-level ewmaAlpha so existing tests can call it as a method.
func (r *RateLimitAllocator) ewmaAlpha(elapsed time.Duration) float64 {
	return ewmaAlpha(elapsed, r.halfLife)
}

// RecordFill updates the EWMA capture and volume for the given asset.
func (r *RateLimitAllocator) RecordFill(asset string, captureBps, volumeUSD float64) {
	r.mu.Lock()
	defer r.mu.Unlock()

	a, ok := r.perAsset[asset]
	if !ok {
		return
	}

	now := time.Now()
	if a.lastFill.IsZero() {
		// First fill: seed the EWMAs directly.
		a.captureEWMA = captureBps
		a.volumeEWMA = volumeUSD
	} else {
		elapsed := now.Sub(a.lastFill)
		alpha := r.ewmaAlpha(elapsed)
		a.captureEWMA = alpha*captureBps + (1-alpha)*a.captureEWMA
		a.volumeEWMA = alpha*volumeUSD + (1-alpha)*a.volumeEWMA
	}
	a.lastFill = now
}

// RecordAPICall increments the API call counter for the given asset.
func (r *RateLimitAllocator) RecordAPICall(asset string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if a, ok := r.perAsset[asset]; ok {
		a.apiCalls++
	}
}

// Evaluate recomputes ROI scores per asset: captureBps * volumeUSD / max(apiCalls, 1).
// Resets apiCalls and fillCount after evaluation. No-op for single asset.
func (r *RateLimitAllocator) Evaluate() {
	r.mu.Lock()
	defer r.mu.Unlock()

	if len(r.assets) <= 1 {
		return
	}

	for _, a := range r.perAsset {
		calls := a.apiCalls
		if calls < 1 {
			calls = 1
		}
		a.roiScore = a.captureEWMA * a.volumeEWMA / float64(calls)
		a.apiCalls = 0
	}
}

// RefreshMultiplier returns the refresh interval multiplier for the given asset.
// High ROI -> low mult (faster refresh), low ROI -> high mult (slower refresh).
// Returns 1.0 for single asset, unknown asset, or before first Evaluate.
func (r *RateLimitAllocator) RefreshMultiplier(asset string) float64 {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Single asset: always 1.0.
	if len(r.assets) <= 1 {
		return 1.0
	}

	a, ok := r.perAsset[asset]
	if !ok {
		return 1.0
	}

	// Find min and max ROI scores across all assets.
	minROI := math.Inf(1)
	maxROI := math.Inf(-1)
	for _, pa := range r.perAsset {
		if pa.roiScore < minROI {
			minROI = pa.roiScore
		}
		if pa.roiScore > maxROI {
			maxROI = pa.roiScore
		}
	}

	// If all scores are equal (including all zero before first Evaluate), return 1.0.
	if maxROI == minROI {
		return 1.0
	}

	// Normalize ROI to [0, 1]: 0 = worst, 1 = best.
	norm := (a.roiScore - minROI) / (maxROI - minROI)

	// Map [0, 1] to [maxMult, minMult] (inverted: high ROI -> low mult).
	return r.maxMult + norm*(r.minMult-r.maxMult)
}

// AssetROIScores returns the current ROI scores per asset, for logging.
func (r *RateLimitAllocator) AssetROIScores() map[string]float64 {
	r.mu.Lock()
	defer r.mu.Unlock()

	scores := make(map[string]float64, len(r.perAsset))
	for asset, a := range r.perAsset {
		scores[asset] = a.roiScore
	}
	return scores
}
