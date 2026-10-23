package tradeflow

import (
	"math"
	"testing"
	"time"
)

func almostEqual(a, b, tol float64) bool {
	return math.Abs(a-b) < tol
}

func TestNotReadyBeforeTrades(t *testing.T) {
	c := NewCalculator(3*time.Second, 1.0)
	_, ready := c.Imbalance()
	if ready {
		t.Fatal("expected not ready before any trades")
	}
}

func TestPureBuyImbalance(t *testing.T) {
	c := NewCalculator(3*time.Second, 1.0)
	now := time.Now()
	c.AddTrade(10.0, "buy", now)
	c.AddTrade(5.0, "buy", now)

	imb, ready := c.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	if !almostEqual(imb, 1.0, 0.01) {
		t.Fatalf("expected imbalance ~1.0, got %f", imb)
	}
}

func TestPureSellImbalance(t *testing.T) {
	c := NewCalculator(3*time.Second, 1.0)
	now := time.Now()
	c.AddTrade(10.0, "sell", now)

	imb, ready := c.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	if !almostEqual(imb, -1.0, 0.01) {
		t.Fatalf("expected imbalance ~-1.0, got %f", imb)
	}
}

func TestBalancedFlow(t *testing.T) {
	c := NewCalculator(3*time.Second, 1.0)
	now := time.Now()
	c.AddTrade(10.0, "buy", now)
	c.AddTrade(10.0, "sell", now)

	imb, ready := c.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	if !almostEqual(imb, 0.0, 0.01) {
		t.Fatalf("expected imbalance ~0.0, got %f", imb)
	}
}

func TestTimeEviction(t *testing.T) {
	c := NewCalculator(100*time.Millisecond, 1.0)
	old := time.Now().Add(-200 * time.Millisecond)
	c.AddTrade(10.0, "buy", old)

	// Old trade should be evicted
	_, ready := c.Imbalance()
	if ready {
		t.Fatal("expected not ready after eviction")
	}

	// Add fresh trade
	c.AddTrade(5.0, "sell", time.Now())
	imb, ready := c.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	if !almostEqual(imb, -1.0, 0.01) {
		t.Fatalf("expected pure sell imbalance, got %f", imb)
	}
}

func TestDriftCorrection(t *testing.T) {
	c := NewCalculator(1*time.Hour, 1.0) // long window so nothing evicts naturally
	now := time.Now()

	// Add enough trades to trigger drift correction
	for i := 0; i < 600; i++ {
		c.AddTrade(1.0, "buy", now.Add(-time.Duration(600-i)*time.Millisecond))
	}

	imb, ready := c.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	if !almostEqual(imb, 1.0, 0.01) {
		t.Fatalf("expected imbalance ~1.0 after drift correction, got %f", imb)
	}
}

func TestSaturationRatio(t *testing.T) {
	c := NewCalculator(1*time.Hour, 1.0)
	now := time.Now()

	// No readings yet → 0
	if r := c.SaturationRatio(); r != 0 {
		t.Fatalf("expected 0 saturation ratio with no readings, got %f", r)
	}

	// Add trades that create 100% buy imbalance (saturated at +1.0)
	c.AddTrade(100.0, "buy", now)
	for i := 0; i < 32; i++ {
		imb, ready := c.ImbalanceAndRecord()
		if !ready {
			t.Fatal("expected ready")
		}
		if !almostEqual(imb, 1.0, 0.01) {
			t.Fatalf("expected saturated imbalance, got %f", imb)
		}
	}

	// All 32 readings should be saturated
	r := c.SaturationRatio()
	if !almostEqual(r, 1.0, 0.01) {
		t.Fatalf("expected saturation ratio ~1.0, got %f", r)
	}

	// Add balanced trades to de-saturate
	c.AddTrade(100.0, "sell", now)
	for i := 0; i < 32; i++ {
		c.ImbalanceAndRecord()
	}

	// Now half are saturated (first 32) and half are not (last 32) = ~0.5
	r2 := c.SaturationRatio()
	if !almostEqual(r2, 0.5, 0.05) {
		t.Fatalf("expected saturation ratio ~0.5, got %f", r2)
	}

	// Fill the rest with non-saturated to push out all saturated entries
	for i := 0; i < 32; i++ {
		c.ImbalanceAndRecord()
	}
	r3 := c.SaturationRatio()
	if !almostEqual(r3, 0.0, 0.05) {
		t.Fatalf("expected saturation ratio ~0.0, got %f", r3)
	}
}

