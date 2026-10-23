package execution

import (
	"context"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
	"testing"
)

func TestZeusCapAtSubmissionAndPartialInventory(t *testing.T) {
	cfg := &config.Config{ZeusMode: true, Risk: config.RiskConfig{MaxPositionNotional: 40}}
	le := &LiveExecutor{cfg: cfg, positions: map[string]*livePosition{}, szDecimals: map[string]int{"xyz:SP500": 3}, liveOrders: map[string][]trackedOrder{}}
	req := types.OrderRequest{Coin: "xyz:SP500", Side: "buy", Price: 100.1, Size: 1}
	capped, err := le.capZeusOrder(req, 0)
	if err != nil || capped.Size*capped.Price > 40 {
		t.Fatalf("%v %v", capped, err)
	}
	le.positions[req.Coin] = &livePosition{side: "long", size: 0.2}
	capped, err = le.capZeusOrder(req, 0)
	if err != nil || (capped.Size+0.2)*req.Price > 40 {
		t.Fatalf("%v %v", capped, err)
	}
	req.Side = "sell"
	capped, err = le.capZeusOrder(req, 0)
	if err != nil || capped.Size != 0.2 || !capped.ReduceOnly {
		t.Fatalf("exit not bounded/reduce-only: %v %v", capped, err)
	}
	le.liveOrders[req.Coin] = []trackedOrder{{OID: 1, Side: "buy", Size: 0.19}}
	req.Side = "buy"
	if _, err = le.capZeusOrder(req, 0); err == nil {
		t.Fatal("pending exposure not reserved")
	}
	if _, err = le.capZeusOrder(req, 1); err != nil {
		t.Fatal("replacement double-counted old order", err)
	}
}

func TestZeusAmbiguousPlacementIsNeverBlindlyRetried(t *testing.T) {
	le := NewLiveExecutor(nil, nil, []string{"xyz:SP500"}, 10)
	le.cfg = &config.Config{ZeusMode: true}
	calls := 0
	result, err := le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) {
		calls++
		return nil, hyperliquid.NewError(hyperliquid.ErrorCodeTimeout, "uncertain")
	})
	if err == nil || calls != 1 {
		t.Fatalf("ambiguous placement retried: %d %v", calls, err)
	}
	le.zeusSubmissionResult("xyz:SP500", result, err)
	if !le.HealthSnapshot().Coins["xyz:SP500"].QuoteHalt {
		t.Fatal("uncertain state did not halt new quotes")
	}
}
func TestZeusTrackingUsesActuallySubmittedSize(t *testing.T) {
	order := types.OrderRequest{Side: "buy", Size: 10, Price: 100}
	tracked := trackedFromResult(placeOrderResult{oid: 1, size: 0.2}, order)
	if tracked.Size != 0.2 {
		t.Fatal("tracking retained pre-fill-race requested quantity")
	}
}
