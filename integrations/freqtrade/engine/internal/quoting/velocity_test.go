package quoting

import (
	"testing"
	"time"
)

func TestVelocityTrackerInitialState(t *testing.T) {
	vt := NewTradeVelocityTracker(5, 60, 2.0, 0.5, 2, 30)

	// Before any trades, should return base levels and ratio 1.0.
	if levels := vt.EffectiveLevels(2); levels != 2 {
		t.Errorf("expected 2 levels before any trades, got %d", levels)
	}
	if ratio := vt.RateRatio(); ratio != 1.0 {
		t.Errorf("expected ratio 1.0 before any trades, got %f", ratio)
	}
	if vt.IsBurst() {
		t.Error("expected no burst before any trades")
	}
}

func TestVelocityTrackerNeedsMinTrades(t *testing.T) {
	// Tracker needs 5 trades before producing a non-1.0 ratio.
	vt := NewTradeVelocityTracker(5, 60, 2.0, 0.5, 2, 30)

	for i := 0; i < 4; i++ {
		vt.RecordTrade()
		time.Sleep(time.Millisecond)
	}

	if ratio := vt.RateRatio(); ratio != 1.0 {
		t.Errorf("expected ratio 1.0 with <5 trades, got %f", ratio)
	}
}

func TestVelocityTrackerSteadyState(t *testing.T) {
	// Trades arriving at a steady rate should produce ratio ~1.0.
	// Use similar half-lives so both EWMAs converge at similar speed.
	vt := NewTradeVelocityTracker(1.0, 2.0, 2.0, 0.1, 2, 30)

	// Send 30 trades at ~100ms intervals (10/sec) over 3 seconds.
	// Both EWMAs should converge to ~10/sec.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(100 * time.Millisecond)
	}

	ratio := vt.RateRatio()
	// Both EWMAs should converge to similar values, ratio near 1.0.
	// Allow generous tolerance since EWMA convergence isn't perfect in 3s.
	if ratio < 0.3 || ratio > 3.0 {
		t.Errorf("expected ratio roughly near 1.0 at steady state, got %f", ratio)
	}

	if levels := vt.EffectiveLevels(2); levels != 2 {
		t.Errorf("expected base 2 levels at steady state, got %d", levels)
	}
}

func TestVelocityTrackerBurstActivation(t *testing.T) {
	// Fast half-life very short so it responds quickly; slow half-life long so baseline is low.
	vt := NewTradeVelocityTracker(0.1, 10, 2.0, 0.1, 2, 30)

	// Seed with slow trades to establish baseline.
	for i := 0; i < 6; i++ {
		vt.RecordTrade()
		time.Sleep(200 * time.Millisecond)
	}

	// Now send a burst of rapid trades.
	for i := 0; i < 20; i++ {
		vt.RecordTrade()
		time.Sleep(5 * time.Millisecond)
	}

	ratio := vt.RateRatio()
	if ratio < 2.0 {
		t.Errorf("expected ratio >= 2.0 after burst, got %f", ratio)
	}

	if !vt.IsBurst() {
		t.Error("expected burst to be active after rapid trades")
	}

	if levels := vt.EffectiveLevels(2); levels != 4 {
		t.Errorf("expected 4 levels during burst (2*2), got %d", levels)
	}
}

func TestVelocityTrackerBurstDecay(t *testing.T) {
	// Use very short decay for testing.
	vt := NewTradeVelocityTracker(0.05, 10, 2.0, 0.1, 2, 0.5) // 500ms decay

	// Seed baseline.
	for i := 0; i < 6; i++ {
		vt.RecordTrade()
		time.Sleep(200 * time.Millisecond)
	}

	// Burst of rapid trades.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(2 * time.Millisecond)
	}

	// Should be in burst.
	if levels := vt.EffectiveLevels(2); levels != 4 {
		t.Errorf("expected 4 levels during burst, got %d", levels)
	}

	// Wait for fast EWMA to decay (fast half-life is 50ms, after 500ms = 10 half-lives).
	time.Sleep(600 * time.Millisecond)

	// Burst should be deactivated now; check that decay is happening.
	levels := vt.EffectiveLevels(2)
	// Immediately after deactivation, we might be mid-decay or done.
	// After 600ms with 500ms decay, should be near or at base.
	if levels < 2 || levels > 4 {
		t.Errorf("expected levels between 2 and 4 during/after decay, got %d", levels)
	}

	// Wait for full decay.
	time.Sleep(600 * time.Millisecond)
	levels = vt.EffectiveLevels(2)
	if levels != 2 {
		t.Errorf("expected 2 levels after full decay, got %d", levels)
	}
}

