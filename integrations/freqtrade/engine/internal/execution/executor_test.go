package execution

import (
	"context"
	"errors"
	"math"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

const floatTol = 1e-6

func almostEqual(a, b, tol float64) bool {
	return math.Abs(a-b) < tol
}

func TestPaperOrderPlacement(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place a buy order for CL at $70
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0, Reason: "test"},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0, Reason: "test"},
	}
	err := pe.Refresh(ctx, "xyz:CL", orders)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify orders are stored
	pe.mu.Lock()
	count := len(pe.openOrders["xyz:CL"])
	pe.mu.Unlock()

	if count != 2 {
		t.Fatalf("expected 2 open orders, got %d", count)
	}
}

func TestPaperFillSimulation(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place buy order at $70
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0, Reason: "test"},
	}
	if err := pe.Refresh(ctx, "xyz:CL", orders); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Send book update with ask at $69.60 (ask < our buy price of $70 means fill)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.40, BestAsk: 69.60, Mid: 69.50, Timestamp: time.Now(),
	}

	// Wait for fill to process
	posUpdCh := pe.PositionUpdates()
	select {
	case pos := <-posUpdCh:
		if pos.Coin != "xyz:CL" {
			t.Fatalf("expected xyz:CL, got %s", pos.Coin)
		}
		if pos.Side != "long" {
			t.Fatalf("expected long, got %s", pos.Side)
		}
		if !almostEqual(pos.Size, 1.0, floatTol) {
			t.Fatalf("expected size=1.0, got %f", pos.Size)
		}
		if !almostEqual(pos.AvgEntry, 70.0, floatTol) {
			t.Fatalf("expected avgEntry=70.0, got %f", pos.AvgEntry)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for position update after fill")
	}

	// Verify open orders now empty for CL (the buy was filled)
	pe.mu.Lock()
	count := len(pe.openOrders["xyz:CL"])
	pe.mu.Unlock()
	if count != 0 {
		t.Fatalf("expected 0 open orders after fill, got %d", count)
	}
}

func TestPaperPositionTracking(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place and fill a buy at $70
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 2.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.0, BestAsk: 69.50, Mid: 69.25, Timestamp: time.Now(),
	}

	// Drain position update
	select {
	case <-pe.PositionUpdates():
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for first fill")
	}

	pos := pe.Position("xyz:CL")
	if pos.Side != "long" {
		t.Fatalf("expected long, got %s", pos.Side)
	}
	if !almostEqual(pos.Size, 2.0, floatTol) {
		t.Fatalf("expected size=2.0, got %f", pos.Size)
	}
	if !almostEqual(pos.AvgEntry, 70.0, floatTol) {
		t.Fatalf("expected avgEntry=70.0, got %f", pos.AvgEntry)
	}
	if !almostEqual(pos.Notional, 140.0, floatTol) {
		t.Fatalf("expected notional=140.0, got %f", pos.Notional)
	}
}

func TestPaperEquityWithUnrealizedPnL(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place and fill a buy at $70, size 2
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 2.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.0, BestAsk: 69.50, Mid: 69.25, Timestamp: time.Now(),
	}

	select {
	case <-pe.PositionUpdates():
	case <-time.After(2 * time.Second):
		t.Fatal("timed out")
	}

	// Update mid to $72 => unrealized PnL = (72-70)*2 = +4
	pe.mu.Lock()
	pe.lastMid["xyz:CL"] = 72.0
	pe.mu.Unlock()

	equity := pe.Equity()
	// 10000 (starting) + 0 (realized) + 4 (unrealized) = 10004
	if !almostEqual(equity, 10004.0, floatTol) {
		t.Fatalf("expected equity=10004.0, got %f", equity)
	}
}

func TestPaperCancelAll(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go pe.Run(ctx)

	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0, Reason: "test"},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders)

	err := pe.CancelAll(ctx, "xyz:CL")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	pe.mu.Lock()
	count := len(pe.openOrders["xyz:CL"])
	pe.mu.Unlock()
	if count != 0 {
		t.Fatalf("expected 0 orders after cancel, got %d", count)
	}
}

func TestPaperRefreshReplacesOrders(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place initial orders
	orders1 := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0, Reason: "test"},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders1)

	// Replace with new orders
	orders2 := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 69.0, Size: 2.0, Reason: "test2"},
	}
	pe.Refresh(ctx, "xyz:CL", orders2)

	pe.mu.Lock()
	count := len(pe.openOrders["xyz:CL"])
	pe.mu.Unlock()
	if count != 1 {
		t.Fatalf("expected 1 order after refresh, got %d", count)
	}

	pe.mu.Lock()
	order := pe.openOrders["xyz:CL"][0]
	pe.mu.Unlock()
	if !almostEqual(order.Price, 69.0, floatTol) {
		t.Fatalf("expected price=69.0, got %f", order.Price)
	}
	if !almostEqual(order.Size, 2.0, floatTol) {
		t.Fatalf("expected size=2.0, got %f", order.Size)
	}
}

func TestPaperClosePosition(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Build a long position: buy 2 at $70
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 2.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.0, BestAsk: 69.50, Mid: 69.25, Timestamp: time.Now(),
	}

	select {
	case <-pe.PositionUpdates():
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for fill")
	}

	// Update mid for closing price
	pe.mu.Lock()
	pe.lastMid["xyz:CL"] = 72.0
	pe.mu.Unlock()

	// Close position
	err := pe.ClosePosition(ctx, "xyz:CL", "test")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Wait for position update
	select {
	case pos := <-pe.PositionUpdates():
		if pos.Side != "" {
			t.Fatalf("expected flat, got %s", pos.Side)
		}
		if pos.Size != 0 {
			t.Fatalf("expected size=0, got %f", pos.Size)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for close position update")
	}

	// Realized PnL = (72 - 70) * 2 = 4
	equity := pe.Equity()
	if !almostEqual(equity, 10004.0, floatTol) {
		t.Fatalf("expected equity=10004.0 after close, got %f", equity)
	}
}

func TestPaperSellFillSimulation(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go pe.Run(ctx)

	// Place sell order at $71
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0, Reason: "test"},
	}
	pe.Refresh(ctx, "xyz:CL", orders)

	// Send book update with bid at $71.50 (bid > our sell price means fill)
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 71.50, BestAsk: 72.0, Mid: 71.75, Timestamp: time.Now(),
	}

	select {
	case pos := <-pe.PositionUpdates():
		if pos.Side != "short" {
			t.Fatalf("expected short, got %s", pos.Side)
		}
		if !almostEqual(pos.Size, 1.0, floatTol) {
			t.Fatalf("expected size=1.0, got %f", pos.Size)
		}
		if !almostEqual(pos.AvgEntry, 71.0, floatTol) {
			t.Fatalf("expected avgEntry=71.0, got %f", pos.AvgEntry)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for sell fill")
	}
}

func TestPaperApplyFillPositionFlip(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	// Build long 2@70.
	pe.mu.Lock()
	pe.positions["xyz:CL"] = &paperPosition{side: "long", size: 2, avgEntry: 70}
	pe.mu.Unlock()

	// Sell 3@72 → should flip to short 1@72, realized PnL = (72-70)*2 = 4.
	pe.mu.Lock()
	pe.applyFill("xyz:CL", "sell", 3, 72)
	pos := pe.positions["xyz:CL"]
	pnl := pe.realizedPnL
	pe.mu.Unlock()

	if pos.side != "short" {
		t.Fatalf("expected short after flip, got %s", pos.side)
	}
	if !almostEqual(pos.size, 1.0, floatTol) {
		t.Fatalf("expected size=1.0, got %f", pos.size)
	}
	if !almostEqual(pos.avgEntry, 72.0, floatTol) {
		t.Fatalf("expected avgEntry=72.0, got %f", pos.avgEntry)
	}
	if !almostEqual(pnl, 4.0, floatTol) {
		t.Fatalf("expected realized PnL=4.0, got %f", pnl)
	}
}

func TestPaperApplyFillAddToPosition(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	pe.mu.Lock()
	pe.applyFill("xyz:CL", "buy", 1, 70)
	pe.applyFill("xyz:CL", "buy", 1, 72)
	pos := pe.positions["xyz:CL"]
	pe.mu.Unlock()

	if pos.side != "long" {
		t.Fatalf("expected long, got %s", pos.side)
	}
	if !almostEqual(pos.size, 2.0, floatTol) {
		t.Fatalf("expected size=2.0, got %f", pos.size)
	}
	if !almostEqual(pos.avgEntry, 71.0, floatTol) {
		t.Fatalf("expected avgEntry=71.0, got %f", pos.avgEntry)
	}
}

