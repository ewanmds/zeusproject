package quoting

import (
	"math"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// makeLevels builds N book levels starting at startPrice, stepping by priceStep,
// all with the given size.
func makeLevels(n int, startPrice, priceStep, size float64) []types.BookLevel {
	levels := make([]types.BookLevel, n)
	for i := range levels {
		levels[i] = types.BookLevel{
			Price: startPrice + float64(i)*priceStep,
			Size:  size,
		}
	}
	return levels
}

func makeBook(coin string, bestBid, bestAsk float64, bids, asks []types.BookLevel) types.BookUpdate {
	return types.BookUpdate{
		Coin:      coin,
		BestBid:   bestBid,
		BestAsk:   bestAsk,
		Mid:       (bestBid + bestAsk) / 2.0,
		Bids:      bids,
		Asks:      asks,
		Timestamp: time.Now(),
	}
}

// TestWidthDuringVolWarmup feeds 59 updates to a window of 60.
// The module should not have warmed up, so multipliers stay {1.0, 1.0}.
func TestWidthDuringVolWarmup(t *testing.T) {
	ms := NewMicrostructure(60, 2.0, 5, 50.0)
	coin := "xyz:CL"

	for i := 0; i < 59; i++ {
		mid := 70.0 + float64(i)*0.01
		halfSpread := 0.01
		bid := mid - halfSpread
		ask := mid + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	w := ms.Width(coin)
	if w.BidMultiplier != 1.0 || w.AskMultiplier != 1.0 {
		t.Errorf("expected {1.0, 1.0} during warmup, got {%.4f, %.4f}",
			w.BidMultiplier, w.AskMultiplier)
	}
}

// TestWidthVolSpike feeds calm data to establish a baseline, then injects
// large mid moves. The bid multiplier should increase above 1.0.
func TestWidthVolSpike(t *testing.T) {
	window := 60
	ms := NewMicrostructure(window, 2.0, 5, 50.0)
	coin := "xyz:CL"

	// Calm phase: tiny moves to fill window + establish EWMA baseline
	for i := 0; i < window+20; i++ {
		mid := 70.0 + float64(i)*0.001 // very small increments
		halfSpread := 0.01
		bid := mid - halfSpread
		ask := mid + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	baseWidth := ms.Width(coin)

	// Volatile phase: large jumps
	for i := 0; i < 30; i++ {
		// Alternate big up / big down moves
		var mid float64
		if i%2 == 0 {
			mid = 72.0
		} else {
			mid = 68.0
		}
		halfSpread := 0.01
		bid := mid - halfSpread
		ask := mid + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	spikeWidth := ms.Width(coin)
	if spikeWidth.BidMultiplier <= baseWidth.BidMultiplier {
		t.Errorf("expected bid multiplier to increase after vol spike: base=%.4f spike=%.4f",
			baseWidth.BidMultiplier, spikeWidth.BidMultiplier)
	}
}

// TestWidthSpreadFloor checks that when venue spread (20 bps) exceeds our
// quote spread (10 bps), multipliers are at least ~2.0.
func TestWidthSpreadFloor(t *testing.T) {
	ms := NewMicrostructure(60, 5.0, 5, 10.0) // quoteSpreadBps = 10
	coin := "xyz:CL"

	mid := 70.0
	// Venue spread = 20 bps of mid => ~0.14
	venueHalf := mid * 20.0 / 20000.0 // 0.07
	bid := mid - venueHalf
	ask := mid + venueHalf

	// Feed enough updates to warm up (use stable mid so vol stays low)
	for i := 0; i < 65; i++ {
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	w := ms.Width(coin)
	// venueSpreadBps / quoteSpreadBps = 20 / 10 = 2.0
	if w.BidMultiplier < 1.9 || w.AskMultiplier < 1.9 {
		t.Errorf("expected multipliers >= ~2.0 for spread floor, got {%.4f, %.4f}",
			w.BidMultiplier, w.AskMultiplier)
	}
}

// TestWidthImbalanceAsymmetry checks that a bid-heavy book (large bid depth,
// small ask depth) causes the ask multiplier to exceed the bid multiplier.
func TestWidthImbalanceAsymmetry(t *testing.T) {
	ms := NewMicrostructure(60, 5.0, 5, 50.0)
	coin := "xyz:CL"

	mid := 70.0
	halfSpread := 0.01
	bid := mid - halfSpread
	ask := mid + halfSpread

	// Bid-heavy: 50 size per bid level, 5 size per ask level
	bigBids := makeLevels(5, bid, -0.01, 50.0)
	smallAsks := makeLevels(5, ask, 0.01, 5.0)

	for i := 0; i < 65; i++ {
		book := makeBook(coin, bid, ask, bigBids, smallAsks)
		ms.Update(coin, book)
	}

	w := ms.Width(coin)
	if w.AskMultiplier <= w.BidMultiplier {
		t.Errorf("expected ask multiplier > bid multiplier for bid-heavy book, got bid=%.4f ask=%.4f",
			w.BidMultiplier, w.AskMultiplier)
	}
}

// TestWidthImbalanceBalanced checks that equal depth yields roughly symmetric
// multipliers (difference < 0.1).
func TestWidthImbalanceBalanced(t *testing.T) {
	ms := NewMicrostructure(60, 5.0, 5, 50.0)
	coin := "xyz:CL"

	mid := 70.0
	halfSpread := 0.01
	bid := mid - halfSpread
	ask := mid + halfSpread

	bids := makeLevels(5, bid, -0.01, 10.0)
	asks := makeLevels(5, ask, 0.01, 10.0)

	for i := 0; i < 65; i++ {
		book := makeBook(coin, bid, ask, bids, asks)
		ms.Update(coin, book)
	}

	w := ms.Width(coin)
	diff := math.Abs(w.BidMultiplier - w.AskMultiplier)
	if diff > 0.1 {
		t.Errorf("expected symmetric multipliers (diff < 0.1), got bid=%.4f ask=%.4f diff=%.4f",
			w.BidMultiplier, w.AskMultiplier, diff)
	}
}

// TestWidthClamping checks that extreme inputs result in multipliers capped
// at max_multiplier (2.0).
func TestWidthClamping(t *testing.T) {
	ms := NewMicrostructure(60, 2.0, 5, 5.0) // low quoteSpread, low max
	coin := "xyz:CL"

	mid := 70.0
	// Very wide venue spread: 100 bps
	venueHalf := mid * 100.0 / 20000.0
	bid := mid - venueHalf
	ask := mid + venueHalf

	// Very imbalanced: 100 size bids, 1 size asks
	bigBids := makeLevels(5, bid, -0.01, 100.0)
	tinyAsks := makeLevels(5, ask, 0.01, 1.0)

	for i := 0; i < 65; i++ {
		book := makeBook(coin, bid, ask, bigBids, tinyAsks)
		ms.Update(coin, book)
	}

	w := ms.Width(coin)
	if w.BidMultiplier > 2.0 {
		t.Errorf("expected bid multiplier capped at 2.0, got %.4f", w.BidMultiplier)
	}
	if w.AskMultiplier > 2.0 {
		t.Errorf("expected ask multiplier capped at 2.0, got %.4f", w.AskMultiplier)
	}
}

// TestWidthUnknownCoin checks that querying Width for an unknown coin
// returns {1.0, 1.0}.
func TestWidthUnknownCoin(t *testing.T) {
	ms := NewMicrostructure(60, 2.0, 5, 50.0)

	w := ms.Width("xyz:UNKNOWN")
	if w.BidMultiplier != 1.0 || w.AskMultiplier != 1.0 {
		t.Errorf("expected {1.0, 1.0} for unknown coin, got {%.4f, %.4f}",
			w.BidMultiplier, w.AskMultiplier)
	}
}

func TestClamp(t *testing.T) {
	tests := []struct {
		name      string
		v, lo, hi float64
		want      float64
	}{
		{"below_lo", 0.5, 1.0, 3.0, 1.0},
		{"above_hi", 5.0, 1.0, 3.0, 3.0},
		{"within_range", 2.0, 1.0, 3.0, 2.0},
		{"at_lo", 1.0, 1.0, 3.0, 1.0},
		{"at_hi", 3.0, 1.0, 3.0, 3.0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := clamp(tt.v, tt.lo, tt.hi)
			if got != tt.want {
				t.Errorf("clamp(%f, %f, %f) = %f, want %f", tt.v, tt.lo, tt.hi, got, tt.want)
			}
		})
	}
}

// TestCircularBufferAlignment verifies that after wrap-around, the oldest
// return is correctly overwritten (not an adjacent slot due to off-by-one).
func TestCircularBufferAlignment(t *testing.T) {
	window := 10
	ms := NewMicrostructure(window, 5.0, 5, 50.0)
	coin := "xyz:CL"

	// Feed window+1 updates (window returns) with known increasing prices,
	// then 5 more to wrap around.
	totalUpdates := window + 1 + 5
	prices := make([]float64, totalUpdates)
	for i := range prices {
		prices[i] = 100.0 + float64(i)*0.1
	}

	for _, p := range prices {
		halfSpread := 0.01
		bid := p - halfSpread
		ask := p + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	st := ms.state[coin]

	// We should have exactly `window` returns in the buffer.
	if len(st.returns) != window {
		t.Fatalf("expected %d returns, got %d", window, len(st.returns))
	}

	// Verify returns match the last `window` price transitions.
	// The buffer should contain returns for prices[totalUpdates-window : totalUpdates].
	// After wrap, count == totalUpdates-1 (since count only increments when lastMid > 0).
	// The most recent return was written at index (count-1) % window.
	// Let's verify all returns are approximately math.Log(1 + 0.1/price).
	for i := 0; i < window; i++ {
		ret := st.returns[i]
		if math.IsNaN(ret) || ret == 0 {
			t.Errorf("return[%d] is zero or NaN: %f", i, ret)
		}
	}

	// The critical check: count should equal totalUpdates-1 (we skip the first
	// update since lastMid==0). With the fix, count tracks actual writes.
	expectedCount := totalUpdates - 1
	if st.count != expectedCount {
		t.Errorf("expected count=%d, got %d", expectedCount, st.count)
	}

	// Verify the most recently written slot by checking that the return at
	// (count-1)%window matches the last price transition.
	lastIdx := (st.count - 1) % window
	expectedReturn := math.Log(prices[totalUpdates-1] / prices[totalUpdates-2])
	if math.Abs(st.returns[lastIdx]-expectedReturn) > 1e-10 {
		t.Errorf("last written return at index %d: got %f, want %f",
			lastIdx, st.returns[lastIdx], expectedReturn)
	}
}

// TestEWMARecoveryFromZeroVol verifies that after a calm period where
// baseVol converges to near-zero, the first volatile tick re-seeds EWMA
// instead of jumping to maxMultiplier.
func TestEWMARecoveryFromZeroVol(t *testing.T) {
	window := 20
	ms := NewMicrostructure(window, 3.0, 5, 50.0)
	coin := "xyz:CL"

	mid := 70.0
	halfSpread := 0.01

	// Feed window+20 updates with identical mid (returns = 0, stddev → 0).
	for i := 0; i < window+20; i++ {
		bid := mid - halfSpread
		ask := mid + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	st := ms.state[coin]
	if !st.ewmaInit {
		t.Fatal("expected EWMA to be initialized after warmup")
	}

	// baseVol should be near zero (but floored at 1e-15).
	if st.baseVol > 1e-10 {
		t.Fatalf("expected baseVol near zero, got %e", st.baseVol)
	}

	// Now feed 10 volatile updates (alternating big moves).
	for i := 0; i < 10; i++ {
		var p float64
		if i%2 == 0 {
			p = 72.0
		} else {
			p = 68.0
		}
		bid := p - halfSpread
		ask := p + halfSpread
		book := makeBook(coin, bid, ask,
			makeLevels(5, bid, -0.01, 10.0),
			makeLevels(5, ask, 0.01, 10.0),
		)
		ms.Update(coin, book)
	}

	// The first volatile tick should have re-seeded baseVol (epsilon guard).
	// Multiplier should be reasonable, not maxMultiplier.
	w := ms.Width(coin)
	if w.BidMultiplier >= ms.maxMultiplier {
		t.Errorf("expected bid multiplier < maxMultiplier after EWMA reseed, got %.4f (max=%.1f)",
			w.BidMultiplier, ms.maxMultiplier)
	}
	if w.AskMultiplier >= ms.maxMultiplier {
		t.Errorf("expected ask multiplier < maxMultiplier after EWMA reseed, got %.4f (max=%.1f)",
			w.AskMultiplier, ms.maxMultiplier)
	}
}

func TestVolMultiplier(t *testing.T) {
	t.Run("unknown_coin", func(t *testing.T) {
		ms := NewMicrostructure(20, 3.0, 5, 10.0)
		if got := ms.VolMultiplier("UNKNOWN"); got != 1.0 {
			t.Errorf("expected 1.0 for unknown coin, got %f", got)
		}
	})

	t.Run("during_warmup", func(t *testing.T) {
		ms := NewMicrostructure(20, 3.0, 5, 10.0)
		coin := "xyz:CL"
		// Feed fewer updates than volWindow — still warming up.
		for i := 0; i < 10; i++ {
			mid := 100.0 + float64(i)*0.01
			book := makeBook(coin, mid-0.05, mid+0.05,
				makeLevels(5, mid-0.05, -0.01, 10),
				makeLevels(5, mid+0.05, 0.01, 10))
			ms.Update(coin, book)
		}
		if got := ms.VolMultiplier(coin); got != 1.0 {
			t.Errorf("expected 1.0 during warmup, got %f", got)
		}
	})

	t.Run("after_vol_spike", func(t *testing.T) {
		ms := NewMicrostructure(20, 3.0, 5, 10.0)
		coin := "xyz:CL"
		// Calm period to seed baseline.
		for i := 0; i < 30; i++ {
			mid := 100.0 + float64(i)*0.001
			book := makeBook(coin, mid-0.05, mid+0.05,
				makeLevels(5, mid-0.05, -0.01, 10),
				makeLevels(5, mid+0.05, 0.01, 10))
			ms.Update(coin, book)
		}
		// Vol spike.
		for i := 0; i < 25; i++ {
			mid := 100.0
			if i%2 == 0 {
				mid += 0.5
			} else {
				mid -= 0.5
			}
			book := makeBook(coin, mid-0.05, mid+0.05,
				makeLevels(5, mid-0.05, -0.01, 10),
				makeLevels(5, mid+0.05, 0.01, 10))
			ms.Update(coin, book)
		}
		got := ms.VolMultiplier(coin)
		if got <= 1.0 {
			t.Errorf("expected > 1.0 after vol spike, got %f", got)
		}
		if got > 3.0 {
			t.Errorf("expected <= maxMultiplier (3.0), got %f", got)
		}
	})

	t.Run("clamped_at_max", func(t *testing.T) {
		maxMult := 1.2 // low clamp so it's easy to trigger
		ms := NewMicrostructure(20, maxMult, 5, 10.0)
		coin := "xyz:CL"
		// Calm baseline.
		for i := 0; i < 25; i++ {
			mid := 100.0 + float64(i)*0.0001
			book := makeBook(coin, mid-0.05, mid+0.05,
				makeLevels(5, mid-0.05, -0.01, 10),
				makeLevels(5, mid+0.05, 0.01, 10))
			ms.Update(coin, book)
		}
		// Vol spike.
		for i := 0; i < 30; i++ {
			mid := 100.0
			if i%2 == 0 {
				mid += 0.5
			} else {
				mid -= 0.5
			}
			book := makeBook(coin, mid-0.05, mid+0.05,
				makeLevels(5, mid-0.05, -0.01, 10),
				makeLevels(5, mid+0.05, 0.01, 10))
			ms.Update(coin, book)
		}
		got := ms.VolMultiplier(coin)
		if got > maxMult+1e-9 {
			t.Errorf("expected clamped at maxMultiplier %f, got %f", maxMult, got)
		}
		if got < 1.0 {
			t.Errorf("expected >= 1.0, got %f", got)
		}
	})
}
