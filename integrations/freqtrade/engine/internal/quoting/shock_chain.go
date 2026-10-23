package quoting

import (
	"sync"
	"time"
)

// ShockChainTracker counts distinct price_shock activations in a rolling
// window. When the count crosses a threshold, the caller treats the cascade
// as a regime-change event and escalates beyond the per-shock cooldown:
// cancel all orders, close position passively, pause quoting, then enter
// an extended recovery window. Introduced in V30 to address the V28 cascade
// (6 shocks in ~4 min) that whipsawed the bot into a session stop-loss at
// −101 bps; individual per-shock cooldowns re-armed between echoes without
// memory of the chain.
type ShockChainTracker struct {
	window    time.Duration
	threshold int

	shocks []time.Time // activation timestamps

	mu sync.Mutex
}

// NewShockChainTracker creates a tracker with the given rolling window and
// threshold. threshold <= 0 disables escalation (the tracker still records
// but never reports chain activation).
func NewShockChainTracker(window time.Duration, threshold int) *ShockChainTracker {
	return &ShockChainTracker{
		window:    window,
		threshold: threshold,
		shocks:    make([]time.Time, 0, 8),
	}
}

// Record registers a new shock activation at time `at`, prunes expired
// entries, and returns (current count, true if the count crossed the
// threshold on this call). The "newly-crossed" flag fires exactly once per
// escalation: subsequent calls at or above the threshold return false until
// prior entries age out of the window and the count drops back below.
func (t *ShockChainTracker) Record(at time.Time) (int, bool) {
	if t == nil {
		return 0, false
	}
	t.mu.Lock()
	defer t.mu.Unlock()

	cutoff := at.Add(-t.window)
	i := 0
	for i < len(t.shocks) && t.shocks[i].Before(cutoff) {
		i++
	}
	if i > 0 {
		copy(t.shocks, t.shocks[i:])
		t.shocks = t.shocks[:len(t.shocks)-i]
	}

	t.shocks = append(t.shocks, at)
	count := len(t.shocks)

	// count == threshold: we just appended the threshold-crossing activation.
	// Because each Record adds exactly one entry, prevCount < threshold and
	// count >= threshold reduces to count == threshold.
	crossed := t.threshold > 0 && count == t.threshold
	return count, crossed
}

// Count returns the current number of shocks in the rolling window (prunes
// stale entries as a side effect). Safe to call concurrently with Record.
func (t *ShockChainTracker) Count(at time.Time) int {
	if t == nil {
		return 0
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	cutoff := at.Add(-t.window)
	i := 0
	for i < len(t.shocks) && t.shocks[i].Before(cutoff) {
		i++
	}
	if i > 0 {
		copy(t.shocks, t.shocks[i:])
		t.shocks = t.shocks[:len(t.shocks)-i]
	}
	return len(t.shocks)
}

// Reset clears all recorded shocks. Call after a successful recovery so the
// next cascade requires a fresh threshold crossing.
func (t *ShockChainTracker) Reset() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.shocks = t.shocks[:0]
}
