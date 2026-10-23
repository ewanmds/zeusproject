package quoting

import (
	"testing"
)

// TestFourthDim_DefaultBudgetPressure verifies that with FourthDimension
// unset (""), the 4th-axis state carries inputs.BudgetPressure and ignores
// SessionPnLBps — bit-exactly v22 behavior.
func TestFourthDim_DefaultBudgetPressure(t *testing.T) {
	cfg := defaultV2Config()
	cfg.FourthDimension = ""
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := neutralInputs()
	in.BudgetPressure = 0.42
	in.SessionPnLBps = -50.0 // would map to 1.0 under session_pnl

	state := opt.computeState(in)
	if state.BudgetPressure != 0.42 {
		t.Fatalf("default mode: expected BudgetPressure=0.42, got %g", state.BudgetPressure)
	}
}

// TestFourthDim_SessionPnLBucketization verifies the bucket mapping for
// default edges [-20, -10, 10, 20].
func TestFourthDim_SessionPnLBucketization(t *testing.T) {
	cfg := defaultV2Config()
	cfg.FourthDimension = "session_pnl"
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	cases := []struct {
		bps  float64
		want float64
	}{
		{-50, 1.0},  // very adverse
		{-20, 0.75}, // boundary — "< -10" -> adverse
		{-15, 0.75}, // adverse
		{-10, 0.50}, // boundary — neutral
		{0, 0.50},   // neutral
		{10, 0.50},  // boundary — neutral
		{15, 0.25},  // good
		{20, 0.25},  // boundary — good
		{50, 0.0},   // very good
	}
	for _, tc := range cases {
		in := neutralInputs()
		in.BudgetPressure = 0.0 // must be ignored
		in.SessionPnLBps = tc.bps
		state := opt.computeState(in)
		if state.BudgetPressure != tc.want {
			t.Errorf("session_pnl bps=%g: expected %g, got %g", tc.bps, tc.want, state.BudgetPressure)
		}
	}
}

// TestFourthDim_CustomBucketEdges verifies that a caller-provided edge list
// is honored.
func TestFourthDim_CustomBucketEdges(t *testing.T) {
	cfg := defaultV2Config()
	cfg.FourthDimension = "session_pnl"
	cfg.SessionPnLBucketEdgesBps = []float64{-40, -20, 20, 40}
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	cases := []struct {
		bps  float64
		want float64
	}{
		{-100, 1.0},
		{-30, 0.75},
		{0, 0.50},
		{30, 0.25},
		{100, 0.0},
	}
	for _, tc := range cases {
		in := neutralInputs()
		in.SessionPnLBps = tc.bps
		state := opt.computeState(in)
		if state.BudgetPressure != tc.want {
			t.Errorf("custom edges bps=%g: expected %g, got %g", tc.bps, tc.want, state.BudgetPressure)
		}
	}
}

// TestFourthDim_DefensiveFloorClamp verifies that when session_pnl is in the
// defensive bucket (>= FourthDimDefensiveFloor), the opp_tighten policy
// branch is clamped so the base spread delta does not go negative.
func TestFourthDim_DefensiveFloorClamp(t *testing.T) {
	cfg := defaultV2Config()
	cfg.FourthDimension = "session_pnl"
	cfg.FourthDimDefensiveFloor = 0.75
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Opportunity-tighten conditions + session_pnl in adverse bucket (0.75).
	in := neutralInputs()
	in.WorstMarkoutBps = 1.5
	in.BuyMarkoutBps = 1.5
	in.SellMarkoutBps = 1.5
	in.CompositeMarkoutBps = 1.5
	in.BaselineBps = 2.0
	in.FillRateRatio = 2.0
	in.BuyFillRateRatio = 2.0
	in.SellFillRateRatio = 2.0
	in.VWAPDeviation = 0.10
	in.WickThreshold = 0.10
	in.SpreadBBORatio = 5.0
	in.SessionPnLBps = -15.0 // adverse bucket -> 0.75

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets := opt.policyTargets(state, in)
		// spreadBase should have been clamped to >= 0 by the defensive floor.
		if targets["spreadDeltaBuy"] < 0 && targets["spreadDeltaSell"] < 0 {
			t.Errorf("defensive clamp: expected at least one side delta >= 0, got buy=%g sell=%g",
				targets["spreadDeltaBuy"], targets["spreadDeltaSell"])
		}
	})
	fired := extractFired(t, logs)
	if !firedHas(fired, "fourth_dim_defensive_clamp") {
		t.Errorf("expected fourth_dim_defensive_clamp in fired=%q", fired)
	}
}

// TestFourthDim_DefensiveFloorDisabledWhenBudgetPressure verifies that the
// defensive floor clamp does NOT fire when FourthDimension defaults to
// budget_pressure, even if BudgetPressure is high. Tightening is still
// allowed as before.
func TestFourthDim_DefensiveFloorDisabledWhenBudgetPressure(t *testing.T) {
	cfg := defaultV2Config()
	cfg.FourthDimension = "" // default = budget_pressure
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Opportunity-tighten conditions + high BudgetPressure.
	in := neutralInputs()
	in.WorstMarkoutBps = 1.5
	in.BuyMarkoutBps = 1.5
	in.SellMarkoutBps = 1.5
	in.CompositeMarkoutBps = 1.5
	in.BaselineBps = 2.0
	in.FillRateRatio = 2.0
	in.BuyFillRateRatio = 2.0
	in.SellFillRateRatio = 2.0
	in.VWAPDeviation = 0.10
	in.WickThreshold = 0.10
	in.SpreadBBORatio = 5.0
	in.BudgetPressure = 0.9 // high — irrelevant to the clamp

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)
	if firedHas(fired, "fourth_dim_defensive_clamp") {
		t.Errorf("budget_pressure mode: expected no fourth_dim_defensive_clamp, got fired=%q", fired)
	}
	// opp_tighten should still fire freely.
	if !firedHas(fired, "opp_tighten") {
		t.Errorf("budget_pressure mode: expected opp_tighten in fired=%q", fired)
	}
}

// TestMapSessionPnLToStress_FallsBackToDefaultEdges verifies that an empty
// or wrong-length edges slice triggers the default edge fallback.
func TestMapSessionPnLToStress_FallsBackToDefaultEdges(t *testing.T) {
	// Empty list uses default [-20,-10,10,20].
	if got := mapSessionPnLToStress(-50, nil); got != 1.0 {
		t.Errorf("nil edges: expected 1.0, got %g", got)
	}
	if got := mapSessionPnLToStress(50, nil); got != 0.0 {
		t.Errorf("nil edges: expected 0.0, got %g", got)
	}
	if got := mapSessionPnLToStress(0, []float64{1, 2, 3}); got != 0.50 {
		t.Errorf("wrong-length edges: expected fallback to default bucket (0 -> 0.5), got %g", got)
	}
}
