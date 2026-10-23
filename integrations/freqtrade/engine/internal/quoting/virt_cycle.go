package quoting

import (
	"sync"
	"time"
)

// VirtCycleEngine tracks the relationship between trading volume (which
// regenerates Hyperliquid API rate-limit budget — docs say 1 request per 1 USD
// traded but observed exchange-side credit ratios run far lower) and API call
// burn rate. Exposes Headroom() in [-1, 1] indicating whether actual regen
// outpaces API-call burn.
//
// V30 tuning part 5: regenRate is EWMA of OBSERVED cap growth from the
// exchange (`RecordRegen` called when nRequestsCap ticks up). V29 session
// showed 4 833 cap credits for $48 594 of volume — a ~10 % effective ratio
// that the old 1:1 assumption on volumeRate silently masked, keeping
// headroom pegged at 1.0 despite a −1 994 req/session drain. If regenRate is
// seeded, Headroom uses it; otherwise it falls back to volumeRate (legacy
// path, still exposed for informational logging).
type VirtCycleEngine struct {
	halfLife     time.Duration
	cumVolumeUSD float64
	cumRegenReqs int     // cumulative observed cap growth
	volumeRate   float64 // EWMA $/min traded (informational)
	regenRate    float64 // EWMA req/min actually credited by exchange
	burnRate     float64 // EWMA API calls/min
	lastVolume   time.Time
	lastRegen    time.Time
	lastBurn     time.Time
	seeded       bool
	regenSeeded  bool
	mu           sync.Mutex
}

// NewVirtCycleEngine creates a new engine with the given EWMA half-life.
func NewVirtCycleEngine(halfLife time.Duration) *VirtCycleEngine {
	return &VirtCycleEngine{
		halfLife: halfLife,
	}
}

// RecordVolume updates the EWMA volume rate with a new fill of the given USD value.
// Seeds on first call.
func (v *VirtCycleEngine) RecordVolume(usd float64) {
	v.mu.Lock()
	defer v.mu.Unlock()

	now := time.Now()
	v.cumVolumeUSD += usd

	if !v.seeded {
		// Seed with the target-rate estimate: assume this fill repeats once per half-life.
		// This avoids a wild initial spike (raw USD is not $/min).
		v.volumeRate = usd / v.halfLife.Minutes()
		v.lastVolume = now
		v.seeded = true
		return
	}

	elapsed := now.Sub(v.lastVolume)
	alpha := v.ewmaAlpha(elapsed)

	// Convert fill to $/min rate contribution.
	var minuteRate float64
	if elapsed > 0 {
		minuteRate = usd / elapsed.Minutes()
	} else {
		minuteRate = usd
	}

	v.volumeRate = alpha*minuteRate + (1-alpha)*v.volumeRate
	v.lastVolume = now
}

// RecordAPICall is a convenience for RecordAPICalls(1).
func (v *VirtCycleEngine) RecordAPICall() {
	v.RecordAPICalls(1)
}

// RecordAPICalls updates the EWMA burn rate with n API calls.
func (v *VirtCycleEngine) RecordAPICalls(n int) {
	v.mu.Lock()
	defer v.mu.Unlock()

	now := time.Now()

	if v.lastBurn.IsZero() {
		// Seed with rate estimate: assume these calls repeat once per half-life.
		v.burnRate = float64(n) / v.halfLife.Minutes()
		v.lastBurn = now
		return
	}

	elapsed := now.Sub(v.lastBurn)
	alpha := v.ewmaAlpha(elapsed)

	var minuteRate float64
	if elapsed > 0 {
		minuteRate = float64(n) / elapsed.Minutes()
	} else {
		minuteRate = float64(n)
	}

	v.burnRate = alpha*minuteRate + (1-alpha)*v.burnRate
	v.lastBurn = now
}

