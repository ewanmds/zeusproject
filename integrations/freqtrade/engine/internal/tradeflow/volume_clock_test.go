package tradeflow

import (
	"math"
	"testing"
	"time"
)

// Compile-time interface checks: both types must satisfy ImbalanceCalculator.
var _ ImbalanceCalculator = (*Calculator)(nil)
var _ ImbalanceCalculator = (*VolumeClockCalculator)(nil)

func TestVolumeClockImplementsInterface(t *testing.T) {
	// Compile-time check above is sufficient; this test just documents intent.
	var ic ImbalanceCalculator
	ic = NewCalculator(3*time.Second, 1.0)
	_ = ic
	ic = NewVolumeClockCalculator(1000, 10, 1.0)
	_ = ic
}

func TestVolumeClockNoCompletedBuckets(t *testing.T) {
	vc := NewVolumeClockCalculator(1000, 10, 1.0)

	// No trades at all.
	_, ready := vc.Imbalance()
	if ready {
		t.Fatal("expected not ready with no trades")
	}

	// Add a trade that doesn't fill a bucket.
	vc.AddTrade(500, "buy", time.Now())
	_, ready = vc.Imbalance()
	if ready {
		t.Fatal("expected not ready with no completed buckets")
	}
}

func TestVolumeClockSingleBucket(t *testing.T) {
	vc := NewVolumeClockCalculator(1000, 10, 1.0)
	now := time.Now()

	// Fill one bucket: 700 buy + 300 sell = 1000
	vc.AddTrade(700, "buy", now)
	vc.AddTrade(300, "sell", now)

	imb, ready := vc.Imbalance()
	if !ready {
		t.Fatal("expected ready after one completed bucket")
	}
	// (700 - 300) / (700 + 300) = 400/1000 = 0.4
	if !almostEqual(imb, 0.4, 0.001) {
		t.Fatalf("expected imbalance 0.4, got %f", imb)
	}
}

func TestVolumeClockMultipleBuckets(t *testing.T) {
	vc := NewVolumeClockCalculator(100, 10, 1.0)
	now := time.Now()

	// Fill 3 pure-buy buckets.
	for i := 0; i < 3; i++ {
		vc.AddTrade(100, "buy", now)
	}

	imb, ready := vc.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	// All buy: imbalance = 1.0
	if !almostEqual(imb, 1.0, 0.001) {
		t.Fatalf("expected imbalance 1.0, got %f", imb)
	}
}

func TestVolumeClockCircularBuffer(t *testing.T) {
	// numBuckets = 3, so oldest are evicted.
	vc := NewVolumeClockCalculator(100, 3, 1.0)
	now := time.Now()

	// Fill 3 pure-buy buckets.
	for i := 0; i < 3; i++ {
		vc.AddTrade(100, "buy", now)
	}
	imb1, _ := vc.Imbalance()
	if !almostEqual(imb1, 1.0, 0.001) {
		t.Fatalf("expected 1.0 after 3 buy buckets, got %f", imb1)
	}

	// Fill 3 pure-sell buckets (evicts the 3 buy buckets).
	for i := 0; i < 3; i++ {
		vc.AddTrade(100, "sell", now)
	}
	imb2, ready := vc.Imbalance()
	if !ready {
		t.Fatal("expected ready")
	}
	// Now only sell buckets remain.
	if !almostEqual(imb2, -1.0, 0.001) {
		t.Fatalf("expected -1.0 after sell buckets evict buy buckets, got %f", imb2)
	}
}