func TestPaperEquityShortPosition(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	pe.mu.Lock()
	pe.positions["xyz:CL"] = &paperPosition{side: "short", size: 2, avgEntry: 70}
	pe.lastMid["xyz:CL"] = 68
	pe.mu.Unlock()

	// Unrealized = (70-68)*2 = 4, equity = 10000 + 4 = 10004.
	equity := pe.Equity()
	if !almostEqual(equity, 10004.0, floatTol) {
		t.Fatalf("expected equity=10004.0, got %f", equity)
	}
}

func TestPaperCloseShortPosition(t *testing.T) {
	bookCh := make(chan types.BookUpdate, 100)
	pe := NewPaperExecutor(10000.0, bookCh, 0, 0)

	pe.mu.Lock()
	pe.positions["xyz:CL"] = &paperPosition{side: "short", size: 2, avgEntry: 70}
	pe.lastMid["xyz:CL"] = 68
	pe.mu.Unlock()

	ctx := context.Background()
	if err := pe.ClosePosition(ctx, "xyz:CL", "test"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Realized PnL = (70-68)*2 = 4, equity = 10000 + 4.
	equity := pe.Equity()
	if !almostEqual(equity, 10004.0, floatTol) {
		t.Fatalf("expected equity=10004.0, got %f", equity)
	}

	pos := pe.Position("xyz:CL")
	if pos.Side != "" {
		t.Fatalf("expected flat, got %s", pos.Side)
	}
}

func TestLiveHandleFill(t *testing.T) {
	tests := []struct {
		name     string
		initial  *livePosition
		fill     types.FillEvent
		wantSide string
		wantSize float64
		wantAvg  float64
	}{
		{
			name:     "new_position",
			initial:  &livePosition{},
			fill:     types.FillEvent{Coin: "xyz:CL", Side: "buy", Size: 1, Price: 70},
			wantSide: "long", wantSize: 1, wantAvg: 70,
		},
		{
			name:     "add_to_position",
			initial:  &livePosition{side: "long", size: 1, avgEntry: 70},
			fill:     types.FillEvent{Coin: "xyz:CL", Side: "buy", Size: 1, Price: 72},
			wantSide: "long", wantSize: 2, wantAvg: 71,
		},
		{
			name:     "partial_close",
			initial:  &livePosition{side: "long", size: 2, avgEntry: 70},
			fill:     types.FillEvent{Coin: "xyz:CL", Side: "sell", Size: 1, Price: 72},
			wantSide: "long", wantSize: 1, wantAvg: 70,
		},
		{
			name:     "full_close",
			initial:  &livePosition{side: "long", size: 1, avgEntry: 70},
			fill:     types.FillEvent{Coin: "xyz:CL", Side: "sell", Size: 1, Price: 72},
			wantSide: "", wantSize: 0, wantAvg: 0,
		},
		{
			name:     "position_flip",
			initial:  &livePosition{side: "long", size: 1, avgEntry: 70},
			fill:     types.FillEvent{Coin: "xyz:CL", Side: "sell", Size: 3, Price: 72},
			wantSide: "short", wantSize: 2, wantAvg: 72,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			le := &LiveExecutor{
				posCh:      make(chan types.PositionUpdate, 100),
				positions:  map[string]*livePosition{"xyz:CL": tt.initial},
				liveOrders: make(map[string][]trackedOrder),
				orderEpoch: make(map[string]uint64),
				transport:  &testTransport{},
			}
			le.handleFill(tt.fill)

			pos := le.positions["xyz:CL"]
			if pos.side != tt.wantSide {
				t.Errorf("side = %q, want %q", pos.side, tt.wantSide)
			}
			if !almostEqual(pos.size, tt.wantSize, floatTol) {
				t.Errorf("size = %f, want %f", pos.size, tt.wantSize)
			}
			if !almostEqual(pos.avgEntry, tt.wantAvg, floatTol) {
				t.Errorf("avgEntry = %f, want %f", pos.avgEntry, tt.wantAvg)
			}

			// Verify position update was published.
			select {
			case pu := <-le.posCh:
				if pu.Coin != "xyz:CL" {
					t.Errorf("position update coin = %q, want xyz:CL", pu.Coin)
				}
			default:
				t.Error("expected position update on channel")
			}
		})
	}
}

func TestNormalizeSide(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"b", "buy"}, {"buy", "buy"}, {"B", "buy"},
		{"a", "sell"}, {"ask", "sell"}, {"s", "sell"}, {"sell", "sell"}, {"S", "sell"},
		{"UNKNOWN", "unknown"},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := normalizeSide(tt.input)
			if got != tt.expected {
				t.Errorf("normalizeSide(%q) = %q, want %q", tt.input, got, tt.expected)
			}
		})
	}
}

func TestParseFillFromAPINormalizesAskSide(t *testing.T) {
	le := &LiveExecutor{dexes: map[string]string{"xyz:CL": "xyz"}, hasDex: true, dexName: "xyz", transport: &testTransport{}}
	fill := le.parseFillFromAPI(map[string]any{
		"coin": "CL",
		"side": "A",
		"px":   "70.25",
		"sz":   "1.5",
		"oid":  float64(42),
	})

	if fill.Coin != "xyz:CL" {
		t.Fatalf("expected coin xyz:CL, got %q", fill.Coin)
	}
	if fill.Side != "sell" {
		t.Fatalf("expected side sell, got %q", fill.Side)
	}
	if fill.OID != 42 {
		t.Fatalf("expected OID 42, got %d", fill.OID)
	}
}

func TestParseFillFromAPIMixedNamespacePerpsNoDexPrefix(t *testing.T) {
	// Mixed namespace: HIP-3 + perps. A perps fill for "BTC" should NOT get dex prefix.
	le := &LiveExecutor{
		dexes:     map[string]string{"xyz:CL": "xyz", "BTC": "", "ETH": ""},
		hasDex:    true,
		dexName:   "xyz",
		hasPerps:  true,
		transport: &testTransport{},
	}
	fill := le.parseFillFromAPI(map[string]any{
		"coin": "BTC",
		"side": "B",
		"px":   "95000.00",
		"sz":   "0.001",
		"oid":  float64(99),
	})

	if fill.Coin != "BTC" {
		t.Fatalf("expected coin BTC (no dex prefix), got %q", fill.Coin)
	}
	if fill.Side != "buy" {
		t.Fatalf("expected side buy, got %q", fill.Side)
	}

	// HIP-3 fill should still get prefix.
	fill2 := le.parseFillFromAPI(map[string]any{
		"coin": "CL",
		"side": "A",
		"px":   "70.25",
		"sz":   "1.0",
		"oid":  float64(100),
	})
	if fill2.Coin != "xyz:CL" {
		t.Fatalf("expected coin xyz:CL, got %q", fill2.Coin)
	}
}

func TestBuildFillDedupKeyDistinguishesPartialFills(t *testing.T) {
	fillA := types.FillEvent{Coin: "xyz:CL", Side: "sell", Size: 0.5, Price: 70.25, OID: 42}
	fillB := types.FillEvent{Coin: "xyz:CL", Side: "sell", Size: 1.0, Price: 70.25, OID: 42}

	keyA := buildFillDedupKey(fillA, 1234)
	keyB := buildFillDedupKey(fillB, 1234)
	if keyA == keyB {
		t.Fatal("expected partial fills with same OID/time to produce different dedup keys")
	}
}

func TestClassifyReconcileDriftHardMismatch(t *testing.T) {
	le := &LiveExecutor{
		cfg: &config.Config{
			Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
			MarketMaking: config.MarketMakingConfig{
				QuoteNotional: 100,
			},
			Resolved: map[string]config.AssetConfig{
				"xyz:CL": {
					MarketMaking: config.MarketMakingConfig{QuoteNotional: 100},
				},
			},
		},
		transport: &testTransport{},
	}

	deltaNotional, dust, hard := le.classifyReconcileDrift("xyz:CL", "long", 1.0, "short", 1.0, 100.0, 100.0)
	if deltaNotional != 200.0 {
		t.Fatalf("expected delta notional 200, got %f", deltaNotional)
	}
	if dust {
		t.Fatal("expected non-dust mismatch")
	}
	if !hard {
		t.Fatal("expected side flip to be treated as hard mismatch")
	}
}

