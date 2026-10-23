package quoting

import (
	"math"
	"testing"
	"time"
)

func TestSideToxicityInitial(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	if m := st.SpreadMultiplier("buy"); m != 1.0 {
		t.Errorf("expected 1.0 before data, got %f", m)
	}
	if m := st.SpreadMultiplier("sell"); m != 1.0 {
		t.Errorf("expected 1.0 before data, got %f", m)
	}
}

func TestSideToxicityPerSide(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	// Toxic buy-side markouts
	for i := 0; i < 10; i++ {
		st.RecordMarkout("buy", -5.0)
	}
	// Benign sell-side markouts
	for i := 0; i < 10; i++ {
		st.RecordMarkout("sell", 5.0)
	}
	buyMult := st.SpreadMultiplier("buy")
	sellMult := st.SpreadMultiplier("sell")
	if buyMult <= 1.0 {
		t.Errorf("toxic buy side should widen, got %f", buyMult)
	}
	if sellMult != 1.0 {
		t.Errorf("benign sell side should not widen, got %f", sellMult)
	}
}

func TestSideToxicitySizeScaling(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	// Record toxic fills below threshold
	for i := 0; i < 10; i++ {
		st.RecordMarkout("buy", -5.0)
	}
	factor := st.SizeFactor("buy", -2.0, 0.5)
	if factor >= 1.0 {
		t.Errorf("toxic side should reduce size, got %f", factor)
	}
	if factor < 0.5 {
		t.Errorf("size factor should not go below max reduction (0.5), got %f", factor)
	}
	// Benign side should have factor 1.0
	factor2 := st.SizeFactor("sell", -2.0, 0.5)
	if factor2 != 1.0 {
		t.Errorf("benign side should have factor 1.0, got %f", factor2)
	}
}

func TestSideToxicityMaxWiden(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 1.5, 3.0, 0, 0, 1.0)
	for i := 0; i < 20; i++ {
		st.RecordMarkout("buy", -100.0)
	}
	mult := st.SpreadMultiplier("buy")
	if math.Abs(mult-1.5) > 0.01 {
		t.Errorf("expected clamped to maxWiden=1.5, got %f", mult)
	}
}

func TestSideToxicityWorstMarkout(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	for i := 0; i < 10; i++ {
		st.RecordMarkout("buy", -3.0)
		st.RecordMarkout("sell", 1.0)
	}
	worst := st.WorstMarkoutEWMA()
	if worst >= 0 {
		t.Errorf("worst markout should be negative (buy side), got %f", worst)
	}
}

func TestSideToxicityIdleDecayHoldsWithinGrace(t *testing.T) {
	// Within idleDecayDelay half-lives, the EWMA should hold its last value
	// (A3 intent: don't immediately forgive toxic fills).
	halfLife := 100 * time.Millisecond
	st := NewSideToxicityTracker(halfLife, 2.0, 3.0, 0, 0, 1.0)
	st.RecordMarkout("buy", -5.0)

	// Check immediately — should be widened
	mult1 := st.SpreadMultiplier("buy")
	if mult1 <= 1.0 {
		t.Fatalf("expected widening immediately after toxic fill, got %f", mult1)
	}

	// Wait less than grace period (idleDecayDelay=4 half-lives = 400ms)
	time.Sleep(200 * time.Millisecond)
	mult2 := st.SpreadMultiplier("buy")
	if math.Abs(mult2-mult1) > 0.05 {
		t.Errorf("expected EWMA to hold within grace period, got %f (was %f)", mult2, mult1)
	}
}