func TestVelocityTrackerMinFloorPreventsSpuriousTrigger(t *testing.T) {
	// With high minFloor, even a fast rate won't trigger burst if slow rate is low.
	vt := NewTradeVelocityTracker(0.1, 60, 2.0, 100.0, 2, 30) // minFloor = 100 trades/sec

	// Send some trades.
	for i := 0; i < 10; i++ {
		vt.RecordTrade()
		time.Sleep(10 * time.Millisecond)
	}

	// Even though fast rate >> slow rate, the floor makes the denominator 100,
	// so the ratio stays well below threshold.
	if vt.IsBurst() {
		t.Error("expected no burst when minFloor is very high")
	}
}

func TestVelocityTrackerReactivatesDuringDecay(t *testing.T) {
	// If velocity spikes again during decay, burst should reactivate immediately.
	vt := NewTradeVelocityTracker(0.05, 10, 2.0, 0.1, 2, 2.0) // 2s decay

	// Seed baseline.
	for i := 0; i < 6; i++ {
		vt.RecordTrade()
		time.Sleep(200 * time.Millisecond)
	}

	// First burst.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(2 * time.Millisecond)
	}

	// Wait for fast EWMA to decay so burst deactivates.
	time.Sleep(400 * time.Millisecond)

	// Check we're in decay phase.
	levels := vt.EffectiveLevels(2)
	if levels < 2 {
		t.Fatalf("expected levels >= 2 during decay, got %d", levels)
	}

	// Second burst.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(2 * time.Millisecond)
	}

	// Should reactivate at full burst levels.
	levels = vt.EffectiveLevels(2)
	if levels != 4 {
		t.Errorf("expected 4 levels after reactivation, got %d", levels)
	}
}

func TestVelocityTrackerLevelMultiplier(t *testing.T) {
	// Test different level multipliers.
	vt := NewTradeVelocityTracker(0.05, 10, 2.0, 0.1, 3, 30) // 3x multiplier

	// Seed baseline.
	for i := 0; i < 6; i++ {
		vt.RecordTrade()
		time.Sleep(200 * time.Millisecond)
	}

	// Burst.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(2 * time.Millisecond)
	}

	if levels := vt.EffectiveLevels(2); levels != 6 {
		t.Errorf("expected 6 levels during burst (2*3), got %d", levels)
	}
	if levels := vt.EffectiveLevels(3); levels != 9 {
		t.Errorf("expected 9 levels during burst (3*3), got %d", levels)
	}
}

func TestVelocityTrackerDiagState(t *testing.T) {
	vt := NewTradeVelocityTracker(5, 60, 2.0, 0.5, 2, 30)

	snap := vt.Snapshot(2)
	if snap.RateRatio != 1.0 {
		t.Errorf("expected snapshot ratio 1.0 initially, got %f", snap.RateRatio)
	}
	if snap.IsBurst {
		t.Error("expected snapshot isBurst false initially")
	}
	if snap.EffectiveLevels != 2 {
		t.Errorf("expected snapshot levels 2 initially, got %d", snap.EffectiveLevels)
	}
}

func TestVelocityTrackerSnapshotConsistency(t *testing.T) {
	// Verify that Snapshot returns consistent values from a single read,
	// unlike calling RateRatio/IsBurst/EffectiveLevels separately.
	vt := NewTradeVelocityTracker(0.05, 10, 2.0, 0.1, 2, 30)

	// Seed baseline.
	for i := 0; i < 6; i++ {
		vt.RecordTrade()
		time.Sleep(200 * time.Millisecond)
	}

	// Burst.
	for i := 0; i < 30; i++ {
		vt.RecordTrade()
		time.Sleep(2 * time.Millisecond)
	}

	snap := vt.Snapshot(2)
	// During active burst, all three values should be consistent.
	if snap.IsBurst && snap.EffectiveLevels != 4 {
		t.Errorf("burst active but levels=%d (expected 4)", snap.EffectiveLevels)
	}
	if snap.RateRatio < 2.0 && snap.IsBurst {
		t.Errorf("ratio %.2f < threshold but isBurst=true", snap.RateRatio)
	}
	if snap.RateRatio >= 2.0 && !snap.IsBurst {
		t.Errorf("ratio %.2f >= threshold but isBurst=false", snap.RateRatio)
	}
}

