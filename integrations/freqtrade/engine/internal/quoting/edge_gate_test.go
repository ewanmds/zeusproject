package quoting

import (
	"testing"

	"hyperliquid-market-maker/internal/types"
)

func TestRequiredHalfSpreadBps(t *testing.T) {
	cases := []struct {
		fee, adverse, target, want float64
	}{
		{0.5, 0.75, 1.0, 1.75},
		{1.5, 0.5, 2.0, 3.0},
		{-1, -1, -1, 0},
		{0, 0, 0, 0},
	}
	for _, c := range cases {
		if got := requiredHalfSpreadBps(c.fee, c.adverse, c.target); got != c.want {
			t.Fatalf("requiredHalfSpreadBps(%g,%g,%g)=%g want %g", c.fee, c.adverse, c.target, got, c.want)
		}
	}
}

func TestApplyEdgeFloorWidensOnlyWhenTooTight(t *testing.T) {
	ref := 100.0
	// Too tight bid at 0.1 bps: widen to the 1.5 bps floor.
	bid, skip := applyEdgeFloor(99.99, ref, true, 1.5, 10)
	if skip {
		t.Fatal("did not expect skip")
	}
	if want := 100.0 - 100.0*1.5/10000.0; bid != want {
		t.Fatalf("bid=%g want %g", bid, want)
	}
	// Already wider than the floor: unchanged.
	wideBid := 99.90 // 10 bps
	if got, _ := applyEdgeFloor(wideBid, ref, true, 1.5, 10); got != wideBid {
		t.Fatalf("wide bid changed to %g", got)
	}
	// Ask side.
	ask, skip := applyEdgeFloor(100.001, ref, false, 1.5, 10)
	if skip {
		t.Fatal("did not expect ask skip")
	}
	if want := 100.0 + 100.0*1.5/10000.0; ask != want {
		t.Fatalf("ask=%g want %g", ask, want)
	}
	// Requirement above the cap: skip the side.
	if _, skip := applyEdgeFloor(99.99, ref, true, 11, 10); !skip {
		t.Fatal("expected side to be skipped when the required width exceeds the cap")
	}
	// Disabled gate: no change.
	if got, skip := applyEdgeFloor(99.99, ref, true, 0, 10); got != 99.99 || skip {
		t.Fatalf("disabled gate changed price to %g (skip=%v)", got, skip)
	}
}

func TestCapOrdersPerSideKeepsBothSides(t *testing.T) {
	orders := []types.OrderRequest{
		{Coin: "x", Side: "buy", Price: 99, Size: 1},
		{Coin: "x", Side: "buy", Price: 98, Size: 1},
		{Coin: "x", Side: "sell", Price: 101, Size: 1},
		{Coin: "x", Side: "sell", Price: 102, Size: 1},
	}
	got := capOrdersPerSide(orders, 1)
	if len(got) != 2 {
		t.Fatalf("expected one order per side, got %+v", got)
	}
	if got[0].Side != "buy" || got[1].Side != "sell" {
		t.Fatalf("both sides must survive, got %+v", got)
	}
	if got[0].Price != 99 || got[1].Price != 101 {
		t.Fatalf("inner levels must be kept, got %+v", got)
	}
	if len(capOrdersPerSide(orders, 0)) != len(orders) {
		t.Fatal("zero cap should be treated as disabled")
	}
}
