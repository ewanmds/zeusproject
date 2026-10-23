package risk

import (
	"context"
	"math"
	"sync/atomic"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

func TestPositionAt80PctNoAction(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
		},
	}

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Position at 90% of limit: 450 notional — below 100% limit, no action expected
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "long", Size: 6.428, Notional: 450, AvgEntry: 70.0,
	}

	select {
	case action := <-riskCh:
		t.Fatalf("expected no risk action for 90%% of limit, got %s", action)
	case <-time.After(200 * time.Millisecond):
		// Good: no action
	}
}

func TestMaxPositionNotionalHalt(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
		},
	}

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Position at limit: 500 notional
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "long", Size: 7.142, Notional: 500, AvgEntry: 70.0,
	}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for risk action")
	}
}

func TestMaxDrawdownHalt(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Let the manager start and record high-water mark
	time.Sleep(50 * time.Millisecond)

	// Simulate drawdown: equity drops by 5.1%
	equity.Store(math.Float64bits(9490.0)) // (10000 - 9490) / 10000 = 5.1%

	// Send a benign position update to trigger evaluation
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0,
	}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for risk action")
	}
}

func TestResumeAfterHalt(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
		},
	}

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Trigger halt via position limit
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "long", Size: 8.0, Notional: 560, AvgEntry: 70.0,
	}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT first, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}

	// Positions cleared
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "", Size: 0, Notional: 0, AvgEntry: 0,
	}

	select {
	case action := <-riskCh:
		if action != types.RiskResume {
			t.Fatalf("expected RESUME after halt, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for resume")
	}
}

// Test that drawdown-triggered halt stays halted (sticky) when cooldown == 0.
func TestDrawdownHaltStaysHalted(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 0, // stay halted forever
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Trigger drawdown: equity drops > 5%
	equity.Store(math.Float64bits(9490.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}

	// Positions cleared — should NOT resume, should HALT (drawdown is sticky).
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT (sticky drawdown), got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}
}

// Test that drawdown-triggered halt resumes after cooldown expires.
func TestDrawdownCooldownResume(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 1, // 1 second cooldown
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Trigger drawdown.
	equity.Store(math.Float64bits(9490.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}

	// Positions cleared immediately — should stay HALT (cooldown not expired yet).
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT (cooldown not expired), got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}

	// Wait for cooldown to expire, then trigger evaluation.
	time.Sleep(1100 * time.Millisecond)
	equity.Store(math.Float64bits(9490.0)) // equity unchanged
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	select {
	case action := <-riskCh:
		if action != types.RiskResumeFromDrawdown {
			t.Fatalf("expected RESUME_FROM_DRAWDOWN after cooldown, got %s", action)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for resume after cooldown")
	}
}

// Test that position-limit halt still auto-resumes (not sticky).
func TestPositionLimitStillAutoResumes(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 0, // irrelevant — position-limit halt is not drawdown
		},
	}

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Trigger halt via position limit (not drawdown).
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 8.0, Notional: 560, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action != types.RiskHalt {
			t.Fatalf("expected HALT, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for halt")
	}

	// Positions cleared — should auto-resume (not sticky).
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	select {
	case action := <-riskCh:
		if action != types.RiskResume {
			t.Fatalf("expected RESUME (position-limit, not drawdown), got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for resume")
	}
}

func TestSmallPositionNoAction(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      5.0,
			MaxOpenOrders:       20,
		},
	}

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Small position, well within limits
	posCh <- types.PositionUpdate{
		Coin: "xyz:CL", Side: "long", Size: 1.0, Notional: 70.0, AvgEntry: 70.0,
	}

	// No risk action expected
	select {
	case action := <-riskCh:
		t.Fatalf("expected no risk action for small position, got %s", action)
	case <-time.After(200 * time.Millisecond):
		// Good: no action
	}
}

// expectAction is a shared helper for the consecutive halt / equity threshold tests.
func expectAction(t *testing.T, riskCh <-chan types.RiskAction, expected types.RiskAction, label string) {
	t.Helper()
	select {
	case action := <-riskCh:
		if action != expected {
			t.Fatalf("[%s] expected %s, got %s", label, expected, action)
		}
	case <-time.After(3 * time.Second):
		t.Fatalf("[%s] timed out waiting for %s", label, expected)
	}
}

