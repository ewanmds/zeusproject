package quoting

import (
	"math"
	"testing"
	"time"
)

func TestFillRateTracker_NotInitialized(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)
	if m := ft.NotionalMultiplier(); m != 1.0 {
		t.Errorf("expected 1.0 before any fills, got %f", m)
	}
}

func TestFillRateTracker_RecordFillAndMultiplier(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)

	// Record a fill. Rate is seeded to filledNotional.
	ft.RecordFill(200)

	// Immediately after, multiplier should be target/rate = 200/200 = 1.0.
	m := ft.NotionalMultiplier()
	if m < 0.5 || m > 2.0 {
		t.Errorf("expected multiplier near 1.0, got %f", m)
	}
}

func TestFillRateTracker_Clamping(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)
	ft.RecordFill(200)

	// Min clamp: min/base = 100/500 = 0.2
	// Max clamp: max/base = 1000/500 = 2.0
	m := ft.NotionalMultiplier()
	minMult := 100.0 / 500.0
	maxMult := 1000.0 / 500.0
	if m < minMult-0.01 || m > maxMult+0.01 {
		t.Errorf("multiplier %f outside clamp range [%f, %f]", m, minMult, maxMult)
	}
}

func TestFillRateTracker_DecayTowardTarget(t *testing.T) {
	ft := NewFillRateTracker(1*time.Second, 200, 500, 100, 1000)
	ft.RecordFill(200)

	// Immediately after seeding at target rate, multiplier should be ~1.0.
	m1 := ft.NotionalMultiplier()

	// Manually set lastUpdate far in the past to simulate long decay.
	// Rate should decay toward target, so multiplier stays near 1.0.
	ft.mu.Lock()
	ft.global.lastUpdate = time.Now().Add(-10 * time.Second)
	ft.mu.Unlock()

	m2 := ft.NotionalMultiplier()
	if m2 < 0.9 || m2 > 1.1 {
		t.Errorf("expected multiplier near 1.0 after decay toward target, got %f (was %f)", m2, m1)
	}
}

func TestFillRateTracker_DecayFromHighRate(t *testing.T) {
	ft := NewFillRateTracker(1*time.Second, 200, 500, 100, 1000)
	ft.RecordFill(200) // seed

	// Record a very large fill to push rate well above target.
	ft.mu.Lock()
	ft.global.rate = 1000 // 5x target
	ft.global.lastUpdate = time.Now()
	ft.mu.Unlock()

	m1 := ft.NotionalMultiplier()
	// rate=1000, target=200, mult = 200/1000 = 0.2 (clamped to min 0.2)
	if m1 > 0.3 {
		t.Errorf("expected low multiplier with high rate, got %f", m1)
	}

	// After decay, rate should move toward target (200), increasing multiplier toward 1.0
	ft.mu.Lock()
	ft.global.lastUpdate = time.Now().Add(-10 * time.Second)
	ft.mu.Unlock()

	m2 := ft.NotionalMultiplier()
	if m2 < m1 {
		t.Errorf("expected multiplier to increase as rate decays toward target, got %f -> %f", m1, m2)
	}
}

func TestFillRateTracker_DecayFromLowRate(t *testing.T) {
	ft := NewFillRateTracker(1*time.Second, 200, 500, 100, 1000)
	ft.RecordFill(200) // seed

	// Set rate well below target.
	ft.mu.Lock()
	ft.global.rate = 50 // 0.25x target
	ft.global.lastUpdate = time.Now()
	ft.mu.Unlock()

	m1 := ft.NotionalMultiplier()
	// rate=50, target=200, mult = 200/50 = 4.0 (clamped to max 2.0)
	if m1 < 1.5 {
		t.Errorf("expected high multiplier with low rate, got %f", m1)
	}

	// After decay, rate should move toward target (200), decreasing multiplier toward 1.0
	ft.mu.Lock()
	ft.global.lastUpdate = time.Now().Add(-10 * time.Second)
	ft.mu.Unlock()

	m2 := ft.NotionalMultiplier()
	if m2 > 1.1 {
		t.Errorf("expected multiplier to decrease toward 1.0 as rate decays toward target, got %f -> %f", m1, m2)
	}
}

func TestFillRateTracker_EWMAAlpha(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)
	alpha := ft.ewmaAlpha(60 * time.Second)
	// At halfLife, alpha = 1 - 2^(-1) = 0.5
	if math.Abs(alpha-0.5) > 0.001 {
		t.Errorf("expected alpha ≈ 0.5 at half-life, got %f", alpha)
	}
}

