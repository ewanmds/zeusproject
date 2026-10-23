package spread

import (
	"context"
	"math"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

const floatTol = 1e-6

func almostEqual(a, b, tol float64) bool {
	return math.Abs(a-b) < tol
}

// Test rolling stats with known data: window=5, values [10,12,11,13,9]
// Expected: mean=11.0, variance=2.0, stddev=sqrt(2)=1.4142135
func TestRollingStatsBasic(t *testing.T) {
	rs := newRollingStats(5)

	values := []float64{10, 12, 11, 13, 9}
	for _, v := range values {
		rs.add(v)
	}

	if !rs.ready() {
		t.Fatal("expected ready after 5 values")
	}
	if !almostEqual(rs.mean(), 11.0, floatTol) {
		t.Fatalf("expected mean=11.0, got %f", rs.mean())
	}
	if !almostEqual(rs.stddev(), math.Sqrt(2.0), floatTol) {
		t.Fatalf("expected stddev=sqrt(2)=%f, got %f", math.Sqrt(2.0), rs.stddev())
	}
}

// Test sliding window: add value when full, oldest gets removed.
// After adding 15 to [10,12,11,13,9], window becomes [12,11,13,9,15]
// Expected: mean=12.0, variance=4.0, stddev=2.0
func TestRollingStatsSlide(t *testing.T) {
	rs := newRollingStats(5)

	values := []float64{10, 12, 11, 13, 9}
	for _, v := range values {
		rs.add(v)
	}

	// Add 15, removes 10
	rs.add(15)

	if !almostEqual(rs.mean(), 12.0, floatTol) {
		t.Fatalf("expected mean=12.0, got %f", rs.mean())
	}
	if !almostEqual(rs.stddev(), 2.0, floatTol) {
		t.Fatalf("expected stddev=2.0, got %f", rs.stddev())
	}
}

// Test z-score computation
// Window [12,11,13,9,15]: mean=12.0, stddev=2.0
// z-score for spread=16: (16-12)/2 = 2.0
func TestRollingStatsZScore(t *testing.T) {
	rs := newRollingStats(5)

	values := []float64{10, 12, 11, 13, 9}
	for _, v := range values {
		rs.add(v)
	}
	rs.add(15)

	sd := rs.stddev()
	z := 0.0
	if sd != 0 {
		z = (16.0 - rs.mean()) / sd
	}
	if !almostEqual(z, 2.0, floatTol) {
		t.Fatalf("expected z=2.0, got %f", z)
	}
}

// Test warm-up: no signal emitted until window is full
func TestEngineWarmUp(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Send 4 pairs of updates (not enough to fill window of 5)
	for i := 0; i < 4; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: 70.0, BestAsk: 70.10, Mid: 70.05, Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: 68.0, BestAsk: 68.10, Mid: 68.05, Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond) // give goroutine time to process
	}

	// Check no signals emitted
	select {
	case sig := <-signalCh:
		t.Fatalf("expected no signal during warm-up, got %v", sig)
	case <-time.After(50 * time.Millisecond):
		// Good: no signal
	}
}

