package quoting

import (
	"testing"
	"time"
)

// fillTrendingWindow feeds enough upward-trending prices to produce positive autocorrelation.
func fillTrendingWindow(rd *RegimeDetector, n int) {
	price := 100.0
	for i := 0; i < n; i++ {
		rd.Update(price)
		price *= 1.001
	}
}

func TestIsTrending_NotReady(t *testing.T) {
	rd := NewRegimeDetector(20)
	// Only a few samples — autocorrelation won't be ready.
	rd.Update(100.0)
	rd.Update(100.1)

	active, dir, _ := rd.IsTrending(time.Now(), 0.35, 2.0, 30*time.Second, 100.1, 100.0, 0.1)
	if active {
		t.Fatal("expected not active when autocorrelation isn't ready")
	}
	if dir != 0 {
		t.Fatalf("expected dir=0, got %d", dir)
	}
}

func TestIsTrending_SignalsMetButDurationNotMet(t *testing.T) {
	rd := NewRegimeDetector(20)
	fillTrendingWindow(rd, 25)

	t0 := time.Now()
	shortV, longV, sigma := 110.0, 100.0, 1.0 // divergence = 10σ >> 2σ

	active, _, _ := rd.IsTrending(t0, 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if active {
		t.Fatal("expected not active at first qualifying call (duration not met)")
	}

	// Advance less than min duration.
	active, _, _ = rd.IsTrending(t0.Add(10*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if active {
		t.Fatal("expected not active at 10s (< 30s min duration)")
	}
}

func TestIsTrending_DurationMetActivates(t *testing.T) {
	rd := NewRegimeDetector(20)
	fillTrendingWindow(rd, 25)

	t0 := time.Now()
	shortV, longV, sigma := 110.0, 100.0, 1.0
	// Seed pending.
	rd.IsTrending(t0, 0.35, 2.0, 30*time.Second, shortV, longV, sigma)

	// Advance past duration.
	active, dir, since := rd.IsTrending(t0.Add(31*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if !active {
		t.Fatal("expected active after duration met")
	}
	if dir != 1 {
		t.Fatalf("expected dir=+1 (shortV > longV), got %d", dir)
	}
	if since < 0 {
		t.Fatalf("since should be non-negative, got %v", since)
	}
}

func TestIsTrending_SignalDropDeactivatesImmediately(t *testing.T) {
	rd := NewRegimeDetector(20)
	fillTrendingWindow(rd, 25)

	t0 := time.Now()
	shortV, longV, sigma := 110.0, 100.0, 1.0
	rd.IsTrending(t0, 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	rd.IsTrending(t0.Add(31*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)

	// Divergence collapses — signals fail.
	active, _, _ := rd.IsTrending(t0.Add(32*time.Second), 0.35, 2.0, 30*time.Second, 100.5, 100.0, 1.0)
	if active {
		t.Fatal("expected immediate deactivation when divergence drops below threshold")
	}
}

func TestIsTrending_PendingResets(t *testing.T) {
	rd := NewRegimeDetector(20)
	fillTrendingWindow(rd, 25)

	t0 := time.Now()
	shortV, longV, sigma := 110.0, 100.0, 1.0

	// 20s qualified (below 30s duration).
	rd.IsTrending(t0, 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	active, _, _ := rd.IsTrending(t0.Add(20*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if active {
		t.Fatal("should not yet be active at 20s")
	}

	// Drop signal.
	rd.IsTrending(t0.Add(22*time.Second), 0.35, 2.0, 30*time.Second, 100.5, 100.0, 1.0)

	// Re-qualify — pending must restart.
	rd.IsTrending(t0.Add(23*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	// Only 20s past re-qualification, should still be inactive.
	active, _, _ = rd.IsTrending(t0.Add(43*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if active {
		t.Fatal("pending did not reset; became active too early after re-qualification")
	}
	// 31s after re-qualification — now active.
	active, _, _ = rd.IsTrending(t0.Add(55*time.Second), 0.35, 2.0, 30*time.Second, shortV, longV, sigma)
	if !active {
		t.Fatal("expected active 31s after re-qualification (pending should have restarted)")
	}
}

func TestIsTrending_DirectionSign(t *testing.T) {
	// Uptrend: shortV > longV → dir=+1.
	rdUp := NewRegimeDetector(20)
	fillTrendingWindow(rdUp, 25)
	t0 := time.Now()
	rdUp.IsTrending(t0, 0.35, 2.0, 30*time.Second, 110.0, 100.0, 1.0)
	_, dir, _ := rdUp.IsTrending(t0.Add(31*time.Second), 0.35, 2.0, 30*time.Second, 110.0, 100.0, 1.0)
	if dir != 1 {
		t.Fatalf("uptrend: expected dir=+1, got %d", dir)
	}

	// Downtrend: shortV < longV → dir=-1. Use slightly varying drops to avoid
	// a degenerate constant log-return window (which would short-circuit
	// autocorrelation to 0 via the zero-variance branch).
	rdDown := NewRegimeDetector(20)
	price := 100.0
	for i := 0; i < 25; i++ {
		rdDown.Update(price)
		if i%2 == 0 {
			price *= 0.999
		} else {
			price *= 0.9995
		}
	}
	rdDown.IsTrending(t0, 0.35, 2.0, 30*time.Second, 90.0, 100.0, 1.0)
	_, dirDown, _ := rdDown.IsTrending(t0.Add(31*time.Second), 0.35, 2.0, 30*time.Second, 90.0, 100.0, 1.0)
	if dirDown != -1 {
		t.Fatalf("downtrend: expected dir=-1, got %d", dirDown)
	}
}
