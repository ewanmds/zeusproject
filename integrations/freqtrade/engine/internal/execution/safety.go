package execution

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/types"
)

type cleanupKey struct{}

var ErrExecutionHalted = errors.New("execution halted: manual restart required")
var ErrQuoteIneligible = errors.New("quote ineligible")

// CleanupContext is used only by the shutdown coordinator, never by quotes.
func CleanupContext(ctx context.Context) context.Context {
	return context.WithValue(ctx, cleanupKey{}, true)
}

func validateCancelAck(response map[string]any) error {
	if response["status"] != "ok" {
		return fmt.Errorf("cancel rejected or invalid response")
	}
	body, ok := response["response"].(map[string]any)
	if !ok {
		return fmt.Errorf("invalid cancel acknowledgement")
	}
	data, ok := body["data"].(map[string]any)
	if !ok {
		return fmt.Errorf("invalid cancel data")
	}
	statuses, ok := data["statuses"].([]any)
	if !ok || len(statuses) != 1 || statuses[0] != "success" {
		return fmt.Errorf("cancel rejected or incomplete acknowledgement")
	}
	return nil
}

func validatePositionState(state map[string]any) error {
	rows, ok := state["assetPositions"].([]any)
	if !ok {
		return fmt.Errorf("invalid account positions")
	}
	for _, raw := range rows {
		row, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("invalid account row")
		}
		pos, ok := row["position"].(map[string]any)
		if !ok {
			return fmt.Errorf("invalid position")
		}
		coin, ok := pos["coin"].(string)
		if !ok || coin == "" {
			return fmt.Errorf("invalid position symbol")
		}
		qty, err := strconv.ParseFloat(fmt.Sprint(pos["szi"]), 64)
		if err != nil || math.IsNaN(qty) || math.IsInf(qty, 0) {
			return fmt.Errorf("invalid position quantity")
		}
	}
	return nil
}

type executionSafety struct {
	action    sync.Mutex
	halted    atomic.Bool
	mu        sync.Mutex
	first     error
	handler   func(error)
	owned     map[int64]string
	cloids    map[string]string
	stopCheck func() bool
}

func (le *LiveExecutor) SetStopCheck(check func() bool) { le.safety.stopCheck = check }
func (le *LiveExecutor) HaltOnRuntimeError(err error) {
	if !le.safety.halted.Load() {
		le.failExecution(err)
	}
}

func (le *LiveExecutor) guardedWorker(name string, run func()) {
	defer func() {
		if value := recover(); value != nil {
			if le.cfg == nil || !le.cfg.ZeusMode {
				panic(value)
			}
			le.failExecution(fmt.Errorf("%s panic: %v", name, value))
		}
	}()
	run()
}

func (le *LiveExecutor) SetFailureHandler(fn func(error)) {
	le.safety.mu.Lock()
	le.safety.handler = fn
	le.safety.mu.Unlock()
}
func (le *LiveExecutor) SafetyError() error {
	le.safety.mu.Lock()
	defer le.safety.mu.Unlock()
	return le.safety.first
}

func (le *LiveExecutor) failExecution(err error) {
	if err == nil || le.cfg == nil || !le.cfg.ZeusMode || errors.Is(err, context.Canceled) || errors.Is(err, ErrRefreshSuperseded) || errors.Is(err, ErrExecutionHalted) || errors.Is(err, ErrQuoteIneligible) {
		return
	}
	le.safety.halted.Store(true)
	le.safety.mu.Lock()
	first := le.safety.first == nil
	if first {
		le.safety.first = err
	}
	handler := le.safety.handler
	le.safety.mu.Unlock()
	if first && handler != nil {
		handler(err)
	}
}

// Quiesce closes the gate before draining the last in-flight SDK operation.
func (le *LiveExecutor) Quiesce() {
	le.safety.halted.Store(true)
	le.safety.action.Lock()
	le.safety.action.Unlock()
}

func (le *LiveExecutor) beginAction(ctx context.Context) (func(), error) {
	if le.cfg == nil || !le.cfg.ZeusMode {
		return func() {}, nil
	}
	le.safety.action.Lock()
	if le.safety.stopCheck != nil && le.safety.stopCheck() {
		le.safety.halted.Store(true)
	}
	if ctx.Err() != nil || le.safety.halted.Load() && ctx.Value(cleanupKey{}) != true {
		le.safety.action.Unlock()
		return nil, ErrExecutionHalted
	}
	return le.safety.action.Unlock, nil
}

func (le *LiveExecutor) rememberOrder(coin string, oid int64) {
	if oid <= 0 {
		return
	}
	le.safety.mu.Lock()
	defer le.safety.mu.Unlock()
	if le.safety.owned == nil {
		le.safety.owned = make(map[int64]string)
	}
	le.safety.owned[oid] = coin
}

func (le *LiveExecutor) newClientID(coin string) string {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		panic(err)
	}
	id := "0x" + hex.EncodeToString(bytes[:])
	le.safety.mu.Lock()
	defer le.safety.mu.Unlock()
	if le.safety.cloids == nil {
		le.safety.cloids = make(map[string]string)
	}
	le.safety.cloids[id] = coin
	return id
}