func TestVolumeClockLargeTradeOverflow(t *testing.T) {
	// Bucket size 100, numBuckets 10.
	vc := NewVolumeClockCalculator(100, 10, 1.0)
	now := time.Now()

	// Single trade of 350 should fill 3 complete buckets (100 each)
	// with 50 remaining in the current (incomplete) bucket.
	vc.AddTrade(350, "buy", now)

	imb, ready := vc.Imbalance()
	if !ready {
		t.Fatal("expected ready after large trade fills multiple buckets")
	}
	// 3 completed buckets, all pure buy.
	if !almostEqual(imb, 1.0, 0.001) {
		t.Fatalf("expected imbalance 1.0 from pure-buy overflow, got %f", imb)
	}

	// Verify count is 3 (not more).
	vc.mu.RLock()
	count := vc.count
	currentTotal := vc.currentTotal
	vc.mu.RUnlock()

	if count != 3 {
		t.Fatalf("expected 3 completed buckets, got %d", count)
	}
	if !almostEqual(currentTotal, 50, 0.001) {
		t.Fatalf("expected 50 remaining in current bucket, got %f", currentTotal)
	}
}

func TestVolumeClockSizeExponent(t *testing.T) {
	// With exponent 0.5 (sqrt), a trade of size 100 becomes sqrt(100)=10.
	vc := NewVolumeClockCalculator(10, 10, 0.5)
	now := time.Now()

	// Trade of raw size 100: weighted = 100^0.5 = 10 (fills exactly 1 bucket).
	vc.AddTrade(100, "buy", now)

	imb, ready := vc.Imbalance()
	if !ready {
		t.Fatal("expected ready after weighted trade fills bucket")
	}
	if !almostEqual(imb, 1.0, 0.001) {
		t.Fatalf("expected 1.0, got %f", imb)
	}

	// Verify that the weighted size (10) was used, not the raw size (100).
	// With bucket size 10, raw size 100 would fill 10 buckets.
	// With exponent 0.5, weighted size 10 fills exactly 1 bucket.
	vc.mu.RLock()
	count := vc.count
	vc.mu.RUnlock()
	if count != 1 {
		t.Fatalf("expected 1 completed bucket (weighted size), got %d (raw size would give 10)", count)
	}

	// Verify exponent < 1 produces smaller weighted value.
	raw := 100.0
	weighted := math.Pow(raw, 0.5)
	if weighted >= raw {
		t.Fatal("exponent < 1 should produce smaller weighted value")
	}
}

func TestVolumeClockSaturation(t *testing.T) {
	vc := NewVolumeClockCalculator(100, 10, 1.0)
	now := time.Now()

	// No readings yet.
	if r := vc.SaturationRatio(); r != 0 {
		t.Fatalf("expected 0 saturation ratio with no readings, got %f", r)
	}

	// Fill one pure-buy bucket (imbalance = +1.0, saturated).
	vc.AddTrade(100, "buy", now)

	// Record 32 saturated readings.
	for i := 0; i < 32; i++ {
		imb, ready := vc.ImbalanceAndRecord()
		if !ready {
			t.Fatal("expected ready")
		}
		if !almostEqual(imb, 1.0, 0.001) {
			t.Fatalf("expected saturated imbalance, got %f", imb)
		}
	}

	r := vc.SaturationRatio()
	if !almostEqual(r, 1.0, 0.01) {
		t.Fatalf("expected saturation ratio ~1.0, got %f", r)
	}

	// Add balanced bucket (imbalance now = ~0.5 across 2 buckets).
	vc.AddTrade(50, "buy", now)
	vc.AddTrade(50, "sell", now)

	// Record 32 non-saturated readings.
	for i := 0; i < 32; i++ {
		vc.ImbalanceAndRecord()
	}

	// Half saturated, half not.
	r2 := vc.SaturationRatio()
	if !almostEqual(r2, 0.5, 0.05) {
		t.Fatalf("expected saturation ratio ~0.5, got %f", r2)
	}

	// Fill ring buffer with non-saturated.
	for i := 0; i < 32; i++ {
		vc.ImbalanceAndRecord()
	}
	r3 := vc.SaturationRatio()
	if !almostEqual(r3, 0.0, 0.05) {
		t.Fatalf("expected saturation ratio ~0.0, got %f", r3)
	}
}
