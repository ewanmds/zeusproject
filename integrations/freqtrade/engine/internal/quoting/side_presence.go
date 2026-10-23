package quoting

import "sync"

// SidePresenceTracker counts quote cycles where a side produced zero orders
// (i.e., allowed notional fell below the exchange minimum, so the side was
// effectively absent from the book). This exposes how often stacked entry-side
// suppression (wick/flow/schedule/toxicity/A6) collapses one side entirely —
// a market-maker that's frequently single-sided has drifted into directional
// holding mode. Per-coin, thread-safe, rolling between snapshots.
type SidePresenceTracker struct {
	mu          sync.Mutex
	cycles      int
	bidAbsent   int
	askAbsent   int
	bothAbsent  int
}

// Record one quote cycle. buyOrders and sellOrders are the order counts
// produced by buildSideOrders for each side.
func (t *SidePresenceTracker) Record(buyOrders, sellOrders int) {
	t.mu.Lock()
	t.cycles++
	bidOut := buyOrders == 0
	askOut := sellOrders == 0
	if bidOut {
		t.bidAbsent++
	}
	if askOut {
		t.askAbsent++
	}
	if bidOut && askOut {
		t.bothAbsent++
	}
	t.mu.Unlock()
}

// SnapshotAndReset returns the current absence percentages and resets counters.
// Returns (bidAbsentPct, askAbsentPct, bothAbsentPct, cycles).
// When cycles == 0, returns zeros (no data yet).
func (t *SidePresenceTracker) SnapshotAndReset() (float64, float64, float64, int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.cycles == 0 {
		return 0, 0, 0, 0
	}
	cycles := t.cycles
	bid := float64(t.bidAbsent) / float64(cycles) * 100.0
	ask := float64(t.askAbsent) / float64(cycles) * 100.0
	both := float64(t.bothAbsent) / float64(cycles) * 100.0
	t.cycles = 0
	t.bidAbsent = 0
	t.askAbsent = 0
	t.bothAbsent = 0
	return bid, ask, both, cycles
}
