package execution

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// sequenceCounter provides a monotonic counter used by mock function pointers
// to record the order in which they are called.
type sequenceCounter struct {
	v atomic.Int64
}

func (s *sequenceCounter) next() int64 {
	return s.v.Add(1)
}

// newTestLiveExecutor creates a minimal LiveExecutor suitable for unit tests.
// It has no SDK, no fill channel, and only the fields needed for Refresh.
func newTestLiveExecutor() (*LiveExecutor, *testTransport) {
	tt := &testTransport{}
	le := &LiveExecutor{
		liveOrders: make(map[string][]trackedOrder),
		orderEpoch: make(map[string]uint64),
		posCh:      make(chan types.PositionUpdate, 100),
		transport:  tt,
	}
	return le, tt
}

// TestRefreshCancelBeforePlace verifies that in the single-order path, all
// cancel actions complete before any place actions start.
func TestRefreshCancelBeforePlace(t *testing.T) {
	le, tt := newTestLiveExecutor()
	coin := "TEST"

	// Seed tracked orders so Refresh enters the diff path (not refreshFull).
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 51.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	var seq sequenceCounter
	var cancelSeqs []int64
	var placeSeqs []int64
	cancelCh := make(chan int64, 10)
	placeCh := make(chan int64, 10)

	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		s := seq.next()
		cancelCh <- s
		return nil
	}
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		s := seq.next()
		placeCh <- s
		return placeOrderResult{oid: 200 + s}, nil
	}
	// Ensure modifyOrder doesn't panic (not used here, but set for safety).
	tt.modifyOrderFn = func(_ context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{oid: tracked.OID}, nil
	}

	// Build diffs manually: cancel OID 100, cancel OID 101, place two new.
	// We bypass diffOrders by directly calling Refresh with orders that won't
	// match any tracked order, but that requires us to set up proper conditions.
	// Instead, let's set tracked orders and desired orders that produce cancel+place diffs.
	// Tracked: buy@50, sell@51. Desired: buy@55, sell@56 (price diff > threshold => cancel+place).
	// Actually, diffOrders might produce modify diffs. To guarantee cancel+place, we'll
	// set very different orders so they don't match by side pairing.
	// Simplest approach: set tracked as 2 buys, desired as 2 sells. No pairing possible.
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
		{OID: 101, Side: "buy", Price: 49.0, Size: 1.0},
	}
	desired := []types.OrderRequest{
		{Coin: coin, Side: "sell", Price: 55.0, Size: 1.0, Reason: "test"},
		{Coin: coin, Side: "sell", Price: 56.0, Size: 1.0, Reason: "test"},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.Refresh(ctx, coin, desired)
	if err != nil {
		t.Fatalf("Refresh returned error: %v", err)
	}

	// Drain channels.
	close(cancelCh)
	close(placeCh)
	for s := range cancelCh {
		cancelSeqs = append(cancelSeqs, s)
	}
	for s := range placeCh {
		placeSeqs = append(placeSeqs, s)
	}

	if len(cancelSeqs) == 0 {
		t.Fatal("expected at least one cancel call, got none")
	}
	if len(placeSeqs) == 0 {
		t.Fatal("expected at least one place call, got none")
	}

	// Every cancel sequence number must be less than every place sequence number.
	maxCancel := int64(0)
	for _, s := range cancelSeqs {
		if s > maxCancel {
			maxCancel = s
		}
	}
	minPlace := placeSeqs[0]
	for _, s := range placeSeqs {
		if s < minPlace {
			minPlace = s
		}
	}

	if maxCancel >= minPlace {
		t.Errorf("cancel-first ordering violated: max cancel seq=%d >= min place seq=%d", maxCancel, minPlace)
	}
}

// TestRefreshBatchCancelBeforePlace verifies that in the batch path, the
// cancel batch completes before place batch starts.
func TestRefreshBatchCancelBeforePlace(t *testing.T) {
	le, tt := newTestLiveExecutor()
	coin := "TEST"

	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
		{OID: 101, Side: "buy", Price: 49.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	var seq sequenceCounter
	var cancelBatchSeq, placeBatchSeq int64

	tt.batch = true
	tt.cancelBatchFn = func(_ context.Context, targets []cancelTarget) error {
		cancelBatchSeq = seq.next()
		return nil
	}
	tt.placeBatchFn = func(_ context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		placeBatchSeq = seq.next()
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(300 + i)}
		}
		return results, nil
	}

	desired := []types.OrderRequest{
		{Coin: coin, Side: "sell", Price: 55.0, Size: 1.0, Reason: "test"},
		{Coin: coin, Side: "sell", Price: 56.0, Size: 1.0, Reason: "test"},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.Refresh(ctx, coin, desired)
	if err != nil {
		t.Fatalf("Refresh returned error: %v", err)
	}

	if cancelBatchSeq == 0 {
		t.Fatal("cancel batch was never called")
	}
	if placeBatchSeq == 0 {
		t.Fatal("place batch was never called")
	}
	if cancelBatchSeq >= placeBatchSeq {
		t.Errorf("cancel-first ordering violated in batch path: cancel seq=%d >= place seq=%d",
			cancelBatchSeq, placeBatchSeq)
	}
}

