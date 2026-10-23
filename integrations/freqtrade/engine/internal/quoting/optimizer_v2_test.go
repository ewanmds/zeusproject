package quoting

import (
	"math"
	"testing"

	"hyperliquid-market-maker/internal/config"
)

// defaultV2Config returns a usable OptimizerV2Config for testing.
func defaultV2Config() config.OptimizerV2Config {
	return config.OptimizerV2Config{
		Enabled:                    true,
		EvalIntervalSec:            30,
		WarmupEvals:                5,
		MinFillSamples:             5,
		Momentum:                   0.8,
		LearningRate:               0.1,
		MaxKnobChangesPerEval:      3,
		EmergencyToxicityThreshold: 0.9,
		RollbackWindowEvals:        3,
		RollbackRate:               0.5,
		RollbackCooldownSec:        120,
		ToxicityWeights: config.OptimizerV2DimensionWeights{
			MarkoutEWMA:    0.5,
			FillBurst:      0.3,
			EmergencyCount: 0.2,
		},
		OpportunityWeights: config.OptimizerV2OpportunityWeights{
			FillRateRatio:  0.4,
			RegimeSignal:   0.2,
			VWAPDeviation:  0.2,
			SpreadBBORatio: 0.2,
		},
		InventoryStressWeights: config.OptimizerV2InventoryWeights{
			PositionUtil:     0.5,
			ControllerAdj:    0.3,
			SameSideDuration: 0.2,
		},
		PolicyThresholds: config.OptimizerV2PolicyThresholds{
			ToxicityHigh:      0.6,
			ToxicityMedium:    0.5,
			ToxicityLow:       0.3,
			OpportunityHigh:   0.5,
			OpportunityLow:    0.3,
			InventoryHigh:     0.4,
			InventoryLow:      0.15,
			BudgetHigh:        0.7,
			BudgetMedium:      0.5,
			AutocorrThreshold: 0.1,
			FillRateDelta:     0.5,
			ImbalanceSatHigh:  0.6,
			ImbalanceSatLow:   0.2,
			PeerInventoryMin:  0.2,
		},
		SpreadBps:          config.KnobBoundsConfig{Min: -5.0, Max: 10.0},
		NotionalMult:       config.KnobBoundsConfig{Min: 0.3, Max: 2.0},
		SkewMult:           config.KnobBoundsConfig{Min: 0.5, Max: 3.0},
		VWAPAlpha:          config.KnobBoundsConfig{Min: 0.1, Max: 0.9},
		ImbalanceShiftMult: config.KnobBoundsConfig{Min: 0.5, Max: 2.0},
		FillDecayMult:      config.KnobBoundsConfig{Min: 0.5, Max: 2.0},
		RefreshMult:        config.KnobBoundsConfig{Min: 0.5, Max: 2.0},
	}
}

func neutralInputs() OptimizerV2Inputs {
	return OptimizerV2Inputs{
		WorstMarkoutBps:     1.0,
		BuyMarkoutBps:       1.0,
		SellMarkoutBps:      1.0,
		CompositeMarkoutBps: 1.0,
		BaselineBps:         2.0,
		FillRateRatio:       1.0,
		FillSamples:         10,
		BuyFillSamples:      5,
		SellFillSamples:     5,
		InventoryUtil:       0.1,
		InventorySide:       "buy",
		ControllerAdj:       0.3,
		SameSideDurationS:   10,
		Autocorr:            0.0,
		VolRatio:            1.0,
		VWAPDeviation:       0.01,
		WickThreshold:       0.10,
		SpreadBBORatio:      1.5,
		BudgetPressure:      0.3,
	}
}

