package quoting

import (
	"math"
	"testing"
)

func TestPerSideStats_Empty(t *testing.T) {
	s := NewPerSideStats(10)
	if got := s.Samples("buy"); got != 0 {
		t.Errorf("empty Samples(buy) = %d, want 0", got)
	}
	if got := s.Samples("sell"); got != 0 {
		t.Errorf("empty Samples(sell) = %d, want 0", got)
	}
	if got := s.WinRate("buy"); got != 0 {
		t.Errorf("empty WinRate(buy) = %v, want 0", got)
	}
	if got := s.ProfitFactor("buy"); got != 0 {
		t.Errorf("empty ProfitFactor(buy) = %v, want 0", got)
	}
}

func TestPerSideStats_AllWins(t *testing.T) {
	s := NewPerSideStats(10)
	for _, m := range []float64{1, 2, 3, 4, 5} {
		s.Record("buy", m, 100)
	}
	if got := s.WinRate("buy"); got != 1.0 {
		t.Errorf("all wins WinRate = %v, want 1.0", got)
	}
	pf := s.ProfitFactor("buy")
	if !math.IsInf(pf, 1) {
		t.Errorf("all wins ProfitFactor = %v, want +Inf", pf)
	}
}

func TestPerSideStats_AllLosses(t *testing.T) {
	s := NewPerSideStats(10)
	for _, m := range []float64{-1, -2, -3, -4, -5} {
		s.Record("sell", m, 100)
	}
	if got := s.WinRate("sell"); got != 0.0 {
		t.Errorf("all losses WinRate = %v, want 0.0", got)
	}
	if got := s.ProfitFactor("sell"); got != 0.0 {
		t.Errorf("all losses ProfitFactor = %v, want 0.0", got)
	}
}

func TestPerSideStats_Mixed(t *testing.T) {
	s := NewPerSideStats(20)
	mks := []float64{1, 2, 3, 1, 2, 1, -1, -2, -1, -1}
	for _, m := range mks {
		s.Record("buy", m, 100)
	}
	if got := s.Samples("buy"); got != 10 {
		t.Errorf("Samples = %d, want 10", got)
	}
	if got := s.WinRate("buy"); math.Abs(got-0.6) > 1e-9 {
		t.Errorf("WinRate = %v, want 0.6", got)
	}
	if got := s.ProfitFactor("buy"); math.Abs(got-2.0) > 1e-9 {
		t.Errorf("ProfitFactor = %v, want 2.0 (10/5)", got)
	}
}

func TestPerSideStats_RingEviction(t *testing.T) {
	s := NewPerSideStats(10)
	for i := 0; i < 15; i++ {
		s.Record("buy", 1.0, 100)
	}
	if got := s.Samples("buy"); got != 10 {
		t.Errorf("Samples after 15 records into window=10 = %d, want 10", got)
	}
	for i := 0; i < 10; i++ {
		s.Record("buy", -1.0, 100)
	}
	if got := s.WinRate("buy"); got != 0.0 {
		t.Errorf("after eviction WinRate = %v, want 0.0 (last 10 all losses)", got)
	}
}

func TestPerSideStats_PerSideIsolation(t *testing.T) {
	s := NewPerSideStats(10)
	s.Record("buy", 5, 100)
	s.Record("buy", 5, 100)
	s.Record("sell", -3, 100)
	if got := s.Samples("buy"); got != 2 {
		t.Errorf("buy Samples = %d, want 2", got)
	}
	if got := s.Samples("sell"); got != 1 {
		t.Errorf("sell Samples = %d, want 1", got)
	}
	if got := s.WinRate("buy"); got != 1.0 {
		t.Errorf("buy WinRate = %v, want 1.0", got)
	}
	if got := s.WinRate("sell"); got != 0.0 {
		t.Errorf("sell WinRate = %v, want 0.0", got)
	}
}

func TestPerSideStats_ZeroMarkoutsNeutral(t *testing.T) {
	s := NewPerSideStats(10)
	s.Record("buy", 0, 100)
	s.Record("buy", 0, 100)
	if got := s.WinRate("buy"); got != 0 {
		t.Errorf("all-zero WinRate = %v, want 0 (zero is not a win)", got)
	}
	if got := s.ProfitFactor("buy"); got != 0 {
		t.Errorf("all-zero ProfitFactor = %v, want 0 (no gains, no losses)", got)
	}
}

func TestPerSideStats_NilSafe(t *testing.T) {
	var s *PerSideStats
	s.Record("buy", 1, 100)
	if got := s.Samples("buy"); got != 0 {
		t.Errorf("nil Samples = %d, want 0", got)
	}
	if got := s.WinRate("buy"); got != 0 {
		t.Errorf("nil WinRate = %v, want 0", got)
	}
	if got := s.ProfitFactor("buy"); got != 0 {
		t.Errorf("nil ProfitFactor = %v, want 0", got)
	}
}
