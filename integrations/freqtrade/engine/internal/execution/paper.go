package execution

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// paperOrder is an order in the paper executor's internal book.
type paperOrder struct {
	Coin      string
	Side      string
	Price     float64
	Size      float64
	Reason    string
	WallFront bool
}

// paperPosition tracks the simulated position for one coin.
type paperPosition struct {
	side     string  // "long", "short", or ""
	size     float64 // absolute size
	avgEntry float64
}

// PaperExecutor simulates order execution without touching the real exchange.
type PaperExecutor struct {
	startingEquity float64
	realizedPnL    float64
	totalFees      float64 // cumulative simulated fees

	makerFeeBps   float64 // fee rate per fill
	builderFeeBps float64 // builder fee rate per fill

	bookCh   <-chan types.BookUpdate
	posCh    chan types.PositionUpdate
	lastMid  map[string]float64
	lastBook map[string]types.BookUpdate

	openOrders map[string][]paperOrder   // coin -> orders
	positions  map[string]*paperPosition // coin -> position

	mu sync.Mutex

	onFillCallback func(types.FillEvent)
}

func (pe *PaperExecutor) publishPositionUpdate(pu types.PositionUpdate) {
	select {
	case pe.posCh <- pu:
	default:
		slog.Warn("[PAPER] dropping position update, channel full",
			"coin", pu.Coin, "side", pu.Side, "size", pu.Size)
	}
}

// NewPaperExecutor creates a paper executor.
// bookCh is used for fill simulation: when book prices cross a paper order's price, it fills.
func NewPaperExecutor(startingEquity float64, bookCh <-chan types.BookUpdate, makerFeeBps, builderFeeBps float64) *PaperExecutor {
	return &PaperExecutor{
		startingEquity: startingEquity,
		makerFeeBps:    makerFeeBps,
		builderFeeBps:  builderFeeBps,
		bookCh:         bookCh,
		posCh:          make(chan types.PositionUpdate, 100),
		lastMid:        make(map[string]float64),
		lastBook:       make(map[string]types.BookUpdate),
		openOrders:     make(map[string][]paperOrder),
		positions:      make(map[string]*paperPosition),
	}
}

// SetOnFill sets a callback invoked after each paper fill is processed.
func (pe *PaperExecutor) SetOnFill(fn func(types.FillEvent)) {
	pe.onFillCallback = fn
}

// Init is a no-op for paper executor (no on-chain state to bootstrap).
func (pe *PaperExecutor) Init(ctx context.Context, assets []string) error {
	return nil
}

// Run processes book updates for fill simulation. Blocks until ctx is cancelled.
func (pe *PaperExecutor) Run(ctx context.Context) {
	slog.Info("paper executor started", "starting_equity", pe.startingEquity)

	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("paper executor stopped",
				"realized_pnl", pe.realizedPnL,
				"equity", pe.Equity(),
			)
			return
		case update := <-pe.bookCh:
			pe.handleBookUpdate(update)
		case <-ticker.C:
			pe.logSummary()
		}
	}
}

func (pe *PaperExecutor) handleBookUpdate(update types.BookUpdate) {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	pe.lastMid[update.Coin] = update.Mid
	pe.lastBook[update.Coin] = update

	orders, ok := pe.openOrders[update.Coin]
	if !ok || len(orders) == 0 {
		return
	}

	var remaining []paperOrder
	for _, o := range orders {
		filled := false
		switch o.Side {
		case "buy":
			// Buy limit fills when ask <= order price.
			if update.BestAsk <= o.Price {
				filled = true
			}
		case "sell":
			// Sell limit fills when bid >= order price.
			if update.BestBid >= o.Price {
				filled = true
			}
		}

		if filled {
			slog.Info("[PAPER] FILL",
				"coin", o.Coin,
				"side", o.Side,
				"size", o.Size,
				"price", o.Price,
				"reason", o.Reason,
			)
			pe.applyFillWithMeta(o.Coin, o.Side, o.Size, o.Price, o.WallFront)
		} else {
			remaining = append(remaining, o)
		}
	}
	pe.openOrders[update.Coin] = remaining
}

// applyFill updates the paper position after a fill. Caller must hold pe.mu.
func (pe *PaperExecutor) applyFill(coin, side string, size, price float64) {
	pe.applyFillWithMeta(coin, side, size, price, false)
}

