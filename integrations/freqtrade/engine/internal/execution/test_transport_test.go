package execution

import (
	"context"

	"hyperliquid-market-maker/internal/types"
)

// testTransport implements Transport for use in unit tests.
// Each operation delegates to the corresponding function pointer
// if set; otherwise it returns a zero value / nil error (or
// errBatchNotSupported for batch operations when batch == false).
type testTransport struct {
	placeOrderFn  func(context.Context, types.OrderRequest) (placeOrderResult, error)
	placeBatchFn  func(context.Context, []types.OrderRequest) ([]placeOrderResult, error)
	modifyOrderFn func(context.Context, *trackedOrder, *types.OrderRequest) (placeOrderResult, error)
	modifyBatchFn func(context.Context, []modifyTarget) ([]placeOrderResult, error)
	cancelOrderFn func(context.Context, int64, string) error
	cancelBatchFn func(context.Context, []cancelTarget) error
	cancelAllFn   func(context.Context, string) error
	batch         bool
}

// Compile-time interface check.
var _ Transport = (*testTransport)(nil)

func (t *testTransport) PlaceOrder(ctx context.Context, req types.OrderRequest) (placeOrderResult, error) {
	if t.placeOrderFn != nil {
		return t.placeOrderFn(ctx, req)
	}
	return placeOrderResult{}, nil
}

func (t *testTransport) PlaceBatch(ctx context.Context, orders []types.OrderRequest) ([]placeOrderResult, error) {
	if t.placeBatchFn != nil {
		return t.placeBatchFn(ctx, orders)
	}
	return nil, errBatchNotSupported
}

func (t *testTransport) ModifyOrder(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
	if t.modifyOrderFn != nil {
		return t.modifyOrderFn(ctx, tracked, desired)
	}
	return placeOrderResult{}, nil
}

func (t *testTransport) ModifyBatch(ctx context.Context, targets []modifyTarget) ([]placeOrderResult, error) {
	if t.modifyBatchFn != nil {
		return t.modifyBatchFn(ctx, targets)
	}
	return nil, errBatchNotSupported
}

func (t *testTransport) CancelOrder(ctx context.Context, oid int64, coin string) error {
	if t.cancelOrderFn != nil {
		return t.cancelOrderFn(ctx, oid, coin)
	}
	return nil
}

func (t *testTransport) CancelBatch(ctx context.Context, targets []cancelTarget) error {
	if t.cancelBatchFn != nil {
		return t.cancelBatchFn(ctx, targets)
	}
	return errBatchNotSupported
}

func (t *testTransport) CancelAll(ctx context.Context, coin string) error {
	if t.cancelAllFn != nil {
		return t.cancelAllFn(ctx, coin)
	}
	return nil
}

func (t *testTransport) SupportsBatch() bool {
	return t.batch
}
