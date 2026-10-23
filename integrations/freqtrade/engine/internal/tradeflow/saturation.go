package tradeflow

// saturationRing tracks the fraction of recent imbalance reads that hit the
// ±cap (|imbalance| > 0.95) over a rolling 64-sample window.
type saturationRing struct {
	ring  [64]bool
	idx   int
	count int // count of true entries in ring
	total int // total entries written (capped at 64)
}

// Record adds a saturated/non-saturated observation to the ring.
func (s *saturationRing) Record(saturated bool) {
	old := s.ring[s.idx]
	s.ring[s.idx] = saturated
	s.idx = (s.idx + 1) % 64
	if s.total < 64 {
		s.total++
		if saturated {
			s.count++
		}
		return
	}
	if saturated && !old {
		s.count++
	} else if !saturated && old {
		s.count--
	}
}

// Fraction returns the fraction of recent reads that were saturated, or 0 if
// no readings have been recorded.
func (s *saturationRing) Fraction() float64 {
	if s.total == 0 {
		return 0
	}
	n := s.total
	if n > 64 {
		n = 64
	}
	return float64(s.count) / float64(n)
}