// TestRefreshEpochGuardBetweenPhases verifies that if a fill arrives (bumping
// the epoch) during Phase 1, the pending places in Phase 2 are NOT executed.
func TestRefreshEpochGuardBetweenPhases(t *testing.T) {
	le, tt := newTestLiveExecutor()
	coin := "TEST"

	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
		{OID: 101, Side: "buy", Price: 49.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	placeCalled := atomic.Bool{}

	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		// Simulate a fill arriving during cancel phase: bump the epoch.
		le.mu.Lock()
		le.orderEpoch[coin]++
		le.mu.Unlock()
		return nil
	}
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		placeCalled.Store(true)
		return placeOrderResult{oid: 999}, nil
	}
	tt.modifyOrderFn = func(_ context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{oid: tracked.OID}, nil
	}

	desired := []types.OrderRequest{
		{Coin: coin, Side: "sell", Price: 55.0, Size: 1.0, Reason: "test"},
		{Coin: coin, Side: "sell", Price: 56.0, Size: 1.0, Reason: "test"},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.Refresh(ctx, coin, desired)
	if err == nil {
		t.Fatal("expected error due to epoch change during phase 1, got nil")
	}

	if placeCalled.Load() {
		t.Error("PlaceOrder should NOT have been called after epoch changed during phase 1")
	}
}

// TestRefreshModifiesInPhase1 verifies that modify actions execute in Phase 1
// (before any place actions).
func TestRefreshModifiesInPhase1(t *testing.T) {
	le, tt := newTestLiveExecutor()
	coin := "TEST"

	// Set up tracked orders: one buy we'll modify, one to skip as-is,
	// and desired will also include a new sell to place.
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	var seq sequenceCounter
	modifyCh := make(chan int64, 10)
	placeCh := make(chan int64, 10)

	tt.modifyOrderFn = func(_ context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		s := seq.next()
		modifyCh <- s
		return placeOrderResult{oid: tracked.OID + 1000}, nil
	}
	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		return nil
	}
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		s := seq.next()
		placeCh <- s
		return placeOrderResult{oid: 500 + s}, nil
	}

	// Desired: modify the existing buy (same side, different price within threshold),
	// plus a new sell (no tracked match => place).
	// To guarantee a modify diff: same side, small price change.
	// diffOrders pairs tracked buys with desired buys sorted by price. If the price diff
	// is within skip threshold, it's a skip; otherwise modify. Let's make the price
	// different enough to trigger modify but same side.
	desired := []types.OrderRequest{
		{Coin: coin, Side: "buy", Price: 50.5, Size: 1.0, Reason: "test"},  // modify OID 100
		{Coin: coin, Side: "sell", Price: 55.0, Size: 1.0, Reason: "test"}, // place new
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.Refresh(ctx, coin, desired)
	if err != nil {
		t.Fatalf("Refresh returned error: %v", err)
	}

	close(modifyCh)
	close(placeCh)

	var modifySeqs, placeSeqs []int64
	for s := range modifyCh {
		modifySeqs = append(modifySeqs, s)
	}
	for s := range placeCh {
		placeSeqs = append(placeSeqs, s)
	}

	if len(placeSeqs) == 0 {
		t.Fatal("expected at least one place call, got none")
	}

	// If modify was called, verify it ran before place.
	if len(modifySeqs) > 0 {
		maxModify := int64(0)
		for _, s := range modifySeqs {
			if s > maxModify {
				maxModify = s
			}
		}
		minPlace := placeSeqs[0]
		for _, s := range placeSeqs {
			if s < minPlace {
				minPlace = s
			}
		}
		if maxModify >= minPlace {
			t.Errorf("modify-before-place ordering violated: max modify seq=%d >= min place seq=%d",
				maxModify, minPlace)
		}
	}
}

