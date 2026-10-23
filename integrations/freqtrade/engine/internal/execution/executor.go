package execution

import (
	"context"
	"errors"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// ErrRefreshSuperseded reports that a refresh was abandoned because the order
// state changed underneath it (fill, reconciliation, or an order that was
// already gone when we tried to cancel it). The executor has cleared its local
// tracking; the next refresh will resync with a full cancel-and-replace. This
// is a normal race and must not be treated as an operational API failure.
var ErrRefreshSuperseded = errors.New("quote refresh superseded")

// Executor defines the interface for order management, implemented by
// PaperExecutor (simulation) and LiveExecutor (real trading).
type Executor interface {
	// Init bootstraps the executor with current on-chain state (positions, equity).
	// Must be called before Run(). assets is the list of traded assets (e.g. "xyz:CL").
	Init(ctx context.Context, assets []string) error

	// Run starts the executor's event loop. Blocks until ctx is cancelled.
	Run(ctx context.Context)

	// Refresh updates orders for the coin. Modifies existing orders in-place when
	// possible, cancels removed orders, and places new ones. Falls back to full
	// cancel-and-replace when order tracking is unavailable.
	Refresh(ctx context.Context, coin string, orders []types.OrderRequest) error

	// RefreshMulti refreshes quotes for multiple coins in coalesced batch API calls.
	// Each coin's orders are diffed independently, then all modifies/cancels/places
	// are batched into at most 3 API calls total (vs 2N for N coins).
	RefreshMulti(ctx context.Context, ordersByCoin map[string][]types.OrderRequest) error

	// CancelAll cancels all open orders for the coin.
	CancelAll(ctx context.Context, coin string) error

	// CancelSide cancels only one side of open orders for the coin. This is used
	// by protective guards that need to remove the inventory-increasing side
	// without disturbing exit-side orders.
	CancelSide(ctx context.Context, coin string, side string, reason string) error

	// ClosePosition closes any open position for the coin at current market price.
	// reason describes why the position is being closed (e.g. "risk_flatten", "exit_spread").
	ClosePosition(ctx context.Context, coin string, reason string) error

	// ReducePositionIOC sends a single limit-IOC order sized to close `fraction`
	// of the current position on the position-reducing side, priced at
	// best-opposite ± slippageBps. Does not retry; if the IOC does not fill
	// (e.g. because liquidity moved) the method returns without error — this
	// is a best-effort protective tool, not a guaranteed-close primitive.
	// fraction is a value in (0, 1]. Returns (size_closed, error) where
	// size_closed is the actual filled size (may be 0 if nothing crossed).
	// V30 part 4: used for fractional profit-taking on favorable price shocks.
	ReducePositionIOC(ctx context.Context, coin string, fraction float64, slippageBps float64, reason string) (float64, error)

	// Position returns the current position state for the coin.
	Position(coin string) types.PositionUpdate

	// PositionUpdates returns a channel that emits position changes.
	PositionUpdates() <-chan types.PositionUpdate

	// Equity returns the current account equity.
	Equity() float64

	// OpenOrders returns the current open orders for a coin.
	OpenOrders(coin string) []types.OrderRequest
}

// CoinHealth captures per-coin execution state that can affect quoting safety.
type CoinHealth struct {
	QuoteHalt          bool
	Reason             string
	LastMismatchAt     time.Time
	MatchingReconciles int
}

// HealthSnapshot captures live execution health for optional quote-manager gating.
type HealthSnapshot struct {
	LastFillPollAt            time.Time
	LastFillPollSuccessAt     time.Time
	FillPollConsecutiveErrors int
	Coins                     map[string]CoinHealth
}

// HealthReporter is implemented by executors that can expose health telemetry.
type HealthReporter interface {
	HealthSnapshot() HealthSnapshot
}
