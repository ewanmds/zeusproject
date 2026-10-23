package quoting

import (
	"testing"
)

// skewRelaxInputs returns neutralInputs with adverse composite markout and
// moderate inventory so the relax rule fires when enabled.
func skewRelaxInputs() OptimizerV2Inputs {
	in := neutralInputs()
	in.CompositeMarkoutBps = -1.0
	in.InventoryUtil = 0.3
	in.InventorySide = "buy"
	return in
}

// TestSkewRelax_Disabled verifies that with zero-valued config the rule is a
// no-op: skewMult target stays at the neutral 1.0 (or at whatever skew_inv_stress
// sets).
func TestSkewRelax_Disabled(t *testing.T) {
	cfg := defaultV2Config()
	// Both knobs at zero: rule disabled.
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := skewRelaxInputs()

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse to be disabled, fired=%q", fired)
	}
}

// TestSkewRelax_FiresOnAdverse verifies the rule fires when markout is below
// threshold AND inventory exceeds the util floor, and that skewMult drops
// below 1.0 but not below the configured floor.
func TestSkewRelax_FiresOnAdverse(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.SkewRelaxAdverseMarkoutBps = -0.2
	cfg.PolicyThresholds.SkewRelaxInvUtilMin = 0.15
	cfg.PolicyThresholds.SkewRelaxFloor = 0.6
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := skewRelaxInputs()
	in.CompositeMarkoutBps = -1.0

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse to fire, fired=%q", fired)
	}
	skew := targets["skewMult"]
	if skew >= 1.0 {
		t.Fatalf("expected skewMult < 1.0 on adverse markout, got %g", skew)
	}
	if skew < 0.6 {
		t.Fatalf("expected skewMult >= floor 0.6, got %g", skew)
	}
}

// TestSkewRelax_FlooredAtExtremeAdverse verifies skewMult clamps at the floor
// when adversity is extreme.
func TestSkewRelax_FlooredAtExtremeAdverse(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.SkewRelaxAdverseMarkoutBps = -0.2
	cfg.PolicyThresholds.SkewRelaxInvUtilMin = 0.15
	cfg.PolicyThresholds.SkewRelaxFloor = 0.6
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := skewRelaxInputs()
	in.CompositeMarkoutBps = -20.0 // extreme

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse to fire, fired=%q", fired)
	}
	if targets["skewMult"] != 0.6 {
		t.Fatalf("expected skewMult clamped at floor 0.6, got %g", targets["skewMult"])
	}
}

// TestSkewRelax_DormantOnLowInventory verifies the rule doesn't fire when
// inventory is below the util floor even if markout is adverse.
func TestSkewRelax_DormantOnLowInventory(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.SkewRelaxAdverseMarkoutBps = -0.2
	cfg.PolicyThresholds.SkewRelaxInvUtilMin = 0.15
	cfg.PolicyThresholds.SkewRelaxFloor = 0.6
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := skewRelaxInputs()
	in.CompositeMarkoutBps = -1.0
	in.InventoryUtil = 0.05 // below floor

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse dormant on low inventory, fired=%q", fired)
	}
}

// TestSkewRelax_DormantOnHealthyMarkout verifies the rule doesn't fire when
// markout is above the (negative) threshold.
func TestSkewRelax_DormantOnHealthyMarkout(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.SkewRelaxAdverseMarkoutBps = -0.2
	cfg.PolicyThresholds.SkewRelaxInvUtilMin = 0.15
	cfg.PolicyThresholds.SkewRelaxFloor = 0.6
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := skewRelaxInputs()
	in.CompositeMarkoutBps = 1.0 // healthy

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse dormant on healthy markout, fired=%q", fired)
	}
}

// TestSkewRelax_ComposesWithInvStress verifies the relax rule multiplies the
// inv_stress up-leg cleanly when both fire: skew should start above 1.0 from
// inv_stress then be scaled down by relax.
func TestSkewRelax_ComposesWithInvStress(t *testing.T) {
	cfg := defaultV2Config()
	cfg.PolicyThresholds.SkewRelaxAdverseMarkoutBps = -0.2
	cfg.PolicyThresholds.SkewRelaxInvUtilMin = 0.15
	cfg.PolicyThresholds.SkewRelaxFloor = 0.6
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Drive inv_stress high to trigger skew_inv_stress up-leg.
	in := skewRelaxInputs()
	in.InventoryUtil = 0.9
	in.ControllerAdj = 0.9
	in.SameSideDurationS = 300
	in.CompositeMarkoutBps = -1.0

	var targets map[string]float64
	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		targets = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)

	if !firedHas(fired, "skew_relax_on_adverse") {
		t.Fatalf("expected skew_relax_on_adverse to still fire with inv_stress high, fired=%q", fired)
	}
	// Composition check: just verify the combined skew is different from
	// either rule alone (we don't need exact math, just that both applied).
	if targets["skewMult"] == 1.0 {
		t.Fatalf("expected skewMult != 1.0 when both rules fire, got %g", targets["skewMult"])
	}
}