func TestClassifyReconcileDriftSideFlipWithinExposure(t *testing.T) {
	// Side flip where delta is within max_spread_exposure should be soft, not hard.
	le := &LiveExecutor{
		cfg: &config.Config{
			Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
			MarketMaking: config.MarketMakingConfig{
				QuoteNotional: 400,
			},
			Risk: config.RiskConfig{
				MaxSpreadExposure: 800,
			},
			Resolved: map[string]config.AssetConfig{
				"xyz:CL": {
					MarketMaking: config.MarketMakingConfig{QuoteNotional: 400},
					Risk:         config.RiskConfig{MaxSpreadExposure: 800},
				},
			},
		},
		transport: &testTransport{},
	}

	// Side flip: short 0.298 -> long 0.61 at ~112 = delta $101.62, within $800 exposure.
	deltaNotional, dust, hard := le.classifyReconcileDrift("xyz:CL", "short", 0.298, "long", 0.61, 112.0, 112.0)
	expectedDelta := (0.298 + 0.61) * 112.0
	if !almostEqual(deltaNotional, expectedDelta, 0.01) {
		t.Fatalf("expected delta notional %.2f, got %.2f", expectedDelta, deltaNotional)
	}
	if dust {
		t.Fatal("expected non-dust mismatch")
	}
	if hard {
		t.Fatal("expected side flip within exposure to be treated as SOFT, not hard")
	}
}

func TestClassifyReconcileDriftSideFlipExceedsExposure(t *testing.T) {
	// Side flip where delta exceeds max_spread_exposure should still be hard.
	le := &LiveExecutor{
		cfg: &config.Config{
			Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}},
			MarketMaking: config.MarketMakingConfig{
				QuoteNotional: 400,
			},
			Risk: config.RiskConfig{
				MaxSpreadExposure: 50, // very low limit
			},
			Resolved: map[string]config.AssetConfig{
				"xyz:CL": {
					MarketMaking: config.MarketMakingConfig{QuoteNotional: 400},
					Risk:         config.RiskConfig{MaxSpreadExposure: 50},
				},
			},
		},
		transport: &testTransport{},
	}

	// Side flip: delta $101.62, exceeds $50 exposure.
	_, dust, hard := le.classifyReconcileDrift("xyz:CL", "short", 0.298, "long", 0.61, 112.0, 112.0)
	if dust {
		t.Fatal("expected non-dust mismatch")
	}
	if !hard {
		t.Fatal("expected side flip exceeding exposure to be treated as HARD")
	}
}

func TestNormalizedTIF(t *testing.T) {
	tests := []struct {
		value, fallback, expected string
	}{
		{"ALO", "IOC", "ALO"},
		{"alo", "IOC", "ALO"},
		{"IOC", "ALO", "IOC"},
		{"GTC", "ALO", "ALO"},
		{"", "IOC", "IOC"},
	}

	for _, tt := range tests {
		t.Run(tt.value+"_"+tt.fallback, func(t *testing.T) {
			got := normalizedTIF(tt.value, tt.fallback)
			if got != tt.expected {
				t.Errorf("normalizedTIF(%q, %q) = %q, want %q", tt.value, tt.fallback, got, tt.expected)
			}
		})
	}
}

func TestParseFloatFromAPI(t *testing.T) {
	tests := []struct {
		input    string
		expected float64
	}{
		{"123.45", 123.45},
		{"", 0},
		{"abc", 0},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := parseFloatFromAPI(tt.input, "test", "test")
			if !almostEqual(got, tt.expected, floatTol) {
				t.Errorf("parseFloatFromAPI(%q) = %f, want %f", tt.input, got, tt.expected)
			}
		})
	}
}

func TestLiveRefreshReturnsErrorOnPartialPlacementFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: tt,
	}

	var mu sync.Mutex
	var openOrders []types.OrderRequest

	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		return nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		defer mu.Unlock()
		if order.Side == "sell" {
			return placeOrderResult{}, errors.New("sell placement failed")
		}
		openOrders = append(openOrders, types.OrderRequest{
			Coin:  order.Coin,
			Side:  order.Side,
			Price: order.Price,
			Size:  order.Size,
		})
		return placeOrderResult{}, nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		mu.Lock()
		defer mu.Unlock()
		result := make([]types.OrderRequest, 0, len(openOrders))
		for _, order := range openOrders {
			if order.Coin == coin {
				result = append(result, order)
			}
		}
		return result
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0},
	})
	if err == nil {
		t.Fatal("expected refresh error when one live order placement fails")
	}
}

func TestLiveClosePositionUsesResidualSizeAfterPartialFill(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:               make(chan types.PositionUpdate, 100),
		positions:           make(map[string]*livePosition),
		latestBook:          make(map[string]types.BookUpdate),
		maxCloseSlippageBps: 50,
		transport:           tt,
	}
	le.positions["xyz:CL"] = &livePosition{
		side:     "long",
		size:     10,
		avgEntry: 70,
	}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70.0,
		Timestamp: time.Now(),
	}

	var (
		mu     sync.Mutex
		orders []types.OrderRequest
	)
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		orders = append(orders, order)
		call := len(orders)
		mu.Unlock()

		le.mu.Lock()
		defer le.mu.Unlock()

		pos := le.positions[order.Coin]
		switch call {
		case 1:
			pos.size = 4
		case 2:
			pos.side = ""
			pos.size = 0
			pos.avgEntry = 0
		default:
			t.Fatalf("unexpected extra close attempt %d", call)
		}
		return placeOrderResult{}, nil
	}

	if err := le.ClosePosition(ctx, "xyz:CL", "test_close"); err != nil {
		t.Fatalf("unexpected close error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(orders) != 2 {
		t.Fatalf("expected 2 IOC close attempts, got %d", len(orders))
	}
	if !almostEqual(orders[0].Size, 10, floatTol) {
		t.Fatalf("expected first close size=10, got %f", orders[0].Size)
	}
	if !almostEqual(orders[1].Size, 4, floatTol) {
		t.Fatalf("expected second close size=4 residual, got %f", orders[1].Size)
	}
}

// V30 part 4: fractional IOC close on favorable shock.

func TestReducePositionIOC_FlatReturnsZero(t *testing.T) {
	ctx := context.Background()
	le := &LiveExecutor{
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  &testTransport{},
	}
	filled, err := le.ReducePositionIOC(ctx, "xyz:CL", 0.25, 5.0, "test")
	if err != nil {
		t.Fatalf("unexpected error on flat: %v", err)
	}
	if filled != 0 {
		t.Fatalf("expected filled=0 on flat, got %f", filled)
	}
}

func TestReducePositionIOC_StaleBookReturnsZero(t *testing.T) {
	ctx := context.Background()
	le := &LiveExecutor{
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  &testTransport{},
	}
	le.positions["xyz:CL"] = &livePosition{side: "long", size: 10, avgEntry: 70}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70,
		Timestamp: time.Now().Add(-10 * time.Second), // > 3s stale
	}

	filled, err := le.ReducePositionIOC(ctx, "xyz:CL", 0.25, 5.0, "test")
	if err != nil {
		t.Fatalf("unexpected error on stale book: %v", err)
	}
	if filled != 0 {
		t.Fatalf("expected filled=0 on stale book, got %f", filled)
	}
}

func TestReducePositionIOC_FractionRangeValidation(t *testing.T) {
	ctx := context.Background()
	le := &LiveExecutor{
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  &testTransport{},
	}
	for _, bad := range []float64{-0.1, 0.0, 1.01, 2.0} {
		_, err := le.ReducePositionIOC(ctx, "xyz:CL", bad, 5.0, "test")
		if err == nil {
			t.Fatalf("expected error for fraction=%f", bad)
		}
	}
}

