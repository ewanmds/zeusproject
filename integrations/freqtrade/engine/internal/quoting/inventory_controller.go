package quoting

import (
	"math"
	"sync"
	"time"
)

// InventoryController dynamically adjusts the inventory skew factor to achieve
// a target inventory decay rate (specified as a half-life in minutes).
// Uses a PID controller with configurable proportional, integral, and derivative gains.
type InventoryController struct {
	halfLifeMinutes float64
	baseFactor      float64
	skewMin         float64
	skewMax         float64
	gainP           float64
	gainI           float64
	gainD           float64

	// Timestamp-based circular buffer of inventory samples
	samples    []inventorySample
	sampleIdx  int
	sampleFull bool

	// Internal skew adjustment (multiplicative offset from base)
	adjustment float64 // starts at 1.0 (neutral)

	// PID state
	integralError  float64 // accumulated error for I term
	lastError      float64 // previous error for D term
	smoothedError  float64 // EMA-smoothed error for D term (reduces noise spikes)
	lastErrorValid bool    // true after first error computation

	mu sync.Mutex
}

type inventorySample struct {
	ratio float64 // |position_notional| / max_position_notional
	ts    time.Time
}

const inventoryControllerMaxSamples = 60

// NewInventoryController creates a new controller.
// halfLifeMinutes=0 disables the controller (EffectiveSkewFactor returns base).
func NewInventoryController(halfLifeMinutes, baseFactor, skewMin, skewMax, gainP, gainI, gainD float64) *InventoryController {
	return &InventoryController{
		halfLifeMinutes: halfLifeMinutes,
		baseFactor:      baseFactor,
		skewMin:         skewMin,
		skewMax:         skewMax,
		gainP:           gainP,
		gainI:           gainI,
		gainD:           gainD,
		samples:         make([]inventorySample, inventoryControllerMaxSamples),
		adjustment:      1.0,
	}
}

// RecordSample records an inventory utilization sample at the given time.
func (ic *InventoryController) RecordSample(ratio float64, ts time.Time) {
	ic.mu.Lock()
	defer ic.mu.Unlock()

	ic.samples[ic.sampleIdx] = inventorySample{ratio: ratio, ts: ts}
	ic.sampleIdx = (ic.sampleIdx + 1) % inventoryControllerMaxSamples
	if ic.sampleIdx == 0 {
		ic.sampleFull = true
	}

	ic.updateAdjustment()
}

// EffectiveSkewFactor returns the adjusted skew factor, clamped to [skewMin, skewMax].
// If the controller is disabled (halfLifeMinutes=0), returns baseFactor unchanged.
func (ic *InventoryController) EffectiveSkewFactor(baseFactor float64) float64 {
	if ic.halfLifeMinutes == 0 {
		return baseFactor
	}

	ic.mu.Lock()
	adj := ic.adjustment
	ic.mu.Unlock()

	result := baseFactor * adj
	if result < ic.skewMin {
		result = ic.skewMin
	}
	if result > ic.skewMax {
		result = ic.skewMax
	}
	return result
}

