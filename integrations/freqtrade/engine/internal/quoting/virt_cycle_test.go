package quoting

import (
	"math"
	"testing"
	"time"
)

func TestVirtCycleEngine_InitialHeadroom(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	h := vc.Headroom()
	if h != 0.0 {
		t.Errorf("expected headroom 0.0 before seeding, got %f", h)
	}

	bp := vc.BudgetPressure()
	if bp != 0.5 {
		t.Errorf("expected budget pressure 0.5 when headroom=0, got %f", bp)
	}
}

func TestVirtCycleEngine_PositiveHeadroom(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	// Seed the engine so seeded=true.
	vc.RecordVolume(100)

	// Directly set rates: volume regenerates faster than API burns.
	vc.mu.Lock()
	vc.volumeRate = 500 // 500 $/min => 500 req/min regen
	vc.burnRate = 100   // 100 API calls/min burn
	vc.mu.Unlock()

	h := vc.Headroom()
	// netBurn = 100 - 500 = -400; headroom = -(-400)/100 = 4.0 => clamped to 1.0
	if h <= 0 {
		t.Errorf("expected positive headroom when volumeRate > burnRate, got %f", h)
	}
	if h != 1.0 {
		t.Errorf("expected headroom clamped to 1.0, got %f", h)
	}
}

func TestVirtCycleEngine_NegativeHeadroom(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	// Seed the engine.
	vc.RecordVolume(1)

	// Directly set rates: API burns far exceed volume regen.
	vc.mu.Lock()
	vc.volumeRate = 10 // 10 $/min regen
	vc.burnRate = 100  // 100 API calls/min burn
	vc.mu.Unlock()

	h := vc.Headroom()
	// netBurn = 100 - 10 = 90; headroom = -90/100 = -0.9
	if h >= 0 {
		t.Errorf("expected negative headroom when burnRate >> volumeRate, got %f", h)
	}
	expected := -0.9
	if math.Abs(h-expected) > 0.01 {
		t.Errorf("expected headroom ~%f, got %f", expected, h)
	}
}

func TestVirtCycleEngine_BudgetPressure(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	// Seed the engine.
	vc.RecordVolume(100)

	// Positive headroom scenario: volume >> burn.
	vc.mu.Lock()
	vc.volumeRate = 500
	vc.burnRate = 100
	vc.mu.Unlock()

	bp := vc.BudgetPressure()
	// headroom = 1.0 (clamped), pressure = (1 - 1.0) / 2 = 0.0
	if bp > 0.1 {
		t.Errorf("expected low budget pressure with positive headroom, got %f", bp)
	}

	// Negative headroom scenario: burn >> volume.
	vc.mu.Lock()
	vc.volumeRate = 10
	vc.burnRate = 100
	vc.mu.Unlock()

	bp = vc.BudgetPressure()
	// headroom = -0.9, pressure = (1 - (-0.9)) / 2 = 0.95
	if bp < 0.5 {
		t.Errorf("expected high budget pressure with negative headroom, got %f", bp)
	}
	expected := 0.95
	if math.Abs(bp-expected) > 0.01 {
		t.Errorf("expected budget pressure ~%f, got %f", expected, bp)
	}
}

func TestVirtCycleEngine_CumVolume(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	vc.RecordVolume(100)
	vc.RecordVolume(250)
	vc.RecordVolume(50)

	cum := vc.CumVolumeUSD()
	if cum != 400 {
		t.Errorf("expected cumulative volume 400, got %f", cum)
	}
}

func TestVirtCycleEngine_RecordAPICall(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	vc.RecordAPICall()
	br := vc.BurnRate()
	if br <= 0 {
		t.Errorf("expected positive burn rate after RecordAPICall, got %f", br)
	}
}

// V30 part 5: once RecordRegen has been called, Headroom uses the observed
// exchange-side regenRate (req/min) instead of the legacy volumeRate proxy
// (USD/min). V29 session observed a 10% regen ratio — the old path with
// volumeRate=$167/min and burnRate=20/min pegged headroom at 1.0, masking a
// session drain of 1 994 requests. With RecordRegen fed actual cap growth
// (7.5/min at 10% ratio), headroom reflects reality.
func TestVirtCycleEngine_RegenRateDominatesHeadroom(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	// Seed both paths.
	vc.RecordVolume(100)
	vc.RecordRegen(1)

	// Simulate V29 numbers: USD volume high, but actual regen low.
	vc.mu.Lock()
	vc.volumeRate = 166.63 // USD/min (V29 end)
	vc.regenRate = 7.5     // req/min (V29 observed cap growth rate)
	vc.burnRate = 10.6     // calls/min (V29 average)
	vc.mu.Unlock()

	h := vc.Headroom()
	if h > 0 {
		t.Errorf("expected negative headroom when regenRate (7.5) < burnRate (10.6), got %f — volumeRate (166.63) should NOT dominate", h)
	}

	// Now disable the observed path — fall back to volumeRate proxy. Headroom
	// should flip positive (mis-representing reality), confirming the switch.
	vc.mu.Lock()
	vc.regenSeeded = false
	vc.mu.Unlock()

	h2 := vc.Headroom()
	if h2 <= 0 {
		t.Errorf("legacy path: expected positive headroom when volumeRate (166.63) dominates burnRate (10.6), got %f", h2)
	}
}

// V30 part 5: RegenRatio surfaces the empirical cap-growth-per-USD-traded
// ratio. V29 observed ~0.10 despite docs claiming 1.0.
func TestVirtCycleEngine_RegenRatio(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)
	if r := vc.RegenRatio(); r != 0 {
		t.Errorf("expected ratio 0 before any data, got %f", r)
	}
	vc.RecordVolume(1000)
	vc.RecordRegen(100)
	r := vc.RegenRatio()
	if math.Abs(r-0.10) > 1e-9 {
		t.Errorf("expected ratio 0.10 (100 reqs / 1000 USD), got %f", r)
	}
	if c := vc.CumRegenReqs(); c != 100 {
		t.Errorf("expected CumRegenReqs=100, got %d", c)
	}
}

func TestVirtCycleEngine_EWMAAlpha(t *testing.T) {
	vc := NewVirtCycleEngine(60 * time.Second)

	alpha := vc.ewmaAlpha(60 * time.Second)
	// At halfLife, alpha = 1 - 2^(-1) = 0.5
	if math.Abs(alpha-0.5) > 0.001 {
		t.Errorf("expected alpha ~0.5 at half-life, got %f", alpha)
	}

	alpha0 := vc.ewmaAlpha(0)
	if alpha0 != 0.0 {
		t.Errorf("expected alpha 0.0 at zero elapsed, got %f", alpha0)
	}
}
