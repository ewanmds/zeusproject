package quoting

import (
	"math"
	"testing"
)

// Tests for composeGateFactors — the min-factor composition that replaced the
// prior broken divide-out-and-reapply multiplicative pattern for trend_guard /
// flow_gate / regime_gate. A factor of 1.0 means "no suppression"; smaller is
// more conservative.

const compFloatEps = 1e-9

func approxEq(a, b float64) bool {
	return math.Abs(a-b) < compFloatEps
}

// 1. Trend guard alone: factor on its side unchanged; other side unsuppressed.
func TestComposeGateFactors_TrendGuardAlone(t *testing.T) {
	// trend_guard suppresses buy at 0.5; flow_gate and regime_gate neutral.
	buy, sell := composeGateFactors(0.5, 1.0, 1.0, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.5) {
		t.Fatalf("buy: expected 0.5, got %v", buy)
	}
	if !approxEq(sell, 1.0) {
		t.Fatalf("sell: expected 1.0, got %v", sell)
	}

	// Symmetric check on sell side.
	buy, sell = composeGateFactors(1.0, 1.0, 1.0, 0.3, 1.0, 1.0)
	if !approxEq(buy, 1.0) {
		t.Fatalf("buy: expected 1.0, got %v", buy)
	}
	if !approxEq(sell, 0.3) {
		t.Fatalf("sell: expected 0.3, got %v", sell)
	}
}

// 2. Regime gate alone: factor on its side unchanged; other side unsuppressed.
func TestComposeGateFactors_RegimeGateAlone(t *testing.T) {
	buy, sell := composeGateFactors(1.0, 1.0, 0.4, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.4) {
		t.Fatalf("buy: expected 0.4, got %v", buy)
	}
	if !approxEq(sell, 1.0) {
		t.Fatalf("sell: expected 1.0, got %v", sell)
	}
}

// 3. Both trend_guard and regime_gate on the SAME side: result is the min, NOT
// the product. (Prior buggy code multiplied: 0.5 * 0.4 = 0.20. Correct: 0.4.)
func TestComposeGateFactors_SameSideMinNotProduct(t *testing.T) {
	// Buy side: trend_guard=0.5, regime=0.4. Min = 0.4.
	buy, sell := composeGateFactors(0.5, 1.0, 0.4, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.4) {
		t.Fatalf("buy: expected min 0.4, got %v (product would be 0.20)", buy)
	}
	if !approxEq(sell, 1.0) {
		t.Fatalf("sell: expected 1.0, got %v", sell)
	}
	// Assert this is strictly different from the broken multiplicative result.
	if approxEq(buy, 0.5*0.4) {
		t.Fatalf("composition appears multiplicative (double-suppression): got %v", buy)
	}

	// Flip: trend_guard=0.3, regime=0.7. Min = 0.3.
	buy, _ = composeGateFactors(0.3, 1.0, 0.7, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.3) {
		t.Fatalf("buy: expected min 0.3, got %v", buy)
	}
}

// 4. Trend_guard and regime_gate on DIFFERENT sides: each side gets its own
// factor unaffected by the other.
func TestComposeGateFactors_DifferentSidesIndependent(t *testing.T) {
	// trend_guard suppresses buy; regime suppresses sell.
	buy, sell := composeGateFactors(0.5, 1.0, 1.0, 1.0, 1.0, 0.4)
	if !approxEq(buy, 0.5) {
		t.Fatalf("buy: expected 0.5 (trend_guard only), got %v", buy)
	}
	if !approxEq(sell, 0.4) {
		t.Fatalf("sell: expected 0.4 (regime only), got %v", sell)
	}
}

// 5. Three-way composition (trend_guard + flow_gate + regime_gate on same
// side): result is min of all three.
func TestComposeGateFactors_ThreeWaySameSide(t *testing.T) {
	// Buy: trend_guard=0.7, flow_gate=0.5, regime=0.3. Min = 0.3.
	buy, sell := composeGateFactors(0.7, 0.5, 0.3, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.3) {
		t.Fatalf("buy: expected 0.3 (min of three), got %v", buy)
	}
	if !approxEq(sell, 1.0) {
		t.Fatalf("sell: expected 1.0, got %v", sell)
	}

	// Reorder the smallest — min should still pick it up regardless of arg order.
	buy, _ = composeGateFactors(0.2, 0.9, 0.6, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.2) {
		t.Fatalf("buy: expected 0.2, got %v", buy)
	}

	// Three-way on SELL side too.
	_, sell = composeGateFactors(1.0, 1.0, 1.0, 0.8, 0.4, 0.6)
	if !approxEq(sell, 0.4) {
		t.Fatalf("sell: expected 0.4, got %v", sell)
	}
}

// 6. Neutral case (no gate active): both sides pass through unchanged.
func TestComposeGateFactors_Neutral(t *testing.T) {
	buy, sell := composeGateFactors(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
	if !approxEq(buy, 1.0) || !approxEq(sell, 1.0) {
		t.Fatalf("neutral: expected (1.0, 1.0), got (%v, %v)", buy, sell)
	}
}

// 7. Full-freeze factor (0.0) propagates correctly (trend_guard freeze path).
func TestComposeGateFactors_FreezeFactor(t *testing.T) {
	buy, sell := composeGateFactors(0.0, 1.0, 0.5, 1.0, 1.0, 1.0)
	if !approxEq(buy, 0.0) {
		t.Fatalf("buy: expected 0.0 (freeze dominates), got %v", buy)
	}
	if !approxEq(sell, 1.0) {
		t.Fatalf("sell: expected 1.0, got %v", sell)
	}
}