func (ic *InventoryController) updateAdjustment() {
	n := ic.sampleCount()
	if n < 5 {
		return // not enough data
	}

	// Target decay rate: ln(2) / halfLifeMinutes per minute.
	targetDecayRate := math.Ln2 / ic.halfLifeMinutes // per minute

	// Get samples in chronological order
	samples := ic.orderedSamples()
	if len(samples) < 5 {
		return
	}

	// Compute average inventory level over window
	var avgRatio float64
	for _, s := range samples {
		avgRatio += s.ratio
	}
	avgRatio /= float64(len(samples))

	// If average inventory is very low, no adjustment needed
	if avgRatio < 0.05 {
		// Gently return to neutral
		ic.adjustment = ic.adjustment*0.95 + 1.0*0.05
		// Reset integral to zero on flat — stale integral from previous inventory
		// period drives bang-bang behavior when inventory returns.
		ic.integralError = 0
		ic.lastErrorValid = false
		return
	}

	// Compute observed decay: compare first third avg to last third avg
	third := len(samples) / 3
	if third < 2 {
		return
	}
	var firstAvg, lastAvg float64
	for _, s := range samples[:third] {
		firstAvg += s.ratio
	}
	firstAvg /= float64(third)
	for _, s := range samples[len(samples)-third:] {
		lastAvg += s.ratio
	}
	lastAvg /= float64(third)

	windowDuration := samples[len(samples)-1].ts.Sub(samples[0].ts).Minutes()
	if windowDuration < 0.1 {
		return
	}

	// Observed decay rate (positive means decaying)
	observedDecayRate := 0.0
	if firstAvg > 0.01 {
		observedDecayRate = (firstAvg - lastAvg) / firstAvg / windowDuration
	}

	// Error: positive means we're decaying too slowly
	err := targetDecayRate - observedDecayRate

	// PID controller
	dt := windowDuration
	if dt < 0.1 {
		return
	}

	pTerm := err * ic.gainP

	// V24: anti-windup. Before integrating, check if the output is already saturated
	// AND the new error would push it further into saturation. If so, freeze the
	// integrator (do not accumulate). This prevents the V23 failure where skew_factor
	// pegged at skew_max for 80% of session while integralError kept growing — when
	// inventory finally reversed, the integrator took minutes to unwind.
	adjMax, adjMin := math.Inf(1), math.Inf(-1)
	if ic.baseFactor > 0 {
		adjMax = ic.skewMax / ic.baseFactor
		adjMin = ic.skewMin / ic.baseFactor
	}
	saturatedHigh := ic.adjustment >= adjMax-1e-9
	saturatedLow := ic.adjustment <= adjMin+1e-9
	freezeIntegrator := (saturatedHigh && err > 0) || (saturatedLow && err < 0)
	if !freezeIntegrator {
		ic.integralError += err * dt
	}
	// Keep the existing hard clamp as a belt-and-suspenders bound.
	if ic.integralError > 2.0 {
		ic.integralError = 2.0
	}
	if ic.integralError < -2.0 {
		ic.integralError = -2.0
	}
	iTerm := ic.integralError * ic.gainI

	// Derivative: EMA-smoothed error change (raw derivative on noisy error signal
	// produces large spikes that drive adjustment to the rails)
	dTerm := 0.0
	if ic.lastErrorValid && dt > 0 {
		ic.smoothedError = 0.8*ic.smoothedError + 0.2*err
		dTerm = (ic.smoothedError - ic.lastError) / dt * ic.gainD
	} else {
		ic.smoothedError = err
	}
	ic.lastError = ic.smoothedError
	ic.lastErrorValid = true

	ic.adjustment += pTerm + iTerm + dTerm

	// Clamp adjustment so baseFactor*adjustment stays within [skewMin, skewMax]
	if ic.baseFactor > 0 {
		adjMin := ic.skewMin / ic.baseFactor
		adjMax := ic.skewMax / ic.baseFactor
		if ic.adjustment < adjMin {
			ic.adjustment = adjMin
		}
		if ic.adjustment > adjMax {
			ic.adjustment = adjMax
		}
	}
}

func (ic *InventoryController) sampleCount() int {
	if ic.sampleFull {
		return inventoryControllerMaxSamples
	}
	return ic.sampleIdx
}

func (ic *InventoryController) orderedSamples() []inventorySample {
	n := ic.sampleCount()
	result := make([]inventorySample, 0, n)
	if ic.sampleFull {
		for i := ic.sampleIdx; i < inventoryControllerMaxSamples; i++ {
			result = append(result, ic.samples[i])
		}
	}
	for i := 0; i < ic.sampleIdx; i++ {
		result = append(result, ic.samples[i])
	}
	return result
}