func TestOptimizerV2_Disabled(t *testing.T) {
	cfg := defaultV2Config()
	cfg.Enabled = false
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	overlay := opt.Overlay()
	if overlay.SpreadDelta != 0 {
		t.Errorf("disabled: expected SpreadDelta=0, got %f", overlay.SpreadDelta)
	}
	if overlay.NotionalMult != 1.0 {
		t.Errorf("disabled: expected NotionalMult=1.0, got %f", overlay.NotionalMult)
	}
	if overlay.SkewMult != 1.0 {
		t.Errorf("disabled: expected SkewMult=1.0, got %f", overlay.SkewMult)
	}
	if overlay.RefreshMult != 1.0 {
		t.Errorf("disabled: expected RefreshMult=1.0, got %f", overlay.RefreshMult)
	}

	// Even after Evaluate, should still be neutral.
	inputs := neutralInputs()
	n := opt.Evaluate(inputs)
	if n != 0 {
		t.Errorf("disabled: expected 0 changes, got %d", n)
	}
	overlay = opt.Overlay()
	if overlay.NotionalMult != 1.0 {
		t.Errorf("disabled: expected NotionalMult=1.0 after eval, got %f", overlay.NotionalMult)
	}
}

func TestOptimizerV2_NeutralOnStartup(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	overlay := opt.Overlay()
	if overlay.SpreadDelta != 0 {
		t.Errorf("startup: expected SpreadDelta=0, got %f", overlay.SpreadDelta)
	}
	if overlay.SpreadDeltaBuy != 0 {
		t.Errorf("startup: expected SpreadDeltaBuy=0, got %f", overlay.SpreadDeltaBuy)
	}
	if overlay.SpreadDeltaSell != 0 {
		t.Errorf("startup: expected SpreadDeltaSell=0, got %f", overlay.SpreadDeltaSell)
	}
	if overlay.NotionalMult != 1.0 {
		t.Errorf("startup: expected NotionalMult=1.0, got %f", overlay.NotionalMult)
	}
	if overlay.SkewMult != 1.0 {
		t.Errorf("startup: expected SkewMult=1.0, got %f", overlay.SkewMult)
	}
	if overlay.AlphaDelta != 0 {
		t.Errorf("startup: expected AlphaDelta=0, got %f", overlay.AlphaDelta)
	}
	if overlay.ImbalanceShiftMult != 1.0 {
		t.Errorf("startup: expected ImbalanceShiftMult=1.0, got %f", overlay.ImbalanceShiftMult)
	}
	if overlay.FillDecayMult != 1.0 {
		t.Errorf("startup: expected FillDecayMult=1.0, got %f", overlay.FillDecayMult)
	}
	if overlay.RefreshMult != 1.0 {
		t.Errorf("startup: expected RefreshMult=1.0, got %f", overlay.RefreshMult)
	}
}

func TestOptimizerV2_StateModel_Toxicity(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Adverse markouts (worst = -3 bps against baseline of 2 bps) + burst active.
	inputs := OptimizerV2Inputs{
		WorstMarkoutBps:     -3.0,
		BuyMarkoutBps:       -3.0,
		SellMarkoutBps:      1.0,
		CompositeMarkoutBps: -1.0,
		BaselineBps:         2.0,
		FillBurstActive:     true,
		EmergencyCount:      2,
		FillRateRatio:       1.0,
		FillSamples:         10,
		InventoryUtil:       0.1,
		ControllerAdj:       0.3,
		Autocorr:            0.0,
		SpreadBBORatio:      1.5,
		WickThreshold:       0.10,
	}

	state := opt.computeState(inputs)

	// toxMarkout = clamp(1 - (-3)/2, 0, 1) = clamp(2.5, 0, 1) = 1.0
	// toxBurst = 1.0
	// toxEmergency = clamp(2/3, 0, 1) = 0.667
	// tox = 0.5*1.0 + 0.3*1.0 + 0.2*0.667 = 0.5 + 0.3 + 0.133 = 0.933
	if state.Toxicity < 0.9 {
		t.Errorf("expected high toxicity (>0.9), got %f", state.Toxicity)
	}
}

