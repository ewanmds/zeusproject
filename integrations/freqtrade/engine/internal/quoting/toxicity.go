package quoting

import (
	"math"
	"sync"
	"time"
)

// defaultIdleDecayHalfLives is the default number of half-lives to wait before
// starting idle decay toward baseline. Used when no config value is provided.
const defaultIdleDecayHalfLives = 4.0

// SideToxicityTracker maintains independent EWMA markout trackers for buy
// and sell sides. Produces per-side spread multipliers and size scaling factors.
//
// After idleDecayHalfLives × halfLife with no fills, the EWMA decays toward
// baselineBps using the same exponential half-life. This prevents the frozen-
// EWMA stickiness problem on sparse assets (e.g. BRENT with 6 fills/session)
// while preserving short-term toxic fill memory.
type SideToxicityTracker struct {
	halfLife           time.Duration
	maxWiden           float64
	baselineBps        float64
	idleDecayHalfLives float64
	warmupFills        int
	sizeExponent       float64

	buyEWMA      float64
	buyUpdated   time.Time
	buyInit      bool
	buyFillCount int

	sellEWMA      float64
	sellUpdated   time.Time
	sellInit      bool
	sellFillCount int

	mu sync.Mutex
}

// NewSideToxicityTracker creates a new per-side toxicity tracker.
// idleDecayHalfLives controls the grace period before idle decay starts
// (in multiples of halfLife). Use 0 for the default (4.0). Use a negative
// value to disable idle decay entirely (A3 legacy behavior).
// warmupFills sets the minimum number of markout recordings per side before
// toxicity output is trusted; 0 or negative means no gating (backward compatible).
// sizeExponent controls the power-law curve for SizeFactor: 1.0 = linear (legacy),
// >1.0 = convex (gentle at mild toxicity, aggressive at extreme). If <= 0, defaults to 1.0.
func NewSideToxicityTracker(halfLife time.Duration, maxWiden, baselineBps, idleDecayHalfLives float64, warmupFills int, sizeExponent float64) *SideToxicityTracker {
	if idleDecayHalfLives == 0 {
		idleDecayHalfLives = defaultIdleDecayHalfLives
	}
	if sizeExponent <= 0 {
		sizeExponent = 1.0
	}
	return &SideToxicityTracker{
		halfLife:           halfLife,
		maxWiden:           maxWiden,
		baselineBps:        baselineBps,
		idleDecayHalfLives: idleDecayHalfLives,
		warmupFills:        warmupFills,
		sizeExponent:       sizeExponent,
	}
}

// RecordMarkout updates the specified side's EWMA with a markout value.
// Positive = profitable, negative = adversely selected.
func (s *SideToxicityTracker) RecordMarkout(side string, markoutBps float64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now()
	switch normalizeSide(side) {
	case "buy":
		s.buyFillCount++
		if !s.buyInit {
			s.buyEWMA = markoutBps
			s.buyUpdated = now
			s.buyInit = true
			return
		}
		alpha := 1.0 - math.Pow(2.0, -now.Sub(s.buyUpdated).Seconds()/s.halfLife.Seconds())
		s.buyEWMA = alpha*markoutBps + (1-alpha)*s.buyEWMA
		s.buyUpdated = now
	case "sell":
		s.sellFillCount++
		if !s.sellInit {
			s.sellEWMA = markoutBps
			s.sellUpdated = now
			s.sellInit = true
			return
		}
		alpha := 1.0 - math.Pow(2.0, -now.Sub(s.sellUpdated).Seconds()/s.halfLife.Seconds())
		s.sellEWMA = alpha*markoutBps + (1-alpha)*s.sellEWMA
		s.sellUpdated = now
	}
}

