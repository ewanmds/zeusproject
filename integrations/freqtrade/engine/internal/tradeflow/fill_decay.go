package tradeflow

import (
	"math"
	"sync"
	"time"
)

type decayEntry struct {
	shiftBps float64
	halfLife time.Duration
	ts       time.Time
}

// FillDecay tracks exponentially decaying post-fill fair value shifts.
type FillDecay struct {
	entries []decayEntry
	mu      sync.Mutex
}

func NewFillDecay() *FillDecay {
	return &FillDecay{}
}

// RecordFill adds a decaying shift. Buy fill -> negative shift (FV down). Sell fill -> positive shift (FV up).
func (fd *FillDecay) RecordFill(side string, shiftBps float64, halfLife time.Duration) {
	fd.mu.Lock()
	defer fd.mu.Unlock()

	signed := shiftBps
	if side == "buy" {
		signed = -shiftBps
	}
	fd.entries = append(fd.entries, decayEntry{
		shiftBps: signed,
		halfLife: halfLife,
		ts:       time.Now(),
	})
}

// CurrentShiftBps returns the sum of all active shifts with exponential decay.
// Prunes entries that have decayed below 0.01 bps.
func (fd *FillDecay) CurrentShiftBps() float64 {
	fd.mu.Lock()
	defer fd.mu.Unlock()

	now := time.Now()
	var total float64
	var active []decayEntry

	for _, e := range fd.entries {
		elapsed := now.Sub(e.ts)
		decay := math.Pow(2, -float64(elapsed)/float64(e.halfLife))
		shifted := e.shiftBps * decay
		if math.Abs(shifted) < 0.01 {
			continue
		}
		total += shifted
		active = append(active, e)
	}

	fd.entries = active
	return total
}
