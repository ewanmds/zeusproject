package quoting

import (
	"math"
	"sync"
	"time"
)

// FillBurstTracker detects rapid same-side fill bursts and produces
// spread widening multipliers and size reduction factors.
// When N or more fills on the same side occur within windowSec,
// the tracker activates with a spread multiplier and size factor
// that decay exponentially with the configured half-life.
type FillBurstTracker struct {
	windowSec    float64
	threshold    int
	spreadMult   float64 // spread multiplier when burst active (e.g. 2.0)
	sizeFactor   float64 // size scaling when burst active (e.g. 0.3)
	decayHalfSec float64 // half-life for burst decay

	buyFills  []time.Time // recent buy fill timestamps
	sellFills []time.Time // recent sell fill timestamps

	burstBuyAt  time.Time // when buy burst was last detected
	burstSellAt time.Time // when sell burst was last detected

	mu sync.Mutex
}

func NewFillBurstTracker(windowSec float64, threshold int, spreadMult, sizeFactor, decayHalfSec float64) *FillBurstTracker {
	return &FillBurstTracker{
		windowSec:    windowSec,
		threshold:    threshold,
		spreadMult:   spreadMult,
		sizeFactor:   sizeFactor,
		decayHalfSec: decayHalfSec,
	}
}

// RecordFill records a fill and checks for burst conditions.
func (t *FillBurstTracker) RecordFill(side string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	now := time.Now()
	cutoff := now.Add(-time.Duration(t.windowSec * float64(time.Second)))

	switch side {
	case "buy":
		// Prune old fills
		t.buyFills = pruneOld(t.buyFills, cutoff)
		t.buyFills = append(t.buyFills, now)
		if len(t.buyFills) >= t.threshold {
			t.burstBuyAt = now
		}
	case "sell":
		t.sellFills = pruneOld(t.sellFills, cutoff)
		t.sellFills = append(t.sellFills, now)
		if len(t.sellFills) >= t.threshold {
			t.burstSellAt = now
		}
	}
}

// SpreadMultiplier returns per-side spread widening multipliers.
// Returns 1.0 when no burst is active.
func (t *FillBurstTracker) SpreadMultiplier() (buyMult, sellMult float64) {
	t.mu.Lock()
	defer t.mu.Unlock()

	buyMult = t.decayedMult(t.burstBuyAt)
	sellMult = t.decayedMult(t.burstSellAt)
	return
}

// SizeFactor returns per-side size scaling factors.
// Returns 1.0 when no burst is active. Returns sizeFactor (e.g. 0.3) at peak.
func (t *FillBurstTracker) SizeFactor() (buySizeFactor, sellSizeFactor float64) {
	t.mu.Lock()
	defer t.mu.Unlock()

	buySizeFactor = t.decayedSizeFactor(t.burstBuyAt)
	sellSizeFactor = t.decayedSizeFactor(t.burstSellAt)
	return
}

func (t *FillBurstTracker) decayedMult(burstAt time.Time) float64 {
	if burstAt.IsZero() || t.decayHalfSec <= 0 {
		return 1.0
	}
	elapsed := time.Since(burstAt).Seconds()
	decay := math.Pow(2.0, -elapsed/t.decayHalfSec)
	// Linearly interpolate: 1.0 at full decay, spreadMult at peak
	mult := 1.0 + (t.spreadMult-1.0)*decay
	if mult < 1.0 {
		mult = 1.0
	}
	return mult
}

func (t *FillBurstTracker) decayedSizeFactor(burstAt time.Time) float64 {
	if burstAt.IsZero() || t.decayHalfSec <= 0 {
		return 1.0
	}
	elapsed := time.Since(burstAt).Seconds()
	decay := math.Pow(2.0, -elapsed/t.decayHalfSec)
	// Linearly interpolate: 1.0 at full decay, sizeFactor at peak
	factor := 1.0 + (t.sizeFactor-1.0)*decay
	if factor < t.sizeFactor {
		factor = t.sizeFactor
	}
	if factor > 1.0 {
		factor = 1.0
	}
	return factor
}

func pruneOld(fills []time.Time, cutoff time.Time) []time.Time {
	i := 0
	for i < len(fills) && fills[i].Before(cutoff) {
		i++
	}
	return fills[i:]
}