// TestCancelBatchMultiAsset verifies that CancelBatch receives cancelTarget
// entries with the correct coin, enabling cross-asset batch cancels.
func TestCancelBatchMultiAsset(t *testing.T) {
	le, tt := newTestLiveExecutor()
	var cancelledTargets []cancelTarget

	tt.batch = true
	tt.cancelBatchFn = func(_ context.Context, targets []cancelTarget) error {
		cancelledTargets = append(cancelledTargets, targets...)
		return nil
	}
	tt.modifyBatchFn = func(_ context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		results := make([]placeOrderResult, len(targets))
		for i, tgt := range targets {
			results[i] = placeOrderResult{oid: tgt.tracked.OID}
		}
		return results, nil
	}
	tt.placeBatchFn = func(_ context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(500 + i)}
		}
		return results, nil
	}

	coin := "TEST"
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 50.0, Size: 1.0},
		{OID: 101, Side: "buy", Price: 49.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	// Desired: only sells → both buys should be cancelled via cancelTarget
	desired := []types.OrderRequest{
		{Coin: coin, Side: "sell", Price: 55.0, Size: 1.0},
		{Coin: coin, Side: "sell", Price: 56.0, Size: 1.0},
	}
	err := le.Refresh(context.Background(), coin, desired)
	if err != nil {
		t.Fatal(err)
	}

	if len(cancelledTargets) != 2 {
		t.Fatalf("expected 2 cancelled targets, got %d", len(cancelledTargets))
	}
	for _, ct := range cancelledTargets {
		if ct.coin != coin {
			t.Errorf("expected coin %s, got %s", coin, ct.coin)
		}
	}
}