// --- Per-side tests ---

func TestFillRateTracker_PerSideMultiplier(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)

	// Record only buy fills.
	ft.RecordFillSide("buy", 100)
	ft.RecordFillSide("buy", 100)
	ft.RecordFillSide("buy", 100)

	// Buy side has data -- multiplier should be near 1.0 (seeded at target, then fills recorded).
	buyMult := ft.NotionalMultiplierSide("buy")
	if buyMult < 0.1 || buyMult > 5.0 {
		t.Errorf("expected buy multiplier in reasonable range, got %f", buyMult)
	}

	// Sell side has NO data -- should return 1.0 (neutral).
	sellMult := ft.NotionalMultiplierSide("sell")
	if sellMult != 1.0 {
		t.Errorf("expected sell multiplier = 1.0 (no data), got %f", sellMult)
	}

	// Global should also have data (RecordFillSide records to global too).
	globalMult := ft.NotionalMultiplier()
	if globalMult < 0.1 || globalMult > 5.0 {
		t.Errorf("expected global multiplier in reasonable range, got %f", globalMult)
	}
}

func TestFillRateTracker_PerSideAsymmetric(t *testing.T) {
	ft := NewFillRateTracker(1*time.Second, 200, 500, 100, 1000)

	// Seed both sides.
	ft.RecordFillSide("buy", 100)
	ft.RecordFillSide("sell", 100)

	// Push buy rate high (above per-side target of 100).
	ft.mu.Lock()
	ft.buySide.rate = 500 // 5x per-side target
	ft.buySide.lastUpdate = time.Now()
	ft.mu.Unlock()

	// Push sell rate low (below per-side target of 100).
	ft.mu.Lock()
	ft.sellSide.rate = 20 // 0.2x per-side target
	ft.sellSide.lastUpdate = time.Now()
	ft.mu.Unlock()

	buyMult := ft.NotionalMultiplierSide("buy")
	sellMult := ft.NotionalMultiplierSide("sell")

	// Buy overfilling -> multiplier should be low (scale down).
	if buyMult > 0.5 {
		t.Errorf("expected low buy multiplier (overfilling), got %f", buyMult)
	}

	// Sell underfilling -> multiplier should be high (scale up).
	if sellMult < 1.5 {
		t.Errorf("expected high sell multiplier (underfilling), got %f", sellMult)
	}

	// Asymmetry: sell mult should be greater than buy mult.
	if sellMult <= buyMult {
		t.Errorf("expected sell mult > buy mult, got sell=%f, buy=%f", sellMult, buyMult)
	}
}

func TestFillRateTracker_GlobalStillWorks(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)

	// Use only the legacy RecordFill (global-only).
	ft.RecordFill(200)

	m := ft.NotionalMultiplier()
	// Seeded at target, should be near 1.0.
	if m < 0.5 || m > 2.0 {
		t.Errorf("expected global multiplier near 1.0, got %f", m)
	}

	// Per-side should still be 1.0 (not initialized via RecordFill).
	buyMult := ft.NotionalMultiplierSide("buy")
	if buyMult != 1.0 {
		t.Errorf("expected buy multiplier = 1.0 (not initialized), got %f", buyMult)
	}
	sellMult := ft.NotionalMultiplierSide("sell")
	if sellMult != 1.0 {
		t.Errorf("expected sell multiplier = 1.0 (not initialized), got %f", sellMult)
	}
}

func TestFillRateTracker_SideAliases(t *testing.T) {
	ft := NewFillRateTracker(60*time.Second, 200, 500, 100, 1000)

	// Record via different aliases.
	ft.RecordFillSide("B", 100)
	ft.buySide.lastUpdate = time.Now().Add(-30 * time.Second)
	ft.RecordFillSide("bid", 100)

	buyMult := ft.NotionalMultiplierSide("buy")
	if buyMult == 1.0 {
		t.Errorf("expected buy side to be initialized via aliases, got 1.0")
	}

	ft.RecordFillSide("A", 100)
	ft.sellSide.lastUpdate = time.Now().Add(-30 * time.Second)
	ft.RecordFillSide("ask", 100)

	sellMult := ft.NotionalMultiplierSide("sell")
	if sellMult == 1.0 {
		t.Errorf("expected sell side to be initialized via aliases, got 1.0")
	}

	// Unknown side should return 1.0.
	unknownMult := ft.NotionalMultiplierSide("unknown")
	if unknownMult != 1.0 {
		t.Errorf("expected 1.0 for unknown side, got %f", unknownMult)
	}
}
