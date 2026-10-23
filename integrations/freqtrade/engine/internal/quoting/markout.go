package quoting

import (
	"math"
	"sync"
	"time"
)

// markoutWindows are the fixed horizons at which mid-price markout is measured after a fill.
var markoutWindows = [4]time.Duration{
	500 * time.Millisecond,
	1 * time.Second,
	2 * time.Second,
	5 * time.Second,
}

// markoutEntry tracks a single pending fill awaiting markout measurement.
type markoutEntry struct {
	fillTime  time.Time
	fillPrice float64
	midAtFill float64
	direction float64 // +1 buy, -1 sell
	side      string
	size      float64
	wallFront bool
	markouts  [4]float64
	completed [4]bool
}

// MarkoutResult is returned when all windows for a fill have been measured.
type MarkoutResult struct {
	Side      string
	FillPrice float64
	MidAtFill float64
	Size      float64
	WallFront bool
	Markouts  [4]float64 // bps at each window
}

// MarkoutEWMA holds the rolling EWMA markout values and fill count.
type MarkoutEWMA struct {
	Values [4]float64
	Count  int
}

// MarkoutTracker records fills and measures post-fill mid-price movement (markout)
// at fixed time horizons. Maintains EWMA rolling averages of markout in basis points.
type MarkoutTracker struct {
	pending    []markoutEntry
	ewma       [4]float64
	ewmaSeeded [4]bool // per-window: true once the first value has been recorded
	alpha      float64 // EWMA smoothing factor derived from half-life
	count      int
	mu         sync.Mutex
}

// NewMarkoutTracker creates a tracker with the given EWMA half-life.
// Alpha is computed as 1 - exp(-1/halfLife.Seconds()), giving exponential
// smoothing where weight decays to ~50% after halfLife seconds of fills at 1/sec.
func NewMarkoutTracker(halfLife time.Duration) *MarkoutTracker {
	hl := halfLife.Seconds()
	if hl <= 0 {
		hl = 300
	}
	return &MarkoutTracker{
		alpha: 1 - math.Exp(-1.0/hl),
	}
}

// maxFillAge is the maximum age of a fill timestamp accepted for markout tracking.
// Fills older than this are considered replay artifacts from WebSocket reconnects and are dropped.
const maxFillAge = 30 * time.Second

// RecordFill adds a fill to be tracked for markout measurement. Uses time.Now() as the fill time.
// This is the production entry point; stale fills (older than maxFillAge) from WebSocket
// reconnect replays are silently dropped to prevent EWMA contamination.
func (mt *MarkoutTracker) RecordFill(fillPrice, midAtFill float64, side string, size float64) {
	now := time.Now()
	mt.recordFillInternal(fillPrice, midAtFill, side, size, false, now)
}

// RecordFillWithMeta adds a fill with execution metadata used by downstream
// attribution systems such as wall-front quality scoring.
func (mt *MarkoutTracker) RecordFillWithMeta(fillPrice, midAtFill float64, side string, size float64, wallFront bool) {
	now := time.Now()
	mt.recordFillInternal(fillPrice, midAtFill, side, size, wallFront, now)
}

// RecordFillAt adds a fill with an explicit timestamp (useful for testing).
// Does NOT apply the replay guard — callers are responsible for timestamp validity.
func (mt *MarkoutTracker) RecordFillAt(fillPrice, midAtFill float64, side string, size float64, now time.Time) {
	mt.recordFillInternal(fillPrice, midAtFill, side, size, false, now)
}

func (mt *MarkoutTracker) recordFillInternal(fillPrice, midAtFill float64, side string, size float64, wallFront bool, now time.Time) {
	dir := 1.0
	if side == "sell" {
		dir = -1.0
	}
	mt.mu.Lock()
	mt.pending = append(mt.pending, markoutEntry{
		fillTime:  now,
		fillPrice: fillPrice,
		midAtFill: midAtFill,
		direction: dir,
		side:      side,
		size:      size,
		wallFront: wallFront,
	})
	mt.mu.Unlock()
}

// OnBookUpdate checks all pending fills against the current mid and time.
// For each fill, any window where elapsed >= windowDuration is measured.
// Returns results for fills where all 4 windows are complete (and prunes them).
func (mt *MarkoutTracker) OnBookUpdate(mid float64, now time.Time) []MarkoutResult {
	mt.mu.Lock()
	defer mt.mu.Unlock()

	var results []MarkoutResult
	alive := mt.pending[:0] // reuse backing array

	for i := range mt.pending {
		e := &mt.pending[i]
		elapsed := now.Sub(e.fillTime)
		allDone := true

		for w := 0; w < 4; w++ {
			if e.completed[w] {
				continue
			}
			if elapsed >= markoutWindows[w] {
				// markout = (mid_now - fill_price) / fill_price * 10000 * direction
				e.markouts[w] = (mid - e.fillPrice) / e.fillPrice * 10000.0 * e.direction
				e.completed[w] = true

				// Update EWMA for this window.
				if !mt.ewmaSeeded[w] {
					// First value for this window: seed EWMA directly.
					mt.ewma[w] = e.markouts[w]
					mt.ewmaSeeded[w] = true
				} else {
					mt.ewma[w] = mt.ewma[w]*(1-mt.alpha) + e.markouts[w]*mt.alpha
				}
			} else {
				allDone = false
			}
		}

		if allDone {
			mt.count++
			results = append(results, MarkoutResult{
				Side:      e.side,
				FillPrice: e.fillPrice,
				MidAtFill: e.midAtFill,
				Size:      e.size,
				WallFront: e.wallFront,
				Markouts:  e.markouts,
			})
		} else {
			alive = append(alive, *e)
		}
	}

	mt.pending = alive
	return results
}

// EWMA returns the current rolling EWMA markout values and fill count.
func (mt *MarkoutTracker) EWMA() MarkoutEWMA {
	mt.mu.Lock()
	defer mt.mu.Unlock()
	return MarkoutEWMA{
		Values: mt.ewma,
		Count:  mt.count,
	}
}

// PendingCount returns the number of fills awaiting markout completion.
func (mt *MarkoutTracker) PendingCount() int {
	mt.mu.Lock()
	defer mt.mu.Unlock()
	return len(mt.pending)
}