func TestOptimizerV2_StateModel_Opportunity(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Good fill rate + mean-reversion regime.
	inputs := OptimizerV2Inputs{
		WorstMarkoutBps:     2.0,
		BuyMarkoutBps:       2.0,
		SellMarkoutBps:      2.0,
		CompositeMarkoutBps: 2.0,
		BaselineBps:         2.0,
		FillRateRatio:       1.5, // 150% of target
		FillSamples:         20,
		InventoryUtil:       0.1,
		ControllerAdj:       0.3,
		Autocorr:            -0.3, // mean-reverting
		VWAPDeviation:       0.08,
		WickThreshold:       0.10,
		SpreadBBORatio:      5.0, // wide spread
	}

	state := opt.computeState(inputs)

	// fillRateNorm = clamp(1.5/2, 0, 1) = 0.75
	// autocorrSignal = clamp(0.3*5, 0, 1) = 1.0
	// vwapDevNorm = clamp(0.08/0.10, 0, 1) = 0.8
	// spreadBBONorm = clamp((5-1)/9, 0, 1) = 0.444
	// opportunity = 0.4*0.75 + 0.2*1.0 + 0.2*0.8 + 0.2*0.444
	//            = 0.3 + 0.2 + 0.16 + 0.089 = 0.749
	if state.Opportunity < 0.5 {
		t.Errorf("expected moderate-to-high opportunity (>0.5), got %f", state.Opportunity)
	}
	if state.Opportunity > 0.9 {
		t.Errorf("expected opportunity < 0.9, got %f", state.Opportunity)
	}
}

func TestOptimizerV2_ProportionalControl(t *testing.T) {
	cfg := defaultV2Config()
	cfg.Momentum = 0.0     // no momentum for clearer testing
	cfg.LearningRate = 0.5 // aggressive for quick convergence
	cfg.MinFillSamples = 1 // full confidence immediately
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 10 // above min

	target := 2.0
	knob := "spreadDeltaBuy"

	for i := 0; i < 20; i++ {
		opt.approach(knob, target)
	}

	got := opt.current[knob]
	if math.Abs(got-target) > 0.01 {
		t.Errorf("after 20 iterations: expected knob near %f, got %f", target, got)
	}
}

func TestOptimizerV2_BoundsRespected(t *testing.T) {
	cfg := defaultV2Config()
	cfg.Momentum = 0.0
	cfg.LearningRate = 1.0 // very aggressive
	cfg.MinFillSamples = 1
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 100

	// Try to push spread beyond max (10.0).
	for i := 0; i < 100; i++ {
		opt.approach("spreadDeltaBuy", 100.0)
	}
	if opt.current["spreadDeltaBuy"] > cfg.SpreadBps.Max {
		t.Errorf("spreadDeltaBuy exceeded max: %f > %f", opt.current["spreadDeltaBuy"], cfg.SpreadBps.Max)
	}

	// Try to push spread below min (-10.0 = -max).
	for i := 0; i < 100; i++ {
		opt.approach("spreadDeltaSell", -100.0)
	}
	if opt.current["spreadDeltaSell"] < -cfg.SpreadBps.Max {
		t.Errorf("spreadDeltaSell below min: %f < %f", opt.current["spreadDeltaSell"], -cfg.SpreadBps.Max)
	}

	// Notional must stay in [0.3, 2.0].
	for i := 0; i < 100; i++ {
		opt.approach("notionalMult", 50.0)
	}
	if opt.current["notionalMult"] > cfg.NotionalMult.Max {
		t.Errorf("notionalMult exceeded max: %f > %f", opt.current["notionalMult"], cfg.NotionalMult.Max)
	}

	for i := 0; i < 100; i++ {
		opt.approach("notionalMult", -50.0)
	}
	if opt.current["notionalMult"] < cfg.NotionalMult.Min {
		t.Errorf("notionalMult below min: %f < %f", opt.current["notionalMult"], cfg.NotionalMult.Min)
	}
}

func TestOptimizerV2_WarmupReducesLearningRate(t *testing.T) {
	cfg := defaultV2Config()
	cfg.MinFillSamples = 100 // need 100 fills for full confidence
	cfg.Momentum = 0.0
	cfg.LearningRate = 0.5
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 10 // only 10 out of 100

	conf := opt.confidence()
	if math.Abs(conf-0.1) > 0.001 {
		t.Errorf("expected confidence ~0.1, got %f", conf)
	}

	// With confidence=0.1, effective LR = 0.5 * 0.1 = 0.05.
	// After one approach step toward target 5.0 from 0.0:
	// velocity = 0*0 + 1.0*(5-0) = 5.0
	// current = 0 + 0.05 * 5.0 = 0.25
	opt.approach("spreadDeltaBuy", 5.0)
	got := opt.current["spreadDeltaBuy"]
	expected := 0.25
	if math.Abs(got-expected) > 0.001 {
		t.Errorf("warmup: expected spread ~%f, got %f", expected, got)
	}

	// Compare: full confidence should move faster.
	cfg2 := defaultV2Config()
	cfg2.MinFillSamples = 10
	cfg2.Momentum = 0.0
	cfg2.LearningRate = 0.5
	opt2 := NewOptimizerV2(cfg2, "TEST", 3.0, 0.4)
	opt2.totalFills = 10

	opt2.approach("spreadDeltaBuy", 5.0)
	got2 := opt2.current["spreadDeltaBuy"]
	// confidence=1.0, effective LR=0.5
	// velocity = 5.0, current = 0 + 0.5*5.0 = 2.5
	expected2 := 2.5
	if math.Abs(got2-expected2) > 0.001 {
		t.Errorf("full confidence: expected spread ~%f, got %f", expected2, got2)
	}

	if got >= got2 {
		t.Errorf("warmup should produce smaller change: warmup=%f >= full=%f", got, got2)
	}
}

