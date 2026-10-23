package quoting

import (
	"math"
	"testing"
	"time"
)

func TestFundingUrgency_NeutralFarFromReset(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.0001,
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(59 * time.Minute), // 59 min away, horizon is 30
				lastPoll:        time.Now(),
			},
		},
	}
	// 59 min from reset with 30 min horizon -> timeDecay = max(0, 1-59/30) = 0
	mult := ft.UrgencyMultiplier("BTC", "long")
	if math.Abs(mult-1.0) > 0.001 {
		t.Errorf("expected ~1.0 far from reset, got %f", mult)
	}
}

func TestFundingUrgency_IncreasedWhenPaying(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.0001, // positive rate
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(5 * time.Minute), // 5 min from reset
				lastPoll:        time.Now(),
			},
		},
	}
	// long + positive rate = paying funding
	// timeDecay = max(0, 1 - 5/30) = 5/6 ≈ 0.8333
	// mult = 1 + 1.5 * 0.0001 * 0.8333 ≈ 1.000125
	mult := ft.UrgencyMultiplier("BTC", "long")
	if mult <= 1.0 {
		t.Errorf("expected > 1.0 when paying funding near reset, got %f", mult)
	}
}

func TestFundingUrgency_ReducedWhenReceiving(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.0001, // positive rate
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(5 * time.Minute), // 5 min from reset
				lastPoll:        time.Now(),
			},
		},
	}
	// short + positive rate = receiving funding (fundingCost = rate * -1 < 0)
	mult := ft.UrgencyMultiplier("BTC", "short")
	if mult >= 1.0 {
		t.Errorf("expected < 1.0 when receiving funding near reset, got %f", mult)
	}
}

func TestFundingUrgency_NoData(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates:          make(map[string]fundingState),
	}
	mult := ft.UrgencyMultiplier("BTC", "long")
	if mult != 1.0 {
		t.Errorf("expected 1.0 with no data, got %f", mult)
	}
}

func TestFundingUrgency_NoPosition(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.0001,
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(5 * time.Minute),
				lastPoll:        time.Now(),
			},
		},
	}
	mult := ft.UrgencyMultiplier("BTC", "")
	if mult != 1.0 {
		t.Errorf("expected 1.0 with no position, got %f", mult)
	}
}

func TestFundingUrgency_AtFundingTime(t *testing.T) {
	ft := &FundingTracker{
		urgencyScale:   1.5,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.001, // 10 bps — larger rate for significant effect
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(1 * time.Second), // essentially at reset
				lastPoll:        time.Now(),
			},
		},
	}
	// At funding time: timeDecay ≈ 1.0
	// mult = 1 + 1.5 * 0.001 * ~1.0 = 1.0015
	mult := ft.UrgencyMultiplier("BTC", "long")
	if mult <= 1.001 {
		t.Errorf("expected significant multiplier at funding time, got %f", mult)
	}
	// Verify short side is reduced
	multShort := ft.UrgencyMultiplier("BTC", "short")
	if multShort >= 0.999 {
		t.Errorf("expected significant reduction at funding time for short, got %f", multShort)
	}
}

func TestComputeNextFunding_PreservesExistingFuture(t *testing.T) {
	now := time.Now()
	existing := now.Add(10 * time.Minute)
	result := computeNextFunding(now, 1.0, existing)
	if !result.Equal(existing) {
		t.Errorf("expected existing future time preserved, got %v vs %v", result, existing)
	}
}

func TestComputeNextFunding_ComputesFromEpoch(t *testing.T) {
	now := time.Now()
	result := computeNextFunding(now, 1.0, time.Time{})
	if !result.After(now) {
		t.Errorf("expected future time, got %v (now=%v)", result, now)
	}
	// Should be within 1 hour of now
	if result.Sub(now) > time.Hour {
		t.Errorf("expected within 1 hour, got %v ahead", result.Sub(now))
	}
}

func TestStripDexPrefix(t *testing.T) {
	tests := []struct {
		input, expected string
	}{
		{"xyz:CL", "CL"},
		{"xyz:NATGAS", "NATGAS"},
		{"BTC", "BTC"},
		{"", ""},
	}
	for _, tc := range tests {
		result := stripDexPrefix(tc.input)
		if result != tc.expected {
			t.Errorf("stripDexPrefix(%q) = %q, want %q", tc.input, result, tc.expected)
		}
	}
}

func TestToFloat64(t *testing.T) {
	tests := []struct {
		input    any
		expected float64
		ok       bool
	}{
		{1.5, 1.5, true},
		{float32(1.5), 1.5, true},
		{42, 42.0, true},
		{int64(42), 42.0, true},
		{"0.0001", 0.0001, true},
		{"invalid", 0, false},
	}
	for _, tc := range tests {
		result, ok := toFloat64(tc.input)
		if ok != tc.ok {
			t.Errorf("toFloat64(%v): ok=%v, want %v", tc.input, ok, tc.ok)
		}
		if ok && math.Abs(result-tc.expected) > 1e-10 {
			t.Errorf("toFloat64(%v) = %f, want %f", tc.input, result, tc.expected)
		}
	}
}

func TestFundingUrgency_DexPrefixLookup(t *testing.T) {
	// Rates map is keyed by stripped name ("CL") from the API,
	// but UrgencyMultiplier is called with full name ("xyz:CL") from config.
	ft := &FundingTracker{
		urgencyScale:   2.0,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"CL": { // API returns stripped name
				rate:            0.0001,
				intervalHours:   1.0,
				nextFundingTime: time.Now().Add(5 * time.Minute),
			},
		},
	}
	mult := ft.UrgencyMultiplier("xyz:CL", "long") // config uses full name
	if mult <= 1.0 {
		t.Errorf("expected urgency > 1.0 for xyz:CL looking up CL, got %f", mult)
	}
}

func TestFundingUrgency_StaleTimeDecayCapped(t *testing.T) {
	// When nextFundingTime is in the past (stale data), timeDecay should cap at 1.0
	ft := &FundingTracker{
		urgencyScale:   2.0,
		horizonMinutes: 30.0,
		rates: map[string]fundingState{
			"BTC": {
				rate:            0.001,
				intervalHours:   8.0,
				nextFundingTime: time.Now().Add(-10 * time.Minute), // in the past
			},
		},
	}
	mult := ft.UrgencyMultiplier("BTC", "long")
	// timeDecay capped at 1.0, so mult = 1 + 2.0 * 0.001 * 1.0 = 1.002
	maxExpected := 1.0 + 2.0*0.001*1.0
	if mult > maxExpected+0.001 {
		t.Errorf("expected mult <= %f with stale time (capped decay), got %f", maxExpected, mult)
	}
}

func TestAssetInterval_Override(t *testing.T) {
	ft := &FundingTracker{
		intervalOverride: map[string]float64{
			"BTC": 8.0,
		},
	}
	if v := ft.assetInterval("BTC"); v != 8.0 {
		t.Errorf("expected 8.0 for BTC override, got %f", v)
	}
	if v := ft.assetInterval("ETH"); v != 1.0 {
		t.Errorf("expected 1.0 default for ETH, got %f", v)
	}
}

func TestAssetInterval_DexPrefixFallback(t *testing.T) {
	ft := &FundingTracker{
		intervalOverride: map[string]float64{
			"CL": 1.0,
		},
	}
	// "xyz:CL" should find the "CL" override after stripping prefix
	if v := ft.assetInterval("xyz:CL"); v != 1.0 {
		t.Errorf("expected 1.0 for xyz:CL via stripped prefix, got %f", v)
	}
}
