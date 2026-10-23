package tradeflow

import (
	"math"
	"sync"
	"time"
)

// ImbalanceCalculator is the interface for trade imbalance computation.
// Implemented by both time-window Calculator and volume-clock VolumeClockCalculator.
type ImbalanceCalculator interface {
	AddTrade(size float64, side string, ts time.Time)
	Imbalance() (float64, bool)
	ImbalanceAndRecord() (float64, bool)
	SaturationRatio() float64
}

type signedTrade struct {
	size float64 // positive = buy, negative = sell
	ts   int64   // UnixNano
}

const driftCorrectionInterval = 500

type Calculator struct {
	window       time.Duration
	sizeExponent float64
	trades       []signedTrade
	buyVol       float64
	sellVol      float64
	evictions    int

	// Saturation ring buffer: tracks fraction of recent imbalance reads at ±cap
	// (|imbalance| > 0.95).
	saturation saturationRing

	mu sync.RWMutex
}

func NewCalculator(window time.Duration, sizeExponent float64) *Calculator {
	if sizeExponent <= 0 {
		sizeExponent = 1.0
	}
	return &Calculator{
		window:       window,
		sizeExponent: sizeExponent,
		trades:       make([]signedTrade, 0, 1024),
	}
}

// AddTrade records a trade with direction. side is "buy" or "sell".
// Trade sizes are power-law weighted by sizeExponent before recording:
// weighted = size^sizeExponent. Exponent 1.0 = raw (legacy), <1.0 dampens
// whale impact, >1.0 amplifies large-trade influence.
func (c *Calculator) AddTrade(size float64, side string, ts time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()

	tsNano := ts.UnixNano()
	weighted := math.Pow(size, c.sizeExponent)
	signed := weighted
	if side == "sell" {
		signed = -weighted
	}
	c.trades = append(c.trades, signedTrade{size: signed, ts: tsNano})
	if side == "buy" {
		c.buyVol += weighted
	} else {
		c.sellVol += weighted
	}
	c.evict(tsNano)
}

// imbalanceUnlocked computes the imbalance ratio without acquiring any lock.
// Caller must hold at least c.mu.RLock().
func (c *Calculator) imbalanceUnlocked() (float64, bool) {
	nowNano := time.Now().UnixNano()
	cutoff := nowNano - c.window.Nanoseconds()
	buyVol, sellVol := c.buyVol, c.sellVol

	// Subtract expired trades.
	for _, tr := range c.trades {
		if tr.ts >= cutoff {
			break
		}
		if tr.size > 0 {
			buyVol -= tr.size
		} else {
			sellVol -= (-tr.size)
		}
	}

	// Clamp to zero: float drift from incremental eviction can make these negative.
	if buyVol < 0 {
		buyVol = 0
	}
	if sellVol < 0 {
		sellVol = 0
	}

	total := buyVol + sellVol
	if total <= 0 {
		return 0, false
	}
	imb := (buyVol - sellVol) / total
	// Defensive clamp: should be in [-1, +1] by construction, but float
	// arithmetic can violate this after drift correction.
	if imb > 1 {
		imb = 1
	} else if imb < -1 {
		imb = -1
	}
	return imb, true
}

// Imbalance returns (buyVol - sellVol) / (buyVol + sellVol) in [-1, +1].
// Returns (0, false) if no trades in window.
func (c *Calculator) Imbalance() (float64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.imbalanceUnlocked()
}

// ImbalanceAndRecord is like Imbalance but also records saturation status
// into the ring buffer. Uses a write lock since it mutates ring buffer state.
func (c *Calculator) ImbalanceAndRecord() (float64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	imb, ready := c.imbalanceUnlocked()
	if !ready {
		return 0, false
	}

	c.saturation.Record(imb > 0.95 || imb < -0.95)
	return imb, true
}

// SaturationRatio returns the fraction of recent imbalance reads that were
// at the ±cap (|imbalance| > 0.95). Returns 0 if no readings recorded.
func (c *Calculator) SaturationRatio() float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.saturation.Fraction()
}

func (c *Calculator) evict(nowNano int64) {
	cutoff := nowNano - c.window.Nanoseconds()
	i := 0
	for i < len(c.trades) && c.trades[i].ts < cutoff {
		if c.trades[i].size > 0 {
			c.buyVol -= c.trades[i].size
		} else {
			c.sellVol -= (-c.trades[i].size)
		}
		i++
	}
	if i == 0 {
		return
	}
	c.trades = c.trades[i:]
	c.evictions += i

	if c.evictions >= driftCorrectionInterval {
		c.recompute()
		c.evictions = 0
	}

	if cap(c.trades) > 4*len(c.trades) && cap(c.trades) > 1024 {
		fresh := make([]signedTrade, len(c.trades), max(len(c.trades)*2, 1024))
		copy(fresh, c.trades)
		c.trades = fresh
	}
}

func (c *Calculator) recompute() {
	var buyVol, sellVol float64
	for _, t := range c.trades {
		if t.size > 0 {
			buyVol += t.size
		} else {
			sellVol += (-t.size)
		}
	}
	c.buyVol = buyVol
	c.sellVol = sellVol
}