func TestSideToxicityIdleDecayAfterGrace(t *testing.T) {
	// After idleDecayDelay half-lives with no fills, EWMA should decay toward baseline.
	halfLife := 50 * time.Millisecond
	st := NewSideToxicityTracker(halfLife, 2.0, 3.0, 0, 0, 1.0)
	st.RecordMarkout("buy", -10.0)

	multBefore := st.SpreadMultiplier("buy")
	if multBefore <= 1.0 {
		t.Fatalf("expected widening after toxic fill, got %f", multBefore)
	}

	// Wait past grace period (4 half-lives = 200ms) plus several more half-lives for decay
	time.Sleep(500 * time.Millisecond)
	multAfter := st.SpreadMultiplier("buy")
	if multAfter >= multBefore {
		t.Errorf("expected multiplier to decay after grace period, got %f (was %f)", multAfter, multBefore)
	}
	// After 6 additional half-lives (500ms - 200ms grace = 300ms = 6 half-lives),
	// decay should be substantial (2^-6 = 1.5% of original offset remaining).
	if multAfter > 1.1 {
		t.Errorf("expected multiplier near 1.0 after extended idle, got %f", multAfter)
	}
}

func TestSideToxicityIdleDecayResetOnFill(t *testing.T) {
	// A new fill should reset the grace period timer.
	halfLife := 50 * time.Millisecond
	st := NewSideToxicityTracker(halfLife, 2.0, 3.0, 0, 0, 1.0)
	st.RecordMarkout("buy", -10.0)

	// Wait past grace period
	time.Sleep(400 * time.Millisecond)
	multDecayed := st.SpreadMultiplier("buy")

	// Record another toxic fill — should reset the timer and re-widen
	st.RecordMarkout("buy", -10.0)
	multFresh := st.SpreadMultiplier("buy")
	if multFresh <= multDecayed {
		t.Errorf("expected fresh fill to re-widen, got %f (decayed was %f)", multFresh, multDecayed)
	}
}

func TestSideToxicityIdleDecaySideEWMAs(t *testing.T) {
	// SideEWMAs should reflect decayed values for diagnostics.
	halfLife := 50 * time.Millisecond
	baseline := 3.0
	st := NewSideToxicityTracker(halfLife, 2.0, baseline, 0, 0, 1.0)
	st.RecordMarkout("buy", -10.0)
	st.RecordMarkout("sell", -10.0)

	// Wait past grace period + several decay half-lives
	time.Sleep(500 * time.Millisecond)
	buyEWMA, sellEWMA := st.SideEWMAs()

	// Both should have decayed substantially toward baseline (3.0)
	if buyEWMA < 1.0 {
		t.Errorf("expected buyEWMA to decay toward baseline, got %f", buyEWMA)
	}
	if sellEWMA < 1.0 {
		t.Errorf("expected sellEWMA to decay toward baseline, got %f", sellEWMA)
	}
}

func TestSideToxicityWarmupGate(t *testing.T) {
	// With warmupFills=3, the multiplier should be 1.0 until 3 fills are recorded.
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 3, 1.0)

	// Record 1 toxic fill
	st.RecordMarkout("buy", -10.0)
	if m := st.SpreadMultiplier("buy"); m != 1.0 {
		t.Errorf("expected 1.0 during warmup (1 fill), got %f", m)
	}
	if f := st.SizeFactor("buy", -2.0, 0.5); f != 1.0 {
		t.Errorf("expected size factor 1.0 during warmup, got %f", f)
	}

	// Record 2nd fill
	st.RecordMarkout("buy", -10.0)
	if m := st.SpreadMultiplier("buy"); m != 1.0 {
		t.Errorf("expected 1.0 during warmup (2 fills), got %f", m)
	}

	// Record 3rd fill — now should be active
	st.RecordMarkout("buy", -10.0)
	m := st.SpreadMultiplier("buy")
	if m <= 1.0 {
		t.Errorf("expected widening after warmup (3 fills), got %f", m)
	}

	// Sell side should still be gated (0 fills)
	if sm := st.SpreadMultiplier("sell"); sm != 1.0 {
		t.Errorf("expected sell side gated (0 fills), got %f", sm)
	}

	// WorstMarkoutEWMA should use baseline for gated side
	worst := st.WorstMarkoutEWMA()
	// Buy EWMA is -10, sell is baseline (3.0). Worst should be buy EWMA.
	if worst >= 0 {
		t.Errorf("worst should reflect buy EWMA, got %f", worst)
	}
}