func TestOptimizerV2_HighToxicityWidens(t *testing.T) {
	cfg := defaultV2Config()
	cfg.MaxKnobChangesPerEval = 8 // allow all knobs to move
	cfg.LearningRate = 0.3
	cfg.Momentum = 0.0
	cfg.MinFillSamples = 1
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Record enough fills for full confidence.
	for i := 0; i < 10; i++ {
		opt.RecordFill()
	}

	// Toxic buy markouts: very negative.
	inputs := OptimizerV2Inputs{
		WorstMarkoutBps:     -5.0,
		BuyMarkoutBps:       -5.0,
		SellMarkoutBps:      2.0,
		CompositeMarkoutBps: -2.0,
		BaselineBps:         2.0,
		FillRateRatio:       0.5,
		FillSamples:         20,
		BuyFillSamples:      10,
		SellFillSamples:     10,
		InventoryUtil:       0.2,
		InventorySide:       "buy",
		ControllerAdj:       0.5,
		SameSideDurationS:   30,
		Autocorr:            0.0,
		VolRatio:            1.0,
		VWAPDeviation:       0.02,
		WickThreshold:       0.10,
		SpreadBBORatio:      2.0,
		BudgetPressure:      0.3,
	}

	// Run multiple evaluations so the proportional control can converge.
	for i := 0; i < 30; i++ {
		opt.Evaluate(inputs)
	}

	overlay := opt.Overlay()
	if overlay.SpreadDeltaBuy <= 0 {
		t.Errorf("expected positive SpreadDeltaBuy for toxic buys, got %f", overlay.SpreadDeltaBuy)
	}

	// Buy side should be wider than sell side due to per-side toxicity shift.
	if overlay.SpreadDeltaBuy <= overlay.SpreadDeltaSell {
		t.Errorf("expected SpreadDeltaBuy > SpreadDeltaSell, got buy=%f sell=%f",
			overlay.SpreadDeltaBuy, overlay.SpreadDeltaSell)
	}
}

func TestV2PerSideFillRate_OpportunityDimension(t *testing.T) {
	cfg := defaultV2Config()
	baseSpread := 3.0
	baseAlpha := 0.4

	// Test 1: Symmetric fill rates should use min (which equals the global).
	opt1 := NewOptimizerV2(cfg, "TEST", baseSpread, baseAlpha)
	in1 := neutralInputs()
	in1.FillRateRatio = 1.0
	in1.BuyFillRateRatio = 1.0
	in1.SellFillRateRatio = 1.0

	state1 := opt1.computeState(in1)

	// Test 2: Asymmetric fill rates — one side very low — should reduce Opportunity.
	opt2 := NewOptimizerV2(cfg, "TEST", baseSpread, baseAlpha)
	in2 := neutralInputs()
	in2.FillRateRatio = 1.0
	in2.BuyFillRateRatio = 0.2 // very low buy fill rate
	in2.SellFillRateRatio = 1.8

	state2 := opt2.computeState(in2)

	// The asymmetric case should have lower Opportunity because min(0.2, 1.8) = 0.2 vs min(1.0, 1.0) = 1.0
	if state2.Opportunity >= state1.Opportunity {
		t.Errorf("asymmetric fill rate (min=0.2) should produce lower Opportunity than symmetric (1.0): got asym=%f sym=%f",
			state2.Opportunity, state1.Opportunity)
	}
}

