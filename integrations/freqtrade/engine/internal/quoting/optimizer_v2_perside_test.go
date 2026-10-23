package quoting

import (
	"math"
	"strings"
	"testing"
)

func perSideInputs() OptimizerV2Inputs {
	in := neutralInputs()
	in.PerSideWinrateEnabled = true
	in.PerSideWinrateWindow = 50
	in.PerSideWinrateThreshold = 0.35
	in.PerSideProfitFactorThreshold = 0.9
	in.PerSideWinrateWidenBps = 0.5
	in.BuyWinRate = 0.6
	in.SellWinRate = 0.6
	in.BuyProfitFactor = 1.5
	in.SellProfitFactor = 1.5
	in.BuyPnLSamples = 50
	in.SellPnLSamples = 50
	return in
}

func TestPerSide_BelowThresholdWidensBuy(t *testing.T) {
	cfg := defaultV2Config()
	in := perSideInputs()
	in.BuyWinRate = 0.20

	// Compute baseline (gate disabled) and gated targets, capturing only the
	// gated-eval logs so extractFired picks up the per_side_winrate_buy label.
	baseIn := in
	baseIn.PerSideWinrateEnabled = false

	optBase := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	baseState := optBase.computeState(baseIn)
	baseTargets := optBase.policyTargets(baseState, baseIn)

	optGated := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	var gatedTargets map[string]float64
	logs := captureFiredLog(t, func() {
		state := optGated.computeState(in)
		gatedTargets = optGated.policyTargets(state, in)
	})

	got := gatedTargets["spreadDeltaBuy"] - baseTargets["spreadDeltaBuy"]
	if math.Abs(got-in.PerSideWinrateWidenBps) > 1e-9 {
		t.Errorf("expected spreadDeltaBuy delta=%g, got %g (gated=%g base=%g)",
			in.PerSideWinrateWidenBps, got, gatedTargets["spreadDeltaBuy"], baseTargets["spreadDeltaBuy"])
	}
	fired := extractFired(t, logs)
	if !firedHas(fired, "per_side_winrate_buy") {
		t.Errorf("expected per_side_winrate_buy in fired=%q", fired)
	}
	if !strings.Contains(logs, "per-side winrate gate") {
		t.Error("expected per-side winrate gate log")
	}
}

func TestPerSide_BelowProfitFactorWidensSell(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	in := perSideInputs()
	in.SellProfitFactor = 0.5

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)
	if !firedHas(fired, "per_side_winrate_sell") {
		t.Errorf("expected per_side_winrate_sell in fired=%q", fired)
	}
}

func TestPerSide_HealthyNoOp(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	in := perSideInputs()

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)
	if firedHas(fired, "per_side_winrate_buy") || firedHas(fired, "per_side_winrate_sell") {
		t.Errorf("healthy: expected no per_side_winrate_* fired, got %q", fired)
	}
}

func TestPerSide_InsufficientSamplesNoOp(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	in := perSideInputs()
	in.BuyWinRate = 0.10
	in.BuyPnLSamples = 10
	in.SellPnLSamples = 10

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)
	if firedHas(fired, "per_side_winrate_buy") || firedHas(fired, "per_side_winrate_sell") {
		t.Errorf("insufficient samples: expected no per_side_winrate_* fired, got %q", fired)
	}
}

func TestPerSide_DisabledNoOp(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	in := perSideInputs()
	in.PerSideWinrateEnabled = false
	in.BuyWinRate = 0.10

	logs := captureFiredLog(t, func() {
		state := opt.computeState(in)
		_ = opt.policyTargets(state, in)
	})
	fired := extractFired(t, logs)
	if firedHas(fired, "per_side_winrate_buy") || firedHas(fired, "per_side_winrate_sell") {
		t.Errorf("disabled: expected no per_side_winrate_* fired, got %q", fired)
	}
}