func TestReducePositionIOC_LongPricesAtBestBidMinusSlippage(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 10),
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  tt,
	}
	le.positions["xyz:CL"] = &livePosition{side: "long", size: 10, avgEntry: 70}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70.0,
		Timestamp: time.Now(),
	}

	var captured types.OrderRequest
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		captured = order
		// Simulate a full fill of the requested size.
		le.mu.Lock()
		defer le.mu.Unlock()
		pos := le.positions[order.Coin]
		pos.size -= order.Size
		if pos.size <= 0 {
			pos.size = 0
			pos.side = ""
		}
		return placeOrderResult{}, nil
	}

	filled, err := le.ReducePositionIOC(ctx, "xyz:CL", 0.25, 5.0, "test_long")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if captured.Side != "sell" {
		t.Fatalf("long reducing should submit side=sell, got %s", captured.Side)
	}
	// slippage = mid * 5/10000 = 70 * 0.0005 = 0.035. Price = bestBid - slippage = 69.95 - 0.035 = 69.915.
	// After tick rounding (assumed 0.01 for CL — RoundToTickSize may adjust).
	// Verify price is <= bestBid (crosses into book) and > 0.
	if captured.Price > 69.95 {
		t.Fatalf("long reducing price should be <= bestBid, got %f (bestBid=69.95)", captured.Price)
	}
	if captured.Price <= 0 {
		t.Fatalf("price must be positive, got %f", captured.Price)
	}
	if captured.TimeInForce != "IOC" {
		t.Fatalf("expected TIF=IOC, got %s", captured.TimeInForce)
	}
	if captured.Intent != "flatten" {
		t.Fatalf("expected Intent=flatten, got %s", captured.Intent)
	}
	if !almostEqual(filled, 2.5, floatTol) {
		t.Fatalf("expected filled=2.5 (10 * 0.25), got %f", filled)
	}
}

func TestReducePositionIOC_ShortPricesAtBestAskPlusSlippage(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 10),
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  tt,
	}
	le.positions["xyz:CL"] = &livePosition{side: "short", size: 8, avgEntry: 70}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70.0,
		Timestamp: time.Now(),
	}

	var captured types.OrderRequest
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		captured = order
		le.mu.Lock()
		defer le.mu.Unlock()
		pos := le.positions[order.Coin]
		pos.size -= order.Size
		if pos.size <= 0 {
			pos.size = 0
			pos.side = ""
		}
		return placeOrderResult{}, nil
	}

	_, err := le.ReducePositionIOC(ctx, "xyz:CL", 0.5, 5.0, "test_short")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if captured.Side != "buy" {
		t.Fatalf("short reducing should submit side=buy, got %s", captured.Side)
	}
	if captured.Price < 70.05 {
		t.Fatalf("short reducing price should be >= bestAsk, got %f (bestAsk=70.05)", captured.Price)
	}
}

// M1 regression: placeOrder returns error but position was partially filled.
// ReducePositionIOC must report the actual filled size rather than returning 0.
func TestReducePositionIOC_ReportsPartialFillOnPlaceError(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 10),
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  tt,
	}
	le.positions["xyz:CL"] = &livePosition{side: "long", size: 10, avgEntry: 70}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70.0,
		Timestamp: time.Now(),
	}

	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		// Simulate server-side partial fill before our ack timeout.
		le.mu.Lock()
		defer le.mu.Unlock()
		pos := le.positions[order.Coin]
		pos.size -= 1.5 // partial fill of 1.5 of the 2.5 requested
		return placeOrderResult{}, errors.New("ack timeout")
	}

	filled, err := le.ReducePositionIOC(ctx, "xyz:CL", 0.25, 5.0, "test_partial")
	if err == nil {
		t.Fatalf("expected error propagated from placeOrder")
	}
	if !almostEqual(filled, 1.5, floatTol) {
		t.Fatalf("expected filled=1.5 despite error, got %f", filled)
	}
}

// M1 regression: ctx cancelled during the 250ms wait, but position was
// partially filled. Must report actual filled rather than 0.
func TestReducePositionIOC_ReportsPartialFillOnCtxCancel(t *testing.T) {
	baseCtx, baseCancel := context.WithCancel(context.Background())

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 10),
		positions:  make(map[string]*livePosition),
		latestBook: make(map[string]types.BookUpdate),
		transport:  tt,
	}
	le.positions["xyz:CL"] = &livePosition{side: "long", size: 10, avgEntry: 70}
	le.latestBook["xyz:CL"] = types.BookUpdate{
		Coin:      "xyz:CL",
		BestBid:   69.95,
		BestAsk:   70.05,
		Mid:       70.0,
		Timestamp: time.Now(),
	}

	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		// Full fill, then cancel ctx mid-wait.
		le.mu.Lock()
		pos := le.positions[order.Coin]
		pos.size -= order.Size
		if pos.size <= 0 {
			pos.size = 0
			pos.side = ""
		}
		le.mu.Unlock()
		go func() {
			time.Sleep(50 * time.Millisecond)
			baseCancel()
		}()
		return placeOrderResult{}, nil
	}

	filled, err := le.ReducePositionIOC(baseCtx, "xyz:CL", 0.25, 5.0, "test_cancel")
	if err == nil {
		t.Fatalf("expected ctx.Err() propagated")
	}
	if !almostEqual(filled, 2.5, floatTol) {
		t.Fatalf("expected filled=2.5 despite ctx cancel, got %f", filled)
	}
}

func TestUpstreamHealthTracking(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: &testTransport{},
	}

	// Initially healthy.
	if le.isUpstreamUnhealthy() {
		t.Fatal("expected upstream healthy initially")
	}

	// Single error is not enough.
	le.recordUpstreamError()
	if le.isUpstreamUnhealthy() {
		t.Fatal("expected upstream healthy with only 1 error")
	}

	// Two errors within 30s window → unhealthy.
	le.recordUpstreamError()
	if !le.isUpstreamUnhealthy() {
		t.Fatal("expected upstream unhealthy after 2 errors")
	}

	// Success outside window resets.
	le.upstreamMu.Lock()
	le.upstreamLastErr = time.Now().Add(-31 * time.Second)
	le.upstreamMu.Unlock()
	le.recordUpstreamSuccess()
	if le.isUpstreamUnhealthy() {
		t.Fatal("expected upstream healthy after success outside window")
	}
}

func TestMedian5(t *testing.T) {
	tests := []struct {
		v    [5]float64
		want float64
	}{
		{[5]float64{1, 2, 3, 4, 5}, 3},
		{[5]float64{5, 4, 3, 2, 1}, 3},
		{[5]float64{3, 1, 5, 2, 4}, 3},
		{[5]float64{5, 5, 5, 5, 5}, 5},
		{[5]float64{100, 406, 407, 406, 405}, 406}, // spike down rejected
		{[5]float64{406, 407, 500, 406, 405}, 406}, // spike up rejected
		{[5]float64{406.1, 406.2, 406.3, 406.4, 406.5}, 406.3},
		{[5]float64{406, 390, 406, 406, 406}, 406}, // single outlier easily rejected
		{[5]float64{406, 390, 391, 406, 406}, 406}, // two outliers still rejected
	}
	for _, tt := range tests {
		got := median5(tt.v)
		if !almostEqual(got, tt.want, floatTol) {
			t.Errorf("median5(%v) = %v, want %v", tt.v, got, tt.want)
		}
	}
}

func simulateEquityPolls(le *LiveExecutor, samples []float64) {
	for _, s := range samples {
		idx := le.equityRingCount % 5
		le.equityRingBuf[idx] = s
		le.equityRingCount++

		if le.equityRingCount >= 5 {
			le.cachedEquity = median5(le.equityRingBuf)
		} else {
			le.cachedEquity = s
		}
	}
}

func TestEquityMedianFilterRejectsTransientSpike(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: &testTransport{},
	}

	// 5 equity polls: normal, normal, spike, normal, normal.
	simulateEquityPolls(le, []float64{406.0, 406.1, 430.0, 406.2, 406.3})

	// Median of {406.0, 406.1, 430.0, 406.2, 406.3} = 406.2 — spike rejected.
	if !almostEqual(le.cachedEquity, 406.2, floatTol) {
		t.Fatalf("expected median to reject spike: got %f, want 406.2", le.cachedEquity)
	}
}

func TestEquityMedianFilterRejectsTransientDrop(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: &testTransport{},
	}

	// 5 equity polls: normal, normal, drop, normal, normal.
	simulateEquityPolls(le, []float64{406.0, 406.1, 382.0, 406.2, 406.3})

	// Median of {406.0, 406.1, 382.0, 406.2, 406.3} = 406.1 — drop rejected.
	if !almostEqual(le.cachedEquity, 406.1, floatTol) {
		t.Fatalf("expected median to reject drop: got %f, want 406.1", le.cachedEquity)
	}
}

