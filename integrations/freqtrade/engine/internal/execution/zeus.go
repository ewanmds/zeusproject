package execution

import (
	"fmt"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/types"
	"math"
	"regexp"
	"strconv"
	"strings"
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
		return req, fmt.Errorf("%w: inventory cap or exchange minimum", ErrQuoteIneligible)
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

// Recognize an explicit exchange rejection, never an ambiguous acknowledgement.
var aloRejectPattern = regexp.MustCompile(`^Post only order would have immediately matched(, bbo was [0-9.eE+\-]+@[0-9.eE+\-]+)?\.?( asset=[0-9]+)?$`)

func isExpectedALOReject(order *hyperliquid.PlacedOrder, tif string) bool {
	if order == nil || !order.IsError() || !strings.EqualFold(tif, "ALO") {
		return false
	}
	message := strings.TrimSpace(strings.TrimPrefix(order.Status, "error:"))
	message = strings.TrimPrefix(message, "Error placing new order during modify: ")
	return aloRejectPattern.MatchString(message)
}

// A failed modification can remove the original order. Read both exchange
// orders and inventory before permitting a later refresh; never resend here.
func (le *LiveExecutor) reconcileALOmodify(coin string, oid int64) (placeOrderResult, error) {
	readStarted := time.Now()
	qty, err := le.freshZeusPosition(coin)
	if err != nil {
		return placeOrderResult{}, err
	}
	rows, err := le.sdk.Info().OpenOrders(le.sdk.Address(), hyperliquid.WithDex(le.dexForCoin(coin)))
	if err != nil {
		return placeOrderResult{}, err
	}
	present, err := confirmedOrderPresent(rows, oid)
	if err != nil {
		return placeOrderResult{}, err
	}
	le.mu.Lock()
	if le.lastFillTime[coin].After(readStarted) {
		// A newer fill owns the local position; never overwrite it with an older REST read.
		le.mu.Unlock()
		le.deferQuotes(coin)
		return placeOrderResult{benignReject: true, originalGone: !present}, nil
	}
	if qty == 0 {
		delete(le.positions, coin)
	} else if qty > 0 {
		le.positions[coin] = &livePosition{side: "long", size: qty}
	} else {
		le.positions[coin] = &livePosition{side: "short", size: -qty}
	}
	le.mu.Unlock()
	// Keep accepted quotes in place during a bounded anti-spam cooldown.
	le.deferQuotes(coin)
	return placeOrderResult{benignReject: true, originalGone: !present}, nil
}

func confirmedOrderPresent(rows []any, oid int64) (bool, error) {
	present := false
	for _, raw := range rows {
		row, ok := raw.(map[string]any)
		if !ok {
			return false, fmt.Errorf("invalid open order row")
		}
		id, err := strconv.ParseInt(fmt.Sprint(row["oid"]), 10, 64)
		if err != nil || id <= 0 {
			return false, fmt.Errorf("invalid open order identity")
		}
		if id == oid {
			present = true
		}
	}
	return present, nil
}

// Preserve the exchange reason instead of hiding it behind a generic ack error.
func orderAckError(action string, order *hyperliquid.PlacedOrder) error {
	if order != nil && order.IsError() {
		return fmt.Errorf("%s rejected: %s", action, order.Status)
	}
	return fmt.Errorf("invalid %s acknowledgement", action)
}

func (le *LiveExecutor) deferQuotes(coin string) {
	le.mu.Lock()
	defer le.mu.Unlock()
	if le.quoteRetryAfter == nil {
		le.quoteRetryAfter = make(map[string]time.Time)
	}
	le.quoteRetryAfter[coin] = time.Now().Add(2 * time.Second)
}
func (le *LiveExecutor) quoteDeferred(coin string) bool {
	le.mu.Lock()
	defer le.mu.Unlock()
	return time.Now().Before(le.quoteRetryAfter[coin])
}
