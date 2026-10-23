package quoting

import (
	"math"
	"sync"
)

type fillPnLSample struct {
	markoutBps float64
	notional   float64
}

type sideRing struct {
	samples []fillPnLSample
	head    int
	filled  bool
}

func (r *sideRing) record(s fillPnLSample, window int) {
	if window <= 0 {
		return
	}
	if cap(r.samples) < window {
		r.samples = make([]fillPnLSample, window)
		r.head = 0
		r.filled = false
	}
	r.samples = r.samples[:window]
	r.samples[r.head] = s
	r.head = (r.head + 1) % window
	if r.head == 0 {
		r.filled = true
	}
}

func (r *sideRing) size() int {
	if r.filled {
		return len(r.samples)
	}
	return r.head
}

func (r *sideRing) iter() []fillPnLSample {
	n := r.size()
	if n == 0 {
		return nil
	}
	if !r.filled {
		out := make([]fillPnLSample, n)
		copy(out, r.samples[:n])
		return out
	}
	out := make([]fillPnLSample, len(r.samples))
	copy(out, r.samples)
	return out
}

type PerSideStats struct {
	window int
	buy    sideRing
	sell   sideRing
	mu     sync.Mutex
}

func NewPerSideStats(window int) *PerSideStats {
	if window <= 0 {
		window = 50
	}
	return &PerSideStats{window: window}
}

func (s *PerSideStats) ringFor(side string) *sideRing {
	switch normalizeSide(side) {
	case "buy":
		return &s.buy
	case "sell":
		return &s.sell
	default:
		return nil
	}
}

func (s *PerSideStats) Record(side string, markoutBps, notional float64) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r := s.ringFor(side)
	if r == nil {
		return
	}
	r.record(fillPnLSample{markoutBps: markoutBps, notional: notional}, s.window)
}

func (s *PerSideStats) Samples(side string) int {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r := s.ringFor(side)
	if r == nil {
		return 0
	}
	return r.size()
}

func (s *PerSideStats) WinRate(side string) float64 {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r := s.ringFor(side)
	if r == nil {
		return 0
	}
	n := r.size()
	if n == 0 {
		return 0
	}
	wins := 0
	for _, smp := range r.iter() {
		if smp.markoutBps > 0 {
			wins++
		}
	}
	return float64(wins) / float64(n)
}

func (s *PerSideStats) ProfitFactor(side string) float64 {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r := s.ringFor(side)
	if r == nil {
		return 0
	}
	n := r.size()
	if n == 0 {
		return 0
	}
	var gains, losses float64
	for _, smp := range r.iter() {
		if smp.markoutBps > 0 {
			gains += smp.markoutBps
		} else if smp.markoutBps < 0 {
			losses += -smp.markoutBps
		}
	}
	if losses == 0 {
		if gains == 0 {
			return 0
		}
		return math.Inf(1)
	}
	return gains / losses
}