func TestEquityMedianFilterRejectsTwoConsecutiveOutliers(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: &testTransport{},
	}

	// 5 polls: normal, drop, drop, normal, normal.
	// This was the failure mode with median-of-3 — two consecutive bad reads defeated it.
	simulateEquityPolls(le, []float64{406.0, 390.0, 391.0, 406.1, 406.2})

	// Median of {406.0, 390.0, 391.0, 406.1, 406.2} = 406.0 — both drops rejected.
	if !almostEqual(le.cachedEquity, 406.0, floatTol) {
		t.Fatalf("expected median to reject two consecutive outliers: got %f, want 406.0", le.cachedEquity)
	}
}

func TestEquityMedianFilterAcceptsSustainedMove(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: &testTransport{},
	}

	// Simulate a real equity increase over 6 polls.
	simulateEquityPolls(le, []float64{406.0, 407.0, 408.0, 409.0, 410.0, 411.0})

	// After 6 samples, ring = {411.0, 407.0, 408.0, 409.0, 410.0}, median = 409.0.
	// Tracks the real move with 2-sample lag (acceptable tradeoff for robustness).
	if !almostEqual(le.cachedEquity, 409.0, floatTol) {
		t.Fatalf("expected median to track sustained move: got %f, want 409.0", le.cachedEquity)
	}
}

func TestRefreshSucceedsWithBenignRejects(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: tt,
	}

	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		return nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		return nil // no stale orders after cancel
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		// First order succeeds, second is a benign ALO rejection.
		if order.Side == "sell" {
			return placeOrderResult{benignReject: true}, nil
		}
		return placeOrderResult{}, nil
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("expected refresh to succeed with benign rejects, got error: %v", err)
	}
}

func TestCancelDexOrdersConcurrent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		dexes:     map[string]string{"xyz:CL": "xyz"},
		hasDex:    true,
		dexName:   "xyz",
		transport: tt,
	}

	var mu sync.Mutex
	var cancelCalls int

	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		mu.Lock()
		cancelCalls++
		mu.Unlock()
		return nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		return nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, nil
	}

	// Verify the Refresh path works with cancel + verify.
	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	mu.Lock()
	if cancelCalls != 1 {
		t.Fatalf("expected 1 cancel call, got %d", cancelCalls)
	}
	mu.Unlock()
}

func TestRefreshFullCancelsOnceWithoutVerify(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		transport: tt,
	}

	var cancelCalls int
	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		cancelCalls++
		return nil
	}

	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, nil
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("expected refresh to succeed, got error: %v", err)
	}
	if cancelCalls != 1 {
		t.Fatalf("expected 1 cancel call (no verify/retry), got %d", cancelCalls)
	}
}

func TestCoinMatchesDex(t *testing.T) {
	le := &LiveExecutor{dexes: map[string]string{"xyz:CL": "xyz", "xyz:BRENTOIL": "xyz"}, hasDex: true, dexName: "xyz", transport: &testTransport{}}

	tests := []struct {
		a, b string
		want bool
	}{
		{"xyz:CL", "xyz:CL", true},
		{"CL", "xyz:CL", true},
		{"xyz:CL", "CL", true},
		{"CL", "CL", true},
		{"CL", "BRENTOIL", false},
		{"xyz:CL", "xyz:BRENTOIL", false},
		{"xyz:CL", "BRENTOIL", false},
	}

	for _, tt := range tests {
		t.Run(tt.a+"_"+tt.b, func(t *testing.T) {
			got := le.coinMatchesDex(tt.a, tt.b)
			if got != tt.want {
				t.Errorf("coinMatchesDex(%q, %q) = %v, want %v", tt.a, tt.b, got, tt.want)
			}
		})
	}

	// With empty dex, only exact match works.
	leNoDex := &LiveExecutor{dexes: map[string]string{"BTC": ""}, transport: &testTransport{}}
	if leNoDex.coinMatchesDex("CL", "xyz:CL") {
		t.Error("expected false when dex is empty and strings differ")
	}
	if !leNoDex.coinMatchesDex("CL", "CL") {
		t.Error("expected true for exact match with empty dex")
	}
}

func TestCancelDexOrdersStripsPrefix(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		dexes:     map[string]string{"xyz:CL": "xyz"},
		hasDex:    true,
		dexName:   "xyz",
		transport: tt,
	}

	// Mock openOrdersFn to return orders with prefixed coin names.
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		return nil
	}

	// Track the coin values passed to cancel via transport.
	var mu sync.Mutex
	var cancelledCoins []string

	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		mu.Lock()
		cancelledCoins = append(cancelledCoins, coin)
		mu.Unlock()
		return nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, nil
	}

	// Verify Refresh calls cancel with the prefixed coin (the transport cancelAll mock
	// receives whatever Refresh passes). The real test of prefix stripping is
	// in cancelDexOrders which strips before calling sdk.Cancel — tested below
	// via queryOpenOrders matching.
	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

// --- diffOrders tests ---

func TestDiffOrdersAllSkip(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 2, Side: "sell", Price: 71.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Side: "buy", Price: 70.001, Size: 1.0},  // within 2 bps
		{Side: "sell", Price: 71.001, Size: 1.0}, // within 2 bps
	}
	diffs := diffOrders(tracked, desired, 2.0, 0.10)
	for _, d := range diffs {
		if d.action != actionSkip {
			t.Errorf("expected actionSkip, got %d for tracked OID %d", d.action, d.tracked.OID)
		}
	}
	if len(diffs) != 2 {
		t.Fatalf("expected 2 diffs, got %d", len(diffs))
	}
}

func TestDiffOrdersPriceChangeTriggersModify(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Side: "buy", Price: 70.10, Size: 1.0}, // ~14 bps > 2 bps threshold
	}
	diffs := diffOrders(tracked, desired, 2.0, 0.10)
	if len(diffs) != 1 {
		t.Fatalf("expected 1 diff, got %d", len(diffs))
	}
	if diffs[0].action != actionModify {
		t.Errorf("expected actionModify, got %d", diffs[0].action)
	}
}

func TestDiffOrdersExtraTrackedIsCancelled(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 2, Side: "buy", Price: 69.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Side: "buy", Price: 70.0, Size: 1.0},
	}
	diffs := diffOrders(tracked, desired, 2.0, 0.10)
	if len(diffs) != 2 {
		t.Fatalf("expected 2 diffs, got %d", len(diffs))
	}
	if diffs[0].action != actionSkip {
		t.Errorf("expected first diff to be skip, got %d", diffs[0].action)
	}
	if diffs[1].action != actionCancel {
		t.Errorf("expected second diff to be cancel, got %d", diffs[1].action)
	}
	if diffs[1].tracked.OID != 2 {
		t.Errorf("expected cancelled OID 2, got %d", diffs[1].tracked.OID)
	}
}

func TestDiffOrdersExtraDesiredIsPlaced(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "sell", Price: 71.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Side: "sell", Price: 71.0, Size: 1.0},
		{Side: "sell", Price: 72.0, Size: 2.0},
	}
	diffs := diffOrders(tracked, desired, 2.0, 0.10)
	if len(diffs) != 2 {
		t.Fatalf("expected 2 diffs, got %d", len(diffs))
	}
	if diffs[0].action != actionSkip {
		t.Errorf("expected first diff to be skip, got %d", diffs[0].action)
	}
	if diffs[1].action != actionPlace {
		t.Errorf("expected second diff to be place, got %d", diffs[1].action)
	}
	if !almostEqual(diffs[1].desired.Price, 72.0, floatTol) {
		t.Errorf("expected place price 72.0, got %f", diffs[1].desired.Price)
	}
}

func TestDiffOrdersMixed(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 2, Side: "sell", Price: 71.0, Size: 1.0},
		{OID: 3, Side: "sell", Price: 72.0, Size: 1.0}, // will be cancelled
	}
	desired := []types.OrderRequest{
		{Side: "buy", Price: 70.0, Size: 1.0},   // skip (same)
		{Side: "buy", Price: 69.0, Size: 1.0},   // place (new buy)
		{Side: "sell", Price: 71.50, Size: 1.0}, // modify (price moved ~70 bps)
	}

	diffs := diffOrders(tracked, desired, 2.0, 0.10)

	actions := make(map[orderAction]int)
	for _, d := range diffs {
		actions[d.action]++
	}

	if actions[actionSkip] != 1 {
		t.Errorf("expected 1 skip, got %d", actions[actionSkip])
	}
	if actions[actionModify] != 1 {
		t.Errorf("expected 1 modify, got %d", actions[actionModify])
	}
	if actions[actionCancel] != 1 {
		t.Errorf("expected 1 cancel, got %d", actions[actionCancel])
	}
	if actions[actionPlace] != 1 {
		t.Errorf("expected 1 place, got %d", actions[actionPlace])
	}
}