func (pe *PaperExecutor) applyFillWithMeta(coin, side string, size, price float64, wallFront bool) {
	pos, ok := pe.positions[coin]
	if !ok {
		pos = &paperPosition{}
		pe.positions[coin] = pos
	}

	// Compute realized PnL before mutating position state.
	var realizedPnL float64
	var closedSize float64
	isReducing := (pos.side == "long" && side == "sell") || (pos.side == "short" && side == "buy")
	if isReducing && pos.size > 0 {
		closedSize = math.Min(size, pos.size)
		if pos.side == "long" {
			realizedPnL = (price - pos.avgEntry) * closedSize
		} else {
			realizedPnL = (pos.avgEntry - price) * closedSize
		}
	}

	if pos.side == "" || pos.size == 0 {
		// No existing position: open new.
		if side == "buy" {
			pos.side = "long"
		} else {
			pos.side = "short"
		}
		pos.size = size
		pos.avgEntry = price
	} else if (pos.side == "long" && side == "buy") || (pos.side == "short" && side == "sell") {
		// Adding to existing position.
		totalCost := pos.avgEntry*pos.size + price*size
		pos.size += size
		pos.avgEntry = totalCost / pos.size
	} else {
		// Reducing/closing position.
		if size >= pos.size {
			pe.realizedPnL += realizedPnL

			remaining := size - pos.size
			if remaining > 0 {
				// Flip position.
				if side == "buy" {
					pos.side = "long"
				} else {
					pos.side = "short"
				}
				pos.size = remaining
				pos.avgEntry = price
			} else {
				// Flat.
				pos.side = ""
				pos.size = 0
				pos.avgEntry = 0
			}
		} else {
			// Partial close.
			pe.realizedPnL += realizedPnL
			pos.size -= size
		}
	}

	// Deduct simulated fees.
	notional := size * price
	fee := notional * (pe.makerFeeBps + pe.builderFeeBps) / 10000.0
	pe.totalFees += fee
	pe.realizedPnL -= fee

	// Emit position update.
	pu := types.PositionUpdate{
		Coin:     coin,
		Side:     pos.side,
		Size:     pos.size,
		AvgEntry: pos.avgEntry,
		Notional: pos.size * pos.avgEntry,
	}

	pe.publishPositionUpdate(pu)

	if pe.onFillCallback != nil {
		pe.onFillCallback(types.FillEvent{
			Coin:        coin,
			Side:        side,
			Size:        size,
			Price:       price,
			Fee:         fee,
			WallFront:   wallFront,
			RealizedPnL: realizedPnL,
			ClosedSize:  closedSize,
		})
	}
}

// Refresh cancels existing orders for the coin and places new ones.
func (pe *PaperExecutor) Refresh(ctx context.Context, coin string, orders []types.OrderRequest) error {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	// Cancel existing.
	pe.openOrders[coin] = nil

	// Place new.
	for _, o := range orders {
		slog.Info("[PAPER] ORDER",
			"coin", o.Coin,
			"side", o.Side,
			"size", o.Size,
			"price", o.Price,
			"reason", o.Reason,
		)
		pe.openOrders[coin] = append(pe.openOrders[coin], paperOrder{
			Coin:      o.Coin,
			Side:      o.Side,
			Price:     o.Price,
			Size:      o.Size,
			Reason:    o.Reason,
			WallFront: o.WallFront,
		})
	}

	return nil
}

// RefreshMulti delegates to per-coin Refresh for the paper executor.
func (pe *PaperExecutor) RefreshMulti(ctx context.Context, ordersByCoin map[string][]types.OrderRequest) error {
	for coin, orders := range ordersByCoin {
		if err := pe.Refresh(ctx, coin, orders); err != nil {
			return err
		}
	}
	return nil
}

// CancelAll removes all open orders for the coin.
func (pe *PaperExecutor) CancelAll(ctx context.Context, coin string) error {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	count := len(pe.openOrders[coin])
	pe.openOrders[coin] = nil

	slog.Info("[PAPER] CANCEL ALL", "coin", coin, "cancelled", count)
	return nil
}

// CancelSide removes one side of open orders for the coin.
func (pe *PaperExecutor) CancelSide(ctx context.Context, coin string, side string, reason string) error {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	side = normalizeSide(side)
	orders := pe.openOrders[coin]
	if len(orders) == 0 {
		return nil
	}
	kept := orders[:0]
	cancelled := 0
	for _, o := range orders {
		if normalizeSide(o.Side) == side {
			cancelled++
			continue
		}
		kept = append(kept, o)
	}
	pe.openOrders[coin] = kept
	slog.Info("[PAPER] CANCEL SIDE", "coin", coin, "side", side, "cancelled", cancelled, "reason", reason)
	return nil
}

// ClosePosition closes the paper position at the last known mid price.
func (pe *PaperExecutor) ClosePosition(ctx context.Context, coin string, reason string) error {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	pos, ok := pe.positions[coin]
	if !ok || pos.side == "" || pos.size == 0 {
		return nil
	}

	mid := pe.lastMid[coin]
	if mid == 0 {
		mid = pos.avgEntry // fallback
	}

	var pnl float64
	if pos.side == "long" {
		pnl = (mid - pos.avgEntry) * pos.size
	} else {
		pnl = (pos.avgEntry - mid) * pos.size
	}
	pe.realizedPnL += pnl

	slog.Info("[PAPER] CLOSE POSITION",
		"coin", coin,
		"side", pos.side,
		"size", pos.size,
		"entry", pos.avgEntry,
		"exit", mid,
		"pnl", pnl,
		"reason", reason,
	)

	pos.side = ""
	pos.size = 0
	pos.avgEntry = 0

	// Also cancel any open orders for this coin.
	pe.openOrders[coin] = nil

	pu := types.PositionUpdate{
		Coin: coin,
		Side: "",
		Size: 0,
	}

	pe.publishPositionUpdate(pu)

	return nil
}

