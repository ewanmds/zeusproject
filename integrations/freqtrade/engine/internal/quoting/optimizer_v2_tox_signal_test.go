package quoting

import (
	"testing"
)

// V24 tuning: regression test pinning the invariant that committing the v23 raw
// EWMA change (commit af1ed18) is supposed to enforce.
//
// Scenario: the markout EWMA on the buy side is toxic (0 bps vs 8 bps baseline)
// while the sell side is calm (markout equal to baseline). The policy must widen
// the buy-side spread MORE than the sell-side spread — i.e. it must respond to
// the toxic side, not the calm side.
//
// V23 log forensics (03:27 evals 821/823) showed the opposite pattern: with
// tox_buy high the optimizer widened spreadDeltaSell and held spreadDeltaBuy flat.
// That regressed the intended af1ed18 behavior. This test pins the right answer.
func TestOptimizerV2_ToxSignal_BuyToxicWidensBuyMore(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := neutralInputs()
	in.BaselineBps = 8.0
	// Buy side is toxic (markout 0 vs baseline 8 → tox = 1.0)
	// Sell side is calm (markout 8 vs baseline 8 → tox = 0.0)
	in.BuyMarkoutBps = 0.0
	in.SellMarkoutBps = 8.0
	// Ensure the composite and worst signals don't mask the asymmetry.
	in.CompositeMarkoutBps = 4.0
	in.WorstMarkoutBps = 0.0

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)

	buy := targets["spreadDeltaBuy"]
	sell := targets["spreadDeltaSell"]

	if !(buy > sell) {
		t.Fatalf("tox_buy >> tox_sell but spreadDeltaBuy (%.4f) <= spreadDeltaSell (%.4f); "+
			"optimizer must widen the toxic side (buy), not the calm side", buy, sell)
	}
}

// Symmetric test: when sell is the toxic side, sell spread widens more than buy.
func TestOptimizerV2_ToxSignal_SellToxicWidensSellMore(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := neutralInputs()
	in.BaselineBps = 8.0
	in.BuyMarkoutBps = 8.0
	in.SellMarkoutBps = 0.0
	in.CompositeMarkoutBps = 4.0
	in.WorstMarkoutBps = 0.0

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)

	buy := targets["spreadDeltaBuy"]
	sell := targets["spreadDeltaSell"]

	if !(sell > buy) {
		t.Fatalf("tox_sell >> tox_buy but spreadDeltaSell (%.4f) <= spreadDeltaBuy (%.4f); "+
			"optimizer must widen the toxic side (sell), not the calm side", sell, buy)
	}
}

// Symmetric-toxicity sanity check: when both sides are equally toxic, the per-side
// shift must be zero (base spread still applies; the asymmetric shift cancels).
func TestOptimizerV2_ToxSignal_SymmetricToxicityNoAsymShift(t *testing.T) {
	cfg := defaultV2Config()
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	in := neutralInputs()
	in.BaselineBps = 8.0
	in.BuyMarkoutBps = 0.0
	in.SellMarkoutBps = 0.0
	in.CompositeMarkoutBps = 0.0
	in.WorstMarkoutBps = 0.0

	state := opt.computeState(in)
	targets := opt.policyTargets(state, in)

	diff := targets["spreadDeltaBuy"] - targets["spreadDeltaSell"]
	if diff > 0.01 || diff < -0.01 {
		t.Fatalf("symmetric tox but spreadDeltaBuy - spreadDeltaSell = %.4f; expected ~0", diff)
	}
}
