package quoting

import (
	"math"
	"strings"
	"sync"
	"time"
)

// sideTracker is a single-side EWMA fill rate tracker with notional multiplier logic.
type sideTracker struct {
	halfLife    time.Duration
	target      float64 // target $/min
	baseNot     float64 // base notional for clamping
	minNot      float64 // floor notional
	maxNot      float64 // ceiling notional
	rate        float64 // EWMA-smoothed $/min
	lastUpdate  time.Time
	initialized bool
}

// recordFill records a fill into this side tracker.
func (s *sideTracker) recordFill(filledNotional float64, now time.Time) {
	if !s.initialized {
		// Seed at target rate so first cycle quotes at 1.0x multiplier.
		s.rate = s.target
		s.lastUpdate = now
		s.initialized = true
		return
	}

	elapsed := now.Sub(s.lastUpdate)
	alpha := ewmaAlpha(elapsed, s.halfLife)
	var minuteRate float64
	if elapsed > 0 {
		minuteRate = filledNotional / elapsed.Minutes()
	} else {
		minuteRate = filledNotional // instantaneous
	}
	s.rate = alpha*minuteRate + (1-alpha)*s.rate
	s.lastUpdate = now
}

// notionalMultiplier returns the ratio to scale base notional by.
// target / decayed_rate, clamped to [min/base, max/base].
// Returns 1.0 when not initialized.
func (s *sideTracker) notionalMultiplier() float64 {
	if !s.initialized || s.baseNot <= 0 || s.halfLife <= 0 {
		return 1.0
	}

	elapsed := time.Since(s.lastUpdate)
	decayFactor := math.Pow(0.5, elapsed.Seconds()/s.halfLife.Seconds())
	decayedRate := s.target + (s.rate-s.target)*decayFactor

	if decayedRate <= 0 {
		return s.maxNot / s.baseNot
	}

	mult := s.target / decayedRate
	minMult := s.minNot / s.baseNot
	maxMult := s.maxNot / s.baseNot
	if mult < minMult {
		return minMult
	}
	if mult > maxMult {
		return maxMult
	}
	return mult
}

// FillRateTracker maintains EWMA-smoothed fill rates ($/min) and computes
// notional multipliers to scale quote sizes toward a target fill rate.
// Supports both global (backward-compatible) and per-side (buy/sell) tracking.
type FillRateTracker struct {
	buySide  *sideTracker
	sellSide *sideTracker
	global   *sideTracker
	mu       sync.Mutex
}

// NewFillRateTracker creates a new tracker with global and per-side EWMA trackers.
// Per-side targets are half the global target (fills split across buy/sell).
func NewFillRateTracker(halfLife time.Duration, target, baseNotional, minNotional, maxNotional float64) *FillRateTracker {
	perSideTarget := target / 2.0
	return &FillRateTracker{
		buySide: &sideTracker{
			halfLife: halfLife,
			target:   perSideTarget,
			baseNot:  baseNotional,
			minNot:   minNotional,
			maxNot:   maxNotional,
		},
		sellSide: &sideTracker{
			halfLife: halfLife,
			target:   perSideTarget,
			baseNot:  baseNotional,
			minNot:   minNotional,
			maxNot:   maxNotional,
		},
		global: &sideTracker{
			halfLife: halfLife,
			target:   target,
			baseNot:  baseNotional,
			minNot:   minNotional,
			maxNot:   maxNotional,
		},
	}
}

// RecordFill records a fill to the global tracker only (backward compat).
func (f *FillRateTracker) RecordFill(filledNotional float64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.global.recordFill(filledNotional, time.Now())
}

// RecordFillSide records a fill to both the per-side tracker and the global tracker.
// Side values: "buy"/"B"/"bid" -> buySide, "sell"/"A"/"ask" -> sellSide.
func (f *FillRateTracker) RecordFillSide(side string, filledNotional float64) {
	f.mu.Lock()
	defer f.mu.Unlock()

	now := time.Now()
	f.global.recordFill(filledNotional, now)

	s := normalizeSide(side)
	switch s {
	case "buy":
		f.buySide.recordFill(filledNotional, now)
	case "sell":
		f.sellSide.recordFill(filledNotional, now)
	}
}

// NotionalMultiplier returns the global multiplier (backward compat).
func (f *FillRateTracker) NotionalMultiplier() float64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.global.notionalMultiplier()
}

// NotionalMultiplierSide returns the per-side multiplier.
// Returns 1.0 if the side has no data (not yet initialized).
func (f *FillRateTracker) NotionalMultiplierSide(side string) float64 {
	f.mu.Lock()
	defer f.mu.Unlock()

	s := normalizeSide(side)
	switch s {
	case "buy":
		return f.buySide.notionalMultiplier()
	case "sell":
		return f.sellSide.notionalMultiplier()
	default:
		return 1.0
	}
}

// ewmaAlpha computes the EWMA smoothing factor: 1 - 2^(-elapsed/halfLife).
// Exported as a method on FillRateTracker for backward compat with existing tests.
func (f *FillRateTracker) ewmaAlpha(elapsed time.Duration) float64 {
	return ewmaAlpha(elapsed, f.global.halfLife)
}

// ewmaAlpha computes the EWMA smoothing factor: 1 - 2^(-elapsed/halfLife).
func ewmaAlpha(elapsed, halfLife time.Duration) float64 {
	if halfLife <= 0 {
		return 1.0
	}
	return 1.0 - math.Pow(2.0, -elapsed.Seconds()/halfLife.Seconds())
}

// normalizeSide maps various side representations to "buy" or "sell".
func normalizeSide(side string) string {
	lower := strings.ToLower(side)
	switch lower {
	case "buy", "b", "bid":
		return "buy"
	case "sell", "a", "ask":
		return "sell"
	default:
		return lower
	}
}