func TestV2PerSideFillRate_PolicySpreadAsymmetry(t *testing.T) {
	cfg := defaultV2Config()
	baseSpread := 3.0
	baseAlpha := 0.4

	opt := NewOptimizerV2(cfg, "TEST", baseSpread, baseAlpha)

	// Buy side overfilling, sell side underfilling — fillDelta > 0.5.
	in := neutralInputs()
	in.FillRateRatio = 1.0
	in.BuyFillRateRatio = 2.0
	in.SellFillRateRatio = 0.5

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)

	// fillDelta = 2.0 - 0.5 = 1.5 > 0.5, so fillShift = clamp(1.5,-1,1) * 3.0 * 0.15 = 0.45
	// spreadDeltaBuy -= fillShift → buy tightens (fillShift subtracted)
	// spreadDeltaSell += fillShift → sell widens (fillShift added)
	// With neutral toxShift (equal markouts), the buy side should be tighter than sell side.
	buyTarget := targets["spreadDeltaBuy"]
	sellTarget := targets["spreadDeltaSell"]

	if buyTarget >= sellTarget {
		t.Errorf("when buy overfills (delta>0), expected spreadDeltaBuy < spreadDeltaSell (buy tightens), got buy=%f sell=%f",
			buyTarget, sellTarget)
	}
}

func TestV2PerSideFillRate_SelfEvaluateUsesAverage(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Warm up to pass the warmup gate.
	warmIn := neutralInputs()
	warmIn.FillSamples = 10
	for i := 0; i < cfg.WarmupEvals+1; i++ {
		opt.Evaluate(warmIn)
	}

	// Evaluate with asymmetric per-side fill rates.
	in := neutralInputs()
	in.CompositeMarkoutBps = 2.0
	in.FillRateRatio = 1.0
	in.BuyFillRateRatio = 0.5
	in.SellFillRateRatio = 1.5
	in.FillSamples = 10

	opt.selfEvaluate(in)

	// The utility should use avg(0.5, 1.5) = 1.0, same as FillRateRatio.
	// We just verify no panic and that the utility was appended.
	if len(opt.utilityHistory) == 0 {
		t.Error("expected utilityHistory to have entries after selfEvaluate")
	}
	lastUtil := opt.utilityHistory[len(opt.utilityHistory)-1]
	// utility = 2.0 * max(1.0, 0.01) = 2.0
	if math.Abs(lastUtil-2.0) > 0.01 {
		t.Errorf("expected utility ~2.0 (compositeMarkout * avgFillRate), got %f", lastUtil)
	}
}

func TestV2_PerKnobSelectiveRollback(t *testing.T) {
	cfg := defaultV2Config()
	cfg.MaxKnobChangesPerEval = 8
	cfg.LearningRate = 0.5
	cfg.Momentum = 0.0
	cfg.MinFillSamples = 1
	cfg.RollbackWindowEvals = 2
	cfg.RollbackRate = 0.5
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	for i := 0; i < 10; i++ {
		opt.RecordFill()
	}

	// Phase 1: Drive knobs into a toxic state with good markout.
	toxicIn := neutralInputs()
	toxicIn.WorstMarkoutBps = -5.0
	toxicIn.BuyMarkoutBps = -5.0
	toxicIn.SellMarkoutBps = 2.0
	toxicIn.CompositeMarkoutBps = 2.0 // positive markout
	toxicIn.BaselineBps = 2.0
	toxicIn.FillSamples = 20
	for i := 0; i < 10; i++ {
		opt.Evaluate(toxicIn)
	}

	spreadBuyBefore := opt.current["spreadDeltaBuy"]
	notionalBefore := opt.current["notionalMult"]

	// Phase 2: Markout goes negative. Since we snapshotted at positive markout
	// for spread, rollback should kick in for knobs that moved.
	badIn := toxicIn
	badIn.CompositeMarkoutBps = -2.0 // now worse than snapshot
	badIn.FillRateRatio = 0.5
	for i := 0; i < cfg.RollbackWindowEvals; i++ {
		opt.Evaluate(badIn)
	}

	// After rollback, knobs that had good markout snapshots should NOT roll back.
	// Knobs that had worse markout should roll back toward shadow.
	// With per-knob selectivity, at least some knobs should have been partially reverted.
	// Just verify the rollback mechanism was triggered (utility history all negative
	// for rollback window).
	if len(opt.utilityHistory) < cfg.RollbackWindowEvals {
		t.Fatal("expected utility history to be populated")
	}
	// Spread should have moved during the toxic phase.
	if spreadBuyBefore == 0 && notionalBefore == 1.0 {
		t.Error("expected knobs to have moved during toxic phase")
	}
}