func TestSizeExponentDefault(t *testing.T) {
	// Exponent 1.0 should produce identical results to legacy (raw sizes).
	now := time.Now()

	legacy := NewCalculator(3*time.Second, 1.0)
	legacy.AddTrade(10.0, "buy", now)
	legacy.AddTrade(4.0, "sell", now)
	legacy.AddTrade(6.0, "buy", now)

	// With exponent 1.0, weighted = size^1.0 = size, so results are identical.
	imbLeg, readyLeg := legacy.Imbalance()
	if !readyLeg {
		t.Fatal("expected ready")
	}

	// Manual: buy=16, sell=4, imbalance = (16-4)/(16+4) = 12/20 = 0.6
	if !almostEqual(imbLeg, 0.6, 0.001) {
		t.Fatalf("expected imbalance 0.6 with exponent 1.0, got %f", imbLeg)
	}

	// Zero exponent should default to 1.0 (same result).
	zeroExp := NewCalculator(3*time.Second, 0)
	zeroExp.AddTrade(10.0, "buy", now)
	zeroExp.AddTrade(4.0, "sell", now)
	zeroExp.AddTrade(6.0, "buy", now)
	imbZero, readyZero := zeroExp.Imbalance()
	if !readyZero {
		t.Fatal("expected ready")
	}
	if !almostEqual(imbZero, imbLeg, 0.0001) {
		t.Fatalf("zero exponent should default to 1.0: got %f, expected %f", imbZero, imbLeg)
	}

	// Negative exponent should also default to 1.0.
	negExp := NewCalculator(3*time.Second, -0.5)
	negExp.AddTrade(10.0, "buy", now)
	negExp.AddTrade(4.0, "sell", now)
	negExp.AddTrade(6.0, "buy", now)
	imbNeg, readyNeg := negExp.Imbalance()
	if !readyNeg {
		t.Fatal("expected ready")
	}
	if !almostEqual(imbNeg, imbLeg, 0.0001) {
		t.Fatalf("negative exponent should default to 1.0: got %f, expected %f", imbNeg, imbLeg)
	}
}

func TestSizeExponentSuperLinear(t *testing.T) {
	// Exponent > 1.0 amplifies large trades relative to small ones.
	// Scenario: one large buy (100) vs ten small sells (10 each).
	// Raw volumes: buy=100, sell=100 → imbalance = 0.0
	// With exponent 1.2: buy = 100^1.2 ≈ 251.19, sell = 10 × 10^1.2 ≈ 10 × 15.85 = 158.49
	// → imbalance = (251.19 - 158.49) / (251.19 + 158.49) ≈ 92.70 / 409.68 ≈ 0.226
	now := time.Now()

	// First verify exponent 1.0 gives balanced result.
	linear := NewCalculator(3*time.Second, 1.0)
	linear.AddTrade(100.0, "buy", now)
	for i := 0; i < 10; i++ {
		linear.AddTrade(10.0, "sell", now)
	}
	imbLin, readyLin := linear.Imbalance()
	if !readyLin {
		t.Fatal("expected ready")
	}
	if !almostEqual(imbLin, 0.0, 0.001) {
		t.Fatalf("expected balanced imbalance with exponent 1.0, got %f", imbLin)
	}

	// Now with exponent 1.2, the single large buy should dominate.
	super := NewCalculator(3*time.Second, 1.2)
	super.AddTrade(100.0, "buy", now)
	for i := 0; i < 10; i++ {
		super.AddTrade(10.0, "sell", now)
	}
	imbSup, readySup := super.Imbalance()
	if !readySup {
		t.Fatal("expected ready")
	}

	// Imbalance should be positive (large buy dominates).
	if imbSup <= 0 {
		t.Fatalf("expected positive imbalance with super-linear exponent, got %f", imbSup)
	}
	// Check approximate value: (100^1.2 - 10*10^1.2) / (100^1.2 + 10*10^1.2)
	buyW := math.Pow(100, 1.2)
	sellW := 10 * math.Pow(10, 1.2)
	expected := (buyW - sellW) / (buyW + sellW)
	if !almostEqual(imbSup, expected, 0.001) {
		t.Fatalf("expected imbalance ~%.4f with exponent 1.2, got %f", expected, imbSup)
	}

	// Sub-linear exponent (0.5 = sqrt) should dampen whale impact.
	sub := NewCalculator(3*time.Second, 0.5)
	sub.AddTrade(100.0, "buy", now)
	for i := 0; i < 10; i++ {
		sub.AddTrade(10.0, "sell", now)
	}
	imbSub, readySub := sub.Imbalance()
	if !readySub {
		t.Fatal("expected ready")
	}
	// sqrt(100) = 10, 10*sqrt(10) ≈ 31.62 → imbalance = (10-31.62)/(10+31.62) < 0
	// The whale's buy is now worth less than the many small sells.
	if imbSub >= 0 {
		t.Fatalf("expected negative imbalance with sub-linear exponent (whale dampened), got %f", imbSub)
	}
}
