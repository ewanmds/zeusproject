package quoting

import (
	"bytes"
	"log/slog"
	"math"
	"strings"
	"testing"
)

// oppTightenInputs returns inputs that satisfy the toxicity_low + opportunity_high
// pre-conditions and have positive markout health, so opp_tighten will fire
// unless gated by P8.
func oppTightenInputs() OptimizerV2Inputs {
	in := neutralInputs()
	// Drive Toxicity low: small adverse markout, no burst, no emergency.
	in.WorstMarkoutBps = 1.5
	in.BuyMarkoutBps = 1.5
	in.SellMarkoutBps = 1.5
	in.CompositeMarkoutBps = 1.5
	in.BaselineBps = 2.0
	in.EmergencyCount = 0
	in.FillBurstActive = false
	// Drive Opportunity high: high fill rate, high VWAP dev, high spread/BBO.
	in.FillRateRatio = 2.0
	in.BuyFillRateRatio = 2.0
	in.SellFillRateRatio = 2.0
	in.VWAPDeviation = 0.10
	in.WickThreshold = 0.10
	in.SpreadBBORatio = 5.0
	// Neutral inventory.
	in.InventoryUtil = 0.05
	in.ControllerAdj = 0.3
	in.SameSideDurationS = 0
	in.BudgetPressure = 0.0
	return in
}

func captureFiredLog(t *testing.T, fn func()) string {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)
	fn()
	return buf.String()
}

// extractFired pulls the "fired" field from the most recent OptimizerV2 policy
// log line in the captured buffer.
func extractFired(t *testing.T, logs string) string {
	t.Helper()
	var found string
	for _, line := range strings.Split(logs, "\n") {
		if !strings.Contains(line, `"msg":"[OptimizerV2] policy"`) {
			continue
		}
		const key = `"fired":"`
		idx := strings.Index(line, key)
		if idx < 0 {
			continue
		}
		rest := line[idx+len(key):]
		end := strings.Index(rest, `"`)
		if end < 0 {
			continue
		}
		found = rest[:end]
	}
	return found
}

// firedHas reports whether the comma-separated fired label list contains
// label as an exact token (avoids false matches like "opp_tighten" matching
// "opp_tighten_blocked_inv").
func firedHas(fired, label string) bool {
	for _, tok := range strings.Split(fired, ",") {
		if tok == label {
			return true
		}
	}
	return false
}

// TestP8_OppTightenBaseline verifies opp_tighten fires under the baseline
// configuration (no gates set).
func TestP8_OppTightenBaseline(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	in := oppTightenInputs()

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten") {
		t.Fatalf("baseline: expected opp_tighten in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten_blocked_inv") || firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("baseline: unexpected blocked label in fired=%q", fired)
	}
}

// TestP8_OppTightenBlockedByInvGate verifies the inv-util gate blocks
// opp_tighten when inventory is adverse and unrealized PnL is negative.
func TestP8_OppTightenBlockedByInvGate(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenInvUtilGate = 0.40
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.InventoryUtil = 0.6
	in.PositionUnrealizedBps = -5.0

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_blocked_inv") {
		t.Fatalf("expected opp_tighten_blocked_inv in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten to be blocked, but found in fired=%q", fired)
	}
}

// TestP8_OppTightenInvGate_PositiveUnrealized verifies the inv-util gate is a
// no-op when unrealized PnL is positive (we're in profit, fine to tighten).
func TestP8_OppTightenInvGate_PositiveUnrealized(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenInvUtilGate = 0.40
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.InventoryUtil = 0.6
	in.PositionUnrealizedBps = +5.0

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten to fire when unrealized>0, fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten_blocked_inv") {
		t.Fatalf("expected no inv block when unrealized>0, fired=%q", fired)
	}
}

// TestP8_OppTightenBlockedBySessionPnL verifies the session PnL floor blocks
// opp_tighten when session PnL is below the (negative) floor.
func TestP8_OppTightenBlockedBySessionPnL(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenSessionPnLFloorBps = -10.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.SessionPnLBps = -15.0

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("expected opp_tighten_blocked_pnl in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten to be blocked, fired=%q", fired)
	}
}

