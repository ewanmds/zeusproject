package quoting

import (
	"math"
	"sync"
	"time"
)

type priceShockSample struct {
	at  time.Time
	mid float64
}

// PriceShockSnapshot describes the current state of the price shock guard.
type PriceShockSnapshot struct {
	Active      bool
	Direction   string // "up" or "down"
	EntrySide   string // side to suppress/cancel while active
	MoveBps     float64
	ActiveUntil time.Time
}

// PriceShockTracker detects fast mid-price moves over a short rolling window.
type PriceShockTracker struct {
	window    time.Duration
	threshold float64
	cooldown  time.Duration

	samples []priceShockSample
	active  PriceShockSnapshot

	mu sync.Mutex
}

func NewPriceShockTracker(window, cooldown time.Duration, thresholdBps float64) *PriceShockTracker {
	return &PriceShockTracker{
		window:    window,
		threshold: thresholdBps,
		cooldown:  cooldown,
		samples:   make([]priceShockSample, 0, 32),
	}
}

// Record ingests the current mid and returns the snapshot plus whether this
// call newly activated the guard.
func (t *PriceShockTracker) Record(mid float64, now time.Time) (PriceShockSnapshot, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if mid <= 0 || t.window <= 0 || t.threshold <= 0 {
		return t.snapshotLocked(now), false
	}

	cutoff := now.Add(-t.window)
	i := 0
	for i < len(t.samples) && t.samples[i].at.Before(cutoff) {
		i++
	}
	if i > 0 {
		copy(t.samples, t.samples[i:])
		t.samples = t.samples[:len(t.samples)-i]
	}
	t.samples = append(t.samples, priceShockSample{at: now, mid: mid})

	if len(t.samples) < 2 {
		return t.snapshotLocked(now), false
	}

	oldest := t.samples[0]
	if oldest.mid <= 0 {
		return t.snapshotLocked(now), false
	}
	moveBps := (mid - oldest.mid) / oldest.mid * 10000.0
	if math.Abs(moveBps) < t.threshold {
		return t.snapshotLocked(now), false
	}

	snap := PriceShockSnapshot{
		Active:      true,
		Direction:   "up",
		EntrySide:   "sell",
		MoveBps:     moveBps,
		ActiveUntil: now.Add(t.cooldown),
	}
	if moveBps < 0 {
		snap.Direction = "down"
		snap.EntrySide = "buy"
	}

	wasActiveSameDirection := t.active.Active && now.Before(t.active.ActiveUntil) && t.active.Direction == snap.Direction
	t.active = snap
	return t.active, !wasActiveSameDirection
}

func (t *PriceShockTracker) Snapshot(now time.Time) PriceShockSnapshot {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.snapshotLocked(now)
}

func (t *PriceShockTracker) snapshotLocked(now time.Time) PriceShockSnapshot {
	if t.active.Active && !now.Before(t.active.ActiveUntil) {
		t.active.Active = false
		t.active.EntrySide = ""
	}
	return t.active
}

func sideIncreasesPosition(side string, posSide string) bool {
	switch side {
	case "buy":
		return posSide != "short"
	case "sell":
		return posSide != "long"
	default:
		return false
	}
}
