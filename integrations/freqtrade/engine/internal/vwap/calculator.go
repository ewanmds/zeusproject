package vwap

import (
	"math"
	"sync"
	"time"
)

type trade struct {
	price float64
	size  float64
	ts    int64 // UnixNano timestamp
}

// driftCorrectionInterval triggers a full recomputation after this many evictions
// to correct floating-point drift from incremental subtract.
const driftCorrectionInterval = 500

// Calculator computes a rolling time-based VWAP.
// Thread-safe: AddTrade and VWAP may be called from different goroutines.
type Calculator struct {
	window    time.Duration
	trades    []trade
	sumPV     float64 // running Σ(price × size)
	sumV      float64 // running Σ(size)
	evictions int     // counter for drift correction
	mu        sync.RWMutex
}

// NewCalculator creates a VWAP calculator with the given rolling window duration.
func NewCalculator(window time.Duration) *Calculator {
	return &Calculator{
		window: window,
		trades: make([]trade, 0, 4096),
	}
}

// AddTrade records a trade and evicts expired entries.
func (c *Calculator) AddTrade(price, size float64, ts time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()

	tsNano := ts.UnixNano()
	c.trades = append(c.trades, trade{price: price, size: size, ts: tsNano})
	c.sumPV += price * size
	c.sumV += size

	c.evict(tsNano)
}

// VWAP returns the current volume-weighted average price.
// Returns (0, false) if no trades are in the window.
func (c *Calculator) VWAP() (float64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	nowNano := time.Now().UnixNano()
	cutoff := nowNano - c.window.Nanoseconds()
	sumPV, sumV := c.sumPV, c.sumV

	// Subtract any trades that have expired since the last AddTrade.
	for _, tr := range c.trades {
		if tr.ts >= cutoff {
			break
		}
		sumPV -= tr.price * tr.size
		sumV -= tr.size
	}

	if sumV <= 0 {
		return 0, false
	}
	return sumPV / sumV, true
}

// TradeCount returns the number of trades currently in the rolling window.
func (c *Calculator) TradeCount() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.trades)
}

// PriceStddev returns the population standard deviation of trade prices in the
// rolling window. Returns (0, false) until at least 20 trades are present.
// Computed unweighted (price dispersion, not volume-weighted) over the current
// in-window slice. O(n) per call; intended to be called at most a few times
// per refresh cycle.
func (c *Calculator) PriceStddev() (float64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	n := len(c.trades)
	if n < 20 {
		return 0, false
	}
	var sum float64
	for _, t := range c.trades {
		sum += t.price
	}
	mean := sum / float64(n)
	var m2 float64
	for _, t := range c.trades {
		d := t.price - mean
		m2 += d * d
	}
	return math.Sqrt(m2 / float64(n)), true
}

// LastTradeTime returns the timestamp of the most recently recorded trade.
func (c *Calculator) LastTradeTime() (time.Time, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if len(c.trades) == 0 {
		return time.Time{}, false
	}
	return time.Unix(0, c.trades[len(c.trades)-1].ts), true
}

// evict removes trades older than the window from the front of the slice,
// incrementally subtracting each evicted trade's contribution. Periodically
// recomputes from scratch to correct floating-point drift.
func (c *Calculator) evict(nowNano int64) {
	cutoff := nowNano - c.window.Nanoseconds()
	i := 0
	for i < len(c.trades) && c.trades[i].ts < cutoff {
		// Subtract each evicted trade's contribution
		c.sumPV -= c.trades[i].price * c.trades[i].size
		c.sumV -= c.trades[i].size
		i++
	}
	if i == 0 {
		return
	}
	c.trades = c.trades[i:]
	c.evictions += i

	// Drift correction: periodically recompute from scratch
	if c.evictions >= driftCorrectionInterval {
		c.recompute()
		c.evictions = 0
	}

	// Compaction: when backing array is much larger than needed, copy to fresh slice
	if cap(c.trades) > 4*len(c.trades) && cap(c.trades) > 4096 {
		fresh := make([]trade, len(c.trades), max(len(c.trades)*2, 4096))
		copy(fresh, c.trades)
		c.trades = fresh
	}
}

// recompute recalculates sumPV and sumV from scratch over all remaining trades.
func (c *Calculator) recompute() {
	var sumPV, sumV float64
	for _, t := range c.trades {
		sumPV += t.price * t.size
		sumV += t.size
	}
	c.sumPV = sumPV
	c.sumV = sumV
}
