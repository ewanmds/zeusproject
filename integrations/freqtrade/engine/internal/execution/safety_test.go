package execution

import (
	"context"
	"errors"
	"fmt"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/config"
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
