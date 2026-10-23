package execution

import (
	"context"
	"errors"
	"fmt"
	"time"

	"hyperliquid-market-maker/internal/types"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

var errBatchNotSupported = errors.New("batch operations not supported by SDK transport")

// SDKTransport implements Transport using the Hyperliquid SDK via QuickNode.
// It calls the SDK directly (Buy/Sell/Modify/Cancel/CancelAll) with
// exponential backoff retry, bypassing the dispatch wrappers on LiveExecutor.
type SDKTransport struct {
	le *LiveExecutor
}

// NewSDKTransport creates an SDKTransport wrapping the given LiveExecutor.
func NewSDKTransport(le *LiveExecutor) *SDKTransport {
	return &SDKTransport{le: le}
}

func (t *SDKTransport) PlaceOrder(ctx context.Context, req types.OrderRequest) (placeOrderResult, error) {
	// Call SDK Buy/Sell directly with retry (same logic as the old placeOrder
	// fallback path, but without going through a dispatch wrapper).
	tif := normalizedTIF(req.TimeInForce, "ALO")
	submitted := 0.0
	result, err := t.le.placeWithRetry(ctx, tif, func() (*hyperliquid.PlacedOrder, error) {
		capped, err := t.le.capZeusOrder(req, 0)
		if err != nil {
			return nil, err
		}
		submitted = capped.Size
		opts := []hyperliquid.OrderOption{hyperliquid.WithSize(capped.Size), hyperliquid.WithPrice(capped.Price), hyperliquid.WithTIF(timeInForceOpt(tif))}
		if capped.ReduceOnly {
			opts = append(opts, hyperliquid.WithReduceOnly())
		}
		if normalizeSide(capped.Side) == "buy" {
			return t.le.sdk.Buy(capped.Coin, opts...)
		}
		return t.le.sdk.Sell(capped.Coin, opts...)
	})
	result.size = submitted
	t.le.zeusSubmissionResult(req.Coin, result, err)
	return result, err
}

func (t *SDKTransport) PlaceBatch(_ context.Context, _ []types.OrderRequest) ([]placeOrderResult, error) {
	return nil, errBatchNotSupported
}

func (t *SDKTransport) ModifyOrder(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
	// Delegates to modifyWithRetry which wraps sdk.Modify with 3-attempt backoff.
	result, err := t.le.modifyWithRetry(ctx, desired.Coin, tracked, desired)
	t.le.zeusSubmissionResult(desired.Coin, result, err)
	return result, err
}

func (t *SDKTransport) ModifyBatch(_ context.Context, _ []modifyTarget) ([]placeOrderResult, error) {
	return nil, errBatchNotSupported
}

func (t *SDKTransport) CancelOrder(ctx context.Context, oid int64, coin string) error {
	// Call SDK Cancel directly with retry (same logic as the old cancelSingleOID
	// fallback path, but without going through a dispatch wrapper).
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		_, err := t.le.sdk.Cancel(oid, coin)
		t.le.stats.RecordExchange()
		if err == nil {
			t.le.recordUpstreamSuccess()
			return nil
		}
		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			return err
		}
		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}
	t.le.recordUpstreamError()
	return fmt.Errorf("cancel OID %d after retries: %w", oid, lastErr)
}

func (t *SDKTransport) CancelBatch(_ context.Context, _ []cancelTarget) error {
	return errBatchNotSupported
}

func (t *SDKTransport) CancelAll(ctx context.Context, coin string) error {
	// Delegates to cancelWithRetry which calls sdk.CancelAll (perps) or
	// cancelDexOrders (HIP-3) with retry.
	return t.le.cancelWithRetry(ctx, coin)
}

func (t *SDKTransport) SupportsBatch() bool {
	return false
}