func TestSideToxicityTracker_MarkoutEWMA(t *testing.T) {
	st := NewSideToxicityTracker(10*time.Second, 1.5, 2.0, 4.0, 3, 1.0)

	// Before warmup, should return baseline
	buyEWMA, buyReady := st.MarkoutEWMA("buy")
	if buyReady {
		t.Error("expected buy not ready before warmup")
	}
	if buyEWMA != 2.0 {
		t.Errorf("expected baseline 2.0 before warmup, got %f", buyEWMA)
	}

	// Record enough fills to pass warmup (3 fills needed)
	for i := 0; i < 3; i++ {
		st.RecordMarkout("buy", 1.5)
	}

	buyEWMA, buyReady = st.MarkoutEWMA("buy")
	if !buyReady {
		t.Error("expected buy ready after warmup")
	}
	if buyEWMA < 1.0 || buyEWMA > 2.0 {
		t.Errorf("expected EWMA near 1.5, got %f", buyEWMA)
	}

	// Sell side should still not be ready
	_, sellReady := st.MarkoutEWMA("sell")
	if sellReady {
		t.Error("expected sell not ready")
	}
}

func TestSideToxicityWarmupDisabled(t *testing.T) {
	// warmupFills=0 means no gating (backward compatible)
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	st.RecordMarkout("buy", -10.0)
	m := st.SpreadMultiplier("buy")
	if m <= 1.0 {
		t.Errorf("expected immediate widening with warmup disabled, got %f", m)
	}
}

func TestSizeFactorExponent1Linear(t *testing.T) {
	// With sizeExponent=1.0, the power-law formula gives a linear curve
	// from 1.0 (at threshold) to maxReduce (at 2*threshold).
	threshold := -2.0
	maxReduce := 0.5

	// Test several EWMA values covering the range.
	// Each uses a fresh tracker. The first RecordMarkout sets EWMA directly.
	testEWMAs := []float64{-1.0, -2.0, -2.5, -3.0, -3.5, -4.0, -5.0, -10.0}
	for _, ewma := range testEWMAs {
		st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
		st.RecordMarkout("buy", ewma) // first record sets EWMA directly
		factor := st.SizeFactor("buy", threshold, maxReduce)

		// Compute the expected linear result manually:
		// t_val = (threshold - ewma) / |threshold|, clamped to [0, 1]
		// factor = 1.0 - (1.0 - maxReduce) * t_val, clamped to [maxReduce, 1.0]
		tVal := (threshold - ewma) / math.Abs(threshold)
		if tVal < 0 {
			tVal = 0
		}
		if tVal > 1.0 {
			tVal = 1.0
		}
		expected := 1.0 - (1.0-maxReduce)*math.Pow(tVal, 1.0)
		if expected < maxReduce {
			expected = maxReduce
		}
		if expected > 1.0 {
			expected = 1.0
		}

		if math.Abs(factor-expected) > 0.01 {
			t.Errorf("ewma=%f: expected factor ~%f, got %f", ewma, expected, factor)
		}
	}
}

