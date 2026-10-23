package quoting

import "sort"

// quantileNormalizer maps a raw sub-dimension value into [0, 1] using the
// empirical p_low / p_high percentiles of a rolling sample window. Before
// warmup (Ready returns false) the caller must fall back to the legacy
// linear clip — Normalize returns the sentinel -1 in that case.
type quantileNormalizer struct {
	window   int
	low      float64 // [0, 1] target low percentile (e.g. 0.05)
	high     float64 // [0, 1] target high percentile (e.g. 0.95)
	samples  []float64
	head     int
	count    int
	warm     bool // true once Ready has returned true at least once
	warmedAt bool // tracks one-shot transition log emission
}

// newQuantileNormalizer creates a normalizer with the given ring-buffer size
// and percentile endpoints (0-100 integer scale).
func newQuantileNormalizer(window int, lowPct, highPct int) *quantileNormalizer {
	if window <= 0 {
		window = 500
	}
	return &quantileNormalizer{
		window:  window,
		low:     float64(lowPct) / 100.0,
		high:    float64(highPct) / 100.0,
		samples: make([]float64, 0, window),
	}
}

// Observe records a sample in the rolling window.
func (q *quantileNormalizer) Observe(v float64) {
	if q.count < q.window {
		q.samples = append(q.samples, v)
		q.count++
	} else {
		q.samples[q.head] = v
		q.head = (q.head + 1) % q.window
	}
}

// Ready reports whether the window has enough samples (>= window/5) to
// produce a meaningful empirical percentile.
func (q *quantileNormalizer) Ready() bool {
	threshold := q.window / 5
	if threshold < 1 {
		threshold = 1
	}
	return q.count >= threshold
}

// Normalize maps v into [0, 1] using the current empirical p_low/p_high
// endpoints. Returns the sentinel -1 when Ready is false; callers must use
// a linear fallback in that case. Returns 0.5 on a degenerate window
// (p_low == p_high).
func (q *quantileNormalizer) Normalize(v float64) float64 {
	if !q.Ready() {
		return -1
	}
	q.warm = true
	sorted := make([]float64, q.count)
	copy(sorted, q.samples[:q.count])
	sort.Float64s(sorted)
	lowIdx := int(q.low * float64(q.count))
	highIdx := int(q.high * float64(q.count))
	if lowIdx < 0 {
		lowIdx = 0
	}
	if lowIdx >= q.count {
		lowIdx = q.count - 1
	}
	if highIdx < 0 {
		highIdx = 0
	}
	if highIdx >= q.count {
		highIdx = q.count - 1
	}
	pLow := sorted[lowIdx]
	pHigh := sorted[highIdx]
	if pHigh <= pLow {
		return 0.5
	}
	norm := (v - pLow) / (pHigh - pLow)
	if norm < 0 {
		return 0
	}
	if norm > 1 {
		return 1
	}
	return norm
}

// Snapshot returns p_low / p_50 / p_high and the current sample count.
// When count < 2 returns zeros with the current sample count.
func (q *quantileNormalizer) Snapshot() (pLow, pMid, pHigh float64, samples int) {
	if q.count < 2 {
		return 0, 0, 0, q.count
	}
	sorted := make([]float64, q.count)
	copy(sorted, q.samples[:q.count])
	sort.Float64s(sorted)
	lowIdx := int(q.low * float64(q.count))
	highIdx := int(q.high * float64(q.count))
	midIdx := q.count / 2
	if lowIdx >= q.count {
		lowIdx = q.count - 1
	}
	if highIdx >= q.count {
		highIdx = q.count - 1
	}
	return sorted[lowIdx], sorted[midIdx], sorted[highIdx], q.count
}

// transitionedToWarm returns true exactly once — the first time it is called
// after Ready has become true. Callers use this to emit a one-shot log.
func (q *quantileNormalizer) transitionedToWarm() bool {
	if !q.warm {
		return false
	}
	if q.warmedAt {
		return false
	}
	q.warmedAt = true
	return true
}
