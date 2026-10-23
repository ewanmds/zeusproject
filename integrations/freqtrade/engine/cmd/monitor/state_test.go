package main

import (
	"testing"
)

func TestRingBuffer_Basic(t *testing.T) {
	rb := NewRingBuffer[int](3)
	if rb.Len() != 0 {
		t.Fatalf("expected len 0, got %d", rb.Len())
	}

	rb.Add(1)
	rb.Add(2)
	rb.Add(3)
	if rb.Len() != 3 {
		t.Fatalf("expected len 3, got %d", rb.Len())
	}

	// Items returns newest-first
	items := rb.Items()
	if len(items) != 3 || items[0] != 3 || items[1] != 2 || items[2] != 1 {
		t.Fatalf("expected [3,2,1], got %v", items)
	}

	// Overflow: oldest item (1) should be evicted
	rb.Add(4)
	if rb.Len() != 3 {
		t.Fatalf("expected len 3 after overflow, got %d", rb.Len())
	}
	items = rb.Items()
	if items[0] != 4 || items[1] != 3 || items[2] != 2 {
		t.Fatalf("expected [4,3,2], got %v", items)
	}
}

func TestRingBuffer_Empty(t *testing.T) {
	rb := NewRingBuffer[string](5)
	items := rb.Items()
	if len(items) != 0 {
		t.Fatalf("expected empty items, got %v", items)
	}
}

func TestLiveEquity_FallbackWithoutSnapshot(t *testing.T) {
	s := NewState()
	s.Portfolio.Equity = 100.0
	got := s.LiveEquity()
	if got != 100.0 {
		t.Fatalf("expected fallback to Portfolio.Equity=100, got %v", got)
	}
}

func TestLiveEquity_ShortGainsWhenPriceDrops(t *testing.T) {
	s := NewState()
	s.Portfolio.Equity = 354.46
	s.Portfolio.StatusEquity = 354.46
	a := s.GetOrCreateAsset("xyz:CL")
	a.StatusBookMid = 96.126
	a.StatusPosSize = -3.327 // short 3.327
	a.LastBuyWall = 96.052
	a.LastSellWall = 96.089
	got := s.LiveEquity()
	// mid = (96.052 + 96.089)/2 = 96.0705
	// delta = -3.327 * (96.0705 - 96.126) = +0.18465
	want := 354.46 + (-3.327)*((96.052+96.089)/2-96.126)
	if diff := got - want; diff > 1e-6 || diff < -1e-6 {
		t.Fatalf("expected %.6f, got %.6f", want, got)
	}
}

func TestLiveEquity_LongLosesWhenPriceDrops(t *testing.T) {
	s := NewState()
	s.Portfolio.StatusEquity = 100.0
	a := s.GetOrCreateAsset("xyz:CL")
	a.StatusBookMid = 100.0
	a.StatusPosSize = 2.0 // long 2
	a.LastFillPrice = 99.5
	got := s.LiveEquity()
	want := 100.0 + 2.0*(99.5-100.0) // = 99.0
	if got != want {
		t.Fatalf("expected %.2f, got %.2f", want, got)
	}
}

func TestLatestMid_FallbackChain(t *testing.T) {
	a := NewAssetState("xyz:CL")
	// empty → 0
	if a.LatestMid() != 0 {
		t.Fatalf("expected 0, got %v", a.LatestMid())
	}
	// StatusBookMid only
	a.StatusBookMid = 100.0
	if a.LatestMid() != 100.0 {
		t.Fatalf("expected 100, got %v", a.LatestMid())
	}
	// Adding a fill price supersedes StatusBookMid
	a.LastFillPrice = 101.0
	if a.LatestMid() != 101.0 {
		t.Fatalf("expected 101, got %v", a.LatestMid())
	}
	// One wall alone doesn't average; prefers fill
	a.LastBuyWall = 102.0
	if a.LatestMid() != 101.0 {
		t.Fatalf("expected 101 (fill preferred over single wall), got %v", a.LatestMid())
	}
	// Both walls → average
	a.LastSellWall = 104.0
	if a.LatestMid() != 103.0 {
		t.Fatalf("expected 103 (avg of 102 and 104), got %v", a.LatestMid())
	}
}