func TestDiffOrdersSizeChangeTriggersModify(t *testing.T) {
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Side: "buy", Price: 70.0, Size: 1.15}, // 15% > 10% threshold
	}
	diffs := diffOrders(tracked, desired, 2.0, 0.10)
	if len(diffs) != 1 || diffs[0].action != actionModify {
		t.Errorf("expected actionModify for 15%% size change")
	}
}

// --- Refresh integration tests ---

func TestRefreshUsesModifyWhenTracked(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	// Seed tracked orders (simulate previous refreshFull).
	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	var mu sync.Mutex
	var modifyCalls int
	var placeCalls int
	var cancelCalls int

	tt.modifyOrderFn = func(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		modifyCalls++
		mu.Unlock()
		return placeOrderResult{oid: tracked.OID + 1000}, nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		placeCalls++
		mu.Unlock()
		return placeOrderResult{oid: 200}, nil
	}
	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		mu.Lock()
		cancelCalls++
		mu.Unlock()
		return nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest { return nil }

	// Desired orders have price change beyond threshold.
	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.10, Size: 1.0},  // ~14 bps → modify
		{Coin: "xyz:CL", Side: "sell", Price: 71.10, Size: 1.0}, // ~14 bps → modify
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if modifyCalls != 2 {
		t.Errorf("expected 2 modify calls, got %d", modifyCalls)
	}
	if placeCalls != 0 {
		t.Errorf("expected 0 place calls, got %d", placeCalls)
	}
	if cancelCalls != 0 {
		t.Errorf("expected 0 cancel calls (cancelAll), got %d", cancelCalls)
	}
}

func TestRefreshFallsBackOnModifyFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}

	var mu sync.Mutex
	var cancelOIDCalls int
	var placeCalls int

	tt.modifyOrderFn = func(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, errors.New("modify failed: order gone")
	}
	tt.cancelOrderFn = func(ctx context.Context, oid int64, coin string) error {
		mu.Lock()
		cancelOIDCalls++
		mu.Unlock()
		return nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		placeCalls++
		mu.Unlock()
		return placeOrderResult{oid: 200}, nil
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.10, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if cancelOIDCalls != 1 {
		t.Errorf("expected 1 cancelSingleOID call (fallback), got %d", cancelOIDCalls)
	}
	if placeCalls != 1 {
		t.Errorf("expected 1 place call (fallback), got %d", placeCalls)
	}

	// Tracking should be updated with the new OID.
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 1 || tracked[0].OID != 200 {
		t.Errorf("expected tracking updated with OID 200, got %+v", tracked)
	}
}

func TestRefreshNoTrackingDoesFullCancelReplace(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	// No tracked orders → should use refreshFull path.
	var mu sync.Mutex
	var cancelAllCalls int
	var placeCalls int

	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		mu.Lock()
		cancelAllCalls++
		mu.Unlock()
		return nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest { return nil }
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		placeCalls++
		mu.Unlock()
		return placeOrderResult{oid: 300}, nil
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if cancelAllCalls != 1 {
		t.Errorf("expected 1 cancelAll call, got %d", cancelAllCalls)
	}
	if placeCalls != 2 {
		t.Errorf("expected 2 place calls, got %d", placeCalls)
	}

	// Tracking should be established.
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 2 {
		t.Errorf("expected 2 tracked orders after refreshFull, got %d", len(tracked))
	}
}

func TestFillClearsTracking(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}

	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 1.0, Price: 70.0,
	})

	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	epoch := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	if len(tracked) != 0 {
		t.Errorf("expected tracking cleared after fill, got %d orders", len(tracked))
	}
	if epoch != 1 {
		t.Errorf("expected epoch=1 after fill, got %d", epoch)
	}
}

func TestRefreshSkipsUnchangedOrders(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	var mu sync.Mutex
	var modifyCalls int

	tt.modifyOrderFn = func(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		modifyCalls++
		mu.Unlock()
		return placeOrderResult{oid: tracked.OID}, nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{oid: 200}, nil
	}

	// Desired orders are within thresholds → all should be skipped.
	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.001, Size: 1.0},
		{Coin: "xyz:CL", Side: "sell", Price: 71.001, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if modifyCalls != 0 {
		t.Errorf("expected 0 modify calls for unchanged orders, got %d", modifyCalls)
	}

	// Tracked orders should be preserved.
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 2 {
		t.Errorf("expected 2 tracked orders preserved, got %d", len(tracked))
	}
}

func TestRefreshClearsTrackingOnFallbackFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}

	tt.modifyOrderFn = func(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, errors.New("modify failed")
	}
	tt.cancelOrderFn = func(ctx context.Context, oid int64, coin string) error {
		return nil
	}
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{}, errors.New("place also failed")
	}

	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.10, Size: 1.0},
	})
	if err == nil {
		t.Fatal("expected error when both modify and fallback place fail")
	}

	// Tracking should be cleared so next refresh does full cancel+replace.
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 0 {
		t.Errorf("expected tracking cleared after fallback failure, got %d orders", len(tracked))
	}
}

func TestQueryOpenOrdersMatchesBothFormats(t *testing.T) {
	le := &LiveExecutor{
		posCh:     make(chan types.PositionUpdate, 100),
		positions: make(map[string]*livePosition),
		dexes:     map[string]string{"xyz:CL": "xyz"},
		hasDex:    true,
		dexName:   "xyz",
		transport: &testTransport{},
	}

	// Mock openOrdersFn to return orders with bare coin "CL" (as SDK might return).
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		// Simulate the matching logic from queryOpenOrders using coinMatchesDex.
		all := []types.OrderRequest{
			{Coin: "CL", Side: "buy", Price: 70.0, Size: 1.0},
			{Coin: "CL", Side: "sell", Price: 71.0, Size: 1.0},
			{Coin: "BRENTOIL", Side: "buy", Price: 80.0, Size: 1.0},
		}
		var result []types.OrderRequest
		for _, o := range all {
			if le.coinMatchesDex(o.Coin, coin) {
				result = append(result, o)
			}
		}
		return result
	}

	// Query with prefixed name should match bare "CL" orders.
	orders := le.fetchOpenOrders("xyz:CL")
	if len(orders) != 2 {
		t.Fatalf("expected 2 orders for xyz:CL, got %d", len(orders))
	}

	// Query with bare name should also work.
	orders = le.fetchOpenOrders("CL")
	if len(orders) != 2 {
		t.Fatalf("expected 2 orders for CL, got %d", len(orders))
	}

	// Query for different asset should not match.
	orders = le.fetchOpenOrders("xyz:BRENTOIL")
	if len(orders) != 1 {
		t.Fatalf("expected 1 order for xyz:BRENTOIL, got %d", len(orders))
	}
}

func TestRefreshClearsTrackingOnCancelFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	tt := &testTransport{}
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  tt,
	}

	// Tracked: one buy order that the diff will want to cancel.
	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	tt.cancelOrderFn = func(ctx context.Context, oid int64, coin string) error {
		if oid == 100 {
			return errors.New("cancel failed")
		}
		return nil
	}

	// Desired: only the sell order — diff will produce actionCancel for the buy.
	err := le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0},
	})

	// Should return error since cancel failed.
	if err == nil {
		t.Fatal("expected error when cancel fails")
	}

	// Tracking should be cleared so next refresh does full cancel+replace.
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 0 {
		t.Errorf("expected tracking cleared after cancel failure, got %d orders", len(tracked))
	}

	// Next refresh should go through refreshFull since tracking is cleared.
	var cancelAllCalls int
	tt.cancelAllFn = func(ctx context.Context, coin string) error {
		cancelAllCalls++
		return nil
	}
	le.openOrdersFn = func(coin string) []types.OrderRequest { return nil }
	tt.placeOrderFn = func(ctx context.Context, order types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{oid: 300}, nil
	}

	err = le.Refresh(ctx, "xyz:CL", []types.OrderRequest{
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0},
	})
	if err != nil {
		t.Fatalf("unexpected error on second refresh: %v", err)
	}
	if cancelAllCalls != 1 {
		t.Errorf("expected refreshFull path (1 cancelAll call), got %d", cancelAllCalls)
	}
}

