package vwap

import (
	"math"
	"testing"
	"time"
)

func almostEqual(a, b, eps float64) bool {
	return math.Abs(a-b) < eps
}

func TestNotReadyBeforeFirstTrade(t *testing.T) {
	c := NewCalculator(30 * time.Minute)
	_, ok := c.VWAP()
	if ok {
		t.Error("expected VWAP not ready before any trades")
	}
}

func TestSingleTrade(t *testing.T) {
	c := NewCalculator(30 * time.Minute)
	c.AddTrade(70.0, 10.0, time.Now())

	v, ok := c.VWAP()
	if !ok {
		t.Fatal("expected VWAP to be ready")
	}
	if !almostEqual(v, 70.0, 1e-9) {
		t.Errorf("VWAP = %f, want 70.0", v)
	}
}

func TestMultipleTrades(t *testing.T) {
	c := NewCalculator(30 * time.Minute)
	now := time.Now()
	c.AddTrade(70.0, 100.0, now)
	c.AddTrade(71.0, 200.0, now.Add(time.Second))

	v, ok := c.VWAP()
	if !ok {
		t.Fatal("expected VWAP to be ready")
	}
	expected := (70.0*100.0 + 71.0*200.0) / 300.0
	if !almostEqual(v, expected, 1e-6) {
		t.Errorf("VWAP = %f, want %f", v, expected)
	}
}

func TestTimeEviction(t *testing.T) {
	window := 10 * time.Second
	c := NewCalculator(window)

	base := time.Now().Add(-20 * time.Second)
	c.AddTrade(70.0, 100.0, base)
	c.AddTrade(72.0, 50.0, time.Now())

	v, ok := c.VWAP()
	if !ok {
		t.Fatal("expected VWAP to be ready")
	}
	if !almostEqual(v, 72.0, 1e-9) {
		t.Errorf("VWAP = %f, want 72.0 (old trade should be evicted)", v)
	}
}

func TestAllTradesExpired(t *testing.T) {
	window := 1 * time.Millisecond
	c := NewCalculator(window)

	c.AddTrade(70.0, 100.0, time.Now().Add(-time.Second))

	_, ok := c.VWAP()
	if ok {
		t.Error("expected VWAP not ready when all trades are expired")
	}
}

func TestEvictionNoDrift(t *testing.T) {
	window := 10 * time.Second
	c := NewCalculator(window)
	base := time.Now()

	// Insert 5000 trades, each 1ms apart with slightly varying prices.
	for i := 0; i < 5000; i++ {
		px := 70.0 + float64(i)*0.001
		c.AddTrade(px, 1.0, base.Add(time.Duration(i)*time.Millisecond))
	}

	// Only the last ~10s worth of trades remain (10000 trades at 1ms each,
	// but we only inserted 5000 at 1ms so they all fit in the 10s window).
	// Insert one more trade far in the future to evict most of them.
	futureTS := base.Add(15 * time.Second)
	c.AddTrade(80.0, 1.0, futureTS)

	// Compute expected VWAP from scratch over remaining trades.
	c.mu.RLock()
	var expectPV, expectV float64
	for _, tr := range c.trades {
		expectPV += tr.price * tr.size
		expectV += tr.size
	}
	c.mu.RUnlock()

	got, ok := c.VWAP()
	if !ok {
		t.Fatal("expected VWAP to be ready")
	}
	want := expectPV / expectV
	if !almostEqual(got, want, 1e-12) {
		t.Errorf("VWAP drift: got %v, want %v, diff %e", got, want, got-want)
	}
}

func TestPriceStddevNotReadyEmpty(t *testing.T) {
	c := NewCalculator(time.Hour)
	if v, ok := c.PriceStddev(); ok || v != 0 {
		t.Errorf("expected (0,false) for empty calc, got (%v,%v)", v, ok)
	}
}

func TestPriceStddevNotReadyUnderTwenty(t *testing.T) {
	c := NewCalculator(time.Hour)
	now := time.Now()
	for i := 0; i < 19; i++ {
		c.AddTrade(70.0+float64(i), 1.0, now.Add(time.Duration(i)*time.Millisecond))
	}
	if v, ok := c.PriceStddev(); ok || v != 0 {
		t.Errorf("expected (0,false) with 19 trades, got (%v,%v)", v, ok)
	}
}

func TestPriceStddevConstantPrices(t *testing.T) {
	c := NewCalculator(time.Hour)
	now := time.Now()
	for i := 0; i < 20; i++ {
		c.AddTrade(70.0, 1.0, now.Add(time.Duration(i)*time.Millisecond))
	}
	v, ok := c.PriceStddev()
	if !ok {
		t.Fatal("expected stddev ready with 20 trades")
	}
	if !almostEqual(v, 0.0, 1e-9) {
		t.Errorf("expected stddev 0 for constant prices, got %v", v)
	}
}

func TestPriceStddevKnownVariance(t *testing.T) {
	// Prices 1..20: population variance = 33.25, stddev = sqrt(33.25).
	c := NewCalculator(time.Hour)
	now := time.Now()
	for i := 1; i <= 20; i++ {
		c.AddTrade(float64(i), 1.0, now.Add(time.Duration(i)*time.Millisecond))
	}
	v, ok := c.PriceStddev()
	if !ok {
		t.Fatal("expected stddev ready")
	}
	want := math.Sqrt(33.25)
	if !almostEqual(v, want, 1e-9) {
		t.Errorf("PriceStddev = %v, want %v", v, want)
	}
}

func TestRingWrapAround(t *testing.T) {
	c := NewCalculator(1 * time.Hour)
	now := time.Now()

	for i := 0; i < 100; i++ {
		c.AddTrade(70.0+float64(i)*0.01, 1.0, now.Add(time.Duration(i)*time.Millisecond))
	}

	v, ok := c.VWAP()
	if !ok {
		t.Fatal("expected VWAP to be ready after many trades")
	}
	expected := 70.0 + 0.01*99.0/2.0
	if !almostEqual(v, expected, 1e-6) {
		t.Errorf("VWAP = %f, want %f", v, expected)
	}
}