func TestSizeFactorConvex(t *testing.T) {
	// With sizeExponent=2.0, the power-law should be gentler at mild toxicity
	// and more aggressive at extreme toxicity compared to linear (exponent=1.0).
	threshold := -2.0
	maxReduce := 0.5

	// Mild toxicity: EWMA = -2.5, t = 0.25
	stLinear := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	stConvex := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 2.0)
	stLinear.RecordMarkout("buy", -2.5) // sets EWMA directly
	stConvex.RecordMarkout("buy", -2.5)
	linearFactor := stLinear.SizeFactor("buy", threshold, maxReduce)
	convexFactor := stConvex.SizeFactor("buy", threshold, maxReduce)

	// At mild toxicity (t = 0.25), convex should reduce LESS than linear
	// Linear: 1.0 - 0.5*0.25 = 0.875; Convex: 1.0 - 0.5*0.0625 = 0.96875
	if convexFactor <= linearFactor {
		t.Errorf("at mild toxicity, convex factor (%f) should be > linear factor (%f)",
			convexFactor, linearFactor)
	}

	// Extreme toxicity: EWMA = -3.8, t = 0.9
	stLinear2 := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	stConvex2 := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 2.0)
	stLinear2.RecordMarkout("buy", -3.8)
	stConvex2.RecordMarkout("buy", -3.8)
	linearFactor2 := stLinear2.SizeFactor("buy", threshold, maxReduce)
	convexFactor2 := stConvex2.SizeFactor("buy", threshold, maxReduce)

	// For exponent > 1, the power-law curve is always above the linear curve
	// for t in (0,1), so convex factor >= linear factor at all non-boundary points.
	// The "aggressive at extreme" manifests as steeper slope near t=1.
	if convexFactor2 <= linearFactor2 {
		t.Errorf("at extreme toxicity, convex factor (%f) should still be >= linear factor (%f) for exponent>1",
			convexFactor2, linearFactor2)
	}

	// But the GAP between convex and linear should be SMALLER at extreme t than at mild t
	gapMild := convexFactor - linearFactor
	gapExtreme := convexFactor2 - linearFactor2
	if gapExtreme >= gapMild {
		t.Errorf("convex-linear gap should narrow at extreme toxicity: mild gap=%f, extreme gap=%f",
			gapMild, gapExtreme)
	}

	// Verify the midpoint behavior: at t=0.5, linear gives 0.75, convex gives 0.875
	stLinMid := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 1.0)
	stConvMid := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 2.0)
	// ewma = threshold - 0.5 * |threshold| = -2.0 - 1.0 = -3.0
	stLinMid.RecordMarkout("buy", -3.0)
	stConvMid.RecordMarkout("buy", -3.0)
	linMid := stLinMid.SizeFactor("buy", threshold, maxReduce)
	conMid := stConvMid.SizeFactor("buy", threshold, maxReduce)

	// Linear: 1.0 - 0.5 * 0.5 = 0.75; Convex: 1.0 - 0.5 * 0.25 = 0.875
	if math.Abs(linMid-0.75) > 0.01 {
		t.Errorf("linear midpoint: expected ~0.75, got %f", linMid)
	}
	if math.Abs(conMid-0.875) > 0.01 {
		t.Errorf("convex midpoint: expected ~0.875, got %f", conMid)
	}
}

func TestSizeFactorExponentDefault(t *testing.T) {
	// sizeExponent <= 0 should default to 1.0 (linear)
	st := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, 0)
	st.RecordMarkout("buy", -3.0) // sets EWMA = -3.0 directly
	factor := st.SizeFactor("buy", -2.0, 0.5)
	// Should behave like exponent=1.0: t=0.5, factor = 1.0 - 0.5*0.5 = 0.75
	if math.Abs(factor-0.75) > 0.01 {
		t.Errorf("default exponent: expected ~0.75, got %f", factor)
	}

	// Also test negative exponent defaults to 1.0
	stNeg := NewSideToxicityTracker(2*time.Minute, 2.0, 3.0, 0, 0, -1.0)
	stNeg.RecordMarkout("buy", -3.0)
	factorNeg := stNeg.SizeFactor("buy", -2.0, 0.5)
	if math.Abs(factorNeg-0.75) > 0.01 {
		t.Errorf("negative exponent: expected ~0.75, got %f", factorNeg)
	}
}