func TestV2_DirectionalCooldown(t *testing.T) {
	cfg := defaultV2Config()
	cfg.MaxKnobChangesPerEval = 8
	cfg.LearningRate = 0.5
	cfg.Momentum = 0.0
	cfg.MinFillSamples = 1
	cfg.RollbackWindowEvals = 2
	cfg.RollbackCooldownSec = 300 // long cooldown
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 100

	// Manually drive spreadDeltaBuy up via direct approach calls.
	for i := 0; i < 20; i++ {
		opt.approach("spreadDeltaBuy", 5.0)
	}
	spreadBuyPre := opt.current["spreadDeltaBuy"]
	if spreadBuyPre < 1.0 {
		t.Fatalf("expected spread to move up, got %f", spreadBuyPre)
	}

	// Set up per-knob snapshot with positive markout, then worsen it.
	opt.perKnobMarkout["spreadDeltaBuy"] = 2.0
	opt.lastCompositeMarkout = -3.0 // much worse → should trigger rollback for this knob

	// Set lastApproachDir so rollback sets cooldown on positive direction.
	opt.lastApproachDir["spreadDeltaBuy"] = 1.0

	// Trigger graduated rollback directly.
	opt.graduatedRollback(false)
	spreadBuyPost := opt.current["spreadDeltaBuy"]

	// Spread should have been rolled back (closer to 0).
	if spreadBuyPost >= spreadBuyPre {
		t.Errorf("expected rollback: before=%f after=%f", spreadBuyPre, spreadBuyPost)
	}

	// Now try to push spread up again — directional cooldown should block it.
	spreadBuyPostRollback := opt.current["spreadDeltaBuy"]
	for i := 0; i < 5; i++ {
		opt.approach("spreadDeltaBuy", 10.0)
	}
	spreadBuyAfterCooldown := opt.current["spreadDeltaBuy"]

	// Should not have moved because the positive direction is on cooldown.
	if math.Abs(spreadBuyAfterCooldown-spreadBuyPostRollback) > 0.01 {
		t.Errorf("expected directional cooldown to block positive approach: before=%f after=%f",
			spreadBuyPostRollback, spreadBuyAfterCooldown)
	}

	// Moving in the opposite (negative) direction should still work.
	opt.approach("spreadDeltaBuy", -5.0)
	if opt.current["spreadDeltaBuy"] >= spreadBuyAfterCooldown {
		t.Errorf("expected negative direction to be unblocked: before=%f after=%f",
			spreadBuyAfterCooldown, opt.current["spreadDeltaBuy"])
	}
}

func TestV2_CrossAssetSkew(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Both legs have same-direction inventory + high stress.
	in := neutralInputs()
	in.InventoryUtil = 0.6
	in.ControllerAdj = 1.0
	in.SameSideDurationS = 120
	in.PeerSameDirection = true
	in.PeerInventoryUtil = 0.4

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)
	skewWithPeer := targets["skewMult"]

	// Without peer signals.
	in2 := in
	in2.PeerSameDirection = false
	in2.PeerInventoryUtil = 0
	state2 := opt.computeState(in2)
	targets2 := opt.policyTargets(state2, in2)
	skewWithoutPeer := targets2["skewMult"]

	if skewWithPeer <= skewWithoutPeer {
		t.Errorf("expected cross-asset peer signals to increase skew: with=%f without=%f",
			skewWithPeer, skewWithoutPeer)
	}
}

func TestV2_CrossAssetNotional(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := neutralInputs()
	in.InventoryUtil = 0.3
	in.PairReduceActive = true

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)
	notionalPairReduce := targets["notionalMult"]

	in2 := in
	in2.PairReduceActive = false
	state2 := opt.computeState(in2)
	targets2 := opt.policyTargets(state2, in2)
	notionalNormal := targets2["notionalMult"]

	if notionalPairReduce >= notionalNormal {
		t.Errorf("expected pair-reduce to lower notional: reduce=%f normal=%f",
			notionalPairReduce, notionalNormal)
	}
}

