package execution

import (
	"context"

	"hyperliquid-market-maker/internal/types"
)

// Transport abstracts the exchange communication layer for order operations.
// Implemented by SDKTransport (via QuickNode).
type Transport interface {
	PlaceOrder(ctx context.Context, req types.OrderRequest) (placeOrderResult, error)
	PlaceBatch(ctx context.Context, orders []types.OrderRequest) ([]placeOrderResult, error)
	ModifyOrder(ctx context.Context, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error)
	ModifyBatch(ctx context.Context, targets []modifyTarget) ([]placeOrderResult, error)
	CancelOrder(ctx context.Context, oid int64, coin string) error
	CancelBatch(ctx context.Context, targets []cancelTarget) error
	CancelAll(ctx context.Context, coin string) error
	SupportsBatch() bool
}