// RecordMarkoutWeighted updates the specified side's EWMA with a markout value,
// weighted by fill size relative to a reference notional. Larger fills have
// proportionally more impact on the EWMA.
func (s *SideToxicityTracker) RecordMarkoutWeighted(side string, markoutBps, fillNotional, referenceNotional float64) {
	if referenceNotional <= 0 || fillNotional <= 0 {
		s.RecordMarkout(side, markoutBps)
		return
	}
	// Scale alpha by size ratio: larger fills get more weight
	sizeWeight := fillNotional / referenceNotional
	if sizeWeight > 3.0 {
		sizeWeight = 3.0 // cap to prevent single huge fill from dominating
	}
	if sizeWeight < 0.1 {
		sizeWeight = 0.1 // floor to prevent tiny fills from being ignored
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now()
	switch normalizeSide(side) {
	case "buy":
		s.buyFillCount++
		if !s.buyInit {
			s.buyEWMA = markoutBps
			s.buyUpdated = now
			s.buyInit = true
			return
		}
		baseAlpha := 1.0 - math.Pow(2.0, -now.Sub(s.buyUpdated).Seconds()/s.halfLife.Seconds())
		alpha := math.Min(baseAlpha*sizeWeight, 0.95) // cap alpha at 0.95 to prevent full override
		s.buyEWMA = alpha*markoutBps + (1-alpha)*s.buyEWMA
		s.buyUpdated = now
	case "sell":
		s.sellFillCount++
		if !s.sellInit {
			s.sellEWMA = markoutBps
			s.sellUpdated = now
			s.sellInit = true
			return
		}
		baseAlpha := 1.0 - math.Pow(2.0, -now.Sub(s.sellUpdated).Seconds()/s.halfLife.Seconds())
		alpha := math.Min(baseAlpha*sizeWeight, 0.95)
		s.sellEWMA = alpha*markoutBps + (1-alpha)*s.sellEWMA
		s.sellUpdated = now
	}
}

// SpreadMultiplier returns a spread multiplier in [1.0, maxWiden] for the given side.
// When the side's EWMA >= baseline, returns 1.0 (no widening).
// During warmup (fill count < warmupFills), returns 1.0 unconditionally.
func (s *SideToxicityTracker) SpreadMultiplier(side string) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()

	ewma, init := s.sideEWMA(side)
	if !init || s.baselineBps <= 0 {
		return 1.0
	}
	if !s.sideFilledEnough(side) {
		return 1.0
	}
	if ewma >= s.baselineBps {
		return 1.0
	}
	mult := 1.0 + (s.baselineBps-ewma)/s.baselineBps*(s.maxWiden-1.0)
	if mult < 1.0 {
		mult = 1.0
	}
	if mult > s.maxWiden {
		mult = s.maxWiden
	}
	return mult
}

// SizeFactor returns a size scaling factor in [maxReduce, 1.0] for the given side.
// When the side's EWMA is below threshold, reduces size using a power-law curve
// controlled by sizeExponent: 1.0 = linear (legacy), >1.0 = convex (gentle at
// mild toxicity, aggressive at extreme). During warmup, returns 1.0 unconditionally.
func (s *SideToxicityTracker) SizeFactor(side string, threshold, maxReduce float64) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()

	ewma, init := s.sideEWMA(side)
	// threshold is expected to be negative (e.g. -0.75 bps). When zero or positive,
	// size reduction is effectively disabled — EWMA can never be below a non-negative
	// threshold in any meaningful adverse-selection scenario.
	if !init || threshold >= 0 {
		return 1.0
	}
	if !s.sideFilledEnough(side) {
		return 1.0
	}
	if ewma >= threshold {
		return 1.0
	}
	// Power-law scaling: t measures how far past threshold, normalized to [0, 1].
	// At threshold → t=0 → factor=1.0; at 2*threshold → t=1.0 → factor=maxReduce.
	// sizeExponent=1.0 gives a linear curve from 1.0 (at threshold) to maxReduce (at 2*threshold).
	// Note: differs from pre-refactor formula which reached 0.0 at 2*threshold before clamping.
	t := (threshold - ewma) / math.Abs(threshold)
	if t > 1.0 {
		t = 1.0
	}
	factor := 1.0 - (1.0-maxReduce)*math.Pow(t, s.sizeExponent)
	if factor < maxReduce {
		factor = maxReduce
	}
	if factor > 1.0 {
		factor = 1.0
	}
	return factor
}

// WorstMarkoutEWMA returns the minimum (worst) EWMA across both sides,
// including idle decay. Used for refresh-timing and other callers where
// fading back to baseline during idle periods is desirable.
// Sides that haven't accumulated enough fills use baselineBps instead of their EWMA.
func (s *SideToxicityTracker) WorstMarkoutEWMA() float64 {
	s.mu.Lock()
	defer s.mu.Unlock()

	buyVal := s.baselineBps // default to baseline if not initialized
	if s.buyInit && s.sideFilledEnough("buy") {
		buyVal = s.decayedEWMA(s.buyEWMA, s.buyUpdated, s.buyInit)
	}
	sellVal := s.baselineBps
	if s.sellInit && s.sideFilledEnough("sell") {
		sellVal = s.decayedEWMA(s.sellEWMA, s.sellUpdated, s.sellInit)
	}
	if buyVal < sellVal {
		return buyVal
	}
	return sellVal
}

// WorstMarkoutEWMARaw returns the minimum (worst) EWMA across both sides
// WITHOUT idle decay. Used by the runtime optimizer for spread/notional
// decisions — idle decay toward baseline would starve the optimizer of
// toxicity signal during sustained adverse-flow windows where our quotes
// stop getting filled because they are being adversely selected.
// Sides that haven't accumulated enough fills use baselineBps instead.
func (s *SideToxicityTracker) WorstMarkoutEWMARaw() float64 {
	s.mu.Lock()
	defer s.mu.Unlock()

	buyVal := s.baselineBps
	if s.buyInit && s.sideFilledEnough("buy") {
		buyVal = s.buyEWMA
	}
	sellVal := s.baselineBps
	if s.sellInit && s.sideFilledEnough("sell") {
		sellVal = s.sellEWMA
	}
	if buyVal < sellVal {
		return buyVal
	}
	return sellVal
}