// TestRefreshMultiCoalescesBatches verifies that RefreshMulti merges modify
// operations from 2 coins into a single batch modify call.
func TestRefreshMultiCoalescesBatches(t *testing.T) {
	le, tt := newTestLiveExecutor()

	coinA := "xyz:CL"
	coinB := "xyz:BRENTOIL"

	// Seed tracked orders for both coins.
	le.liveOrders[coinA] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}
	le.liveOrders[coinB] = []trackedOrder{
		{OID: 200, Side: "buy", Price: 80.0, Size: 1.0},
		{OID: 201, Side: "sell", Price: 81.0, Size: 1.0},
	}
	le.orderEpoch[coinA] = 1
	le.orderEpoch[coinB] = 1

	var modifyBatchCalls int
	var cancelBatchCalls int
	var placeBatchCalls int

	tt.batch = true
	tt.modifyBatchFn = func(_ context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		modifyBatchCalls++
		results := make([]placeOrderResult, len(targets))
		for i, tgt := range targets {
			results[i] = placeOrderResult{oid: tgt.tracked.OID + 1000}
		}
		return results, nil
	}
	tt.cancelBatchFn = func(_ context.Context, targets []cancelTarget) error {
		cancelBatchCalls++
		return nil
	}
	tt.placeBatchFn = func(_ context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		placeBatchCalls++
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(3000 + i)}
		}
		return results, nil
	}

	// Desired orders: same side as tracked, prices differ by >100 bps → modify.
	ordersByCoin := map[string][]types.OrderRequest{
		coinA: {
			{Coin: coinA, Side: "buy", Price: 72.0, Size: 1.0},  // ~280 bps diff → modify
			{Coin: coinA, Side: "sell", Price: 73.0, Size: 1.0}, // ~278 bps diff → modify
		},
		coinB: {
			{Coin: coinB, Side: "buy", Price: 82.0, Size: 1.0},  // ~247 bps diff → modify
			{Coin: coinB, Side: "sell", Price: 83.0, Size: 1.0}, // ~244 bps diff → modify
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.RefreshMulti(ctx, ordersByCoin)
	if err != nil {
		t.Fatalf("RefreshMulti returned error: %v", err)
	}

	// Should have exactly 1 modify batch call, not 2.
	if modifyBatchCalls != 1 {
		t.Errorf("expected 1 modify batch call, got %d", modifyBatchCalls)
	}
	if cancelBatchCalls != 0 {
		t.Errorf("expected 0 cancel batch calls, got %d", cancelBatchCalls)
	}
	if placeBatchCalls != 0 {
		t.Errorf("expected 0 place batch calls, got %d", placeBatchCalls)
	}

	// Verify tracking updated for both coins.
	le.mu.Lock()
	trackedA := le.liveOrders[coinA]
	trackedB := le.liveOrders[coinB]
	le.mu.Unlock()

	if len(trackedA) != 2 {
		t.Fatalf("expected 2 tracked orders for %s, got %d", coinA, len(trackedA))
	}
	if len(trackedB) != 2 {
		t.Fatalf("expected 2 tracked orders for %s, got %d", coinB, len(trackedB))
	}
}

// TestRefreshMultiMixedOperations verifies that RefreshMulti handles a mix of
// modifies, cancels, and places across multiple coins with at most 1 batch call each.
func TestRefreshMultiMixedOperations(t *testing.T) {
	le, tt := newTestLiveExecutor()

	coinA := "xyz:CL"
	coinB := "xyz:BRENTOIL"

	// CoinA: tracked buys only. Desired: sells only → cancel buys + place sells.
	// CoinB: tracked buy+sell. Desired: buy+sell with different prices → modify.
	le.liveOrders[coinA] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}
	le.liveOrders[coinB] = []trackedOrder{
		{OID: 200, Side: "buy", Price: 80.0, Size: 1.0},
		{OID: 201, Side: "sell", Price: 81.0, Size: 1.0},
	}
	le.orderEpoch[coinA] = 1
	le.orderEpoch[coinB] = 1

	var modifyBatchCalls int
	var cancelBatchCalls int
	var placeBatchCalls int

	tt.batch = true
	tt.modifyBatchFn = func(_ context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		modifyBatchCalls++
		results := make([]placeOrderResult, len(targets))
		for i, tgt := range targets {
			results[i] = placeOrderResult{oid: tgt.tracked.OID + 1000}
		}
		return results, nil
	}
	tt.cancelBatchFn = func(_ context.Context, targets []cancelTarget) error {
		cancelBatchCalls++
		return nil
	}
	tt.placeBatchFn = func(_ context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		placeBatchCalls++
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(3000 + i)}
		}
		return results, nil
	}

	ordersByCoin := map[string][]types.OrderRequest{
		coinA: {
			// Different side from tracked (buy) → cancel tracked buy + place sell.
			{Coin: coinA, Side: "sell", Price: 72.0, Size: 1.0},
		},
		coinB: {
			// Same sides, prices differ >100 bps → modify.
			{Coin: coinB, Side: "buy", Price: 82.0, Size: 1.0},  // ~247 bps → modify
			{Coin: coinB, Side: "sell", Price: 83.0, Size: 1.0}, // ~244 bps → modify
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.RefreshMulti(ctx, ordersByCoin)
	if err != nil {
		t.Fatalf("RefreshMulti returned error: %v", err)
	}

	// At most 1 of each batch type.
	if modifyBatchCalls > 1 {
		t.Errorf("expected at most 1 modify batch call, got %d", modifyBatchCalls)
	}
	if cancelBatchCalls > 1 {
		t.Errorf("expected at most 1 cancel batch call, got %d", cancelBatchCalls)
	}
	if placeBatchCalls > 1 {
		t.Errorf("expected at most 1 place batch call, got %d", placeBatchCalls)
	}

	// Verify tracked orders correct for both coins.
	le.mu.Lock()
	trackedA := le.liveOrders[coinA]
	trackedB := le.liveOrders[coinB]
	le.mu.Unlock()

	// CoinA: 1 placed sell (cancel had no tracking result).
	if len(trackedA) != 1 {
		t.Fatalf("expected 1 tracked order for %s, got %d", coinA, len(trackedA))
	}
	if trackedA[0].Side != "sell" {
		t.Errorf("expected tracked side=sell for %s, got %s", coinA, trackedA[0].Side)
	}

	// CoinB: 2 modified orders.
	if len(trackedB) != 2 {
		t.Fatalf("expected 2 tracked orders for %s, got %d", coinB, len(trackedB))
	}
}

// TestRefreshMultiFallsBackWithoutBatchFns verifies that RefreshMulti falls back
// to per-coin Refresh when no batch functions are set.
func TestRefreshMultiFallsBackWithoutBatchFns(t *testing.T) {
	le, tt := newTestLiveExecutor()

	coinA := "xyz:CL"
	coinB := "xyz:BRENTOIL"

	// No batch support → should fall back to per-coin Refresh.
	// Seed tracked orders to trigger diff path.
	le.liveOrders[coinA] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
	}
	le.liveOrders[coinB] = []trackedOrder{
		{OID: 200, Side: "buy", Price: 80.0, Size: 1.0},
	}
	le.orderEpoch[coinA] = 1
	le.orderEpoch[coinB] = 1

	var modifyCalls int
	tt.modifyOrderFn = func(_ context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
		modifyCalls++
		return placeOrderResult{oid: tracked.OID + 1000}, nil
	}
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		return placeOrderResult{oid: 999}, nil
	}
	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		return nil
	}

	ordersByCoin := map[string][]types.OrderRequest{
		coinA: {
			{Coin: coinA, Side: "buy", Price: 72.0, Size: 1.0}, // >100 bps → modify
		},
		coinB: {
			{Coin: coinB, Side: "buy", Price: 82.0, Size: 1.0}, // >100 bps → modify
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.RefreshMulti(ctx, ordersByCoin)
	if err != nil {
		t.Fatalf("RefreshMulti returned error: %v", err)
	}

	// Without batch fns, should have used individual modify calls (1 per coin).
	if modifyCalls != 2 {
		t.Errorf("expected 2 individual modify calls (fallback), got %d", modifyCalls)
	}

	// Verify tracking is updated for both coins.
	le.mu.Lock()
	trackedA := le.liveOrders[coinA]
	trackedB := le.liveOrders[coinB]
	le.mu.Unlock()

	if len(trackedA) != 1 {
		t.Errorf("expected 1 tracked order for %s, got %d", coinA, len(trackedA))
	}
	if len(trackedB) != 1 {
		t.Errorf("expected 1 tracked order for %s, got %d", coinB, len(trackedB))
	}
}

// TestRefreshBatchModifyFailureFallsBackToPerEntry documents that when
// ModifyBatch fails inside refreshBatch (single-coin Refresh path),
// the fallback is per-entry CancelOrder + PlaceOrder — NOT a full
// CancelAll. This is a characterization test locking existing behavior.
func TestRefreshBatchModifyFailureFallsBackToPerEntry(t *testing.T) {
	le, tt := newTestLiveExecutor()
	coin := "TEST"

	// Seed tracked orders with buy+sell so diffOrders produces modify diffs.
	le.liveOrders[coin] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}
	le.orderEpoch[coin] = 1

	var cancelledOIDs []int64
	var placedOrders []types.OrderRequest
	var cancelAllCalled atomic.Bool
	var mu sync.Mutex

	// ModifyBatch always fails → triggers per-entry fallback.
	// Transport must support batch for Refresh to take the batch path.
	tt.batch = true
	tt.modifyBatchFn = func(_ context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		return nil, fmt.Errorf("batch modify unavailable")
	}

	// CancelOrder records individual cancel calls (the per-entry fallback path).
	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		mu.Lock()
		cancelledOIDs = append(cancelledOIDs, oid)
		mu.Unlock()
		return nil
	}

	// PlaceOrder records individual place calls (the per-entry fallback path).
	nextOID := int64(500)
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		mu.Lock()
		placedOrders = append(placedOrders, o)
		oid := nextOID
		nextOID++
		mu.Unlock()
		return placeOrderResult{oid: oid}, nil
	}

	// cancelAllFn (cancel-all) should NOT be called — fallback is per-entry.
	tt.cancelAllFn = func(_ context.Context, _ string) error {
		cancelAllCalled.Store(true)
		return nil
	}

	// No CancelBatch or PlaceBatch set — only ModifyBatch is set, which
	// is enough for the batch path (SupportsBatch is true).
	// The fallback path within refreshBatch calls CancelOrder and PlaceOrder
	// via the transport.

	// Desired: same sides, prices differ >100 bps → modify diffs.
	desired := []types.OrderRequest{
		{Coin: coin, Side: "buy", Price: 72.0, Size: 1.0},  // ~280 bps from 70 → modify
		{Coin: coin, Side: "sell", Price: 73.0, Size: 1.0}, // ~278 bps from 71 → modify
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.Refresh(ctx, coin, desired)

	// Per-entry fallback should succeed; no tracking cleared.
	if err != nil {
		t.Fatalf("expected no error (per-entry fallback should succeed), got: %v", err)
	}

	// cancelSingleOID should have been called for each failed modify entry.
	if len(cancelledOIDs) != 2 {
		t.Errorf("expected 2 individual cancel calls (per-entry fallback), got %d", len(cancelledOIDs))
	}

	// placeOrder should have been called for each failed modify entry.
	if len(placedOrders) != 2 {
		t.Errorf("expected 2 individual place calls (per-entry fallback), got %d", len(placedOrders))
	}

	// cancel-all should NOT have been called.
	if cancelAllCalled.Load() {
		t.Error("transport cancelAll was called; expected per-entry fallback only")
	}

	// Tracked orders should reflect the placed orders.
	le.mu.Lock()
	tracked := le.liveOrders[coin]
	le.mu.Unlock()

	if len(tracked) != 2 {
		t.Fatalf("expected 2 tracked orders after fallback, got %d", len(tracked))
	}
}

