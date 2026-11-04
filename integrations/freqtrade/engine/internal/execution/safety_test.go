package execution

import (
	"context"
	"errors"
	"fmt"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
	"sync/atomic"
	"testing"
	"time"
)

func safetyExecutor() *LiveExecutor { return &LiveExecutor{cfg: &config.Config{ZeusMode: true}} }

func TestZeusExecutionErrorLatchesWithoutRetry(t *testing.T) {
	le := safetyExecutor()
	var calls, failures atomic.Int32
	le.SetFailureHandler(func(error) { failures.Add(1) })
	send := func() (*hyperliquid.PlacedOrder, error) { calls.Add(1); return nil, errors.New("timeout") }
	if _, err := le.placeWithRetry(context.Background(), "ALO", send); err == nil {
		t.Fatal("expected failure")
	}
	if _, err := le.placeWithRetry(context.Background(), "ALO", send); err == nil {
		t.Fatal("gate reopened")
	}
	if calls.Load() != 1 || failures.Load() != 1 {
		t.Fatalf("calls %d failures %d", calls.Load(), failures.Load())
	}
	if le.SafetyError().Error() != "timeout" {
		t.Fatal(le.SafetyError())
	}
}

func TestZeusALOAndInvalidAcknowledgementsAreFatal(t *testing.T) {
	for _, response := range []*hyperliquid.PlacedOrder{nil, {Status: "error: post only would match"}, {Status: "resting", OID: 0}} {
		le := safetyExecutor()
		_, err := le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) { return response, nil })
		if err == nil || le.SafetyError() == nil {
			t.Fatal("invalid response did not halt")
		}
	}
}

func TestQuiesceDrainsInflightAndRejectsQueuedSend(t *testing.T) {
	le := safetyExecutor()
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	go func() {
		defer close(done)
		_, _ = le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) {
			close(entered)
			<-release
			return &hyperliquid.PlacedOrder{Status: "resting", OID: 123}, nil
		})
	}()
	<-entered
	drained := make(chan struct{})
	go func() { le.Quiesce(); close(drained) }()
	deadline := time.After(time.Second)
	for !le.safety.halted.Load() {
		select {
		case <-deadline:
			t.Fatal("gate did not close")
		default:
			time.Sleep(time.Millisecond)
		}
	}
	select {
	case <-drained:
		t.Fatal("inflight request not drained")
	default:
	}
	close(release)
	<-done
	<-drained
	called := false
	_, err := le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) { called = true; return nil, nil })
	if called || err == nil {
		t.Fatal("queued request reached exchange")
	}
}

func TestCleanupContextAllowsOnlyCoordinatorThroughHaltedGate(t *testing.T) {
	le := safetyExecutor()
	le.Quiesce()
	release, err := le.beginAction(CleanupContext(context.Background()))
	if err != nil {
		t.Fatal(err)
	}
	release()
	ctx, cancel := context.WithCancel(CleanupContext(context.Background()))
	cancel()
	if _, err := le.beginAction(ctx); err == nil {
		t.Fatal("cancelled cleanup allowed")
	}
	if _, err := le.beginAction(context.Background()); err == nil {
		t.Fatal("normal execution reopened")
	}
}

func TestLocalEligibilityIsNotAnExecutionFailure(t *testing.T) {
	le := safetyExecutor()
	le.failExecution(fmt.Errorf("%w: inventory cap", ErrQuoteIneligible))
	if le.SafetyError() != nil || le.safety.halted.Load() {
		t.Fatal("local filter latched trading")
	}
}

func TestInvalidCancelAndPositionPayloadsAreRejected(t *testing.T) {
	for _, response := range []map[string]any{nil, {"status": "ok"}, {"status": "ok", "response": map[string]any{"data": map[string]any{"statuses": []any{map[string]any{"error": "rejected"}}}}}} {
		if validateCancelAck(response) == nil {
			t.Fatal("invalid cancellation accepted")
		}
	}
	if validateCancelAck(map[string]any{"status": "ok", "response": map[string]any{"data": map[string]any{"statuses": []any{"success"}}}}) != nil {
		t.Fatal("valid cancellation rejected")
	}
	for _, state := range []map[string]any{nil, {"assetPositions": []any{map[string]any{"position": map[string]any{"coin": "SP500", "szi": "NaN"}}}}} {
		if validatePositionState(state) == nil {
			t.Fatal("invalid account accepted")
		}
	}
}

func TestStopSentinelIsCheckedBeforeEachSend(t *testing.T) {
	le := safetyExecutor()
	le.SetStopCheck(func() bool { return true })
	called := false
	_, err := le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) { called = true; return nil, nil })
	if called || !errors.Is(err, ErrExecutionHalted) {
		t.Fatal("stop sentinel did not block exchange")
	}
	if le.SafetyError() != nil {
		t.Fatal("manual stop became an execution error")
	}
}

