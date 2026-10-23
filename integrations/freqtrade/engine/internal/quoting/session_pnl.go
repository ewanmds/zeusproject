package quoting

import (
	"sync"
	"time"
)

type pnlSample struct {
	t      time.Time
	equity float64
}

type SessionPnLTracker struct {
	mu            sync.Mutex
	startEquity   float64
	startTime     time.Time
	currentEquity float64
	lastUpdate    time.Time
	window        time.Duration
	samples       []pnlSample
}

func NewSessionPnLTracker(startEquity float64, windowMinutes int) *SessionPnLTracker {
	if windowMinutes <= 0 {
		windowMinutes = 60
	}
	now := time.Now()
	return &SessionPnLTracker{
		startEquity:   startEquity,
		startTime:     now,
		currentEquity: startEquity,
		lastUpdate:    now,
		window:        time.Duration(windowMinutes) * time.Minute,
	}
}

func (t *SessionPnLTracker) Update(equity float64, now time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.currentEquity = equity
	t.lastUpdate = now
	t.samples = append(t.samples, pnlSample{t: now, equity: equity})
	t.pruneLocked(now)
}

func (t *SessionPnLTracker) PnL() (absUSDC float64, bps float64) {
	t.mu.Lock()
	defer t.mu.Unlock()
	abs := t.currentEquity - t.startEquity
	var b float64
	if t.startEquity > 0 {
		b = abs / t.startEquity * 10000.0
	}
	return abs, b
}

// WindowPnLBps prunes samples relative to now (wall clock passed in by caller)
// before computing, so a halted Update path doesn't freeze the rolling window.
func (t *SessionPnLTracker) WindowPnLBps(now time.Time) float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked(now)
	if len(t.samples) < 2 {
		return 0
	}
	first := t.samples[0].equity
	last := t.samples[len(t.samples)-1].equity
	if first <= 0 {
		return 0
	}
	return (last - first) / first * 10000.0
}

func (t *SessionPnLTracker) ElapsedSec() float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	return time.Since(t.startTime).Seconds()
}

// StartEquity returns the equity at session start.
func (t *SessionPnLTracker) StartEquity() float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.startEquity
}

// CurrentEquity returns the most recently observed equity.
func (t *SessionPnLTracker) CurrentEquity() float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.currentEquity
}

func (t *SessionPnLTracker) Reset(equity float64, now time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.startEquity = equity
	t.startTime = now
	t.currentEquity = equity
	t.lastUpdate = now
	t.samples = t.samples[:0]
}

// pruneLocked drops samples older than window relative to now. Caller holds t.mu.
func (t *SessionPnLTracker) pruneLocked(now time.Time) {
	cutoff := now.Add(-t.window)
	i := 0
	for i < len(t.samples) && t.samples[i].t.Before(cutoff) {
		i++
	}
	if i > 0 {
		t.samples = t.samples[i:]
	}
}