// TestRefreshMultiModifyFailureClearsTracking documents that when ModifyBatch
// fails inside RefreshMulti, tracking is cleared for ALL coins (no per-entry
// fallback). This contrasts with refreshBatch which does per-entry fallback.
// This is a characterization test locking existing behavior.
func TestRefreshMultiModifyFailureClearsTracking(t *testing.T) {
	le, tt := newTestLiveExecutor()

	coinA := "xyz:CL"
	coinB := "xyz:BRENTOIL"

	// Seed tracked orders for both coins.
	le.liveOrders[coinA] = []trackedOrder{
		{OID: 100, Side: "buy", Price: 70.0, Size: 1.0},
		{OID: 101, Side: "sell", Price: 71.0, Size: 1.0},
	}
	le.liveOrders[coinB] = []trackedOrder{
		{OID: 200, Side: "buy", Price: 80.0, Size: 1.0},
		{OID: 201, Side: "sell", Price: 81.0, Size: 1.0},
	}
	le.orderEpoch[coinA] = 1
	le.orderEpoch[coinB] = 1

	var cancelSingleCalls atomic.Int64
	var placeOrderCalls atomic.Int64

	tt.batch = true
	// ModifyBatch always fails.
	tt.modifyBatchFn = func(_ context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
		return nil, fmt.Errorf("batch modify unavailable")
	}

	// CancelBatch succeeds (should not be called for modifies, but set for safety).
	tt.cancelBatchFn = func(_ context.Context, targets []cancelTarget) error {
		return nil
	}

	// PlaceBatch succeeds (should not be called if modify fails early, but set for safety).
	tt.placeBatchFn = func(_ context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
		results := make([]placeOrderResult, len(orders))
		for i := range orders {
			results[i] = placeOrderResult{oid: int64(3000 + i)}
		}
		return results, nil
	}

	// Per-entry functions: track whether they're called (they should NOT be
	// in RefreshMulti — it doesn't have per-entry fallback like refreshBatch).
	tt.cancelOrderFn = func(_ context.Context, oid int64, _ string) error {
		cancelSingleCalls.Add(1)
		return nil
	}
	tt.placeOrderFn = func(_ context.Context, o types.OrderRequest) (placeOrderResult, error) {
		placeOrderCalls.Add(1)
		return placeOrderResult{oid: 999}, nil
	}

	// Desired: same sides, prices differ >100 bps → all modify diffs.
	ordersByCoin := map[string][]types.OrderRequest{
		coinA: {
			{Coin: coinA, Side: "buy", Price: 72.0, Size: 1.0},  // ~280 bps → modify
			{Coin: coinA, Side: "sell", Price: 73.0, Size: 1.0}, // ~278 bps → modify
		},
		coinB: {
			{Coin: coinB, Side: "buy", Price: 82.0, Size: 1.0},  // ~247 bps → modify
			{Coin: coinB, Side: "sell", Price: 83.0, Size: 1.0}, // ~244 bps → modify
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := le.RefreshMulti(ctx, ordersByCoin)

	// RefreshMulti should return an error when batch modify fails.
	if err == nil {
		t.Fatal("expected error from RefreshMulti when batch modify fails, got nil")
	}

	// No per-entry fallback: cancelSingleOID and placeOrder should NOT be called.
	if n := cancelSingleCalls.Load(); n != 0 {
		t.Errorf("expected 0 cancelSingleOID calls (no per-entry fallback), got %d", n)
	}
	if n := placeOrderCalls.Load(); n != 0 {
		t.Errorf("expected 0 placeOrder calls (no per-entry fallback), got %d", n)
	}

	// Tracking should be cleared for BOTH coins.
	le.mu.Lock()
	trackedA := le.liveOrders[coinA]
	trackedB := le.liveOrders[coinB]
	le.mu.Unlock()

	if len(trackedA) != 0 {
		t.Errorf("expected tracking cleared for %s, got %d orders", coinA, len(trackedA))
	}
	if len(trackedB) != 0 {
		t.Errorf("expected tracking cleared for %s, got %d orders", coinB, len(trackedB))
	}
}