// TestRawMarkoutEWMA_SkipsIdleDecay — v23 invariant. After an adverse fill
// burst with no further fills, idle decay pulls MarkoutEWMA toward baseline
// (positive). RawMarkoutEWMA must stay at the last recorded adverse value so
// the optimizer can still see the toxicity.
func TestRawMarkoutEWMA_SkipsIdleDecay(t *testing.T) {
	// Short half-life so idle decay kicks in within the test.
	// halfLife=100ms, idleDecayHalfLives=1 (grace 100ms), baseline=8.0, warmup=1.
	st := NewSideToxicityTracker(100*time.Millisecond, 1.5, 8.0, 1.0, 1, 1.0)

	// Record one strongly adverse fill.
	st.RecordMarkout("buy", -20.0)

	// Immediately: both methods return the raw value.
	rawImmediate, rawReady := st.RawMarkoutEWMA("buy")
	if !rawReady {
		t.Fatal("raw expected ready after warmup")
	}
	if math.Abs(rawImmediate-(-20.0)) > 0.01 {
		t.Errorf("raw immediate: expected -20.0, got %f", rawImmediate)
	}

	// Wait past the grace period + several half-lives so idle decay pulls the
	// decayed value strongly toward baseline.
	time.Sleep(600 * time.Millisecond)

	decayed, _ := st.MarkoutEWMA("buy")
	raw, _ := st.RawMarkoutEWMA("buy")

	// Decayed must have moved toward baseline (8.0, positive).
	if decayed <= -10.0 {
		t.Errorf("decayed: expected significant decay toward +baseline=8.0, got %f", decayed)
	}
	// Raw must still reflect the adverse -20.0 (no decay).
	if math.Abs(raw-(-20.0)) > 0.01 {
		t.Errorf("raw: expected -20.0 unchanged, got %f", raw)
	}
	if decayed <= raw {
		t.Errorf("decayed (%f) should be > raw (%f) after idle period", decayed, raw)
	}
}

// TestWorstMarkoutEWMARaw_BothSides — when both sides have raw adverse
// values, the raw-worst is min of the two. Idle decay path diverges.
func TestWorstMarkoutEWMARaw_BothSides(t *testing.T) {
	st := NewSideToxicityTracker(100*time.Millisecond, 1.5, 8.0, 1.0, 1, 1.0)
	st.RecordMarkout("buy", -15.0)
	st.RecordMarkout("sell", -25.0)

	raw := st.WorstMarkoutEWMARaw()
	if math.Abs(raw-(-25.0)) > 0.01 {
		t.Errorf("raw worst: expected -25.0 (sell), got %f", raw)
	}

	// After idle decay, decayed-worst moves up toward baseline while raw
	// stays at -25.0.
	time.Sleep(600 * time.Millisecond)
	decayed := st.WorstMarkoutEWMA()
	raw2 := st.WorstMarkoutEWMARaw()
	if math.Abs(raw2-(-25.0)) > 0.01 {
		t.Errorf("raw worst post-idle: expected -25.0 unchanged, got %f", raw2)
	}
	if decayed <= raw2 {
		t.Errorf("decayed worst (%f) should be > raw worst (%f) after idle", decayed, raw2)
	}
}

// TestRawMarkoutEWMA_NotReadyBeforeWarmup — raw respects the warmup gate.
func TestRawMarkoutEWMA_NotReadyBeforeWarmup(t *testing.T) {
	st := NewSideToxicityTracker(2*time.Minute, 1.5, 8.0, 4.0, 3, 1.0)
	// Record fewer fills than warmup.
	st.RecordMarkout("buy", -10.0)
	st.RecordMarkout("buy", -10.0)
	_, ready := st.RawMarkoutEWMA("buy")
	if ready {
		t.Error("expected not ready before warmup fills (needs 3, got 2)")
	}
	st.RecordMarkout("buy", -10.0) // third fill
	raw, ready := st.RawMarkoutEWMA("buy")
	if !ready {
		t.Error("expected ready after warmup")
	}
	if raw >= 0 {
		t.Errorf("expected negative raw EWMA, got %f", raw)
	}
}