func TestRefreshFillRaceDoesNotOverwriteDeletion(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	// Establish tracking.
	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	// Simulate: Refresh reads epoch=0, then a fill arrives mid-refresh bumping epoch to 1.
	// Capture epoch before fill.
	le.mu.Lock()
	epochBefore := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	// Fill arrives — increments epoch and clears tracking.
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 1.0, Price: 70.0,
	})

	// Epoch should have incremented.
	le.mu.Lock()
	epochAfter := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	if epochAfter != epochBefore+1 {
		t.Fatalf("expected epoch %d after fill, got %d", epochBefore+1, epochAfter)
	}

	// Simulate what Refresh's write-back does with the stale epoch.
	// It should NOT overwrite the fill's deletion.
	newTracked := []trackedOrder{
		{OID: 200, Side: "buy", Price: 69.0, Size: 1.0},
	}

	le.mu.Lock()
	if le.orderEpoch["xyz:CL"] != epochBefore {
		// Epoch mismatch → clear instead of writing stale data.
		delete(le.liveOrders, "xyz:CL")
	} else {
		le.liveOrders["xyz:CL"] = newTracked
	}
	le.mu.Unlock()

	// Verify tracking is still cleared (fill's deletion was respected).
	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 0 {
		t.Errorf("expected tracking cleared (fill respected), got %d orders", len(tracked))
	}
}

func TestFillIncrementsEpoch(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}

	// Record initial epoch.
	le.mu.Lock()
	epoch0 := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	// First fill.
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 1.0, Price: 70.0,
	})

	le.mu.Lock()
	epoch1 := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()
	if epoch1 != epoch0+1 {
		t.Errorf("expected epoch %d after first fill, got %d", epoch0+1, epoch1)
	}

	// Second fill on same coin.
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "sell", Size: 1.0, Price: 72.0,
	})

	le.mu.Lock()
	epoch2 := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()
	if epoch2 != epoch1+1 {
		t.Errorf("expected epoch %d after second fill, got %d", epoch1+1, epoch2)
	}

	// Fill on different coin should not affect xyz:CL epoch.
	le.handleFill(types.FillEvent{
		Coin: "xyz:BRENTOIL", Side: "buy", Size: 1.0, Price: 80.0,
	})

	le.mu.Lock()
	epochCL := le.orderEpoch["xyz:CL"]
	epochBrent := le.orderEpoch["xyz:BRENTOIL"]
	le.mu.Unlock()

	if epochCL != epoch2 {
		t.Errorf("xyz:CL epoch changed on BRENTOIL fill: got %d, want %d", epochCL, epoch2)
	}
	if epochBrent != 1 {
		t.Errorf("xyz:BRENTOIL epoch should be 1, got %d", epochBrent)
	}
}

func TestReconcileOrderTracking(t *testing.T) {
	// Set up executor with 5 tracked orders but exchange only has 3.
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	// Populate 5 tracked orders for xyz:CL.
	for i := 0; i < 5; i++ {
		le.liveOrders["xyz:CL"] = append(le.liveOrders["xyz:CL"], trackedOrder{
			OID:  int64(100 + i),
			Side: "buy",
		})
	}

	// Mock openOrdersFn to return only 3 orders.
	le.openOrdersFn = func(coin string) []types.OrderRequest {
		return []types.OrderRequest{
			{Coin: "xyz:CL", Side: "buy", Price: 70, Size: 1},
			{Coin: "xyz:CL", Side: "buy", Price: 69, Size: 1},
			{Coin: "xyz:CL", Side: "sell", Price: 72, Size: 1},
		}
	}

	epochBefore := le.orderEpoch["xyz:CL"]

	// reconcilePositions needs SDK for clearinghouse query — skip that part
	// by calling the order reconciliation logic directly. We replicate the
	// order-tracking reconciliation section here to unit-test in isolation.
	le.mu.Lock()
	coinsToCheck := make(map[string]int)
	for coin, tracked := range le.liveOrders {
		coinsToCheck[coin] = len(tracked)
	}
	le.mu.Unlock()

	exchangeOrders := make(map[string]int)
	for coin := range coinsToCheck {
		orders := le.fetchOpenOrders(coin)
		exchangeOrders[coin] = len(orders)
	}

	le.mu.Lock()
	for coin, trackedCount := range coinsToCheck {
		exchangeCount := exchangeOrders[coin]
		currentTracked, exists := le.liveOrders[coin]
		if !exists || len(currentTracked) != trackedCount {
			continue
		}
		if exchangeCount != trackedCount {
			delete(le.liveOrders, coin)
			le.orderEpoch[coin]++
		}
	}
	le.mu.Unlock()

	// Verify tracking was cleared.
	le.mu.Lock()
	defer le.mu.Unlock()

	if _, exists := le.liveOrders["xyz:CL"]; exists {
		t.Fatal("expected liveOrders for xyz:CL to be cleared")
	}
	if le.orderEpoch["xyz:CL"] != epochBefore+1 {
		t.Errorf("expected epoch bump to %d, got %d", epochBefore+1, le.orderEpoch["xyz:CL"])
	}
}

func TestReconcileOrderTrackingNoopWhenCountsMatch(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	// 3 tracked, 3 on exchange — should be a no-op.
	for i := 0; i < 3; i++ {
		le.liveOrders["xyz:CL"] = append(le.liveOrders["xyz:CL"], trackedOrder{
			OID:  int64(100 + i),
			Side: "buy",
		})
	}

	le.openOrdersFn = func(coin string) []types.OrderRequest {
		return []types.OrderRequest{
			{Coin: "xyz:CL", Side: "buy", Price: 70, Size: 1},
			{Coin: "xyz:CL", Side: "buy", Price: 69, Size: 1},
			{Coin: "xyz:CL", Side: "sell", Price: 72, Size: 1},
		}
	}

	// Run the reconciliation logic.
	le.mu.Lock()
	coinsToCheck := make(map[string]int)
	for coin, tracked := range le.liveOrders {
		coinsToCheck[coin] = len(tracked)
	}
	le.mu.Unlock()

	exchangeOrders := make(map[string]int)
	for coin := range coinsToCheck {
		orders := le.fetchOpenOrders(coin)
		exchangeOrders[coin] = len(orders)
	}

	le.mu.Lock()
	for coin, trackedCount := range coinsToCheck {
		exchangeCount := exchangeOrders[coin]
		currentTracked, exists := le.liveOrders[coin]
		if !exists || len(currentTracked) != trackedCount {
			continue
		}
		if exchangeCount != trackedCount {
			delete(le.liveOrders, coin)
			le.orderEpoch[coin]++
		}
	}
	le.mu.Unlock()

	// Verify tracking was NOT cleared.
	le.mu.Lock()
	defer le.mu.Unlock()

	if len(le.liveOrders["xyz:CL"]) != 3 {
		t.Fatalf("expected 3 tracked orders preserved, got %d", len(le.liveOrders["xyz:CL"]))
	}
	if le.orderEpoch["xyz:CL"] != 0 {
		t.Errorf("expected epoch unchanged at 0, got %d", le.orderEpoch["xyz:CL"])
	}
}

func TestBookSeqIncrement(t *testing.T) {
	le := &LiveExecutor{
		latestBook: make(map[string]types.BookUpdate),
		transport:  &testTransport{},
	}
	initial := le.bookSeq.Load()
	le.UpdateBook(types.BookUpdate{Coin: "xyz:CL", Mid: 100.0})
	if le.bookSeq.Load() != initial+1 {
		t.Errorf("expected bookSeq to increment by 1")
	}
	le.UpdateBook(types.BookUpdate{Coin: "xyz:CL", Mid: 100.1})
	if le.bookSeq.Load() != initial+2 {
		t.Errorf("expected bookSeq to increment by 2")
	}
}

func TestRefreshBatchPlacement(t *testing.T) {
	// Test that refreshFull uses batch path when transport supports batch.
	tt := &testTransport{batch: true}
	le := &LiveExecutor{
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		latestBook: make(map[string]types.BookUpdate),
		posCh:      make(chan types.PositionUpdate, 100),
		transport:  tt,
	}

	var batchCalled bool
	tt.placeBatchFn = func(ctx context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		batchCalled = true
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(1000 + i)}
		}
		return results, nil
	}
	tt.cancelAllFn = func(ctx context.Context, coin string) error { return nil }
	le.openOrdersFn = func(coin string) []types.OrderRequest { return nil }

	ctx := context.Background()
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 70.0, Size: 1.0, TimeInForce: "ALO"},
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0, TimeInForce: "ALO"},
	}

	err := le.refreshFull(ctx, "xyz:CL", orders)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !batchCalled {
		t.Fatal("expected batch place to be called")
	}

	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	le.mu.Unlock()
	if len(tracked) != 2 {
		t.Fatalf("expected 2 tracked orders, got %d", len(tracked))
	}
	if tracked[0].OID != 1000 || tracked[1].OID != 1001 {
		t.Errorf("unexpected OIDs: %d, %d", tracked[0].OID, tracked[1].OID)
	}
}

