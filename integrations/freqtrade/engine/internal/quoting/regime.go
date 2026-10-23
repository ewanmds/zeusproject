package quoting

import (
	"math"
	"sync"
	"time"
)

// RegimeDetector computes lag-1 autocorrelation of mid-price log-returns
// over a rolling window, used by Features 3 (adaptive skew) and 4 (adaptive alpha).
//
// Positive autocorrelation → trending regime.
// Negative autocorrelation → mean-reverting regime.
type RegimeDetector struct {
	window  int
	returns []float64 // circular buffer of log-returns
	pos     int       // next write position
	count   int       // number of values added (capped at window)
	lastMid float64

	// P1: regime-gated quoting state. Activation requires autocorrelation and
	// VWAP divergence to both qualify for a minimum duration.
	pendingSince   time.Time
	trendActive    bool
	trendDir       int
	trendEnteredAt time.Time

	mu sync.RWMutex
}

// NewRegimeDetector creates a new detector with the given window size.
func NewRegimeDetector(window int) *RegimeDetector {
	return &RegimeDetector{
		window:  window,
		returns: make([]float64, window),
	}
}

// Update ingests a new mid price and computes the log-return from the previous mid.
func (r *RegimeDetector) Update(mid float64) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if mid <= 0 {
		return
	}
	if r.lastMid > 0 {
		logReturn := math.Log(mid / r.lastMid)
		r.returns[r.pos] = logReturn
		r.pos = (r.pos + 1) % r.window
		if r.count < r.window {
			r.count++
		}
	}
	r.lastMid = mid
}

// Autocorrelation returns the lag-1 autocorrelation of the rolling return window.
// Returns (0, false) if the buffer is not yet full.
func (r *RegimeDetector) Autocorrelation() (float64, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.autocorrelationLocked()
}

// autocorrelationLocked is the shared computation used by both Autocorrelation()
// and IsTrending(). Caller must hold r.mu (read or write).
func (r *RegimeDetector) autocorrelationLocked() (float64, bool) {
	if r.count < r.window {
		return 0, false
	}

	// Compute mean.
	var sum float64
	for i := 0; i < r.window; i++ {
		sum += r.returns[i]
	}
	mean := sum / float64(r.window)

	// Compute lag-1 autocorrelation.
	// Denominator is the full sample variance (all N elements).
	var den float64
	for i := 0; i < r.window; i++ {
		d := r.returns[i] - mean
		den += d * d
	}
	if den == 0 {
		return 0, true
	}

	// Numerator: sum of (r_i - mean) * (r_{i-1} - mean) in chronological order.
	var num float64
	for i := 1; i < r.window; i++ {
		curr := (r.pos - r.window + i + r.window) % r.window
		prev := (r.pos - r.window + i - 1 + r.window) % r.window
		num += (r.returns[curr] - mean) * (r.returns[prev] - mean)
	}

	return num / den, true
}

// IsTrending returns whether a sustained trend regime is currently active, the
// direction (+1 up, -1 down), and how long the trend has been active.
//
// Activation requires BOTH:
//   - |autocorrelation| >= autocorrThreshold
//   - |shortV - longV| / sigma >= divergenceSigma
//
// sustained for at least minDuration. Direction = sign(shortV - longV).
//
// Callers pass signals and thresholds (detector stays pure/testable).
func (r *RegimeDetector) IsTrending(
	now time.Time,
	autocorrThreshold float64,
	divergenceSigma float64,
	minDuration time.Duration,
	shortV float64,
	longV float64,
	sigma float64,
) (bool, int, time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()

	autocorr, ok := r.autocorrelationLocked()
	if !ok || sigma <= 0 || shortV <= 0 || longV <= 0 {
		r.pendingSince = time.Time{}
		r.trendActive = false
		r.trendDir = 0
		return false, 0, 0
	}

	absAuto := math.Abs(autocorr)
	div := math.Abs(shortV-longV) / sigma
	qualified := absAuto >= autocorrThreshold && div >= divergenceSigma

	if !qualified {
		r.pendingSince = time.Time{}
		r.trendActive = false
		r.trendDir = 0
		return false, 0, 0
	}

	dir := 1
	if shortV < longV {
		dir = -1
	}

	if r.pendingSince.IsZero() {
		r.pendingSince = now
	}

	if !r.trendActive {
		if now.Sub(r.pendingSince) >= minDuration {
			r.trendActive = true
			r.trendEnteredAt = now
			r.trendDir = dir
			return true, dir, 0
		}
		return false, 0, 0
	}

	// Already active: if direction flipped, restart (treat as new trend).
	if dir != r.trendDir {
		r.trendActive = true
		r.trendDir = dir
		r.trendEnteredAt = now
		return true, dir, 0
	}
	return true, r.trendDir, now.Sub(r.trendEnteredAt)
}