// ReducePositionIOC simulates a partial close at the last known mid.
// Paper-mode has no book IOC semantics — we just shrink the position by
// `fraction` at mid and record realized PnL on the reduced slice. Matches
// LiveExecutor.ReducePositionIOC signature; fraction and slippage are used
// only for sizing (slippage is ignored because there is no counterparty).
func (pe *PaperExecutor) ReducePositionIOC(ctx context.Context, coin string, fraction float64, slippageBps float64, reason string) (float64, error) {
	if fraction <= 0 || fraction > 1 {
		return 0, fmt.Errorf("reduce fraction out of range: %g", fraction)
	}

	pe.mu.Lock()
	defer pe.mu.Unlock()

	pos, ok := pe.positions[coin]
	if !ok || pos.side == "" || pos.size == 0 {
		return 0, nil
	}

	mid := pe.lastMid[coin]
	if mid == 0 {
		// No book update observed yet for this coin. Previously we fell back
		// to avgEntry, which silently recorded 0 PnL and shrank the position
		// — masking ordering bugs where an IOC fires before any market data.
		// Safer to refuse and log.
		slog.Warn("[PAPER] REDUCE POSITION IOC skipped: no mid available",
			"coin", coin,
			"reason", reason,
		)
		return 0, nil
	}

	reduceSize := pos.size * fraction
	if reduceSize <= 0 {
		return 0, nil
	}

	var pnl float64
	if pos.side == "long" {
		pnl = (mid - pos.avgEntry) * reduceSize
	} else {
		pnl = (pos.avgEntry - mid) * reduceSize
	}
	pe.realizedPnL += pnl

	slog.Info("[PAPER] REDUCE POSITION IOC",
		"coin", coin,
		"side", pos.side,
		"pos_size_before", pos.size,
		"reduce_size", reduceSize,
		"fraction", fraction,
		"entry", pos.avgEntry,
		"exit", mid,
		"pnl", pnl,
		"reason", reason,
	)

	pos.size -= reduceSize
	if pos.size <= 0 {
		pos.side = ""
		pos.size = 0
		pos.avgEntry = 0
	}

	pu := types.PositionUpdate{
		Coin: coin,
		Side: pos.side,
		Size: pos.size,
	}
	pe.publishPositionUpdate(pu)

	return reduceSize, nil
}

// Position returns the current position for the coin.
func (pe *PaperExecutor) Position(coin string) types.PositionUpdate {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	pos, ok := pe.positions[coin]
	if !ok || pos.side == "" {
		return types.PositionUpdate{Coin: coin}
	}

	return types.PositionUpdate{
		Coin:     coin,
		Side:     pos.side,
		Size:     pos.size,
		Notional: pos.size * pos.avgEntry,
		AvgEntry: pos.avgEntry,
	}
}

// PositionUpdates returns the channel for position change notifications.
func (pe *PaperExecutor) PositionUpdates() <-chan types.PositionUpdate {
	return pe.posCh
}

// Equity returns starting equity + realized PnL + unrealized PnL.
func (pe *PaperExecutor) Equity() float64 {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	unrealized := 0.0
	for coin, pos := range pe.positions {
		if pos.side == "" || pos.size == 0 {
			continue
		}
		mid, ok := pe.lastMid[coin]
		if !ok {
			continue
		}
		if pos.side == "long" {
			unrealized += (mid - pos.avgEntry) * pos.size
		} else {
			unrealized += (pos.avgEntry - mid) * pos.size
		}
	}

	return pe.startingEquity + pe.realizedPnL + unrealized
}

// OpenOrders returns the current open paper orders for a coin.
func (pe *PaperExecutor) OpenOrders(coin string) []types.OrderRequest {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	orders := pe.openOrders[coin]
	result := make([]types.OrderRequest, len(orders))
	for i, o := range orders {
		result[i] = types.OrderRequest{
			Coin:      o.Coin,
			Side:      o.Side,
			Price:     o.Price,
			Size:      o.Size,
			WallFront: o.WallFront,
		}
	}
	return result
}

func (pe *PaperExecutor) logSummary() {
	pe.mu.Lock()
	defer pe.mu.Unlock()

	slog.Info("[PAPER] portfolio summary",
		"realized_pnl", pe.realizedPnL,
		"total_fees", pe.totalFees,
		"equity", pe.startingEquity+pe.realizedPnL,
	)

	for coin, pos := range pe.positions {
		if pos.side == "" {
			continue
		}
		mid := pe.lastMid[coin]
		var unrealized float64
		if pos.side == "long" {
			unrealized = (mid - pos.avgEntry) * pos.size
		} else {
			unrealized = (pos.avgEntry - mid) * pos.size
		}
		slog.Info("[PAPER] position",
			"coin", coin,
			"side", pos.side,
			"size", pos.size,
			"entry", pos.avgEntry,
			"mid", mid,
			"unrealized_pnl", unrealized,
		)
	}
}
