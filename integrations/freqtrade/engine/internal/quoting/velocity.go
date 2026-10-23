package quoting

import (
	"math"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// TradeVelocityTracker measures market trade velocity using a dual-EWMA
// (fast/slow) approach. When the fast rate significantly exceeds the slow
// baseline, a volume burst is detected, signaling the quote manager to
// temporarily increase ladder levels while keeping total notional constant.
type TradeVelocityTracker struct {
	// Config
	fastHalfLife time.Duration
	slowHalfLife time.Duration
	threshold    float64 // fast/slow ratio to trigger burst
	minFloor     float64 // minimum slow rate (trades/sec) to prevent false triggers
	levelMult    int     // multiply base ladder levels by this during burst
	decaySec     float64 // seconds to linearly decay extra levels after deactivation

	// State
	fastRate           float64 // EWMA trades/sec (fast)
	slowRate           float64 // EWMA trades/sec (slow)
	fastNotional       float64 // EWMA notional/sec (fast)
	slowNotional       float64 // EWMA notional/sec (slow)
	fastSignedNotional float64 // EWMA signed notional/sec (fast)
	lastPrice          float64
	priceImpactBps     float64
	lastUpdate         time.Time // last trade timestamp
	lastDecayAt        time.Time // last time decayRatesLocked was applied (prevents double-decay)
	tradeCount         int       // total trades seen (for init)

	// Burst state
	burstActive      bool      // currently in burst mode
	burstDeactivated time.Time // when burst was last deactivated (for decay)

	mu sync.Mutex
}

// NewTradeVelocityTracker creates a new velocity tracker.
func NewTradeVelocityTracker(
	fastHalfLifeSec float64,
	slowHalfLifeSec float64,
	threshold float64,
	minFloor float64,
	levelMult int,
	decaySec float64,
) *TradeVelocityTracker {
	return &TradeVelocityTracker{
		fastHalfLife: time.Duration(fastHalfLifeSec * float64(time.Second)),
		slowHalfLife: time.Duration(slowHalfLifeSec * float64(time.Second)),
		threshold:    threshold,
		minFloor:     minFloor,
		levelMult:    levelMult,
		decaySec:     decaySec,
	}
}

// RecordTrade records a public market trade without metadata. It is kept for
// tests and compatibility; production uses RecordTradeUpdate.
func (t *TradeVelocityTracker) RecordTrade() {
	t.recordTrade(time.Now(), 1.0, 0, 0)
}

// RecordTradeUpdate records a public market trade with price, size, and side.
func (t *TradeVelocityTracker) RecordTradeUpdate(trade types.TradeUpdate) {
	now := trade.Timestamp
	if now.IsZero() {
		now = time.Now()
	}
	notional := math.Abs(trade.Price * trade.Size)
	signed := 0.0
	if trade.Crossed {
		switch trade.Side {
		case "buy":
			signed = notional
		case "sell":
			signed = -notional
		}
	}
	t.recordTrade(now, notional, signed, trade.Price)
}

func (t *TradeVelocityTracker) recordTrade(now time.Time, notional, signedNotional, price float64) {
	t.mu.Lock()
	defer t.mu.Unlock()

	t.tradeCount++

	if t.lastUpdate.IsZero() {
		// First trade: seed the rates at a neutral value.
		t.lastUpdate = now
		t.lastDecayAt = now
		t.fastRate = 1.0 // assume ~1 trade/sec as starting point
		t.slowRate = 1.0
		t.fastNotional = math.Max(notional, 1.0)
		t.slowNotional = t.fastNotional
		t.fastSignedNotional = signedNotional
		t.lastPrice = price
		return
	}

	elapsed := now.Sub(t.lastUpdate).Seconds()
	if elapsed <= 0 {
		elapsed = 0.001 // avoid division by zero for simultaneous trades
	}

	// Instantaneous rate for this inter-trade interval.
	instantRate := 1.0 / elapsed

	// EWMA update: alpha = 1 - 2^(-elapsed / halfLife)
	fastAlpha := 1.0 - math.Pow(2.0, -elapsed/t.fastHalfLife.Seconds())
	slowAlpha := 1.0 - math.Pow(2.0, -elapsed/t.slowHalfLife.Seconds())

	t.fastRate = fastAlpha*instantRate + (1.0-fastAlpha)*t.fastRate
	t.slowRate = slowAlpha*instantRate + (1.0-slowAlpha)*t.slowRate
	instantNotional := notional / elapsed
	instantSigned := signedNotional / elapsed
	t.fastNotional = fastAlpha*instantNotional + (1.0-fastAlpha)*t.fastNotional
	t.slowNotional = slowAlpha*instantNotional + (1.0-slowAlpha)*t.slowNotional
	t.fastSignedNotional = fastAlpha*instantSigned + (1.0-fastAlpha)*t.fastSignedNotional
	if price > 0 && t.lastPrice > 0 {
		t.priceImpactBps = (price - t.lastPrice) / t.lastPrice * 10000.0
	}
	if price > 0 {
		t.lastPrice = price
	}

	t.lastUpdate = now
	t.lastDecayAt = now // reset decay tracking on trade

	// Update burst state.
	t.updateBurstStateLocked(now)
}

// VelocitySnapshot holds a point-in-time snapshot of the velocity tracker state.
// All values are from a single lock acquisition, ensuring consistency.
type VelocitySnapshot struct {
	RateRatio          float64
	NotionalRatio      float64
	SignedNotionalFast float64
	PriceImpactBps     float64
	IsBurst            bool
	EffectiveLevels    int
}

// Snapshot returns a consistent point-in-time snapshot of rate ratio, burst state,
// and effective level count. This is the primary read method -- avoids the
// double-decay bug that would occur from calling RateRatio(), IsBurst(), and
// EffectiveLevels() separately (each would re-decay the EWMAs).
func (t *TradeVelocityTracker) Snapshot(baseLevels int) VelocitySnapshot {
	t.mu.Lock()
	defer t.mu.Unlock()

	t.decayRatesLocked()

	tradeRatio := t.rateRatioLocked()
	notionalRatio := t.notionalRatioLocked()
	ratio := math.Max(tradeRatio, notionalRatio)
	now := time.Now()
	t.updateBurstStateLocked(now)

	burstLevels := baseLevels * t.levelMult
	effectiveLevels := baseLevels

	if t.burstActive {
		effectiveLevels = burstLevels
	} else if !t.burstDeactivated.IsZero() && t.decaySec > 0 {
		elapsed := now.Sub(t.burstDeactivated).Seconds()
		if elapsed < t.decaySec {
			frac := elapsed / t.decaySec
			effectiveFloat := float64(burstLevels) - frac*float64(burstLevels-baseLevels)
			effectiveLevels = int(effectiveFloat)
			if effectiveLevels < baseLevels {
				effectiveLevels = baseLevels
			}
		} else {
			t.burstDeactivated = time.Time{}
		}
	}

	return VelocitySnapshot{
		RateRatio:          ratio,
		NotionalRatio:      notionalRatio,
		SignedNotionalFast: t.fastSignedNotional,
		PriceImpactBps:     t.priceImpactBps,
		IsBurst:            t.burstActive,
		EffectiveLevels:    effectiveLevels,
	}
}

// updateBurstStateLocked updates burstActive and burstDeactivated based on current ratio.
// Caller must hold mu.
func (t *TradeVelocityTracker) updateBurstStateLocked(now time.Time) {
	wasActive := t.burstActive
	ratio := math.Max(t.rateRatioLocked(), t.notionalRatioLocked())
	t.burstActive = ratio >= t.threshold

	if wasActive && !t.burstActive && t.burstDeactivated.IsZero() {
		t.burstDeactivated = now
	}
	if !wasActive && t.burstActive {
		t.burstDeactivated = time.Time{}
	}
}

// rateRatioLocked computes fast/slow ratio. Caller must hold mu.
func (t *TradeVelocityTracker) rateRatioLocked() float64 {
	if t.tradeCount < 5 {
		return 1.0 // not enough data
	}
	denom := math.Max(t.slowRate, t.minFloor)
	if denom <= 0 {
		return 1.0
	}
	return t.fastRate / denom
}

func (t *TradeVelocityTracker) notionalRatioLocked() float64 {
	if t.tradeCount < 5 {
		return 1.0
	}
	denom := math.Max(t.slowNotional, t.minFloor)
	if denom <= 0 {
		return 1.0
	}
	return t.fastNotional / denom
}

// decayRatesLocked decays both EWMAs based on elapsed time since last decay,
// preventing stale high rates from persisting after trading stops.
// Uses lastDecayAt (not lastUpdate) to prevent double-application on
// consecutive reads.
// Caller must hold mu.
func (t *TradeVelocityTracker) decayRatesLocked() {
	if t.lastDecayAt.IsZero() {
		return
	}
	now := time.Now()
	elapsed := now.Sub(t.lastDecayAt).Seconds()
	if elapsed <= 0.1 {
		return // recent update, no need to decay
	}

	// Decay toward zero using the respective half-lives.
	fastDecay := math.Pow(2.0, -elapsed/t.fastHalfLife.Seconds())
	slowDecay := math.Pow(2.0, -elapsed/t.slowHalfLife.Seconds())
	t.fastRate *= fastDecay
	t.slowRate *= slowDecay
	t.fastNotional *= fastDecay
	t.slowNotional *= slowDecay
	t.fastSignedNotional *= fastDecay
	t.priceImpactBps *= fastDecay
	t.lastDecayAt = now // mark decay as applied to prevent double-application
}

// RateRatio returns the current fast/slow EWMA ratio.
// Prefer Snapshot() for getting multiple values consistently.
func (t *TradeVelocityTracker) RateRatio() float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.decayRatesLocked()
	return t.rateRatioLocked()
}

// IsBurst returns true if the velocity ratio exceeds the threshold.
// Prefer Snapshot() for getting multiple values consistently.
func (t *TradeVelocityTracker) IsBurst() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.decayRatesLocked()
	t.updateBurstStateLocked(time.Now())
	return t.burstActive
}

// EffectiveLevels returns the burst-adjusted ladder level count.
// Prefer Snapshot() for getting multiple values consistently.
func (t *TradeVelocityTracker) EffectiveLevels(baseLevels int) int {
	return t.Snapshot(baseLevels).EffectiveLevels
}
