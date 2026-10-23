package quoting

import (
	"math"
	"testing"
	"time"
)

func TestRateLimitAllocator_SingleAsset(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL"}, 5*time.Minute, 0.7, 3.0)

	// Before any fills or evaluation.
	if m := rla.RefreshMultiplier("xyz:CL"); m != 1.0 {
		t.Errorf("expected 1.0 for single asset before Evaluate, got %f", m)
	}

	// Record some activity and evaluate.
	rla.RecordFill("xyz:CL", 3.5, 500)
	rla.RecordAPICall("xyz:CL")
	rla.Evaluate()

	// Still 1.0 for single asset even after Evaluate.
	if m := rla.RefreshMultiplier("xyz:CL"); m != 1.0 {
		t.Errorf("expected 1.0 for single asset after Evaluate, got %f", m)
	}
}

func TestRateLimitAllocator_HighROIGetsFasterRefresh(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH"}, 5*time.Minute, 0.7, 3.0)

	// CL: high capture, decent volume, few API calls -> high ROI.
	rla.RecordFill("xyz:CL", 5.0, 1000)
	rla.RecordFill("xyz:CL", 4.0, 900)
	for i := 0; i < 10; i++ {
		rla.RecordAPICall("xyz:CL")
	}

	// ETH: negative capture (toxic fills), some volume, many API calls -> low ROI.
	rla.RecordFill("ETH", -2.0, 500)
	rla.RecordFill("ETH", -1.5, 400)
	for i := 0; i < 50; i++ {
		rla.RecordAPICall("ETH")
	}

	rla.Evaluate()

	clMult := rla.RefreshMultiplier("xyz:CL")
	ethMult := rla.RefreshMultiplier("ETH")

	if clMult >= ethMult {
		t.Errorf("expected CL mult < ETH mult (CL high ROI, ETH negative), got CL=%f, ETH=%f", clMult, ethMult)
	}

	// CL should be at minMult (fastest), ETH at maxMult (slowest).
	if math.Abs(clMult-0.7) > 0.001 {
		t.Errorf("expected CL mult ≈ 0.7 (minMult), got %f", clMult)
	}
	if math.Abs(ethMult-3.0) > 0.001 {
		t.Errorf("expected ETH mult ≈ 3.0 (maxMult), got %f", ethMult)
	}
}

func TestRateLimitAllocator_UnknownAsset(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH"}, 5*time.Minute, 0.7, 3.0)

	// Unknown asset returns 1.0.
	if m := rla.RefreshMultiplier("DOGE"); m != 1.0 {
		t.Errorf("expected 1.0 for unknown asset, got %f", m)
	}

	// Also works after Evaluate.
	rla.RecordFill("xyz:CL", 3.0, 500)
	rla.RecordAPICall("xyz:CL")
	rla.Evaluate()

	if m := rla.RefreshMultiplier("DOGE"); m != 1.0 {
		t.Errorf("expected 1.0 for unknown asset after Evaluate, got %f", m)
	}
}

func TestRateLimitAllocator_BeforeFirstEvaluate(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH"}, 5*time.Minute, 0.7, 3.0)

	// Before Evaluate, all ROI scores are zero -> equal -> returns 1.0.
	if m := rla.RefreshMultiplier("xyz:CL"); m != 1.0 {
		t.Errorf("expected 1.0 before first Evaluate, got %f", m)
	}
	if m := rla.RefreshMultiplier("ETH"); m != 1.0 {
		t.Errorf("expected 1.0 before first Evaluate, got %f", m)
	}
}

func TestRateLimitAllocator_EWMAAlpha(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL"}, 60*time.Second, 0.7, 3.0)
	alpha := rla.ewmaAlpha(60 * time.Second)
	// At halfLife, alpha = 1 - 2^(-1) = 0.5.
	if math.Abs(alpha-0.5) > 0.001 {
		t.Errorf("expected alpha ≈ 0.5 at half-life, got %f", alpha)
	}
}

