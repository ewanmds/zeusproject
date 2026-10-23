package quoting

import (
	"math"
	"testing"
	"time"
)

func TestInventoryControllerDisabled(t *testing.T) {
	ic := NewInventoryController(0, 2.5, 1.0, 5.0, 0.10, 0.02, 0.05) // halfLife=0 → disabled
	factor := ic.EffectiveSkewFactor(2.5)                            // pass base skew factor
	if factor != 2.5 {
		t.Errorf("disabled controller should return base factor, got %f", factor)
	}
}

func TestInventoryControllerInitial(t *testing.T) {
	ic := NewInventoryController(5.0, 2.5, 1.0, 5.0, 0.10, 0.02, 0.05) // 5 min half-life
	factor := ic.EffectiveSkewFactor(2.5)
	// Before any samples, should return base
	if factor != 2.5 {
		t.Errorf("before samples, should return base factor, got %f", factor)
	}
}

func TestInventoryControllerIncreasesSkew(t *testing.T) {
	ic := NewInventoryController(5.0, 2.5, 1.0, 5.0, 0.10, 0.02, 0.05)
	now := time.Now()
	// Simulate inventory NOT decaying (stuck at 0.8 utilization)
	for i := 0; i < 20; i++ {
		ic.RecordSample(0.8, now.Add(time.Duration(i)*5*time.Second))
	}
	factor := ic.EffectiveSkewFactor(2.5)
	// Inventory is stuck → should increase skew above base
	if factor <= 2.5 {
		t.Errorf("stuck inventory should increase skew, got %f", factor)
	}
}

func TestInventoryControllerDecreasesSkew(t *testing.T) {
	ic := NewInventoryController(5.0, 2.5, 1.0, 5.0, 0.10, 0.02, 0.05)
	now := time.Now()
	// Simulate inventory decaying rapidly (0.8 → 0.1 in 30 seconds)
	for i := 0; i < 20; i++ {
		ratio := 0.8 - float64(i)*0.035
		if ratio < 0.05 {
			ratio = 0.05
		}
		ic.RecordSample(ratio, now.Add(time.Duration(i)*5*time.Second))
	}
	factor := ic.EffectiveSkewFactor(2.5)
	// Inventory is decaying fast → should decrease skew below base
	if factor >= 2.5 {
		t.Errorf("fast-decaying inventory should decrease skew, got %f", factor)
	}
}

func TestInventoryControllerBounds(t *testing.T) {
	ic := NewInventoryController(5.0, 2.5, 1.5, 4.0, 0.10, 0.02, 0.05)
	now := time.Now()
	// Simulate stuck inventory to push skew up
	for i := 0; i < 60; i++ {
		ic.RecordSample(0.95, now.Add(time.Duration(i)*5*time.Second))
	}
	factor := ic.EffectiveSkewFactor(2.5)
	if factor > 4.0 {
		t.Errorf("should be clamped to max=4.0, got %f", factor)
	}
	if factor < 1.5 {
		t.Errorf("should be clamped to min=1.5, got %f", factor)
	}
}

func TestInventoryControllerFlatInventory(t *testing.T) {
	ic := NewInventoryController(5.0, 2.5, 1.0, 5.0, 0.10, 0.02, 0.05)
	now := time.Now()
	// Simulate zero inventory
	for i := 0; i < 20; i++ {
		ic.RecordSample(0.0, now.Add(time.Duration(i)*5*time.Second))
	}
	factor := ic.EffectiveSkewFactor(2.5)
	// No inventory → no adjustment needed, return near base
	if math.Abs(factor-2.5) > 0.5 {
		t.Errorf("zero inventory should return near base, got %f", factor)
	}
}