// TestConsecutiveHaltEscalation verifies that after MaxConsecutiveHalts drawdown
// halt cycles, the manager stays permanently halted.
func TestConsecutiveHaltEscalation(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      10.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 1,
			MaxConsecutiveHalts: 2,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// --- First halt cycle ---
	// Trigger drawdown: equity drops > 10%
	equity.Store(math.Float64bits(8990.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "1st-halt")

	// Positions cleared → still HALT (cooldown not expired)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "1st-cooldown-halt")

	// Wait for cooldown to expire, then trigger evaluation → RESUME_FROM_DRAWDOWN (haltCount=1 < max=2)
	time.Sleep(1100 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskResumeFromDrawdown, "1st-resume")

	// --- Second halt cycle (immediately, no successful run) ---
	// High-water was reset to 8990 on resume, drop > 10% from 8990 → 8090
	equity.Store(math.Float64bits(8080.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "2nd-halt")

	// Positions cleared → still HALT (cooldown)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "2nd-cooldown-halt")

	// Wait for cooldown to expire → should emit HALT (permanent, haltCount=2 >= max=2).
	// With emitActionForce, the HALT is re-emitted even though lastAction is already HALT.
	time.Sleep(1100 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	expectAction(t, riskCh, types.RiskHalt, "permanent-halt")
}

// TestEquityThresholdBlocksResume verifies that resume is blocked when equity
// is below MinResumeEquityPct of the high-water mark.
func TestEquityThresholdBlocksResume(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      10.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 1,
			MinResumeEquityPct:  95.0,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Trigger drawdown: equity drops > 10% (10000 → 8990)
	equity.Store(math.Float64bits(8990.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "drawdown-halt")

	// Positions cleared → still HALT (cooldown)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "cooldown-halt")

	// Wait for cooldown to expire. Equity still 8990 < 10000*0.95=9500 → HALT re-emitted.
	// With emitActionForce, the HALT is re-emitted even though lastAction is already HALT.
	time.Sleep(1100 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	expectAction(t, riskCh, types.RiskHalt, "equity-too-low-halt")
}

// TestEquityRecoveryAllowsResume verifies that resume proceeds when equity
// recovers above MinResumeEquityPct of the high-water mark.
func TestEquityRecoveryAllowsResume(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      10.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 1,
			MinResumeEquityPct:  90.0,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Trigger drawdown: equity drops > 10% (10000 → 8990)
	equity.Store(math.Float64bits(8990.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "drawdown-halt")

	// Positions cleared → still HALT (cooldown)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "cooldown-halt")

	// Wait for cooldown. Equity recovers to 9100 > 10000*0.90=9000 → RESUME_FROM_DRAWDOWN
	time.Sleep(1100 * time.Millisecond)
	equity.Store(math.Float64bits(9100.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskResumeFromDrawdown, "equity-recovery-resume")
}

// TestSuccessfulRunResetsHaltCount verifies that a "successful run" (time since
// last resume >= cooldown) resets the consecutive halt counter.
func TestSuccessfulRunResetsHaltCount(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      10.0,
			MaxOpenOrders:       20,
			DrawdownCooldownSec: 1,
			MaxConsecutiveHalts: 2,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(10000.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// --- First halt cycle ---
	equity.Store(math.Float64bits(8990.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "1st-halt")

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "1st-cooldown-halt")

	time.Sleep(1100 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskResumeFromDrawdown, "1st-resume")

	// --- Successful run: wait >= cooldown_sec after resume ---
	time.Sleep(1100 * time.Millisecond)

	// --- Second halt cycle (after successful run, haltCount should reset) ---
	// High-water was reset to 8990 on first resume. Drop > 10%: 8990 → 8080
	equity.Store(math.Float64bits(8080.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "2nd-halt")

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	expectAction(t, riskCh, types.RiskHalt, "2nd-cooldown-halt")

	time.Sleep(1100 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}
	// Should RESUME_FROM_DRAWDOWN because haltCount was reset by the successful run
	expectAction(t, riskCh, types.RiskResumeFromDrawdown, "2nd-resume-after-reset")
}

func TestAbsoluteEquityFloorPermanentHalt(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:      500,
			MaxSpreadExposure:        1000,
			MaxDrawdownPct:           99.0, // very high to isolate floor test from drawdown
			MaxOpenOrders:            20,
			AbsoluteEquityFloor:      100,
			DrawdownSmoothingSamples: 1, // no smoothing for this test
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(200.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Drop equity below floor — requires 3 consecutive breaches for permanent halt.
	equity.Store(math.Float64bits(90.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	// Breach 1 and 2 — no halt yet (confirmation window).
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	expectAction(t, riskCh, types.RiskHalt, "equity-floor-halt")
}

func TestAbsoluteEquityFloorNoResumeAfterBreach(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:      500,
			MaxSpreadExposure:        1000,
			MaxDrawdownPct:           99.0, // very high to isolate floor test from drawdown
			MaxOpenOrders:            20,
			AbsoluteEquityFloor:      100,
			DrawdownSmoothingSamples: 1,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(200.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Drop below floor — 3 breaches to trigger permanent halt
	equity.Store(math.Float64bits(90.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	expectAction(t, riskCh, types.RiskHalt, "floor-breach")

	// Equity recovers above floor
	equity.Store(math.Float64bits(200.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "", Size: 0, Notional: 0}

	// Should still be halted (permanent)
	expectAction(t, riskCh, types.RiskHalt, "permanent-halt-after-recovery")
}

func TestAbsoluteEquityFloorDisabled(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
			MaxDrawdownPct:      50.0,
			MaxOpenOrders:       20,
			AbsoluteEquityFloor: 0, // disabled
		},
	}

	equity := 50.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Small position, no drawdown — should not halt even with low equity
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		t.Fatalf("expected no risk action with floor disabled, got %s", action)
	case <-time.After(200 * time.Millisecond):
		// Good: no action
	}
}

// TestFloorBreachConfirmationWindow verifies that a single phantom reading
// below the floor does NOT trigger permanent halt (requires 3 consecutive).
func TestFloorBreachConfirmationWindow(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:      500,
			MaxSpreadExposure:        1000,
			MaxDrawdownPct:           99.0, // very high to isolate floor test from drawdown
			MaxOpenOrders:            20,
			AbsoluteEquityFloor:      100,
			DrawdownSmoothingSamples: 1,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(200.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Single phantom dip below floor — should NOT trigger permanent halt.
	equity.Store(math.Float64bits(90.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	// Equity recovers before 3 consecutive breaches.
	time.Sleep(50 * time.Millisecond)
	equity.Store(math.Float64bits(200.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	// No halt should have been emitted.
	select {
	case action := <-riskCh:
		t.Fatalf("expected no risk action after single phantom breach, got %s", action)
	case <-time.After(300 * time.Millisecond):
		// Good: no action
	}
}

// TestSmoothedHighWaterMark verifies that high-water uses smoothed equity
// (phantom spikes are dampened and don't inflate the threshold).
func TestSmoothedHighWaterMark(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:      500,
			MaxSpreadExposure:        1000,
			MaxDrawdownPct:           10.0,
			MaxOpenOrders:            20,
			DrawdownSmoothingSamples: 10, // heavy smoothing
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(500.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// Inject a phantom spike — equity briefly reads 700 (phantom).
	equity.Store(math.Float64bits(700.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	time.Sleep(50 * time.Millisecond)

	// Equity returns to 500 — with smoothing, the EMA should not have reached 700.
	// So a reading of 500 should NOT trigger a 10% drawdown from 700.
	equity.Store(math.Float64bits(500.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	// With raw high-water at 700, a drop to 500 = 28.6% drawdown → would halt.
	// With smoothed high-water (EMA-dampened), the spike doesn't inflate HWM.
	select {
	case action := <-riskCh:
		t.Fatalf("expected no halt after phantom spike + return to normal, got %s", action)
	case <-time.After(300 * time.Millisecond):
		// Good: no drawdown halt
	}
}

// TestFloorBreachResetsOnRecovery verifies that the breach counter resets
// when equity recovers above floor mid-way through the confirmation window.
func TestFloorBreachResetsOnRecovery(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:      500,
			MaxSpreadExposure:        1000,
			MaxDrawdownPct:           99.0, // very high to isolate floor test from drawdown
			MaxOpenOrders:            20,
			AbsoluteEquityFloor:      100,
			DrawdownSmoothingSamples: 1,
		},
	}

	var equity atomic.Uint64
	equity.Store(math.Float64bits(200.0))
	equityFn := func() float64 { return math.Float64frombits(equity.Load()) }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	rm := New(cfg, posCh, riskCh, equityFn, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go rm.Run(ctx)

	time.Sleep(50 * time.Millisecond)

	// 2 breaches, then recovery, then 2 more breaches — should NOT trigger halt
	// because counter resets on recovery.
	equity.Store(math.Float64bits(90.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	time.Sleep(20 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	time.Sleep(20 * time.Millisecond)

	// Recover
	equity.Store(math.Float64bits(200.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	time.Sleep(20 * time.Millisecond)

	// 2 more breaches (counter reset, so only 2 not 4)
	equity.Store(math.Float64bits(90.0))
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	time.Sleep(20 * time.Millisecond)
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		t.Fatalf("expected no halt (breach counter reset by recovery), got %s", action)
	case <-time.After(300 * time.Millisecond):
		// Good: no action
	}
}

// --- P3: Session stop-loss tests ---

// fakeSessionPnL is a test double satisfying risk.SessionPnLReader.
type fakeSessionPnL struct {
	bps        atomic.Uint64 // float64 bits
	elapsedSec float64
}

func (f *fakeSessionPnL) setBps(v float64)        { f.bps.Store(math.Float64bits(v)) }
func (f *fakeSessionPnL) PnL() (float64, float64) { return 0, math.Float64frombits(f.bps.Load()) }
func (f *fakeSessionPnL) ElapsedSec() float64     { return f.elapsedSec }

func baseSessionStopCfg(bps float64, pauseSec int) *config.Config {
	return &config.Config{
		Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
		Risk: config.RiskConfig{
			MaxPositionNotional:                500,
			MaxSpreadExposure:                  1000,
			MaxDrawdownPct:                     50.0,
			MaxOpenOrders:                      20,
			SessionStopLossBps:                 bps,
			SessionStopLossPauseSec:            pauseSec,
			SessionStopLossRecoveryNotionalPct: 0.5,
			SessionStopLossRecoveryDurationSec: 120,
		},
	}
}

func TestSessionStopLossTriggersAtThreshold(t *testing.T) {
	cfg := baseSessionStopCfg(-40.0, 60)
	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	fake := &fakeSessionPnL{elapsedSec: 120}
	fake.setBps(-50.0) // below -40 floor

	rm := New(cfg, posCh, riskCh, equityFn, fake)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	// Trigger evaluation with a benign position update.
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action != types.RiskSessionStopLoss {
			t.Fatalf("expected SESSION_STOP_LOSS, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for session stop-loss action")
	}
}

func TestSessionStopLossDoesNotRetriggerDuringPause(t *testing.T) {
	cfg := baseSessionStopCfg(-40.0, 60)
	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	fake := &fakeSessionPnL{}
	fake.setBps(-100.0)

	rm := New(cfg, posCh, riskCh, equityFn, fake)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go rm.Run(ctx)

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	// First action must be session stop-loss.
	select {
	case action := <-riskCh:
		if action != types.RiskSessionStopLoss {
			t.Fatalf("expected SESSION_STOP_LOSS first, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for session stop-loss")
	}

	// Subsequent evaluations during pause emit forced HALTs, never a second SESSION_STOP_LOSS.
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}
	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	drainDeadline := time.After(300 * time.Millisecond)
	for {
		select {
		case action := <-riskCh:
			if action == types.RiskSessionStopLoss {
				t.Fatalf("unexpected duplicate SESSION_STOP_LOSS during pause")
			}
			// HALT is acceptable (forced re-assertion).
		case <-drainDeadline:
			return
		}
	}
}

func TestSessionStopLossDisabledAtZero(t *testing.T) {
	cfg := baseSessionStopCfg(0, 0)
	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	fake := &fakeSessionPnL{}
	fake.setBps(-500.0) // very adverse, but disabled

	rm := New(cfg, posCh, riskCh, equityFn, fake)

	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()
	go rm.Run(ctx)

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action == types.RiskSessionStopLoss {
			t.Fatalf("expected no SESSION_STOP_LOSS when disabled, got %s", action)
		}
	case <-time.After(300 * time.Millisecond):
		// Good: no stop-loss action
	}
}

func TestSessionStopLossAboveThresholdNoOp(t *testing.T) {
	cfg := baseSessionStopCfg(-40.0, 60)
	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	fake := &fakeSessionPnL{}
	fake.setBps(-30.0) // above floor

	rm := New(cfg, posCh, riskCh, equityFn, fake)

	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()
	go rm.Run(ctx)

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	select {
	case action := <-riskCh:
		if action == types.RiskSessionStopLoss {
			t.Fatalf("expected no trigger above floor, got %s", action)
		}
	case <-time.After(300 * time.Millisecond):
	}
}

func TestSessionStopLossPauseExpiresToResume(t *testing.T) {
	cfg := baseSessionStopCfg(-40.0, 60)
	// Override: use a 1-second pause (validation is enforced in LoadConfig,
	// not in the Manager; we can inject any value directly).
	cfg.Risk.SessionStopLossPauseSec = 1

	equity := 10000.0
	equityFn := func() float64 { return equity }

	posCh := make(chan types.PositionUpdate, 100)
	riskCh := make(chan types.RiskAction, 100)

	fake := &fakeSessionPnL{}
	fake.setBps(-100.0)

	rm := New(cfg, posCh, riskCh, equityFn, fake)

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	go rm.Run(ctx)

	posCh <- types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.1, Notional: 7.0, AvgEntry: 70.0}

	// Consume the stop-loss trigger.
	select {
	case action := <-riskCh:
		if action != types.RiskSessionStopLoss {
			t.Fatalf("expected SESSION_STOP_LOSS, got %s", action)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for trigger")
	}

	// Improve PnL above floor so the re-evaluation path is only gated on the pause timer.
	fake.setBps(-10.0)

	// Wait for the pause (1s) plus the 5s equity ticker to hit.
	deadline := time.After(7 * time.Second)
	for {
		select {
		case action := <-riskCh:
			if action == types.RiskResumeFromSessionStop {
				return
			}
			// HALTs may arrive during the pause — ignore them.
		case <-deadline:
			t.Fatal("timed out waiting for RESUME_FROM_SESSION_STOP")
		}
	}
}
