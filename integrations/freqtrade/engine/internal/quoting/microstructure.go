package quoting

import (
	"math"
	"sync"

	"hyperliquid-market-maker/internal/types"
)

// WidthResult holds per-side width multipliers computed by the microstructure module.
type WidthResult struct {
	BidMultiplier float64
	AskMultiplier float64
}

// Microstructure computes dynamic quote width multipliers from three signals:
// realized volatility, venue spread floor, and order book imbalance.
type Microstructure struct {
	volWindow       int
	maxMultiplier   float64
	imbalanceLevels int
	quoteSpreadBps  float64
	state           map[string]*coinMicroState
	mu              sync.RWMutex
}

// coinMicroState tracks per-coin rolling statistics.
type coinMicroState struct {
	returns        []float64 // circular buffer of log-returns
	lastMid        float64
	count          int     // write index for circular buffer
	baseVol        float64 // EWMA baseline volatility
	ewmaInit       bool    // whether EWMA baseline has been seeded
	venueSpreadBps float64
	bidWeight      float64
	askWeight      float64
	sum            float64 // running sum of values in circular buffer
	sumSq          float64 // running sum of squares
	rotations      int     // count of full buffer rotations for drift correction
	cachedVol      float64 // cached current volatility, computed in Update(), read in Width()
}

// NewMicrostructure creates a Microstructure calculator.
//
//   - volWindow: number of updates for the rolling volatility window
//   - maxMultiplier: upper clamp for width multipliers
//   - imbalanceLevels: number of top depth levels to sum for imbalance
//   - quoteSpreadBps: our configured quote spread in basis points
func NewMicrostructure(volWindow int, maxMultiplier float64, imbalanceLevels int, quoteSpreadBps float64) *Microstructure {
	return &Microstructure{
		volWindow:       volWindow,
		maxMultiplier:   maxMultiplier,
		imbalanceLevels: imbalanceLevels,
		quoteSpreadBps:  quoteSpreadBps,
		state:           make(map[string]*coinMicroState),
	}
}

// Update ingests a book snapshot and updates internal state for the given coin.
func (m *Microstructure) Update(coin string, book types.BookUpdate) {
	m.mu.Lock()
	defer m.mu.Unlock()

	st, ok := m.state[coin]
	if !ok {
		st = &coinMicroState{}
		m.state[coin] = st
	}

	mid := book.Mid
	if mid <= 0 {
		return
	}

	// Compute log-return from previous mid.
	if st.lastMid > 0 {
		logReturn := math.Log(mid / st.lastMid)
		// Circular buffer: append until full, then overwrite oldest.
		if len(st.returns) < m.volWindow {
			st.returns = append(st.returns, logReturn)
			st.sum += logReturn
			st.sumSq += logReturn * logReturn
		} else {
			idx := st.count % m.volWindow
			old := st.returns[idx]
			st.sum -= old
			st.sumSq -= old * old
			st.returns[idx] = logReturn
			st.sum += logReturn
			st.sumSq += logReturn * logReturn
		}
		st.count++

		// Drift correction: every 1000 full rotations, recompute sums from buffer.
		if len(st.returns) == m.volWindow && st.count%m.volWindow == 0 {
			st.rotations++
			if st.rotations%1000 == 0 {
				st.sum, st.sumSq = recomputeSums(st.returns)
			}
		}
	}
	st.lastMid = mid

	// Once the buffer is full, compute cached volatility and update EWMA baseline.
	if len(st.returns) >= m.volWindow {
		n := float64(m.volWindow)
		variance := (st.sumSq - st.sum*st.sum/n) / (n - 1)
		st.cachedVol = math.Sqrt(math.Max(0, variance))

		if !st.ewmaInit {
			st.baseVol = st.cachedVol
			st.ewmaInit = true
		} else if st.baseVol < 1e-12 {
			// Re-seed EWMA when baseVol has converged to near-zero during calm markets.
			st.baseVol = st.cachedVol
		} else {
			st.baseVol = math.Max(1e-15, 0.05*st.cachedVol+0.95*st.baseVol)
		}
	}

	// Update venue spread in bps.
	if book.BestAsk > 0 && book.BestBid > 0 && mid > 0 {
		st.venueSpreadBps = (book.BestAsk - book.BestBid) / mid * 10000.0
	}

	// Sum bid/ask weights from top N depth levels.
	n := minInt(m.imbalanceLevels, len(book.Bids))
	st.bidWeight = 0
	for i := 0; i < n; i++ {
		st.bidWeight += book.Bids[i].Size
	}

	n = minInt(m.imbalanceLevels, len(book.Asks))
	st.askWeight = 0
	for i := 0; i < n; i++ {
		st.askWeight += book.Asks[i].Size
	}
}

// Width returns per-side width multipliers for the given coin.
// Returns {1.0, 1.0} if the coin is unknown or not warmed up.
func (m *Microstructure) Width(coin string) WidthResult {
	m.mu.RLock()
	defer m.mu.RUnlock()

	st, ok := m.state[coin]
	if !ok {
		return WidthResult{BidMultiplier: 1.0, AskMultiplier: 1.0}
	}

	// 1. Volatility multiplier (1.0 if not warmed up).
	volMult := 1.0
	if st.ewmaInit && st.baseVol > 0 {
		volMult = math.Max(1.0, st.cachedVol/st.baseVol)
	}

	// 2. Spread floor: never quote tighter than the exchange.
	spreadFloor := 1.0
	if m.quoteSpreadBps > 0 {
		spreadFloor = math.Max(1.0, st.venueSpreadBps/m.quoteSpreadBps)
	}

	// 3. Imbalance: widen the side that is adversely selected.
	bidImb := 1.0
	askImb := 1.0
	if st.bidWeight > 0 && st.askWeight > 0 {
		bidImb = math.Max(1.0, st.askWeight/st.bidWeight)
		askImb = math.Max(1.0, st.bidWeight/st.askWeight)
	}

	// Combine multiplicatively and clamp.
	bidMult := clamp(volMult*spreadFloor*bidImb, 1.0, m.maxMultiplier)
	askMult := clamp(volMult*spreadFloor*askImb, 1.0, m.maxMultiplier)

	return WidthResult{
		BidMultiplier: bidMult,
		AskMultiplier: askMult,
	}
}

// VolMultiplier returns the ratio of current realized vol to baseline vol for the
// given coin. Returns 1.0 during warmup, for unknown coins, or when baseVol is zero.
// Result is clamped to [1.0, maxMultiplier].
func (m *Microstructure) VolMultiplier(coin string) float64 {
	m.mu.RLock()
	defer m.mu.RUnlock()
	st, ok := m.state[coin]
	if !ok || !st.ewmaInit || st.baseVol <= 0 {
		return 1.0
	}
	volMult := math.Max(1.0, st.cachedVol/st.baseVol)
	if volMult > m.maxMultiplier {
		volMult = m.maxMultiplier
	}
	return volMult
}

// clamp restricts v to [lo, hi].
func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// recomputeSums performs a full recomputation of sum and sumSq from the buffer
// to correct accumulated floating-point drift.
func recomputeSums(data []float64) (sum, sumSq float64) {
	for _, v := range data {
		sum += v
		sumSq += v * v
	}
	return
}

// minInt returns the smaller of a and b.
func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