func TestVelocityTrackerConcurrentSafety(t *testing.T) {
	vt := NewTradeVelocityTracker(0.1, 5, 2.0, 0.5, 2, 30)

	done := make(chan struct{})

	// Writer goroutine.
	go func() {
		for i := 0; i < 100; i++ {
			vt.RecordTrade()
			time.Sleep(time.Millisecond)
		}
		close(done)
	}()

	// Reader goroutine (concurrent reads while writes happen).
	for i := 0; i < 50; i++ {
		_ = vt.RateRatio()
		_ = vt.IsBurst()
		_ = vt.EffectiveLevels(2)
		_ = vt.Snapshot(2)
		time.Sleep(2 * time.Millisecond)
	}

	<-done
}

// TestBuildSideOrdersMinNotionalWithBurstLevels verifies that buildSideOrders
// correctly limits the number of levels when notional is too low to support
// the burst-requested level count (each level must be >= $10).
func TestBuildSideOrdersMinNotionalWithBurstLevels(t *testing.T) {
	// With $25 notional and 4 requested levels:
	// $25 / 4 = $6.25 per level, below $10 minimum.
	// buildSideOrders should cap at floor(25/10) = 2 levels.
	orders := buildSideOrders(
		"xyz:CL", "buy",
		98.00,  // startPrice
		98.10,  // mid
		7.0,    // spacingBps
		25.0,   // allowedNotional ($25 — can only support 2 levels at $10 min)
		4,      // levels (burst would request 4)
		97.90,  // bestBid
		98.20,  // bestAsk
		1000.0, // pow10Mult (3 decimal places)
		"test",
		1.0,   // graduation
		1.0,   // spacingExponent
		nil,   // bookLevels
		false, // wallFrontEnabled
		3.0,   // wallMinSizeMultiplier
		nil,   // wallLog
	)

	if len(orders) > 2 {
		t.Errorf("expected <= 2 orders with $25 notional (min $10/level), got %d", len(orders))
	}
	if len(orders) == 0 {
		t.Error("expected at least 1 order with $25 notional")
	}

	// Verify each order meets minimum notional.
	for i, o := range orders {
		notional := o.Price * o.Size
		if notional < 9.9 { // slight tolerance for rounding
			t.Errorf("order %d notional $%.2f below $10 minimum", i, notional)
		}
	}
}

// TestBuildSideOrdersBurstDoublesSafely verifies that when burst doubles levels
// and there's enough notional, we actually get double the levels.
func TestBuildSideOrdersBurstDoublesSafely(t *testing.T) {
	// $220 notional with 4 levels = $55 per level. Well above $10 minimum.
	orders := buildSideOrders(
		"xyz:CL", "buy",
		98.00,  // startPrice
		98.10,  // mid
		7.0,    // spacingBps
		220.0,  // allowedNotional
		4,      // levels (burst: 2 base * 2)
		97.90,  // bestBid
		98.20,  // bestAsk
		1000.0, // pow10Mult
		"test",
		1.05, // graduation
		1.2,  // spacingExponent
		nil,
		false,
		3.0,
		nil, // wallLog
	)

	if len(orders) != 4 {
		t.Errorf("expected 4 orders with $220 notional and 4 burst levels, got %d", len(orders))
	}

	// Verify total notional is approximately $220.
	totalNotional := 0.0
	for _, o := range orders {
		totalNotional += o.Price * o.Size
	}
	// Allow 10% tolerance for rounding.
	if totalNotional < 200 || totalNotional > 240 {
		t.Errorf("expected total notional ~$220, got $%.2f", totalNotional)
	}
}