// SideEWMAs returns the current EWMA values for diagnostics, including idle decay.
func (s *SideToxicityTracker) SideEWMAs() (buyEWMA, sellEWMA float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.decayedEWMA(s.buyEWMA, s.buyUpdated, s.buyInit),
		s.decayedEWMA(s.sellEWMA, s.sellUpdated, s.sellInit)
}

// SideFillCounts returns the number of markouts recorded per side. V30 part 6:
// used by the periodic markout_ewma_snapshot log event for post-mortem fidelity.
func (s *SideToxicityTracker) SideFillCounts() (buyFills, sellFills int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buyFillCount, s.sellFillCount
}

// MarkoutEWMA returns the EWMA for the given side and whether it's ready
// (initialized and past warmup). Returns (baseline, false) when not ready.
// This is the diagnostic / spread-multiplier path — idle decay toward baseline
// is applied. Use RawMarkoutEWMA for optimizer inputs that must distinguish
// true adverse markout history from idle-decayed-toward-baseline values.
func (s *SideToxicityTracker) MarkoutEWMA(side string) (float64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.sideFilledEnough(side) {
		return s.baselineBps, false
	}

	// Read raw EWMA and init flag directly (not via sideEWMA, which already
	// applies decayedEWMA — calling decayedEWMA again would double-decay).
	var rawEWMA float64
	var init bool
	switch normalizeSide(side) {
	case "buy":
		rawEWMA, init = s.buyEWMA, s.buyInit
	case "sell":
		rawEWMA, init = s.sellEWMA, s.sellInit
	default:
		return s.baselineBps, false
	}
	if !init {
		return s.baselineBps, false
	}

	return s.decayedEWMA(rawEWMA, s.sideUpdatedTime(side), true), true
}

// RawMarkoutEWMA returns the raw per-side EWMA without idle decay, and whether
// it's ready (initialized and past warmup). Returns (baseline, false) when not
// ready. Intended for optimizer consumption where idle decay toward baseline
// would hide sustained adverse markout — v22.1 session showed both sides
// asymptoting to baseline=8.0 during a 15-min adverse window, starving the
// optimizer of toxicity signal exactly when it mattered most.
func (s *SideToxicityTracker) RawMarkoutEWMA(side string) (float64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.sideFilledEnough(side) {
		return s.baselineBps, false
	}

	switch normalizeSide(side) {
	case "buy":
		if !s.buyInit {
			return s.baselineBps, false
		}
		return s.buyEWMA, true
	case "sell":
		if !s.sellInit {
			return s.baselineBps, false
		}
		return s.sellEWMA, true
	}
	return s.baselineBps, false
}

// sideUpdatedTime returns the last update timestamp for the given side.
// Caller must hold s.mu.
func (s *SideToxicityTracker) sideUpdatedTime(side string) time.Time {
	switch normalizeSide(side) {
	case "buy":
		return s.buyUpdated
	case "sell":
		return s.sellUpdated
	}
	return time.Time{}
}

// sideFilledEnough returns true if the given side has accumulated at least
// warmupFills markout recordings, or if warmup gating is disabled (warmupFills <= 0).
// Caller must hold s.mu.
func (s *SideToxicityTracker) sideFilledEnough(side string) bool {
	if s.warmupFills <= 0 {
		return true
	}
	switch normalizeSide(side) {
	case "buy":
		return s.buyFillCount >= s.warmupFills
	case "sell":
		return s.sellFillCount >= s.warmupFills
	default:
		return false
	}
}

func (s *SideToxicityTracker) sideEWMA(side string) (float64, bool) {
	switch side {
	case "buy":
		return s.decayedEWMA(s.buyEWMA, s.buyUpdated, s.buyInit), s.buyInit
	case "sell":
		return s.decayedEWMA(s.sellEWMA, s.sellUpdated, s.sellInit), s.sellInit
	default:
		return 0, false
	}
}

// decayedEWMA applies time-based idle decay toward baselineBps. Returns the
// raw EWMA if not initialized, or if less than idleDecayHalfLives half-lives
// have elapsed since the last fill. After the delay, decays exponentially
// toward baseline using the same half-life, so after ~4 additional half-lives
// the EWMA converges to within 6% of baseline. If idleDecayHalfLives < 0,
// idle decay is disabled (A3 legacy behavior).
func (s *SideToxicityTracker) decayedEWMA(ewma float64, updated time.Time, init bool) float64 {
	if !init || s.halfLife <= 0 || s.idleDecayHalfLives < 0 {
		return ewma
	}
	elapsed := time.Since(updated).Seconds()
	delaySeconds := s.idleDecayHalfLives * s.halfLife.Seconds()
	if elapsed <= delaySeconds {
		return ewma // within grace period — hold last value (A3 intent)
	}
	// Decay only the time beyond the grace period
	decayTime := elapsed - delaySeconds
	alpha := 1.0 - math.Pow(2.0, -decayTime/s.halfLife.Seconds())
	return alpha*s.baselineBps + (1-alpha)*ewma
}