// Headroom returns a value in [-1, 1] indicating rate-limit budget health.
// Positive means observed regen outpaces API call burn.
// Formula: clamp(-netBurn / burnRate, -1, 1) where netBurn = burnRate - regen.
// If RecordRegen has been called, uses EWMA regenRate (observed req/min
// credited by the exchange); otherwise falls back to volumeRate (legacy
// 1 USDC = 1 req assumption, still exposed for backward compatibility).
// Returns 0 if not seeded or burnRate <= 0.
func (v *VirtCycleEngine) Headroom() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()

	if !v.seeded || v.burnRate <= 0 {
		return 0
	}

	regen := v.volumeRate
	if v.regenSeeded {
		regen = v.regenRate
	}

	netBurn := v.burnRate - regen
	headroom := -netBurn / v.burnRate

	return clamp(headroom, -1, 1)
}

// BudgetPressure maps headroom to [0, 1]: (1 - Headroom()) / 2.
// High pressure (near 1) means API burn exceeds volume regen.
// Low pressure (near 0) means fills comfortably regen budget.
func (v *VirtCycleEngine) BudgetPressure() float64 {
	return (1.0 - v.Headroom()) / 2.0
}

// CumVolumeUSD returns the cumulative traded volume in USD.
func (v *VirtCycleEngine) CumVolumeUSD() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.cumVolumeUSD
}

// VolumeRate returns the EWMA volume rate in $/min.
func (v *VirtCycleEngine) VolumeRate() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.volumeRate
}

// BurnRate returns the EWMA API call burn rate in calls/min.
func (v *VirtCycleEngine) BurnRate() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.burnRate
}

// RegenRate returns the EWMA req/min actually credited by the exchange,
// based on observed nRequestsCap growth. 0 until RecordRegen has been
// called at least once.
func (v *VirtCycleEngine) RegenRate() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.regenRate
}

// CumRegenReqs returns the cumulative number of requests credited by
// the exchange (observed cap growth) since the engine was created.
func (v *VirtCycleEngine) CumRegenReqs() int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.cumRegenReqs
}

// RegenRatio returns the observed effective USD-to-request credit ratio
// (cumRegenReqs / cumVolumeUSD). Docs claim 1.0 (1 USDC = 1 req regen);
// V29 observed ~0.10. Returns 0 if either cum counter is zero.
func (v *VirtCycleEngine) RegenRatio() float64 {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.cumVolumeUSD <= 0 || v.cumRegenReqs <= 0 {
		return 0
	}
	return float64(v.cumRegenReqs) / v.cumVolumeUSD
}

// RecordRegen updates the EWMA regen rate with an observed cap growth
// of n requests. Should be called when the polled nRequestsCap field
// ticks upward (caller diffs successive polls). Caller must filter out
// zero or negative deltas.
func (v *VirtCycleEngine) RecordRegen(n int) {
	if n <= 0 {
		return
	}
	v.mu.Lock()
	defer v.mu.Unlock()

	now := time.Now()
	v.cumRegenReqs += n

	if !v.regenSeeded {
		// Seed: assume this delta recurs once per half-life.
		v.regenRate = float64(n) / v.halfLife.Minutes()
		v.lastRegen = now
		v.regenSeeded = true
		return
	}

	elapsed := now.Sub(v.lastRegen)
	alpha := v.ewmaAlpha(elapsed)

	var minuteRate float64
	if elapsed > 0 {
		minuteRate = float64(n) / elapsed.Minutes()
	} else {
		minuteRate = float64(n)
	}

	v.regenRate = alpha*minuteRate + (1-alpha)*v.regenRate
	v.lastRegen = now
}

// ewmaAlpha computes the EWMA smoothing factor: 1 - 2^(-elapsed/halfLife).
// Wraps the package-level ewmaAlpha so existing tests can call it as a method.
func (v *VirtCycleEngine) ewmaAlpha(elapsed time.Duration) float64 {
	return ewmaAlpha(elapsed, v.halfLife)
}