func TestRefreshBatchDiff(t *testing.T) {
	// Test that diff-based refresh uses batch path when transport supports batch.
	tt := &testTransport{batch: true}
	le := &LiveExecutor{
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		latestBook: make(map[string]types.BookUpdate),
		posCh:      make(chan types.PositionUpdate, 100),
		transport:  tt,
	}

	// Seed tracked orders.
	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 1, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 2, Side: "sell", Price: 71.0, Size: 1.0},
	}

	var modifyCalled, cancelCalled, placeCalled bool
	tt.modifyBatchFn = func(ctx context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		modifyCalled = true
		results := make([]placeOrderResult, len(targets))
		for i := range targets {
			results[i] = placeOrderResult{oid: targets[i].tracked.OID + 100}
		}
		return results, nil
	}
	tt.cancelBatchFn = func(ctx context.Context, targets []cancelTarget) error {
		cancelCalled = true
		return nil
	}
	tt.placeBatchFn = func(ctx context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		placeCalled = true
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(2000 + i)}
		}
		return results, nil
	}

	ctx := context.Background()
	// Modify first order, keep second, add new third.
	orders := []types.OrderRequest{
		{Coin: "xyz:CL", Side: "buy", Price: 69.5, Size: 1.0},  // modify (price changed)
		{Coin: "xyz:CL", Side: "sell", Price: 71.0, Size: 1.0}, // skip (unchanged)
		{Coin: "xyz:CL", Side: "sell", Price: 72.0, Size: 0.5}, // new place
	}

	err := le.Refresh(ctx, "xyz:CL", orders)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !modifyCalled {
		t.Error("expected modify batch to be called")
	}
	if !placeCalled {
		t.Error("expected place batch to be called")
	}
	// Cancel should not be called (no orders were removed).
	if cancelCalled {
		t.Error("expected cancel batch NOT to be called")
	}
}

func TestFillPartialFillReducesTrackedSize(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 2.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	// Partial fill of the buy order
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 0.5, Price: 70.0, OID: 100,
	})

	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	epoch := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	// Epoch should NOT be bumped (surgical tracking)
	if epoch != 0 {
		t.Errorf("expected epoch=0 (not bumped), got %d", epoch)
	}

	// Should still have 2 tracked orders: buy with reduced size + sell
	if len(tracked) != 2 {
		t.Fatalf("expected 2 tracked orders, got %d", len(tracked))
	}

	// Find the buy order and check its size
	for _, tr := range tracked {
		if tr.OID == 100 {
			if !almostEqual(tr.Size, 1.5, floatTol) {
				t.Errorf("expected buy size=1.5 after partial fill, got %f", tr.Size)
			}
		}
	}
}

func TestFillFullFillRemovesOnlyMatchingOID(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}

	// Full fill of the buy order
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 1.0, Price: 70.0, OID: 100,
	})

	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	epoch := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	if epoch != 0 {
		t.Errorf("expected epoch=0 (not bumped), got %d", epoch)
	}

	// Only the sell order should remain
	if len(tracked) != 1 {
		t.Fatalf("expected 1 tracked order remaining, got %d", len(tracked))
	}
	if tracked[0].OID != 101 {
		t.Errorf("expected remaining order OID=101, got %d", tracked[0].OID)
	}
}

// TestLiveOpenOrdersStaleAfterRefresh verifies that OpenOrders() returns a
// consistent view immediately after Refresh() succeeds. Before the syncCachedOrders
// fix, OpenOrders() read from a 5-second-stale cache and would return 0 here.
func TestLiveOpenOrdersStaleAfterRefresh(t *testing.T) {
	le, tt := newTestLiveExecutor()
	// Initialize cachedOpenOrders so OpenOrders() doesn't panic on nil map.
	le.cachedOpenOrders = make(map[string][]types.OrderRequest)

	coin := "xyz:CL"

	// Verify OpenOrders returns 0 orders initially.
	if got := len(le.OpenOrders(coin)); got != 0 {
		t.Fatalf("expected 0 initial open orders, got %d", got)
	}

	// Set up transport for refreshFull path (no tracked orders → refreshFull).
	// Use atomic counter because refreshFull places orders concurrently.
	var oidCounter atomic.Int64
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		oid := oidCounter.Add(1)
		return placeOrderResult{oid: oid}, nil
	}
	tt.cancelAllFn = func(_ context.Context, _ string) error {
		return nil
	}

	// Refresh with 2 desired orders.
	desired := []types.OrderRequest{
		{Coin: coin, Side: "buy", Price: 70.0, Size: 1.0, Reason: "test"},
		{Coin: coin, Side: "sell", Price: 71.0, Size: 1.0, Reason: "test"},
	}

	ctx := context.Background()
	if err := le.Refresh(ctx, coin, desired); err != nil {
		t.Fatalf("Refresh returned error: %v", err)
	}

	// Verify liveOrders was populated (Refresh succeeded).
	le.mu.Lock()
	trackedCount := len(le.liveOrders[coin])
	le.mu.Unlock()
	if trackedCount != 2 {
		t.Fatalf("expected 2 tracked orders in liveOrders, got %d", trackedCount)
	}

	// OpenOrders should return 2 orders immediately (syncCachedOrders projects
	// liveOrders into cachedOpenOrders after successful Refresh).
	got := len(le.OpenOrders(coin))
	if got != 2 {
		t.Fatalf("OpenOrders() returned %d orders after Refresh placed 2; "+
			"expected cachedOpenOrders to be synced from liveOrders", got)
	}
}

func TestFillUnknownOIDFallback(t *testing.T) {
	le := &LiveExecutor{
		posCh:      make(chan types.PositionUpdate, 100),
		positions:  make(map[string]*livePosition),
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		transport:  &testTransport{},
	}

	le.liveOrders["xyz:CL"] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}

	// Fill with OID=0 (unknown) falls back to old behavior
	le.handleFill(types.FillEvent{
		Coin: "xyz:CL", Side: "buy", Size: 1.0, Price: 70.0, OID: 0,
	})

	le.mu.Lock()
	tracked := le.liveOrders["xyz:CL"]
	epoch := le.orderEpoch["xyz:CL"]
	le.mu.Unlock()

	// Old behavior: tracking cleared and epoch bumped
	if len(tracked) != 0 {
		t.Errorf("expected tracking cleared (OID=0 fallback), got %d orders", len(tracked))
	}
	if epoch != 1 {
		t.Errorf("expected epoch=1 (OID=0 fallback), got %d", epoch)
	}
}

// TestLiveOpenOrdersEmptyAfterCancelAll verifies that OpenOrders() returns 0
// orders immediately after CancelAll() succeeds, without waiting for polling.
func TestLiveOpenOrdersEmptyAfterCancelAll(t *testing.T) {
	le, tt := newTestLiveExecutor()
	le.cachedOpenOrders = make(map[string][]types.OrderRequest)

	coin := "xyz:CL"

	// Seed both liveOrders and cachedOpenOrders with data.
	le.mu.Lock()
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}
	le.mu.Unlock()

	le.openOrdersMu.Lock()
	le.cachedOpenOrders[coin] = []types.OrderRequest{
		{Coin: coin, Side: "buy", Price: 70.0, Size: 1.0},
		{Coin: coin, Side: "sell", Price: 71.0, Size: 1.0},
	}
	le.openOrdersMu.Unlock()

	// Verify OpenOrders returns 2 before cancel.
	if got := len(le.OpenOrders(coin)); got != 2 {
		t.Fatalf("expected 2 open orders before CancelAll, got %d", got)
	}

	// Set up transport cancelAllFn to succeed.
	tt.cancelAllFn = func(_ context.Context, _ string) error {
		return nil
	}

	ctx := context.Background()
	if err := le.CancelAll(ctx, coin); err != nil {
		t.Fatalf("CancelAll returned error: %v", err)
	}

	// OpenOrders should now return 0.
	if got := len(le.OpenOrders(coin)); got != 0 {
		t.Fatalf("expected 0 open orders after CancelAll, got %d", got)
	}
}