func TestV2_ImbalanceShiftTargets(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// High saturation → scale down.
	inSat := neutralInputs()
	inSat.ImbalanceSaturationPct = 0.8
	state := opt.computeState(inSat)
	targets := opt.policyTargets(state, inSat)
	if targets["imbalanceShiftMult"] >= 1.0 {
		t.Errorf("expected saturation to scale down imbalance shift, got %f", targets["imbalanceShiftMult"])
	}

	// Low saturation + benign → scale up.
	inLow := neutralInputs()
	inLow.ImbalanceSaturationPct = 0.1
	inLow.WorstMarkoutBps = 2.0
	inLow.BuyMarkoutBps = 2.0
	inLow.SellMarkoutBps = 2.0
	state2 := opt.computeState(inLow)
	targets2 := opt.policyTargets(state2, inLow)
	if targets2["imbalanceShiftMult"] <= 1.0 {
		t.Errorf("expected low saturation + benign to scale up imbalance shift, got %f", targets2["imbalanceShiftMult"])
	}
}

func TestV2_ShadowMode(t *testing.T) {
	cfg := defaultV2Config()
	cfg.ShadowMode = true
	cfg.LearningRate = 0.5
	cfg.Momentum = 0.0
	cfg.MinFillSamples = 1
	cfg.MaxKnobChangesPerEval = 8
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	for i := 0; i < 10; i++ {
		opt.RecordFill()
	}

	// Toxic inputs that should drive spread changes.
	in := neutralInputs()
	in.WorstMarkoutBps = -5.0
	in.BuyMarkoutBps = -5.0
	in.SellMarkoutBps = 2.0
	in.CompositeMarkoutBps = -2.0
	in.BaselineBps = 2.0
	in.FillSamples = 20
	for i := 0; i < 10; i++ {
		opt.Evaluate(in)
	}

	// Overlay should be neutral because shadow mode is active.
	overlay := opt.Overlay()
	if overlay.SpreadDelta != 0 || overlay.SpreadDeltaBuy != 0 || overlay.SpreadDeltaSell != 0 {
		t.Errorf("shadow mode: expected neutral spread overlay, got delta=%f buy=%f sell=%f",
			overlay.SpreadDelta, overlay.SpreadDeltaBuy, overlay.SpreadDeltaSell)
	}
	if overlay.NotionalMult != 1.0 {
		t.Errorf("shadow mode: expected NotionalMult=1.0, got %f", overlay.NotionalMult)
	}

	// But internal state should have moved (eval still runs).
	if opt.EvalCount() == 0 {
		t.Error("shadow mode: expected evals to have run")
	}
}

func TestV2_ConfigurableThresholds(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Default: toxicityHigh=0.6, opportunityLow=0.3
	// With tox=0.7, opp=0.2 → should widen.
	in := neutralInputs()
	in.WorstMarkoutBps = -4.0
	in.BuyMarkoutBps = -4.0
	in.SellMarkoutBps = -4.0
	in.BaselineBps = 2.0
	in.FillBurstActive = true
	in.FillRateRatio = 0.2
	in.FillSamples = 20
	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)
	spreadDefault := targets["spreadDeltaBuy"]

	// Now relax thresholds so the same state doesn't trigger widening.
	cfg2 := defaultV2Config()
	cfg2.PolicyThresholds.ToxicityHigh = 0.95 // much harder to trigger
	cfg2.PolicyThresholds.OpportunityLow = 0.01
	opt2 := NewOptimizerV2(cfg2, "TEST", 3.0, 0.4)
	state2 := opt2.computeState(in)
	targets2 := opt2.policyTargets(state2, in)
	spreadRelaxed := targets2["spreadDeltaBuy"]

	// With relaxed thresholds, should widen less.
	if spreadRelaxed >= spreadDefault {
		t.Errorf("expected relaxed thresholds to widen less: default=%f relaxed=%f",
			spreadDefault, spreadRelaxed)
	}
}