func (le *LiveExecutor) cancelZeusOrders(ctx context.Context, coin string) error {
	le.safety.mu.Lock()
	owned := make(map[int64]string)
	clients := make(map[string]string)
	for oid, symbol := range le.safety.owned {
		if symbol == coin {
			owned[oid] = symbol
		}
	}
	for id, symbol := range le.safety.cloids {
		if symbol == coin {
			clients[id] = symbol
		}
	}
	le.safety.mu.Unlock()
	rowsBefore, err := le.sdk.Info().FrontendOpenOrders(le.sdk.Address(), hyperliquid.WithDex(le.dexForCoin(coin)))
	if err != nil {
		return err
	}
	for _, raw := range rowsBefore {
		row, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("invalid open order response")
		}
		name, _ := row["coin"].(string)
		if !le.coinMatchesDex(name, coin) {
			continue
		}
		oid, parseErr := strconv.ParseInt(fmt.Sprint(row["oid"]), 10, 64)
		if parseErr != nil {
			return parseErr
		}
		id := fmt.Sprint(row["cloid"])
		_, byOID := owned[oid]
		_, byClient := clients[id]
		if byOID || byClient {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			response, err := le.sdk.Cancel(oid, coin)
			if err == nil {
				err = validateCancelAck(response)
			}
			if err != nil {
				return err
			}
		}
	}

	rows, err := le.sdk.Info().OpenOrders(le.sdk.Address(), hyperliquid.WithDex(le.dexForCoin(coin)))
	if err != nil {
		return err
	}
	for _, raw := range rows {
		order, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("invalid open order response")
		}
		name, ok := order["coin"].(string)
		if !ok {
			return fmt.Errorf("invalid order symbol")
		}
		if le.coinMatchesDex(name, coin) {
			return fmt.Errorf("orders remain on managed market; cleanup not confirmed")
		}
	}
	return nil
}

func (le *LiveExecutor) freshZeusPosition(coin string) (float64, error) {
	state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address(), hyperliquid.WithDex(le.dexForCoin(coin)))
	if err != nil {
		return 0, err
	}
	rows, ok := state["assetPositions"].([]any)
	if !ok {
		return 0, fmt.Errorf("invalid position response")
	}
	for _, raw := range rows {
		row, ok := raw.(map[string]any)
		if !ok {
			return 0, fmt.Errorf("invalid position row")
		}
		pos, ok := row["position"].(map[string]any)
		if !ok {
			return 0, fmt.Errorf("invalid position")
		}
		name, ok := pos["coin"].(string)
		if !ok {
			return 0, fmt.Errorf("invalid position symbol")
		}
		if le.coinMatchesDex(name, coin) {
			qty, err := strconv.ParseFloat(fmt.Sprint(pos["szi"]), 64)
			if err != nil || math.IsNaN(qty) || math.IsInf(qty, 0) {
				return 0, fmt.Errorf("invalid position size")
			}
			return qty, nil
		}
	}
	return 0, nil
}

func (le *LiveExecutor) closeZeusPosition(ctx context.Context, coin string) error {
	if err := le.cancelZeusOrders(ctx, coin); err != nil {
		return err
	}
	for _, delay := range []time.Duration{0, 2 * time.Second, 4 * time.Second} {
		if delay > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}
		qty, err := le.freshZeusPosition(coin)
		if err != nil {
			return err
		}
		if qty == 0 {
			return nil
		}
		le.mu.Lock()
		if qty > 0 {
			le.positions[coin] = &livePosition{side: "long", size: qty}
		} else {
			le.positions[coin] = &livePosition{side: "short", size: -qty}
		}
		le.mu.Unlock()
		le.bookMu.RLock()
		book := le.latestBook[coin]
		le.bookMu.RUnlock()
		if book.Mid <= 0 || time.Since(book.Timestamp) > 5*time.Second {
			return fmt.Errorf("fresh book unavailable for close")
		}
		slip := book.Mid * float64(le.maxCloseSlippageBps) / 10000
		side, price := "sell", book.BestBid-slip
		if qty < 0 {
			side, price = "buy", book.BestAsk+slip
		}
		if price <= 0 {
			return fmt.Errorf("invalid close price")
		}
		// Only the shutdown coordinator is allowed through a halted gate.
		req := types.OrderRequest{Coin: coin, Side: side, Price: types.RoundToTickSize(price), Size: le.roundSizeForCoin(coin, math.Abs(qty)), TimeInForce: "IOC", ReduceOnly: true, Intent: "flatten"}
		_, err = le.placeOrder(ctx, req)
		if err != nil {
			return err
		} // uncertain sends must not be retried
	}
	qty, err := le.freshZeusPosition(coin)
	if err != nil {
		return err
	}
	if qty != 0 {
		return fmt.Errorf("residual position after close")
	}
	return nil
}
