package execution

import (
	"fmt"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/types"
	"math"
	"time"
)

// capZeusOrder rechecks exposure at submission, including pending same-side orders.
func (le *LiveExecutor) capZeusOrder(req types.OrderRequest, replacing int64) (types.OrderRequest, error) {
	if le.cfg == nil || !le.cfg.ZeusMode {
		return req, nil
	}
	if req.Price <= 0 || !isFiniteZeus(req.Price+req.Size) {
		return req, fmt.Errorf("invalid quote")
	}
	le.mu.Lock()
	defer le.mu.Unlock()
	pos := le.positions[req.Coin]
	qty := 0.0
	side := ""
	if pos != nil {
		qty = pos.size
		side = pos.side
	}
	reducing := (side == "long" && req.Side == "sell") || (side == "short" && req.Side == "buy")
	allowed := le.cfg.ForAsset(req.Coin).Risk.MaxPositionNotional / req.Price
	if reducing {
		allowed = qty
		req.ReduceOnly = true
	} else {
		allowed -= qty
		for _, old := range le.liveOrders[req.Coin] {
			if old.OID != replacing && normalizeSide(old.Side) == normalizeSide(req.Side) {
				allowed -= old.Size
			}
		}
	}
	mult := math.Pow10(le.szDecimals[req.Coin])
	req.Size = math.Floor(math.Min(req.Size, math.Max(0, allowed))*mult+1e-9) / mult
	if req.Size <= 0 || (!reducing && req.Size*req.Price < 10) {
		return req, fmt.Errorf("inventory cap or exchange minimum")
	}
	return req, nil
}
func isFiniteZeus(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }

func (le *LiveExecutor) ZeusBook(coin string) types.BookUpdate {
	le.bookMu.RLock()
	defer le.bookMu.RUnlock()
	return le.latestBook[coin]
}

func trackedFromResult(result placeOrderResult, order types.OrderRequest) trackedOrder {
	if result.size > 0 {
		order.Size = result.size
	}
	return trackedFromOrder(result.oid, order)
}

func (le *LiveExecutor) zeusSubmissionResult(coin string, result placeOrderResult, err error) {
	if le.cfg == nil || !le.cfg.ZeusMode {
		return
	}
	if (err == nil && !result.benignReject && result.oid == 0) || (err != nil && (hyperliquid.IsRetryable(err) || hyperliquid.IsErrorCode(err, hyperliquid.ErrorCodeParseError) || hyperliquid.IsErrorCode(err, hyperliquid.ErrorCodeInvalidJSON))) {
		le.markHardMismatch(coin, "ambiguous_order_result", time.Now())
	}
}