func TestExpectedALORejectionDoesNotHaltOrRetry(t *testing.T) {
	for _, message := range []string{
		"error: Post only order would have immediately matched, bbo was 1744.6@1744.7.",
		"error: Error placing new order during modify: Post only order would have immediately matched, bbo was 1744.6@1744.7.",
	} {
		le := safetyExecutor()
		calls := 0
		result, err := le.placeWithRetry(context.Background(), "ALO", func() (*hyperliquid.PlacedOrder, error) {
			calls++
			return &hyperliquid.PlacedOrder{Status: message}, nil
		})
		if err != nil || !result.benignReject || result.oid != 0 || calls != 1 || le.SafetyError() != nil {
			t.Fatalf("expected rejection without retry/halt: %+v %v calls=%d", result, err, calls)
		}
		release, err := le.beginAction(context.Background())
		if err != nil {
			t.Fatal("gate latched", err)
		}
		release()
	}
}
func TestALOClassificationRemainsStrict(t *testing.T) {
	for _, message := range []string{"error: insufficient margin", "error: unknown", "resting", "error: Post only order would have immediately matched; permission denied"} {
		if isExpectedALOReject(&hyperliquid.PlacedOrder{Status: message}, "ALO") {
			t.Fatal("unsafe classification", message)
		}
	}
	if isExpectedALOReject(&hyperliquid.PlacedOrder{Status: "error: Post only order would have immediately matched."}, "IOC") {
		t.Fatal("IOC classified as benign")
	}
}
func TestRejectedModifyOriginalOrderPresence(t *testing.T) {
	for _, present := range []bool{true, false} {
		id := int64(12)
		if !present {
			id = 13
		}
		got, err := confirmedOrderPresent([]any{map[string]any{"oid": id}}, 12)
		if err != nil || got != present {
			t.Fatalf("present=%v got=%v err=%v", present, got, err)
		}
	}
	if _, err := confirmedOrderPresent([]any{map[string]any{}}, 12); err == nil {
		t.Fatal("unknown is not absent")
	}
}

func TestModifyALOReconcilesBeforeContinuing(t *testing.T) {
	for _, scenario := range []string{"resting", "removed", "unavailable"} {
		t.Run(scenario, func(t *testing.T) {
			le := safetyExecutor()
			le.cfg.Risk.MaxPositionNotional = 1000
			le.positions = map[string]*livePosition{}
			le.szDecimals = map[string]int{"io:ANTH": 3}
			sendCalls, reads := 0, 0
			send := func(int64, string, string, string, string, ...hyperliquid.ModifyOption) (*hyperliquid.PlacedOrder, error) {
				sendCalls++
				return &hyperliquid.PlacedOrder{Status: "error: Error placing new order during modify: Post only order would have immediately matched, bbo was 1744.6@1744.7."}, nil
			}
			reconcile := func(coin string, oid int64) (placeOrderResult, error) {
				reads++
				if coin != "io:ANTH" || oid != 12 {
					t.Fatal("wrong market/order")
				}
				if scenario == "unavailable" {
					return placeOrderResult{}, errors.New("account API unavailable")
				}
				return placeOrderResult{benignReject: true, originalGone: scenario == "removed"}, nil
			}
			result, err := le.modifyWithCallbacks(context.Background(), "io:ANTH", &trackedOrder{OID: 12}, &types.OrderRequest{Coin: "io:ANTH", Side: "buy", Size: 0.1, Price: 1744.6}, send, reconcile)
			if sendCalls != 1 || reads != 1 {
				t.Fatalf("unexpected retries: send=%d read=%d", sendCalls, reads)
			}
			if scenario == "unavailable" {
				if err == nil || le.SafetyError() == nil {
					t.Fatal("failed reconciliation reopened quotes")
				}
			} else if err != nil || le.SafetyError() != nil || !result.benignReject || result.originalGone != (scenario == "removed") {
				t.Fatalf("unexpected result: %+v %v", result, err)
			}
			le.Quiesce()
			if _, err := le.beginAction(context.Background()); err == nil {
				t.Fatal("Stop must remain closed")
			}
		})
	}
}

func TestALOCooldownPreservesOtherMarketsAndStop(t *testing.T) {
	le := safetyExecutor()
	le.deferQuotes("io:ANTH")
	if !le.quoteDeferred("io:ANTH") || le.quoteDeferred("xyz:SP500") {
		t.Fatal("cooldown leaked across markets")
	}
	// No SDK is attached: a deferred quote must never reach it.
	tr := NewSDKTransport(le)
	result, err := tr.PlaceOrder(context.Background(), types.OrderRequest{Coin: "io:ANTH"})
	if err != nil || !result.benignReject || le.SafetyError() != nil {
		t.Fatal("cooldown halted or sent quote", err)
	}
	le.Quiesce()
	if _, err := le.beginAction(context.Background()); err == nil {
		t.Fatal("cooldown reopened Stop")
	}
}

func TestModifyRejectionPreservesOppositeQuote(t *testing.T) {
	for _, gone := range []bool{true, false} {
		le, tr := newTestLiveExecutor()
		coin := "io:ANTH"
		le.liveOrders[coin] = []trackedOrder{{OID: 12, Side: "buy", Price: 1700, Size: 1}, {OID: 13, Side: "sell", Price: 1800, Size: 1}}
		le.orderEpoch[coin] = 1
		tr.modifyOrderFn = func(context.Context, *trackedOrder, *types.OrderRequest) (placeOrderResult, error) {
			return placeOrderResult{benignReject: true, originalGone: gone}, nil
		}
		tr.cancelOrderFn = func(context.Context, int64, string) error { t.Error("unexpected cancel"); return nil }
		tr.placeOrderFn = func(context.Context, types.OrderRequest) (placeOrderResult, error) {
			t.Error("unexpected replacement")
			return placeOrderResult{}, nil
		}
		err := le.Refresh(context.Background(), coin, []types.OrderRequest{{Coin: coin, Side: "buy", Price: 1744, Size: 1}, {Coin: coin, Side: "sell", Price: 1800, Size: 1}})
		if err != nil {
			t.Fatal(err)
		}
		foundBuy, foundSell := false, false
		for _, o := range le.liveOrders[coin] {
			if o.OID == 12 {
				foundBuy = true
			}
			if o.OID == 13 {
				foundSell = true
			}
		}
		if !foundSell || foundBuy == gone {
			t.Fatalf("wrong retained quotes: %+v", le.liveOrders[coin])
		}
	}
}