// TestP8_OppTightenGatesDisabled verifies that with gate thresholds set to
// zero, opp_tighten fires even with adverse inventory + negative unrealized.
func TestP8_OppTightenGatesDisabled(t *testing.T) {
	cfg := defaultV2Config()
	// Both gates disabled (zero values).
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.InventoryUtil = 0.6
	in.PositionUnrealizedBps = -5.0
	in.SessionPnLBps = -50.0

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten to fire when gates disabled, fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten_blocked_inv") || firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("expected no blocked labels when gates disabled, fired=%q", fired)
	}
}

// TestP8_OppTightenBlockedAsymmetric_Long verifies that when the PnL gate
// blocks opp_tighten while carrying a long position, the tighten is applied
// to the sell (exit) side only — not to spreadBase and not to the buy side.
func TestP8_OppTightenBlockedAsymmetric_Long(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenSessionPnLFloorBps = -10.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.SessionPnLBps = -15.0
	in.InventoryUtil = 0.3
	in.InventorySide = "buy"

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("expected opp_tighten_blocked_pnl in fired=%q", fired)
	}
	if !firedHas(fired, "opp_tighten_exit_only") {
		t.Fatalf("expected opp_tighten_exit_only in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten (symmetric) suppressed, fired=%q", fired)
	}
	// Sell side (exit when long) should be tightened (more negative) vs buy.
	if targets["spreadDeltaSell"] >= targets["spreadDeltaBuy"] {
		t.Fatalf("expected spreadDeltaSell < spreadDeltaBuy when long + gate fires, got buy=%g sell=%g", targets["spreadDeltaBuy"], targets["spreadDeltaSell"])
	}
}

// TestP8_OppTightenBlockedAsymmetric_Short verifies the mirror case: when
// carrying short, the buy (exit) side is tightened.
func TestP8_OppTightenBlockedAsymmetric_Short(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenSessionPnLFloorBps = -10.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.SessionPnLBps = -15.0
	in.InventoryUtil = 0.3
	in.InventorySide = "sell"

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_exit_only") {
		t.Fatalf("expected opp_tighten_exit_only in fired=%q", fired)
	}
	if targets["spreadDeltaBuy"] >= targets["spreadDeltaSell"] {
		t.Fatalf("expected spreadDeltaBuy < spreadDeltaSell when short + gate fires, got buy=%g sell=%g", targets["spreadDeltaBuy"], targets["spreadDeltaSell"])
	}
}

// TestP8_OppTightenBlockedAsymmetric_Flat verifies that when the PnL gate
// fires but position is flat (below flat threshold), the original full block
// is preserved — no tighten on either side.
func TestP8_OppTightenBlockedAsymmetric_Flat(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenSessionPnLFloorBps = -10.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.SessionPnLBps = -15.0
	in.InventoryUtil = 0.02
	in.InventorySide = ""

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("expected opp_tighten_blocked_pnl in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten_exit_only") {
		t.Fatalf("expected no opp_tighten_exit_only when flat, fired=%q", fired)
	}
	// Without toxicity asymmetry, buy and sell deltas should be equal.
	if math.Abs(targets["spreadDeltaBuy"]-targets["spreadDeltaSell"]) > 1e-9 {
		t.Fatalf("expected symmetric deltas when flat + blocked, got buy=%g sell=%g", targets["spreadDeltaBuy"], targets["spreadDeltaSell"])
	}
}

// TestP8_OppTightenBothBlocksFire verifies both blocked labels emit when both
// gates trip simultaneously.
func TestP8_OppTightenBothBlocksFire(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.OppTightenInvUtilGate = 0.40
	cfg.PolicyThresholds.OppTightenSessionPnLFloorBps = -10.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := oppTightenInputs()
	in.InventoryUtil = 0.6
	in.PositionUnrealizedBps = -5.0
	in.SessionPnLBps = -20.0

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "opp_tighten_blocked_inv") {
		t.Fatalf("expected opp_tighten_blocked_inv in fired=%q", fired)
	}
	if !firedHas(fired, "opp_tighten_blocked_pnl") {
		t.Fatalf("expected opp_tighten_blocked_pnl in fired=%q", fired)
	}
	if firedHas(fired, "opp_tighten") {
		t.Fatalf("expected opp_tighten suppressed when gates trip, fired=%q", fired)
	}
}