func TestRateLimitAllocator_EvaluateResetsCounters(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH"}, 5*time.Minute, 0.7, 3.0)

	rla.RecordFill("xyz:CL", 3.0, 500)
	rla.RecordAPICall("xyz:CL")
	rla.RecordAPICall("xyz:CL")
	rla.Evaluate()

	// After Evaluate, apiCalls should be reset.
	rla.mu.Lock()
	clCalls := rla.perAsset["xyz:CL"].apiCalls
	rla.mu.Unlock()

	if clCalls != 0 {
		t.Errorf("expected apiCalls=0 after Evaluate, got %d", clCalls)
	}
}

func TestRateLimitAllocator_AssetROIScores(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH"}, 5*time.Minute, 0.7, 3.0)

	rla.RecordFill("xyz:CL", 4.0, 1000)
	for i := 0; i < 10; i++ {
		rla.RecordAPICall("xyz:CL")
	}

	rla.RecordFill("ETH", 1.0, 200)
	for i := 0; i < 20; i++ {
		rla.RecordAPICall("ETH")
	}

	rla.Evaluate()

	scores := rla.AssetROIScores()
	if len(scores) != 2 {
		t.Fatalf("expected 2 scores, got %d", len(scores))
	}

	// CL: 4.0 * 1000 / 10 = 400.
	// ETH: 1.0 * 200 / 20 = 10.
	if scores["xyz:CL"] <= scores["ETH"] {
		t.Errorf("expected CL ROI > ETH ROI, got CL=%f, ETH=%f", scores["xyz:CL"], scores["ETH"])
	}
}

func TestRateLimitAllocator_ThreeAssets(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL", "ETH", "BTC"}, 5*time.Minute, 0.7, 3.0)

	// CL: best ROI.
	rla.RecordFill("xyz:CL", 5.0, 1000)
	for i := 0; i < 5; i++ {
		rla.RecordAPICall("xyz:CL")
	}

	// ETH: medium ROI.
	rla.RecordFill("ETH", 2.0, 500)
	for i := 0; i < 10; i++ {
		rla.RecordAPICall("ETH")
	}

	// BTC: worst ROI.
	rla.RecordFill("BTC", 0.5, 300)
	for i := 0; i < 20; i++ {
		rla.RecordAPICall("BTC")
	}

	rla.Evaluate()

	clMult := rla.RefreshMultiplier("xyz:CL")
	ethMult := rla.RefreshMultiplier("ETH")
	btcMult := rla.RefreshMultiplier("BTC")

	// Ordering: CL < ETH < BTC (best ROI gets lowest mult).
	if clMult >= ethMult {
		t.Errorf("expected CL mult < ETH mult, got CL=%f, ETH=%f", clMult, ethMult)
	}
	if ethMult >= btcMult {
		t.Errorf("expected ETH mult < BTC mult, got ETH=%f, BTC=%f", ethMult, btcMult)
	}

	// Extremes: CL at minMult, BTC at maxMult.
	if math.Abs(clMult-0.7) > 0.001 {
		t.Errorf("expected CL mult ≈ 0.7, got %f", clMult)
	}
	if math.Abs(btcMult-3.0) > 0.001 {
		t.Errorf("expected BTC mult ≈ 3.0, got %f", btcMult)
	}

	// ETH should be somewhere in between.
	if ethMult <= 0.7 || ethMult >= 3.0 {
		t.Errorf("expected ETH mult in (0.7, 3.0), got %f", ethMult)
	}
}

func TestRateLimitAllocator_RecordFillIgnoresUnknown(t *testing.T) {
	rla := NewRateLimitAllocator([]string{"xyz:CL"}, 5*time.Minute, 0.7, 3.0)

	// Should not panic for unknown assets.
	rla.RecordFill("DOGE", 5.0, 1000)
	rla.RecordAPICall("DOGE")
}