// Test signal generation with known z-scores.
// Feed 5 varying spread values, then a 6th that pushes z above entry threshold.
func TestEngineSignalShortSpread(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Spreads: 10, 12, 11, 13, 9 => mean=11, std=sqrt(2)=1.4142
	clMids := []float64{78.0, 80.0, 79.0, 81.0, 77.0}
	brentMids := []float64{68.0, 68.0, 68.0, 68.0, 68.0}

	for i := 0; i < 5; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Drain any signals from warm-up completion (the 5th pair completes the window)
	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Now send spread = 15 (CL=83, BRENT=68). z = (15-11)/sqrt(2) = 2.828 > entry(2.0)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 82.95, BestAsk: 83.05, Mid: 83.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 67.95, BestAsk: 68.05, Mid: 68.0, Timestamp: now,
	}

	// Wait for signal
	select {
	case sig := <-signalCh:
		if sig.ZScore <= 0 {
			t.Fatalf("expected positive z-score for wide spread, got %f", sig.ZScore)
		}
		// z = (15-11)/sqrt(2) = 2.828
		if !almostEqual(sig.ZScore, 4.0/math.Sqrt(2.0), 0.5) {
			t.Fatalf("expected z~2.83, got %f", sig.ZScore)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

// Test LONG_SPREAD signal when z-score < -entry_z_score
func TestEngineSignalLongSpread(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go eng.Run(ctx)

	now := time.Now()

	// Spreads: 10, 12, 11, 13, 9 => mean=11, std=sqrt(2)
	clMids := []float64{78.0, 80.0, 79.0, 81.0, 77.0}
	brentMids := []float64{68.0, 68.0, 68.0, 68.0, 68.0}

	for i := 0; i < 5; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Now send spread = 7 (CL=75, BRENT=68). z = (7-11)/sqrt(2) = -2.828 < -entry(2.0)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 74.95, BestAsk: 75.05, Mid: 75.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 67.95, BestAsk: 68.05, Mid: 68.0, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		if sig.ZScore >= 0 {
			t.Fatalf("expected negative z-score for narrow spread, got %f", sig.ZScore)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

// Test ratio spread mode: spread = CL_mid / BRENT_mid
func TestEngineRatioMode(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "ratio",
			LookbackWindow: 3,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go eng.Run(ctx)

	now := time.Now()

	// Ratios: 70/68=1.02941, 72/68=1.05882, 71/68=1.04412
	clMids := []float64{70.0, 72.0, 71.0}
	brentMids := []float64{68.0, 68.0, 68.0}

	for i := 0; i < 3; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Send a moderately high ratio: CL=73/BRENT=68 = 1.07353
	// z ≈ (1.07353-1.04412)/0.012 ≈ 2.45, above entry(1.5) but below stop(3.0)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 72.95, BestAsk: 73.05, Mid: 73.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 67.95, BestAsk: 68.05, Mid: 68.0, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		if sig.ZScore <= 0 {
			t.Fatalf("expected positive z-score for high ratio, got %f", sig.ZScore)
		}
		if sig.Spread != 73.0/68.0 {
			t.Logf("spread=%f (expected %f)", sig.Spread, 73.0/68.0)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

// TestRollingStatsRecompute cycles the buffer through 1000+ rotations
// and verifies that incremental sums match a from-scratch computation.
func TestRollingStatsRecompute(t *testing.T) {
	window := 50
	rs := newRollingStats(window)

	// Fill window + cycle through 1000+ full rotations.
	totalValues := window + window*recomputeEveryRotations + window/2
	for i := 0; i < totalValues; i++ {
		v := float64(i)*0.1 + 0.001*float64(i%7)
		rs.add(v)
	}

	if !rs.ready() {
		t.Fatal("expected ready")
	}

	// Compute expected values from scratch.
	var wantSum, wantSumSq float64
	for i := 0; i < window; i++ {
		wantSum += rs.buf[i]
		wantSumSq += rs.buf[i] * rs.buf[i]
	}

	if !almostEqual(rs.sum, wantSum, 1e-6) {
		t.Fatalf("sum drift: got %v, want %v, diff %v", rs.sum, wantSum, rs.sum-wantSum)
	}
	if !almostEqual(rs.sumSq, wantSumSq, 1e-3) {
		t.Fatalf("sumSq drift: got %v, want %v, diff %v", rs.sumSq, wantSumSq, rs.sumSq-wantSumSq)
	}
}

// TestRollingOLSRecompute cycles the OLS buffer through 1000+ rotations
// and verifies that incremental sums match a from-scratch computation.
func TestRollingOLSRecompute(t *testing.T) {
	window := 50
	ols := newRollingOLS(window)

	totalValues := window + window*recomputeEveryRotations + window/2
	for i := 0; i < totalValues; i++ {
		x := float64(i)*0.1 + 0.002*float64(i%11)
		y := 0.9*x + 0.5
		ols.add(x, y)
	}

	if !ols.ready() {
		t.Fatal("expected ready")
	}

	// Compute expected values from scratch.
	var wantSumX, wantSumY, wantSumXY, wantSumXX float64
	for i := 0; i < window; i++ {
		wantSumX += ols.xBuf[i]
		wantSumY += ols.yBuf[i]
		wantSumXY += ols.xBuf[i] * ols.yBuf[i]
		wantSumXX += ols.xBuf[i] * ols.xBuf[i]
	}

	if !almostEqual(ols.sumX, wantSumX, 1e-6) {
		t.Fatalf("sumX drift: got %v, want %v", ols.sumX, wantSumX)
	}
	if !almostEqual(ols.sumY, wantSumY, 1e-6) {
		t.Fatalf("sumY drift: got %v, want %v", ols.sumY, wantSumY)
	}
	if !almostEqual(ols.sumXY, wantSumXY, 1e-3) {
		t.Fatalf("sumXY drift: got %v, want %v", ols.sumXY, wantSumXY)
	}
	if !almostEqual(ols.sumXX, wantSumXX, 1e-3) {
		t.Fatalf("sumXX drift: got %v, want %v", ols.sumXX, wantSumXX)
	}
}

func TestEngineSkipsStaleCrossLegSample(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 2,
		},
		MarketMaking: config.MarketMakingConfig{
			MaxCrossLegSkewMs: 150,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go eng.Run(ctx)

	base := time.Now()
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: base,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 68.95, BestAsk: 69.05, Mid: 69.0, Timestamp: base.Add(500 * time.Millisecond),
	}
	time.Sleep(100 * time.Millisecond)

	if count, _ := eng.WarmUpProgress(); count != 0 {
		t.Fatalf("expected stale sample to be skipped, got warmup count %d", count)
	}
}

func TestEngineSamplesWhenEitherLegArrivesSecond(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 2,
		},
		MarketMaking: config.MarketMakingConfig{
			MaxCrossLegSkewMs: 150,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go eng.Run(ctx)

	base := time.Now()
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: base,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 68.95, BestAsk: 69.05, Mid: 69.0, Timestamp: base,
	}
	time.Sleep(50 * time.Millisecond)

	if count, _ := eng.WarmUpProgress(); count != 1 {
		t.Fatalf("expected initial paired sample to warm count=1, got %d", count)
	}

	// Now the second asset updates first, and the first asset arrives second.
	// The old CL-led sampler would miss this; the symmetric sampler should not.
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 69.45, BestAsk: 69.55, Mid: 69.5, Timestamp: base.Add(50 * time.Millisecond),
	}
	time.Sleep(50 * time.Millisecond)
	if count, _ := eng.WarmUpProgress(); count != 1 {
		t.Fatalf("expected no sample until both legs are fresh, got %d", count)
	}

	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 70.45, BestAsk: 70.55, Mid: 70.5, Timestamp: base.Add(60 * time.Millisecond),
	}
	time.Sleep(50 * time.Millisecond)

	if count, _ := eng.WarmUpProgress(); count != 2 {
		t.Fatalf("expected second-leg-arrival sample to advance warm count to 2, got %d", count)
	}
}

// Test that not-ready rolling stats return zero and not ready
func TestRollingStatsNotReady(t *testing.T) {
	rs := newRollingStats(5)
	rs.add(10)
	rs.add(12)

	if rs.ready() {
		t.Fatal("expected not ready with only 2 values in window of 5")
	}
	if rs.mean() != 0 {
		t.Fatalf("expected mean=0 when not ready, got %f", rs.mean())
	}
	if rs.stddev() != 0 {
		t.Fatalf("expected stddev=0 when not ready, got %f", rs.stddev())
	}
}

// Test rolling OLS beta with known linear relationship: Y = 0.9 * X
// beta = Cov(X,Y)/Var(X) = 0.9
func TestRollingOLSBeta(t *testing.T) {
	ols := newRollingOLS(5)
	// X (CL) and Y (BRENT) with known relationship: Y = 0.9 * X
	xs := []float64{100, 101, 102, 103, 104}
	ys := []float64{90, 90.9, 91.8, 92.7, 93.6}
	for i := range xs {
		ols.add(xs[i], ys[i])
	}
	beta := ols.beta()
	if math.Abs(beta-0.9) > 0.01 {
		t.Fatalf("expected beta ≈ 0.9, got %f", beta)
	}
}

// Test OLS beta returns raw values (no clamping at the OLS level).
func TestRollingOLSBetaClamping(t *testing.T) {
	// Test low: Y = 0.5 * X => raw beta = 0.5
	olsLow := newRollingOLS(5)
	xs := []float64{100, 101, 102, 103, 104}
	for _, x := range xs {
		olsLow.add(x, x*0.5)
	}
	betaLow := olsLow.beta()
	if math.Abs(betaLow-0.5) > floatTol {
		t.Fatalf("expected raw beta = 0.5, got %f", betaLow)
	}

	// Test high: Y = 2.0 * X => raw beta = 2.0
	olsHigh := newRollingOLS(5)
	for _, x := range xs {
		olsHigh.add(x, x*2.0)
	}
	betaHigh := olsHigh.beta()
	if math.Abs(betaHigh-2.0) > floatTol {
		t.Fatalf("expected raw beta = 2.0, got %f", betaHigh)
	}
}

// Test that beta = 1.0 in signals when UseBetaHedge is false (default)
func TestBetaEqualOne(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
			UseBetaHedge:   false,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Feed 5 varying pairs + 1 extra to get a signal after warm-up.
	clMids := []float64{78.0, 80.0, 79.0, 81.0, 77.0, 83.0}
	brentMids := []float64{68.0, 68.0, 68.0, 68.0, 68.0, 68.0}

	for i := 0; i < len(clMids); i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Collect the last signal (after warm-up).
	var lastSig types.SpreadSignal
	timeout := time.After(1 * time.Second)
	for {
		select {
		case sig := <-signalCh:
			lastSig = sig
			continue
		case <-timeout:
		}
		break
	}

	if lastSig.Beta != 1.0 {
		t.Fatalf("expected Beta=1.0 when UseBetaHedge=false, got %f", lastSig.Beta)
	}
}

// Test that when UseBetaHedge=true, spread uses beta-adjusted residual formula.
func TestBetaAdjustedSpread(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
			UseBetaHedge:   true,
			BetaMin:        0.5,
			BetaMax:        1.8,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Use a known relationship: BRENT ≈ 0.9 * CL, so beta ≈ 0.9
	// CL prices: 100, 101, 102, 103, 104
	// BRENT prices: 90, 90.9, 91.8, 92.7, 93.6
	clMids := []float64{100, 101, 102, 103, 104}
	brentMids := []float64{90, 90.9, 91.8, 92.7, 93.6}

	for i := 0; i < 5; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Drain warm-up signals.
	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Send a 6th pair. Beta should be ≈ 0.9 now.
	// spread = CL - beta * BRENT = 105 - 0.9 * 94.5 = 105 - 85.05 = 19.95
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 104.95, BestAsk: 105.05, Mid: 105.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 94.45, BestAsk: 94.55, Mid: 94.5, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		if math.Abs(sig.Beta-0.9) > 0.05 {
			t.Fatalf("expected Beta ≈ 0.9, got %f", sig.Beta)
		}
		// With beta ≈ 0.9, spread = CL - 0.9 * BRENT
		expectedSpread := 105.0 - sig.Beta*94.5
		if math.Abs(sig.Spread-expectedSpread) > 0.1 {
			t.Fatalf("expected spread ≈ %f, got %f", expectedSpread, sig.Spread)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

// Test that extreme z-scores pass through unmodified (no circuit breaker).
// TestEngineStdDevFloor verifies that z_min_stddev prevents z-score explosion
// when raw stddev collapses to near-zero values.
func TestEngineStdDevFloor(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
			ZMinStdDev:     0.5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Send 5 nearly identical spreads to create a tiny raw stddev.
	// Spreads: 10.00, 10.01, 10.00, 10.01, 10.00 => mean≈10.004, raw stddev≈0.0049
	clMids := []float64{78.00, 78.01, 78.00, 78.01, 78.00}
	brentMids := []float64{68.00, 68.00, 68.00, 68.00, 68.00}

	for i := 0; i < 5; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Drain warm-up signals
	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Send a spread that's 1.0 away from mean (spread=11.0, mean≈10.004).
	// Without floor: z = 0.996/0.0049 ≈ 203 (explodes!)
	// With floor=0.5: z = 0.996/0.5 ≈ 1.99 (reasonable)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 78.95, BestAsk: 79.05, Mid: 79.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 67.95, BestAsk: 68.05, Mid: 68.0, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		// With floor=0.5, z should be approximately (11.0 - 10.004) / 0.5 ≈ 1.99
		if sig.ZScore > 10 {
			t.Fatalf("stddev floor should prevent z-score explosion, got z=%f", sig.ZScore)
		}
		if sig.StdDev < 0.5 {
			t.Fatalf("signal StdDev should be floored at 0.5, got %f", sig.StdDev)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

func TestEnginePassesThroughExtremeZScore(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)

	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	go eng.Run(ctx)

	now := time.Now()

	// Spreads: 10, 12, 11, 13, 9 => mean=11, std=sqrt(2)=1.4142
	clMids := []float64{78.0, 80.0, 79.0, 81.0, 77.0}
	brentMids := []float64{68.0, 68.0, 68.0, 68.0, 68.0}

	for i := 0; i < 5; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: clMids[i] - 0.05, BestAsk: clMids[i] + 0.05, Mid: clMids[i], Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: brentMids[i] - 0.05, BestAsk: brentMids[i] + 0.05, Mid: brentMids[i], Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Drain warm-up signals
	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Send a massive dislocation: spread = 20 (CL=88, BRENT=68).
	// z = (20-11)/sqrt(2) = 6.36, well above 5.
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 87.95, BestAsk: 88.05, Mid: 88.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 67.95, BestAsk: 68.05, Mid: 68.0, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		if sig.ZScore <= 5.0 {
			t.Fatalf("expected extreme z-score > 5, got %f", sig.ZScore)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}

func TestBetaClampingConfigurable(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 5,
			UseBetaHedge:   true,
			BetaMin:        0.6,
			BetaMax:        1.5,
		},
	}

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	eng := New(cfg, bookCh, signalCh)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go eng.Run(ctx)

	now := time.Now()

	// Feed data where Y = 0.3 * X (raw beta ~0.3, should clamp to BetaMin=0.6)
	xs := []float64{100, 101, 102, 103, 104}
	for _, x := range xs {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: x - 0.05, BestAsk: x + 0.05, Mid: x, Timestamp: now,
		}
		bookCh <- types.BookUpdate{
			Coin: "xyz:BRENTOIL", BestBid: x*0.3 - 0.05, BestAsk: x*0.3 + 0.05, Mid: x * 0.3, Timestamp: now,
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Drain warmup signals
	time.Sleep(50 * time.Millisecond)
	for len(signalCh) > 0 {
		<-signalCh
	}

	// Send one more to get a signal
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 104.95, BestAsk: 105.05, Mid: 105.0, Timestamp: now,
	}
	bookCh <- types.BookUpdate{
		Coin: "xyz:BRENTOIL", BestBid: 31.45, BestAsk: 31.55, Mid: 31.5, Timestamp: now,
	}

	select {
	case sig := <-signalCh:
		if sig.Beta != 0.6 {
			t.Fatalf("expected beta clamped to BetaMin=0.6, got %f", sig.Beta)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("timed out waiting for signal")
	}
}
