package quoting

import (
	"math"
	"testing"
)

func TestRegimeDetector_NotReady(t *testing.T) {
	rd := NewRegimeDetector(10)
	ac, ready := rd.Autocorrelation()
	if ready {
		t.Error("expected not ready before buffer is full")
	}
	if ac != 0 {
		t.Errorf("expected 0 when not ready, got %f", ac)
	}
}

func TestRegimeDetector_TrendingRegime(t *testing.T) {
	rd := NewRegimeDetector(20)
	// Feed trending prices: monotonically increasing.
	price := 100.0
	for i := 0; i < 25; i++ {
		rd.Update(price)
		price *= 1.001 // +0.1% each tick
	}
	ac, ready := rd.Autocorrelation()
	if !ready {
		t.Fatal("expected ready after 25 updates with window=20")
	}
	// Trending should have positive autocorrelation.
	if ac <= 0 {
		t.Errorf("expected positive autocorrelation for trending, got %f", ac)
	}
}

func TestRegimeDetector_MeanRevertingRegime(t *testing.T) {
	rd := NewRegimeDetector(20)
	// Feed oscillating prices: up, down, up, down...
	price := 100.0
	for i := 0; i < 25; i++ {
		rd.Update(price)
		if i%2 == 0 {
			price *= 1.002
		} else {
			price *= 0.998
		}
	}
	ac, ready := rd.Autocorrelation()
	if !ready {
		t.Fatal("expected ready after 25 updates with window=20")
	}
	// Mean-reverting should have negative autocorrelation.
	if ac >= 0 {
		t.Errorf("expected negative autocorrelation for mean-reverting, got %f", ac)
	}
}

func TestRegimeDetector_RandomWalk(t *testing.T) {
	rd := NewRegimeDetector(100)
	// Feed constant price (no returns). Autocorrelation should be 0.
	for i := 0; i < 110; i++ {
		rd.Update(100.0)
	}
	ac, ready := rd.Autocorrelation()
	if !ready {
		t.Fatal("expected ready")
	}
	// All returns are zero → autocorrelation is zero (degenerate case).
	if math.Abs(ac) > 0.01 {
		t.Errorf("expected ~0 autocorrelation for constant prices, got %f", ac)
	}
}

func TestRegimeDetector_SkipsZeroMid(t *testing.T) {
	rd := NewRegimeDetector(10)
	rd.Update(100.0)
	rd.Update(0) // should be ignored
	rd.Update(101.0)
	// Should not crash and should have count=1 (one valid return).
	if rd.count != 1 {
		t.Errorf("expected count=1, got %d", rd.count)
	}
}
