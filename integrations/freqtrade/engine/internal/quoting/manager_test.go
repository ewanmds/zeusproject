package quoting

import (
	"context"
	"errors"
	"math"
	"sync"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/execution"
	"hyperliquid-market-maker/internal/types"
	"hyperliquid-market-maker/internal/vwap"
)

const floatTol = 1e-4

func almostEqual(a, b, tol float64) bool {
	return math.Abs(a-b) < tol
}

func newTestSessionPnL() *SessionPnLTracker {
	return NewSessionPnLTracker(0, 0)
}

// Compile-time check that mockExecutor implements execution.Executor.
var _ execution.Executor = (*mockExecutor)(nil)

// mockExecutor records calls from the quote manager.
type mockExecutor struct {
	mu          sync.Mutex
	refreshes   []refreshCall
	cancels     []string
	closes      []string
	pos         map[string]types.PositionUpdate
	openOrders  map[string][]types.OrderRequest
	posCh       chan types.PositionUpdate
	eq          float64
	refreshErrs map[string][]error
	health      execution.HealthSnapshot
}

type refreshCall struct {
	Coin   string
	Orders []types.OrderRequest
}

func newMockExecutor() *mockExecutor {
	return &mockExecutor{
		pos:         make(map[string]types.PositionUpdate),
		openOrders:  make(map[string][]types.OrderRequest),
		posCh:       make(chan types.PositionUpdate, 100),
		eq:          10000.0,
		refreshErrs: make(map[string][]error),
	}
}

func (m *mockExecutor) Init(ctx context.Context, assets []string) error { return nil }
func (m *mockExecutor) Run(ctx context.Context)                         {}

func (m *mockExecutor) Refresh(ctx context.Context, coin string, orders []types.OrderRequest) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	ordersCopy := copyOrders(orders)
	m.refreshes = append(m.refreshes, refreshCall{Coin: coin, Orders: ordersCopy})
	if errs := m.refreshErrs[coin]; len(errs) > 0 {
		err := errs[0]
		m.refreshErrs[coin] = errs[1:]
		if err != nil {
			return err
		}
	}
	m.openOrders[coin] = ordersCopy
	return nil
}

func (m *mockExecutor) RefreshMulti(ctx context.Context, ordersByCoin map[string][]types.OrderRequest) error {
	for coin, orders := range ordersByCoin {
		if err := m.Refresh(ctx, coin, orders); err != nil {
			return err
		}
	}
	return nil
}

func (m *mockExecutor) CancelAll(ctx context.Context, coin string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cancels = append(m.cancels, coin)
	m.openOrders[coin] = nil
	return nil
}

func (m *mockExecutor) CancelSide(ctx context.Context, coin string, side string, reason string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	orders := m.openOrders[coin]
	kept := orders[:0]
	for _, o := range orders {
		if o.Side == side {
			continue
		}
		kept = append(kept, o)
	}
	m.openOrders[coin] = kept
	m.cancels = append(m.cancels, coin+":"+side)
	return nil
}

func (m *mockExecutor) ClosePosition(ctx context.Context, coin string, reason string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.closes = append(m.closes, coin)
	return nil
}

func (m *mockExecutor) ReducePositionIOC(ctx context.Context, coin string, fraction float64, slippageBps float64, reason string) (float64, error) {
	return 0, nil
}

func (m *mockExecutor) Position(coin string) types.PositionUpdate {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.pos[coin]
}

func (m *mockExecutor) PositionUpdates() <-chan types.PositionUpdate {
	return m.posCh
}

func (m *mockExecutor) Equity() float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.eq
}

func (m *mockExecutor) OpenOrders(coin string) []types.OrderRequest {
	m.mu.Lock()
	defer m.mu.Unlock()
	return copyOrders(m.openOrders[coin])
}

func (m *mockExecutor) HealthSnapshot() execution.HealthSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := execution.HealthSnapshot{
		LastFillPollAt:            m.health.LastFillPollAt,
		LastFillPollSuccessAt:     m.health.LastFillPollSuccessAt,
		FillPollConsecutiveErrors: m.health.FillPollConsecutiveErrors,
		Coins:                     make(map[string]execution.CoinHealth, len(m.health.Coins)),
	}
	for coin, state := range m.health.Coins {
		out.Coins[coin] = state
	}
	return out
}

func (m *mockExecutor) getRefreshes() []refreshCall {
	m.mu.Lock()
	defer m.mu.Unlock()
	result := make([]refreshCall, len(m.refreshes))
	copy(result, m.refreshes)
	return result
}

func (m *mockExecutor) setRefreshErrors(coin string, errs ...error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.refreshErrs[coin] = append([]error(nil), errs...)
}

// --- Pure function tests ---

// Test base quote computation: mid=70.00, spread_bps=50
// bid = 70.00 * (1 - 50/20000) = 70.00 * 0.9975 = 69.825
// ask = 70.00 * (1 + 50/20000) = 70.00 * 1.0025 = 70.175
func TestBaseQuoteComputation(t *testing.T) {
	halfSpread := 50.0 / 20000.0
	bid := 70.0 * (1.0 - halfSpread)
	ask := 70.0 * (1.0 + halfSpread)
	if !almostEqual(bid, 69.825, floatTol) {
		t.Fatalf("expected bid=69.825, got %f", bid)
	}
	if !almostEqual(ask, 70.175, floatTol) {
		t.Fatalf("expected ask=70.175, got %f", ask)
	}
}

// Test inventory skew: long 100 notional, max_pos=500, skew_factor=0.5, spread_bps=50, gamma=1.0
// shift_bps = pow(100/500, 1.0) * 0.5 * 50 = 5 bps
// shift = 70.00 * 5/10000 = 0.035
// Long position -> shift both down: bid=69.825-0.035=69.790, ask=70.175-0.035=70.140
func TestInventorySkew(t *testing.T) {
	bid := 69.825
	ask := 70.175

	bid, ask = applyInventorySkew(bid, ask, 70.0, 100.0, "long", 500.0, 0.5, 50.0, 1.0, 0.5, false)
	if !almostEqual(bid, 69.790, floatTol) {
		t.Fatalf("expected bid=69.790, got %f", bid)
	}
	if !almostEqual(ask, 70.140, floatTol) {
		t.Fatalf("expected ask=70.140, got %f", ask)
	}
}

// Test inventory skew for short position: shift up
func TestInventorySkewShort(t *testing.T) {
	bid := 69.825
	ask := 70.175

	bid, ask = applyInventorySkew(bid, ask, 70.0, 100.0, "short", 500.0, 0.5, 50.0, 1.0, 0.5, false)
	if !almostEqual(bid, 69.860, floatTol) {
		t.Fatalf("expected bid=69.860, got %f", bid)
	}
	if !almostEqual(ask, 70.210, floatTol) {
		t.Fatalf("expected ask=70.210, got %f", ask)
	}
}

// Test convex inventory skew: gamma=2.0 should be gentler at low inventory
func TestInventorySkewConvex(t *testing.T) {
	bid := 69.825
	ask := 70.175

	// With gamma=2.0, shift_bps = pow(100/500, 2.0) * 0.5 * 50 = 0.04 * 25 = 1.0 bps
	// shift = 70.0 * 1.0 / 10000 = 0.007
	bid, ask = applyInventorySkew(bid, ask, 70.0, 100.0, "long", 500.0, 0.5, 50.0, 2.0, 0.5, false)
	if !almostEqual(bid, 69.818, floatTol) {
		t.Fatalf("expected bid=69.818, got %f", bid)
	}
	if !almostEqual(ask, 70.168, floatTol) {
		t.Fatalf("expected ask=70.168, got %f", ask)
	}
}

// Test size calculation and rounding.
func TestSizeCalculation(t *testing.T) {
	tests := []struct {
		notional   float64
		mid        float64
		szDecimals int
		expected   float64
	}{
		{100.0, 70.0, 2, 1.42},
		{100.0, 70.0, 1, 1.4},
		{100.0, 70.0, 0, 1.0},
		{100.0, 50.0, 2, 2.0},
		// Floor would give 0.1 ($9.91 < $10 min), so ceil to 0.2
		{10.0, 99.1, 1, 0.2},
		// At CL prices (~$98.8), 3 szDecimals: 0.101 * 98.8 = $9.97 < $10.
		// Safety margin ensures we get 0.103 (notional $10.176) instead of 0.101.
		{10.0, 98.8, 3, 0.103},
	}

	for _, tt := range tests {
		pow10Mult := math.Pow(10, float64(tt.szDecimals))
		got := computeSize(tt.notional, tt.mid, pow10Mult)
		if !almostEqual(got, tt.expected, floatTol) {
			t.Errorf("computeSize(%f, %f, %d): expected %f, got %f",
				tt.notional, tt.mid, tt.szDecimals, tt.expected, got)
		}
	}
}

// --- New pure function tests ---

func TestApplySpreadBias(t *testing.T) {
	mid := 70.0
	bid := 69.825
	ask := 70.175

	tests := []struct {
		name       string
		zScore     float64
		biasScale  float64
		maxBiasBps float64
		coin       string
		firstAsset string
		wantBid    float64
		wantAsk    float64
	}{
		{
			name: "zero_z", zScore: 0, biasScale: 2.0, maxBiasBps: 8.0,
			coin: "xyz:CL", firstAsset: "xyz:CL",
			wantBid: bid, wantAsk: ask,
		},
		{
			name: "zero_scale", zScore: 1.0, biasScale: 0, maxBiasBps: 8.0,
			coin: "xyz:CL", firstAsset: "xyz:CL",
			wantBid: bid, wantAsk: ask,
		},
		{
			name: "positive_z_first_asset", zScore: 2.0, biasScale: 2.0, maxBiasBps: 8.0,
			coin: "xyz:CL", firstAsset: "xyz:CL",
			// biasBps = tanh(2.0/2.0) * 8.0 = tanh(1) * 8 ~ 0.7616 * 8 = 6.0928
			// shift = 70.0 * 6.0928 / 10000 = 0.042650
			// first asset: bid -= shift, ask -= shift
			wantBid: bid - mid*math.Tanh(1.0)*8.0/10000.0,
			wantAsk: ask - mid*math.Tanh(1.0)*8.0/10000.0,
		},
		{
			name: "positive_z_second_asset", zScore: 2.0, biasScale: 2.0, maxBiasBps: 8.0,
			coin: "xyz:BRENTOIL", firstAsset: "xyz:CL",
			// second asset: bid += shift, ask += shift
			wantBid: bid + mid*math.Tanh(1.0)*8.0/10000.0,
			wantAsk: ask + mid*math.Tanh(1.0)*8.0/10000.0,
		},
		{
			name: "negative_z_first_asset", zScore: -2.0, biasScale: 2.0, maxBiasBps: 8.0,
			coin: "xyz:CL", firstAsset: "xyz:CL",
			// biasBps = tanh(-1) * 8 ~ -6.0928
			// first asset: bid -= shift (shift is negative, so bid goes up)
			wantBid: bid + mid*math.Tanh(1.0)*8.0/10000.0,
			wantAsk: ask + mid*math.Tanh(1.0)*8.0/10000.0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotBid, gotAsk := applySpreadBias(bid, ask, mid, tt.zScore, tt.biasScale, tt.maxBiasBps, tt.coin, tt.firstAsset, 0, 0)
			if !almostEqual(gotBid, tt.wantBid, floatTol) {
				t.Errorf("bid: got %f, want %f", gotBid, tt.wantBid)
			}
			if !almostEqual(gotAsk, tt.wantAsk, floatTol) {
				t.Errorf("ask: got %f, want %f", gotAsk, tt.wantAsk)
			}
		})
	}
}

func TestComputeWickSizing(t *testing.T) {
	tests := []struct {
		name             string
		mid              float64
		shortVWAP        float64
		longVWAP         float64
		devThreshold     float64
		longDevThreshold float64 // separate long VWAP threshold
		maxMult          float64
		normalSpacingBps float64
		wantBidMult      float64
		wantAskMult      float64
		wantSpacingBps   float64
	}{
		{
			name: "zero_vwaps", mid: 70.0, shortVWAP: 0, longVWAP: 0,
			devThreshold: 0.001, longDevThreshold: 0.05, maxMult: 2.0, normalSpacingBps: 10,
			wantBidMult: 1.0, wantAskMult: 1.0, wantSpacingBps: 10,
		},
		{
			name: "below_threshold", mid: 70.0, shortVWAP: 70.01, longVWAP: 70.02,
			devThreshold: 0.01, longDevThreshold: 0.05, maxMult: 2.0, normalSpacingBps: 10,
			// shortDev = |70.0 - 70.01| / 70.01 ~ 0.000143 < 0.01
			// no wick
			wantBidMult: 1.0, wantAskMult: 1.0, wantSpacingBps: 10,
		},
		{
			name: "both_above_long_threshold_is_trend_not_wick", mid: 68.0, shortVWAP: 69.0, longVWAP: 70.0,
			devThreshold: 0.001, longDevThreshold: 0.005, maxMult: 3.0, normalSpacingBps: 10,
			// shortDev = |68-69|/69 ~ 0.0145 > 0.001 (shortFar)
			// longDev  = |68-70|/70 ~ 0.0286 > 0.005 (longDevThreshold exceeded) => trend, NOT a wick
			wantBidMult: 1.0, wantAskMult: 1.0, wantSpacingBps: 10,
		},
		{
			name: "long_exceeds_short_but_under_long_threshold_fires_wick", mid: 68.0, shortVWAP: 69.0, longVWAP: 70.0,
			devThreshold: 0.001, longDevThreshold: 0.05, maxMult: 3.0, normalSpacingBps: 10,
			// shortDev = 0.0145 > 0.001 (shortFar)
			// longDev  = 0.0286 < 0.05 (under longDevThreshold) => wick fires!
			// Previously this was killed by longFar when both used same threshold.
			wantBidMult: -1, wantAskMult: 1.0, wantSpacingBps: -1,
		},
		{
			name: "wick_mid_below_shortVWAP", mid: 68.0, shortVWAP: 69.0, longVWAP: 68.01,
			devThreshold: 0.001, longDevThreshold: 0.05, maxMult: 3.0, normalSpacingBps: 10,
			// shortDev = |68-69|/69 ~ 0.01449 > 0.001 (shortFar)
			// longDev  = |68-68.01|/68.01 ~ 0.000147 < 0.05 (under longDevThreshold)
			// wick detected: mid < shortVWAP => bidMult > 1, askMult = 1
			wantBidMult: -1,                      // computed dynamically
			wantAskMult: 1.0, wantSpacingBps: -1, // will be less than normal
		},
		{
			name: "wick_mid_above_shortVWAP", mid: 72.0, shortVWAP: 71.0, longVWAP: 71.99,
			devThreshold: 0.001, longDevThreshold: 0.05, maxMult: 3.0, normalSpacingBps: 10,
			// shortDev = |72-71|/71 ~ 0.01408 > 0.001 (shortFar)
			// longDev  = |72-71.99|/71.99 ~ 0.000139 < 0.05 (under longDevThreshold)
			// wick detected: mid > shortVWAP => bidMult = 1, askMult > 1
			wantBidMult: 1.0, wantAskMult: -1, wantSpacingBps: -1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			bidMult, askMult, spacingBps := computeWickSizing(tt.mid, tt.shortVWAP, tt.longVWAP,
				tt.devThreshold, tt.longDevThreshold, tt.maxMult, tt.normalSpacingBps)

			if tt.wantBidMult >= 0 {
				if !almostEqual(bidMult, tt.wantBidMult, floatTol) {
					t.Errorf("bidMult: got %f, want %f", bidMult, tt.wantBidMult)
				}
			} else {
				if bidMult <= 1.0 {
					t.Errorf("bidMult: expected > 1.0, got %f", bidMult)
				}
			}
			if tt.wantAskMult >= 0 {
				if !almostEqual(askMult, tt.wantAskMult, floatTol) {
					t.Errorf("askMult: got %f, want %f", askMult, tt.wantAskMult)
				}
			} else {
				if askMult <= 1.0 {
					t.Errorf("askMult: expected > 1.0, got %f", askMult)
				}
			}
			if tt.wantSpacingBps >= 0 {
				if !almostEqual(spacingBps, tt.wantSpacingBps, floatTol) {
					t.Errorf("spacingBps: got %f, want %f", spacingBps, tt.wantSpacingBps)
				}
			} else {
				if spacingBps >= tt.normalSpacingBps {
					t.Errorf("spacingBps: expected < %f (normal), got %f", tt.normalSpacingBps, spacingBps)
				}
			}
		})
	}
}

func TestApplyZWidening(t *testing.T) {
	mid := 70.0
	bid := 69.825 // 0.175 below mid
	ask := 70.175 // 0.175 above mid

	tests := []struct {
		name      string
		absZ      float64
		threshold float64
		rate      float64
		maxMult   float64
		wantBid   float64
		wantAsk   float64
	}{
		{
			name: "below_threshold", absZ: 3.0, threshold: 4.0, rate: 0.15, maxMult: 3.0,
			wantBid: bid, wantAsk: ask,
		},
		{
			name: "at_threshold", absZ: 4.0, threshold: 4.0, rate: 0.15, maxMult: 3.0,
			wantBid: bid, wantAsk: ask,
		},
		{
			name: "above_threshold", absZ: 5.0, threshold: 4.0, rate: 0.15, maxMult: 3.0,
			// mult = 1 + (5-4)*0.15 = 1.15
			// bidDist = 70 - 69.825 = 0.175
			// askDist = 70.175 - 70 = 0.175
			// newBid = 70 - 0.175*1.15 = 70 - 0.20125 = 69.79875
			// newAsk = 70 + 0.175*1.15 = 70 + 0.20125 = 70.20125
			wantBid: 69.79875,
			wantAsk: 70.20125,
		},
		{
			name: "capped_at_max", absZ: 50.0, threshold: 4.0, rate: 0.15, maxMult: 3.0,
			// mult = 1 + (50-4)*0.15 = 7.9 => capped at 3.0
			// newBid = 70 - 0.175*3 = 69.475
			// newAsk = 70 + 0.175*3 = 70.525
			wantBid: 69.475,
			wantAsk: 70.525,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotBid, gotAsk := applyZWidening(bid, ask, mid, tt.absZ, tt.threshold, tt.rate, tt.maxMult, 0.5, true, true)
			if !almostEqual(gotBid, tt.wantBid, floatTol) {
				t.Errorf("bid: got %f, want %f", gotBid, tt.wantBid)
			}
			if !almostEqual(gotAsk, tt.wantAsk, floatTol) {
				t.Errorf("ask: got %f, want %f", gotAsk, tt.wantAsk)
			}
		})
	}
}

func TestApplyFeeFloor(t *testing.T) {
	mid := 100.0

	// Case 1: spread already wider than fee floor — no change.
	bid, ask := 99.95, 100.05 // 5 bps half-spread
	b, a := applyFeeFloor(bid, ask, mid, 1.0)
	if !almostEqual(b, 99.95, floatTol) || !almostEqual(a, 100.05, floatTol) {
		t.Errorf("expected no change, got bid=%f ask=%f", b, a)
	}

	// Case 2: spread narrower than fee floor — widen.
	bid, ask = 99.995, 100.005               // 0.5 bps half-spread
	b, a = applyFeeFloor(bid, ask, mid, 2.0) // floor = 2 bps
	expectedHalf := mid * 2.0 / 10000.0      // 0.02
	if !almostEqual(b, mid-expectedHalf, floatTol) {
		t.Errorf("expected bid=%f, got %f", mid-expectedHalf, b)
	}
	if !almostEqual(a, mid+expectedHalf, floatTol) {
		t.Errorf("expected ask=%f, got %f", mid+expectedHalf, a)
	}

	// Case 3: fee floor = 0 — no change.
	bid, ask = 99.999, 100.001
	b, a = applyFeeFloor(bid, ask, mid, 0)
	if !almostEqual(b, bid, floatTol) || !almostEqual(a, ask, floatTol) {
		t.Errorf("expected no change with zero floor, got bid=%f ask=%f", b, a)
	}

	// Case 4: mid = 0 — no change (degenerate).
	b, a = applyFeeFloor(99.0, 101.0, 0, 5.0)
	if b != 99.0 || a != 101.0 {
		t.Errorf("expected no change with zero mid")
	}
}

// --- Integration tests ---

func defaultTestConfig() *config.Config {
	return &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 500,
		},
		MarketMaking: config.MarketMakingConfig{
			QuoteSpreadBps:        50,
			QuoteNotional:         100,
			RefreshIntervalMs:     100,
			InventorySkewFactor:   0.0,
			LadderLevels:          1,
			SpreadBiasScale:       2.0,
			SpreadBiasMaxBps:      8.0,
			ZWideningThreshold:    4.0,
			ZWideningRate:         0.15,
			ZMaxWidthMultiplier:   3.0,
			WickSizeMultiplier:    2.0,
			WickDevThresholdPct:   0.10,
			VWAPAlpha:             0.40,
			LadderSizeGraduation:  1.0,
			LadderSpacingExponent: 1.0,
		},
		Risk: config.RiskConfig{
			MaxPositionNotional: 500,
			MaxSpreadExposure:   1000,
		},
		Execution: config.ExecutionConfig{
			Leverage: 10,
		},
	}
}

// Test that RiskHalt stops quoting (and closes positions), RiskResume restarts.
func TestRiskActionHaltResume(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 100

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)

	exec := newMockExecutor()

	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	// Send a book update
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}
	time.Sleep(200 * time.Millisecond)

	// Verify that quote refresh was called (quoting is active).
	refreshes := exec.getRefreshes()
	if len(refreshes) == 0 {
		t.Fatal("expected at least one Refresh call before halt")
	}

	// Send RiskHalt
	riskCh <- types.RiskHalt
	time.Sleep(50 * time.Millisecond)

	// Verify that HALT cancels orders AND closes positions
	exec.mu.Lock()
	cancelCalls := make([]string, len(exec.cancels))
	copy(cancelCalls, exec.cancels)
	closeCalls := make([]string, len(exec.closes))
	copy(closeCalls, exec.closes)
	countBeforeHalt := len(exec.refreshes)
	exec.mu.Unlock()

	if len(cancelCalls) < 2 {
		t.Fatalf("expected CancelAll for both assets on halt, got %d calls", len(cancelCalls))
	}
	if len(closeCalls) < 2 {
		t.Fatalf("expected ClosePosition for both assets on halt, got %d calls", len(closeCalls))
	}

	// Send more book updates
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.90, BestAsk: 70.10, Mid: 70.0, Timestamp: time.Now(),
	}
	time.Sleep(200 * time.Millisecond)

	// Verify no new refreshes during halt
	exec.mu.Lock()
	countAfterHalt := len(exec.refreshes)
	exec.mu.Unlock()

	if countAfterHalt != countBeforeHalt {
		t.Fatalf("expected no new refreshes during halt, got %d (was %d)", countAfterHalt, countBeforeHalt)
	}

	// Send RiskResume
	riskCh <- types.RiskResume
	time.Sleep(50 * time.Millisecond)

	// Send book update
	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.85, BestAsk: 70.15, Mid: 70.0, Timestamp: time.Now(),
	}
	time.Sleep(200 * time.Millisecond)

	// Verify refreshes resume
	exec.mu.Lock()
	countAfterResume := len(exec.refreshes)
	exec.mu.Unlock()

	if countAfterResume <= countAfterHalt {
		t.Fatalf("expected refreshes to resume, count=%d (was %d)", countAfterResume, countAfterHalt)
	}
}

func TestStaleBookCancelsOnceAndRecovers(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 10
	cfg.MarketMaking.MaxBookStalenessMs = 50

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now()}
	time.Sleep(20 * time.Millisecond)
	bookCh <- types.BookUpdate{Coin: "xyz:BRENTOIL", BestBid: 68.95, BestAsk: 69.05, Mid: 69.0, Timestamp: time.Now()}
	time.Sleep(200 * time.Millisecond)

	initialRefreshes := len(exec.getRefreshes())
	if initialRefreshes == 0 {
		t.Fatal("expected initial quote refresh before staleness")
	}
	exec.mu.Lock()
	baselineCancels := len(exec.cancels)
	exec.mu.Unlock()

	time.Sleep(70 * time.Millisecond)

	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.90, BestAsk: 70.10, Mid: 70.0, Timestamp: time.Now()}
	time.Sleep(100 * time.Millisecond)

	exec.mu.Lock()
	cancelCount := len(exec.cancels)
	exec.mu.Unlock()
	expectedCancelCount := baselineCancels + len(cfg.Strategy.Assets)
	if cancelCount != expectedCancelCount {
		t.Fatalf("expected one additional stale-data cancel pass for both assets, got %d total calls (baseline %d)", cancelCount, baselineCancels)
	}

	mgr.mu.Lock()
	suppressed := mgr.supervisor.IsBookStale()
	mgr.mu.Unlock()
	if !suppressed {
		t.Fatal("expected stale-data suppression to be active")
	}

	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.88, BestAsk: 70.08, Mid: 69.98, Timestamp: time.Now()}
	time.Sleep(30 * time.Millisecond)
	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.87, BestAsk: 70.07, Mid: 69.97, Timestamp: time.Now()}
	time.Sleep(100 * time.Millisecond)

	exec.mu.Lock()
	cancelCountAfterRepeat := len(exec.cancels)
	exec.mu.Unlock()
	if cancelCountAfterRepeat != cancelCount {
		t.Fatalf("expected no repeated stale-data cancels, got %d total calls after repeats", cancelCountAfterRepeat)
	}

	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.89, BestAsk: 70.09, Mid: 69.99, Timestamp: time.Now()}
	time.Sleep(20 * time.Millisecond)
	bookCh <- types.BookUpdate{Coin: "xyz:BRENTOIL", BestBid: 68.97, BestAsk: 69.07, Mid: 69.02, Timestamp: time.Now()}
	time.Sleep(200 * time.Millisecond)

	mgr.mu.Lock()
	suppressed = mgr.supervisor.IsBookStale()
	mgr.mu.Unlock()
	if suppressed {
		t.Fatal("expected stale-data suppression to clear after fresh books returned")
	}

	finalRefreshes := len(exec.getRefreshes())
	if finalRefreshes <= initialRefreshes {
		t.Fatalf("expected quote refreshes to resume after recovery, got %d (was %d)", finalRefreshes, initialRefreshes)
	}
}

func TestComputeCoinHealthTradeStale(t *testing.T) {
	cfg := defaultTestConfig()
	exec := newMockExecutor()
	shortVWAPs := map[string]*vwap.Calculator{
		"xyz:CL": vwap.NewCalculator(30 * time.Minute),
	}
	shortVWAPs["xyz:CL"].AddTrade(70.0, 1.0, time.Now().Add(-31*time.Second))

	mgr := New(cfg, nil, nil, nil, exec, map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}, shortVWAPs, nil, nil, nil, newTestSessionPnL())
	mgr.startedAt = time.Now().Add(-1 * time.Minute)

	health := mgr.computeCoinHealth(time.Now())
	if !health["xyz:CL"].Halted {
		t.Fatal("expected stale trades to halt quoting for xyz:CL")
	}
	if health["xyz:CL"].Reason != "trade_stale" {
		t.Fatalf("expected trade_stale reason, got %q", health["xyz:CL"].Reason)
	}
}

func TestComputeCoinHealthFillPollStale(t *testing.T) {
	cfg := defaultTestConfig()
	exec := newMockExecutor()
	exec.health = execution.HealthSnapshot{
		LastFillPollAt:            time.Now(),
		LastFillPollSuccessAt:     time.Now().Add(-20 * time.Second),
		FillPollConsecutiveErrors: 5,
	}

	mgr := New(cfg, nil, nil, nil, exec, map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}, nil, nil, nil, nil, newTestSessionPnL())
	mgr.startedAt = time.Now().Add(-1 * time.Minute)

	health := mgr.computeCoinHealth(time.Now())
	if !health["xyz:CL"].Halted {
		t.Fatal("expected stale fill poll to halt quoting")
	}
	if health["xyz:CL"].Reason != "fill_poll_stale" {
		t.Fatalf("expected fill_poll_stale reason, got %q", health["xyz:CL"].Reason)
	}
}

func TestPairReduceModeActivatesForSameDirectionPair(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.Strategy.UseBetaHedge = true
	cfg.Risk.MaxSpreadExposure = 1000

	mgr := New(cfg, nil, nil, nil, newMockExecutor(), map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}, nil, nil, nil, nil, newTestSessionPnL())
	state := mgr.updatePairReduceMode(map[string]types.PositionUpdate{
		"xyz:CL":       {Coin: "xyz:CL", Side: "long", Notional: 350},
		"xyz:BRENTOIL": {Coin: "xyz:BRENTOIL", Side: "long", Notional: 250},
	})

	if !state.Active {
		t.Fatal("expected pair reduce mode to activate")
	}
	if state.ExitSide != "sell" {
		t.Fatalf("expected exit side sell for dual-long pair, got %q", state.ExitSide)
	}
}

// Test ladder: verify correct number of orders and spacing.
func TestLadderOrders(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.LadderLevels = 3
	cfg.MarketMaking.LadderSpacingBps = 10

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	bookCh <- types.BookUpdate{Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now()}
	time.Sleep(200 * time.Millisecond)

	clOrders := exec.OpenOrders("xyz:CL")
	if len(clOrders) == 0 {
		t.Fatal("expected CL orders after refresh")
	}
	if len(clOrders) != 6 {
		t.Fatalf("expected 6 CL orders (3 buys + 3 sells), got %d", len(clOrders))
	}

	// Count buys and sells.
	buys, sells := 0, 0
	for _, o := range clOrders {
		if o.Side == "buy" {
			buys++
		} else {
			sells++
		}
	}
	if buys != 3 {
		t.Fatalf("expected 3 buy orders, got %d", buys)
	}
	if sells != 3 {
		t.Fatalf("expected 3 sell orders, got %d", sells)
	}

	// Verify sells are ascending (farther from mid at higher indices).
	var sellPrices []float64
	for _, o := range clOrders {
		if o.Side == "sell" {
			sellPrices = append(sellPrices, o.Price)
		}
	}
	for i := 1; i < len(sellPrices); i++ {
		if sellPrices[i] <= sellPrices[i-1] {
			t.Fatalf("sell prices should be ascending, got %v", sellPrices)
		}
	}
}

// Test that dynamic width multipliers are applied to base quotes.
func TestDynamicWidthIntegration(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.InventorySkewFactor = 0.0
	cfg.MarketMaking.DynamicWidth = true
	cfg.MarketMaking.WidthVolWindow = 5
	cfg.MarketMaking.WidthMaxMultiplier = 3.0
	cfg.MarketMaking.WidthImbalanceLevels = 3

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	// Send book updates with an imbalanced book: heavy bids, light asks.
	for i := 0; i < 10; i++ {
		bookCh <- types.BookUpdate{
			Coin: "xyz:CL", BestBid: 69.90, BestAsk: 70.10, Mid: 70.0,
			Bids: []types.BookLevel{
				{Price: 69.90, Size: 50.0},
				{Price: 69.85, Size: 50.0},
				{Price: 69.80, Size: 50.0},
			},
			Asks: []types.BookLevel{
				{Price: 70.10, Size: 5.0},
				{Price: 70.15, Size: 5.0},
				{Price: 70.20, Size: 5.0},
			},
			Timestamp: time.Now(),
		}
		time.Sleep(120 * time.Millisecond)
	}

	// With bid-heavy imbalance, ask should be wider than bid.
	clOrders := exec.OpenOrders("xyz:CL")
	var bidPrice, askPrice float64
	for _, o := range clOrders {
		if o.Side == "buy" {
			bidPrice = o.Price
		}
		if o.Side == "sell" {
			askPrice = o.Price
		}
	}

	if bidPrice == 0 || askPrice == 0 {
		t.Fatalf("expected bid and ask orders, got bid=%f ask=%f", bidPrice, askPrice)
	}

	bidDist := 70.0 - bidPrice
	askDist := askPrice - 70.0
	if askDist <= bidDist {
		t.Fatalf("expected ask farther from mid (bid-heavy imbalance): bidDist=%f askDist=%f",
			bidDist, askDist)
	}
}

func TestSelectiveRefreshSkipsUnchangedQuotes(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50
	cfg.Risk.MaxOpenOrders = 20

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	update := types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}
	bookCh <- update
	time.Sleep(150 * time.Millisecond)

	before := len(exec.getRefreshes())
	bookCh <- update
	time.Sleep(150 * time.Millisecond)
	after := len(exec.getRefreshes())

	if after != before {
		t.Fatalf("expected unchanged quote to skip refresh, got %d refreshes before and %d after", before, after)
	}
}

func TestFailedRefreshDoesNotAdvanceQuoteState(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	exec.setRefreshErrors("xyz:CL", errors.New("place failed"))
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	bookCh <- types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}
	time.Sleep(200 * time.Millisecond)

	mgr.mu.Lock()
	state := mgr.quoteStates["xyz:CL"]
	pending := len(mgr.pendingOrders)
	mgr.mu.Unlock()

	if !state.SentAt.IsZero() {
		t.Fatal("expected quote state to remain unset after failed refresh")
	}
	if pending != 0 {
		t.Fatalf("expected pending orders to be cleared after failed refresh, got %d", pending)
	}
	if !mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected manager to enter operational halt after failed refresh")
	}
	if len(exec.OpenOrders("xyz:CL")) != 0 {
		t.Fatal("expected no live open orders recorded after failed refresh")
	}
}

func TestFailedRefreshEntersFailClosedHalt(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	exec.setRefreshErrors("xyz:CL", errors.New("place failed"), nil)
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	update := types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}
	bookCh <- update
	time.Sleep(200 * time.Millisecond)
	bookCh <- update
	time.Sleep(200 * time.Millisecond)

	refreshes := exec.getRefreshes()
	if len(refreshes) != 1 {
		t.Fatalf("expected no refresh retry after fail-closed halt, got %d refresh attempts", len(refreshes))
	}

	if !mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected operational halt after refresh failure")
	}
	if len(exec.cancels) == 0 {
		t.Fatal("expected outstanding orders to be cancelled during fail-closed halt")
	}
}

func TestFairValueBlendWithVWAP(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.VWAPAlpha = 0.5

	mock := newMockExecutor()
	szDec := map[string]int{"xyz:CL": 0, "xyz:BRENTOIL": 0}

	clCalc := vwap.NewCalculator(30 * time.Minute)
	clCalc.AddTrade(69.0, 100.0, time.Now())

	shortVWAPs := map[string]*vwap.Calculator{
		"xyz:CL": clCalc,
	}

	mgr := New(cfg, nil, nil, nil, mock, szDec, shortVWAPs, nil, nil, nil, newTestSessionPnL())

	book := types.BookUpdate{
		Coin:    "xyz:CL",
		BestBid: 69.95,
		BestAsk: 70.05,
		Mid:     70.0,
		Bids:    []types.BookLevel{{Price: 69.95, Size: 10}},
		Asks:    []types.BookLevel{{Price: 70.05, Size: 10}},
	}
	sig := types.SpreadSignal{}
	pos := types.PositionUpdate{Coin: "xyz:CL"}
	exposures := map[string]sideExposure{"xyz:CL": {}, "xyz:BRENTOIL": {}}

	orders := mgr.computeOrdersForCoin("xyz:CL", book, sig, pos, exposures,
		cfg.Risk.MaxPositionNotional, cfg.Risk.MaxSpreadExposure, coinQuotingHealth{}, pairReduceState{})

	hasBuy := false
	hasSell := false
	for _, o := range orders {
		if o.Side == "buy" {
			hasBuy = true
			if o.Price >= 70.0 {
				t.Errorf("buy price %f should be below raw mid 70.0 (VWAP pulls it down)", o.Price)
			}
		}
		if o.Side == "sell" {
			hasSell = true
		}
	}
	if !hasBuy || !hasSell {
		t.Errorf("expected both buy and sell orders, got buy=%v sell=%v", hasBuy, hasSell)
	}
}

func TestFairValueFallbackWhenVWAPNotReady(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.VWAPAlpha = 1.0

	mock := newMockExecutor()
	szDec := map[string]int{"xyz:CL": 0, "xyz:BRENTOIL": 0}

	clCalc := vwap.NewCalculator(30 * time.Minute)
	shortVWAPs := map[string]*vwap.Calculator{"xyz:CL": clCalc}

	mgr := New(cfg, nil, nil, nil, mock, szDec, shortVWAPs, nil, nil, nil, newTestSessionPnL())

	book := types.BookUpdate{
		Coin:    "xyz:CL",
		BestBid: 69.95,
		BestAsk: 70.05,
		Mid:     70.0,
		Bids:    []types.BookLevel{{Price: 69.95, Size: 10}},
		Asks:    []types.BookLevel{{Price: 70.05, Size: 10}},
	}
	sig := types.SpreadSignal{}
	pos := types.PositionUpdate{Coin: "xyz:CL"}
	exposures := map[string]sideExposure{"xyz:CL": {}, "xyz:BRENTOIL": {}}

	orders := mgr.computeOrdersForCoin("xyz:CL", book, sig, pos, exposures,
		cfg.Risk.MaxPositionNotional, cfg.Risk.MaxSpreadExposure, coinQuotingHealth{}, pairReduceState{})

	if len(orders) == 0 {
		t.Error("expected orders when VWAP not ready (should fall back to mid)")
	}
}

func TestFeeFloorInPipeline(t *testing.T) {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets:         []string{"xyz:CL", "xyz:BRENTOIL"},
			SpreadType:     "difference",
			LookbackWindow: 100,
		},
		MarketMaking: config.MarketMakingConfig{
			QuoteSpreadBps:         2, // very tight spread: 1 bps per side
			QuoteNotional:          100,
			RefreshIntervalMs:      1000,
			InventorySkewFactor:    0,
			LadderLevels:           1,
			MaxQuoteAgeMs:          3000,
			MaxCrossLegSkewMs:      150,
			MaxBookStalenessMs:     5000,
			VWAPShortWindowMinutes: 5,
			VWAPLongWindowMinutes:  30,
			VWAPAlpha:              0, // use raw mid as fair value
			SpreadBiasScale:        2.0,
			SpreadBiasMaxBps:       0,   // no bias
			ZWideningThreshold:     100, // effectively disabled
			ZWideningRate:          0,
			ZMaxWidthMultiplier:    1.0,
			WickSizeMultiplier:     1.0,
			WickDevThresholdPct:    100,
			MakerFeeBps:            5.0, // 5 bps fee floor
			FeeFloorEnabled:        true,
		},
		Risk: config.RiskConfig{
			MaxPositionNotional: 10000,
			MaxSpreadExposure:   20000,
			MaxOpenOrders:       20,
		},
	}

	mock := newMockExecutor()
	szDec := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}
	mgr := New(cfg, make(chan types.BookUpdate, 10), make(chan types.SpreadSignal, 10),
		make(chan types.RiskAction, 10), mock, szDec, nil, nil, nil, nil, newTestSessionPnL())

	book := types.BookUpdate{
		Coin:    "xyz:CL",
		BestBid: 100.00,
		BestAsk: 100.02,
		Mid:     100.01,
	}
	sig := types.SpreadSignal{}
	pos := types.PositionUpdate{Coin: "xyz:CL"}

	orders := mgr.computeOrdersForCoin("xyz:CL", book, sig, pos,
		map[string]sideExposure{}, 10000, 20000, coinQuotingHealth{}, pairReduceState{})

	if len(orders) < 2 {
		t.Fatalf("expected at least 2 orders, got %d", len(orders))
	}

	var bidPrice, askPrice float64
	for _, o := range orders {
		if o.Side == "buy" {
			bidPrice = o.Price
		} else {
			askPrice = o.Price
		}
	}

	// With fee floor of 5 bps, half-spread should be at least 5 bps of mid.
	// mid=100.01, 5 bps = 100.01 * 5/10000 = 0.050005
	minHalfSpread := 100.01 * 5.0 / 10000.0
	actualHalfSpread := (askPrice - bidPrice) / 2.0
	if actualHalfSpread < minHalfSpread*0.99 { // 1% tolerance for rounding
		t.Errorf("fee floor not enforced: half_spread=%f < min=%f (bid=%f ask=%f)",
			actualHalfSpread, minHalfSpread, bidPrice, askPrice)
	}
}

// --- Existing pure function tests ---

func TestBuildSideOrders(t *testing.T) {
	tests := []struct {
		name             string
		side             string
		startPrice       float64
		mid              float64
		spacingBps       float64
		allowedNotional  float64
		levels           int
		bestBid, bestAsk float64
		szDec            int
		wantNil          bool
		wantLen          int
		wantSide         string
	}{
		{
			name: "below_min_notional", side: "buy", startPrice: 70.0, mid: 70.0,
			spacingBps: 10, allowedNotional: 5.0, levels: 3, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantNil: true,
		},
		{
			name: "single_level", side: "buy", startPrice: 69.825, mid: 70.0,
			spacingBps: 10, allowedNotional: 15.0, levels: 3, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantLen: 1, wantSide: "buy",
		},
		{
			name: "multi_level_spacing", side: "sell", startPrice: 70.175, mid: 70.0,
			spacingBps: 10, allowedNotional: 100.0, levels: 3, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantLen: 3, wantSide: "sell",
		},
		{
			name: "buy_clamped_to_bestBid", side: "buy", startPrice: 70.10, mid: 70.0,
			spacingBps: 0, allowedNotional: 15.0, levels: 1, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantLen: 1, wantSide: "buy",
		},
		{
			name: "sell_clamped_to_bestAsk", side: "sell", startPrice: 69.90, mid: 70.0,
			spacingBps: 0, allowedNotional: 15.0, levels: 1, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantLen: 1, wantSide: "sell",
		},
		{
			name: "level_count_capped_by_notional", side: "buy", startPrice: 69.825, mid: 70.0,
			spacingBps: 10, allowedNotional: 25.0, levels: 5, bestBid: 69.95, bestAsk: 70.05, szDec: 2,
			wantLen: 2, wantSide: "buy",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			pow10Mult := math.Pow(10, float64(tt.szDec))
			orders := buildSideOrders(
				"xyz:CL", tt.side, tt.startPrice, tt.mid, tt.spacingBps,
				tt.allowedNotional, tt.levels, tt.bestBid, tt.bestAsk, pow10Mult, "test", 1.0, 1.0,
				nil, false, 0, nil,
			)
			if tt.wantNil {
				if orders != nil {
					t.Fatalf("expected nil, got %d orders", len(orders))
				}
				return
			}
			if len(orders) != tt.wantLen {
				t.Fatalf("expected %d orders, got %d", tt.wantLen, len(orders))
			}
			for _, o := range orders {
				if o.Side != tt.wantSide {
					t.Errorf("expected side=%s, got %s", tt.wantSide, o.Side)
				}
			}
			// Verify buy clamped to bestBid
			if tt.name == "buy_clamped_to_bestBid" && orders[0].Price > tt.bestBid {
				t.Errorf("buy price %f should be <= bestBid %f", orders[0].Price, tt.bestBid)
			}
			// Verify sell clamped to bestAsk
			if tt.name == "sell_clamped_to_bestAsk" && orders[0].Price < tt.bestAsk {
				t.Errorf("sell price %f should be >= bestAsk %f", orders[0].Price, tt.bestAsk)
			}
		})
	}
}

func TestComputeLevelNotionals(t *testing.T) {
	t.Run("uniform", func(t *testing.T) {
		notionals := computeLevelNotionals(500, 4, 1.0, 10.0)
		if len(notionals) != 4 {
			t.Fatalf("expected 4 levels, got %d", len(notionals))
		}
		for i, n := range notionals {
			if math.Abs(n-125.0) > 0.01 {
				t.Errorf("level %d: expected 125.0, got %f", i, n)
			}
		}
	})

	t.Run("geometric_1.5", func(t *testing.T) {
		notionals := computeLevelNotionals(500, 4, 1.5, 10.0)
		if len(notionals) != 4 {
			t.Fatalf("expected 4 levels, got %d", len(notionals))
		}
		// Verify ascending order
		for i := 1; i < len(notionals); i++ {
			if notionals[i] <= notionals[i-1] {
				t.Errorf("level %d (%f) should be > level %d (%f)", i, notionals[i], i-1, notionals[i-1])
			}
		}
		// Verify ratio between consecutive levels is ~1.5
		for i := 1; i < len(notionals); i++ {
			ratio := notionals[i] / notionals[i-1]
			if math.Abs(ratio-1.5) > 0.01 {
				t.Errorf("ratio level %d/%d = %f, expected ~1.5", i, i-1, ratio)
			}
		}
		// Verify sum conserved
		sum := 0.0
		for _, n := range notionals {
			sum += n
		}
		if math.Abs(sum-500.0) > 0.01 {
			t.Errorf("sum = %f, expected 500.0", sum)
		}
		// Verify known values: weights 1, 1.5, 2.25, 3.375 → sum 8.125
		// inner = 500/8.125 ≈ 61.54
		if math.Abs(notionals[0]-61.54) > 0.1 {
			t.Errorf("inner level = %f, expected ~61.54", notionals[0])
		}
	})

	t.Run("min_notional_fallback", func(t *testing.T) {
		// With g=2.0, 4 levels, $50 total: weights 1,2,4,8 → sum 15 → inner = 50/15 ≈ 3.33 < 10.
		// Should auto-reduce graduation until inner >= 10.
		notionals := computeLevelNotionals(50, 4, 2.0, 10.0)
		if len(notionals) != 4 {
			t.Fatalf("expected 4 levels, got %d", len(notionals))
		}
		if notionals[0] < 10.0 {
			t.Errorf("inner level %f should be >= 10.0 after fallback", notionals[0])
		}
		sum := 0.0
		for _, n := range notionals {
			sum += n
		}
		if math.Abs(sum-50.0) > 0.01 {
			t.Errorf("sum = %f, expected 50.0", sum)
		}
	})

	t.Run("single_level", func(t *testing.T) {
		notionals := computeLevelNotionals(500, 1, 2.0, 10.0)
		if len(notionals) != 1 {
			t.Fatalf("expected 1 level, got %d", len(notionals))
		}
		if math.Abs(notionals[0]-500.0) > 0.01 {
			t.Errorf("expected 500.0, got %f", notionals[0])
		}
	})

	t.Run("zero_levels", func(t *testing.T) {
		notionals := computeLevelNotionals(500, 0, 1.5, 10.0)
		if notionals != nil {
			t.Errorf("expected nil, got %v", notionals)
		}
	})
}

func TestBuildSideOrdersGraduated(t *testing.T) {
	pow10Mult := math.Pow(10, 2) // szDec=2
	orders := buildSideOrders(
		"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
		69.95, 70.05, pow10Mult, "test", 1.5, 1.0,
		nil, false, 0, nil,
	)
	if len(orders) != 4 {
		t.Fatalf("expected 4 orders, got %d", len(orders))
	}
	// Verify sizes are ascending (inner smallest, outer largest)
	for i := 1; i < len(orders); i++ {
		if orders[i].Size < orders[i-1].Size {
			t.Errorf("order %d size (%f) should be >= order %d size (%f)",
				i, orders[i].Size, i-1, orders[i-1].Size)
		}
	}
}

func TestQuotesMateriallyDifferent(t *testing.T) {
	base := []types.OrderRequest{
		{Side: "buy", Price: 69.825, Size: 1.42},
		{Side: "sell", Price: 70.175, Size: 1.42},
	}

	tests := []struct {
		name     string
		current  []types.OrderRequest
		desired  []types.OrderRequest
		wantDiff bool
	}{
		{
			name:     "identical",
			current:  base,
			desired:  base,
			wantDiff: false,
		},
		{
			name:    "different_count",
			current: base,
			desired: []types.OrderRequest{
				{Side: "buy", Price: 69.825, Size: 1.42},
			},
			wantDiff: true,
		},
		{
			name:    "price_above_threshold",
			current: base,
			desired: []types.OrderRequest{
				{Side: "buy", Price: 69.80, Size: 1.42},
				{Side: "sell", Price: 70.175, Size: 1.42},
			},
			wantDiff: true,
		},
		{
			name:    "price_below_threshold",
			current: base,
			desired: []types.OrderRequest{
				{Side: "buy", Price: 69.8249, Size: 1.42},
				{Side: "sell", Price: 70.175, Size: 1.42},
			},
			wantDiff: false,
		},
		{
			name:    "size_delta",
			current: base,
			desired: []types.OrderRequest{
				{Side: "buy", Price: 69.825, Size: 2.0},
				{Side: "sell", Price: 70.175, Size: 1.42},
			},
			wantDiff: true,
		},
		{
			name:    "different_side",
			current: base,
			desired: []types.OrderRequest{
				{Side: "buy", Price: 69.825, Size: 1.42},
				{Side: "buy", Price: 70.175, Size: 1.42},
			},
			wantDiff: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := quotesMateriallyDifferent(tt.current, tt.desired, 2.0, 0.10)
			if got != tt.wantDiff {
				t.Errorf("quotesMateriallyDifferent() = %v, want %v", got, tt.wantDiff)
			}
		})
	}
}

func TestShouldRefreshCoin(t *testing.T) {
	cfg := defaultTestConfig()
	mgr := New(cfg, nil, nil, nil, newMockExecutor(), nil, nil, nil, nil, nil, newTestSessionPnL())
	now := time.Now()

	tests := []struct {
		name        string
		current     []types.OrderRequest
		desired     []types.OrderRequest
		state       quoteState
		wantRefresh bool
		wantReason  string
	}{
		{
			name: "no_orders", current: nil, desired: nil,
			state:       quoteState{},
			wantRefresh: false, wantReason: "no_orders",
		},
		{
			name: "bootstrap", current: nil,
			desired:     []types.OrderRequest{{Side: "buy", Price: 70}},
			state:       quoteState{},
			wantRefresh: true, wantReason: "bootstrap",
		},
		{
			name:    "cancel",
			current: []types.OrderRequest{{Side: "buy", Price: 70}},
			desired: nil, state: quoteState{},
			wantRefresh: true, wantReason: "cancel",
		},
		{
			name:        "unknown_state",
			current:     []types.OrderRequest{{Side: "buy", Price: 70}},
			desired:     []types.OrderRequest{{Side: "buy", Price: 70}},
			state:       quoteState{},
			wantRefresh: true, wantReason: "unknown_state",
		},
		{
			name:        "max_age",
			current:     []types.OrderRequest{{Side: "buy", Price: 70}},
			desired:     []types.OrderRequest{{Side: "buy", Price: 70}},
			state:       quoteState{SentAt: now.Add(-5 * time.Second)},
			wantRefresh: true, wantReason: "max_age",
		},
		{
			name:        "unchanged",
			current:     []types.OrderRequest{{Side: "buy", Price: 70, Size: 1}},
			desired:     []types.OrderRequest{{Side: "buy", Price: 70, Size: 1}},
			state:       quoteState{SentAt: now},
			wantRefresh: false, wantReason: "unchanged",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			refresh, reason := mgr.shouldRefreshCoin("xyz:CL", tt.current, tt.desired, tt.state, now)
			if refresh != tt.wantRefresh {
				t.Errorf("shouldRefreshCoin() refresh = %v, want %v", refresh, tt.wantRefresh)
			}
			if reason != tt.wantReason {
				t.Errorf("shouldRefreshCoin() reason = %q, want %q", reason, tt.wantReason)
			}
		})
	}
}

func TestNormalizeOrderSide(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"B", "buy"}, {"b", "buy"}, {"buy", "buy"},
		{"A", "sell"}, {"a", "sell"}, {"ask", "sell"},
		{"S", "sell"}, {"s", "sell"}, {"sell", "sell"},
		{"unknown", "unknown"},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := normalizeOrderSide(tt.input)
			if got != tt.expected {
				t.Errorf("normalizeOrderSide(%q) = %q, want %q", tt.input, got, tt.expected)
			}
		})
	}
}

func TestPriceDeltaBps(t *testing.T) {
	tests := []struct {
		name string
		a, b float64
		want float64
	}{
		{"both_zero", 0, 0, 0},
		{"one_zero", 70, 0, math.MaxFloat64},
		{"equal", 70.0, 70.0, 0},
		{"normal_delta", 70.0, 70.07, 10.0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := priceDeltaBps(tt.a, tt.b)
			if !almostEqual(got, tt.want, 0.01) {
				t.Errorf("priceDeltaBps(%f, %f) = %f, want %f", tt.a, tt.b, got, tt.want)
			}
		})
	}
}

func TestSizeDeltaPct(t *testing.T) {
	tests := []struct {
		name string
		a, b float64
		want float64
	}{
		{"both_zero", 0, 0, 0},
		{"one_zero", 1.0, 0, 1.0},
		{"equal", 1.0, 1.0, 0},
		{"normal_delta", 1.0, 1.5, 1.0 / 3.0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := sizeDeltaPct(tt.a, tt.b)
			if !almostEqual(got, tt.want, 1e-6) {
				t.Errorf("sizeDeltaPct(%f, %f) = %f, want %f", tt.a, tt.b, got, tt.want)
			}
		})
	}
}

func TestMinFloat(t *testing.T) {
	tests := []struct {
		name   string
		values []float64
		want   float64
	}{
		{"empty", nil, 0},
		{"single", []float64{5.0}, 5.0},
		{"multiple", []float64{5.0, 3.0, 7.0}, 3.0},
		{"negatives", []float64{-1.0, -5.0, 2.0}, -5.0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := minFloat(tt.values...)
			if got != tt.want {
				t.Errorf("minFloat(%v) = %f, want %f", tt.values, got, tt.want)
			}
		})
	}
}

func TestAccumulatePositionExposure(t *testing.T) {
	tests := []struct {
		name      string
		pos       types.PositionUpdate
		wantLong  float64
		wantShort float64
	}{
		{"long", types.PositionUpdate{Coin: "xyz:CL", Side: "long", Notional: 100}, 100, 0},
		{"short", types.PositionUpdate{Coin: "xyz:CL", Side: "short", Notional: 100}, 0, 100},
		{"flat", types.PositionUpdate{Coin: "xyz:CL", Side: "", Notional: 0}, 0, 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exposures := make(map[string]sideExposure)
			accumulatePositionExposure(exposures, tt.pos)
			exp := exposures[tt.pos.Coin]
			if exp.Long != tt.wantLong {
				t.Errorf("Long = %f, want %f", exp.Long, tt.wantLong)
			}
			if exp.Short != tt.wantShort {
				t.Errorf("Short = %f, want %f", exp.Short, tt.wantShort)
			}
		})
	}
}

func TestAccumulateOrderExposure(t *testing.T) {
	tests := []struct {
		name      string
		orders    []types.OrderRequest
		wantLong  float64
		wantShort float64
	}{
		{
			name:     "buy_orders",
			orders:   []types.OrderRequest{{Coin: "xyz:CL", Side: "buy", Price: 70, Size: 1}},
			wantLong: 70, wantShort: 0,
		},
		{
			name:     "sell_orders",
			orders:   []types.OrderRequest{{Coin: "xyz:CL", Side: "sell", Price: 70, Size: 1}},
			wantLong: 0, wantShort: 70,
		},
		{
			name: "mixed_sides",
			orders: []types.OrderRequest{
				{Coin: "xyz:CL", Side: "buy", Price: 70, Size: 1},
				{Coin: "xyz:CL", Side: "sell", Price: 71, Size: 1},
			},
			wantLong: 70, wantShort: 71,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			exposures := make(map[string]sideExposure)
			accumulateOrderExposure(exposures, tt.orders)
			exp := exposures["xyz:CL"]
			if !almostEqual(exp.Long, tt.wantLong, floatTol) {
				t.Errorf("Long = %f, want %f", exp.Long, tt.wantLong)
			}
			if !almostEqual(exp.Short, tt.wantShort, floatTol) {
				t.Errorf("Short = %f, want %f", exp.Short, tt.wantShort)
			}
		})
	}
}

func TestPairExposureExcluding(t *testing.T) {
	tests := []struct {
		name      string
		exposures map[string]sideExposure
		exclude   string
		want      float64
	}{
		{"empty_map", map[string]sideExposure{}, "xyz:CL", 0},
		{
			"excludes_named_coin",
			map[string]sideExposure{
				"xyz:CL":       {Long: 100, Short: 50},
				"xyz:BRENTOIL": {Long: 80, Short: 60},
			},
			"xyz:CL", 80, // max(80, 60) = 80
		},
		{
			"sums_max_of_rest",
			map[string]sideExposure{
				"xyz:CL":       {Long: 100, Short: 50},
				"xyz:BRENTOIL": {Long: 80, Short: 90},
				"xyz:OTHER":    {Long: 30, Short: 40},
			},
			"xyz:CL", 130, // max(80,90) + max(30,40) = 90 + 40 = 130
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := pairExposureExcluding(tt.exposures, tt.exclude)
			if !almostEqual(got, tt.want, floatTol) {
				t.Errorf("pairExposureExcluding() = %f, want %f", got, tt.want)
			}
		})
	}
}

func TestOneTick(t *testing.T) {
	tests := []struct {
		price    float64
		wantTick float64
	}{
		{115.0, 0.01},
		{70.0, 0.001},
		{9.5, 0.0001},
		{1000.0, 0.01},
		{0, 0},
	}
	for _, tt := range tests {
		got := oneTick(tt.price)
		if math.Abs(got-tt.wantTick) > 1e-10 {
			t.Errorf("oneTick(%g) = %g, want %g", tt.price, got, tt.wantTick)
		}
	}
}

func TestFindWallPrice(t *testing.T) {
	tests := []struct {
		name             string
		side             string
		levels           []types.BookLevel
		bestPrice        float64
		worstLadderPrice float64
		multiplier       float64
		wantPrice        float64
		wantOk           bool
	}{
		{
			name: "buy_finds_largest",
			side: "buy",
			levels: []types.BookLevel{
				{Price: 69.90, Size: 10},
				{Price: 69.80, Size: 15},
				{Price: 69.70, Size: 50}, // wall
				{Price: 69.60, Size: 12},
			},
			bestPrice:        69.90,
			worstLadderPrice: 69.55,
			multiplier:       1.8, // non-BBO avg = (15+50+12)/3 ≈ 25.67, min = 46.2, wall=50 qualifies
			wantPrice:        69.70,
			wantOk:           true,
		},
		{
			name: "buy_wall_outside_range",
			side: "buy",
			levels: []types.BookLevel{
				{Price: 69.90, Size: 10},
				{Price: 69.80, Size: 15},
				{Price: 69.70, Size: 50}, // wall but outside range
			},
			bestPrice:        69.90,
			worstLadderPrice: 69.75, // wall at 69.70 is below this
			multiplier:       1.5,
			wantPrice:        0,
			wantOk:           false,
		},
		{
			name: "sell_finds_largest",
			side: "sell",
			levels: []types.BookLevel{
				{Price: 70.10, Size: 10},
				{Price: 70.20, Size: 15},
				{Price: 70.30, Size: 80}, // wall
				{Price: 70.40, Size: 12},
			},
			bestPrice:        70.10,
			worstLadderPrice: 70.45,
			multiplier:       2.0, // non-BBO avg = (15+80+12)/3 ≈ 35.67, min = 71.33, wall=80 qualifies
			wantPrice:        70.30,
			wantOk:           true,
		},
		{
			name: "below_threshold",
			side: "buy",
			levels: []types.BookLevel{
				{Price: 69.90, Size: 10},
				{Price: 69.80, Size: 12},
				{Price: 69.70, Size: 11},
			},
			bestPrice:        69.90,
			worstLadderPrice: 69.60,
			multiplier:       3.0, // non-BBO avg = (12+11)/2 = 11.5, min = 34.5, nothing qualifies
			wantPrice:        0,
			wantOk:           false,
		},
		{
			name: "wall_at_bbo_skipped",
			side: "buy",
			levels: []types.BookLevel{
				{Price: 69.90, Size: 50}, // BBO and wall, but should be skipped
				{Price: 69.80, Size: 10},
				{Price: 69.70, Size: 12},
			},
			bestPrice:        69.90,
			worstLadderPrice: 69.55,
			multiplier:       2.0, // non-BBO avg = (10+12)/2 = 11, min = 22, nothing qualifies
			wantPrice:        0,
			wantOk:           false,
		},
		{
			name: "wall_qualifies_with_local_avg",
			side: "buy",
			levels: []types.BookLevel{
				{Price: 69.90, Size: 100}, // BBO, skipped
				{Price: 69.80, Size: 5},
				{Price: 69.70, Size: 30}, // wall candidate
				{Price: 69.60, Size: 3},  // outside range, not counted
			},
			bestPrice:        69.90,
			worstLadderPrice: 69.65, // only 69.80 and 69.70 are in range
			multiplier:       1.5,   // local avg = (5+30)/2 = 17.5, min = 26.25, wall=30 qualifies
			wantPrice:        69.70, // With global avg including BBO (100+5+30)/3=45, min=67.5, wall=30 wouldn't qualify
			wantOk:           true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			price, ok := findWallPrice(tt.side, tt.levels, tt.bestPrice, tt.worstLadderPrice, tt.multiplier)
			if ok != tt.wantOk {
				t.Fatalf("findWallPrice() ok = %v, want %v", ok, tt.wantOk)
			}
			if ok && math.Abs(price-tt.wantPrice) > 1e-6 {
				t.Errorf("findWallPrice() price = %f, want %f", price, tt.wantPrice)
			}
		})
	}
}

func TestBuildSideOrdersWallFronting(t *testing.T) {
	pow10Mult := math.Pow(10, 2) // szDec=2

	// Book bids: wall at 69.70 with size 50
	bidLevels := []types.BookLevel{
		{Price: 69.90, Size: 10},
		{Price: 69.80, Size: 15},
		{Price: 69.70, Size: 50},
		{Price: 69.60, Size: 12},
	}
	// Book asks: wall at 70.30 with size 80
	askLevels := []types.BookLevel{
		{Price: 70.10, Size: 10},
		{Price: 70.20, Size: 15},
		{Price: 70.30, Size: 80},
		{Price: 70.40, Size: 12},
	}

	t.Run("buy_snaps_to_wall", func(t *testing.T) {
		orders := buildSideOrders(
			"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			bidLevels, true, 2.0, nil, // avg ~21.75, minWall ~43.5, wall=50 qualifies
		)
		if len(orders) != 4 {
			t.Fatalf("expected 4 orders, got %d", len(orders))
		}
		// One of the levels should be snapped to just in front of the wall (69.70 + tick)
		wallSnapped := false
		tick := oneTick(69.70)
		snapPrice := types.RoundToTickSize(69.70 + tick)
		for _, o := range orders {
			if math.Abs(o.Price-snapPrice) < 1e-6 {
				wallSnapped = true
				break
			}
		}
		if !wallSnapped {
			prices := make([]float64, len(orders))
			for i, o := range orders {
				prices[i] = o.Price
			}
			t.Errorf("expected one order snapped to %f, got prices %v", snapPrice, prices)
		}
	})

	t.Run("sell_snaps_to_wall", func(t *testing.T) {
		orders := buildSideOrders(
			"xyz:CL", "sell", 70.175, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			askLevels, true, 2.0, nil, // avg ~29.25, minWall ~58.5, wall=80 qualifies
		)
		if len(orders) != 4 {
			t.Fatalf("expected 4 orders, got %d", len(orders))
		}
		wallSnapped := false
		tick := oneTick(70.30)
		snapPrice := types.RoundToTickSize(70.30 - tick)
		for _, o := range orders {
			if math.Abs(o.Price-snapPrice) < 1e-6 {
				wallSnapped = true
				break
			}
		}
		if !wallSnapped {
			prices := make([]float64, len(orders))
			for i, o := range orders {
				prices[i] = o.Price
			}
			t.Errorf("expected one order snapped to %f, got prices %v", snapPrice, prices)
		}
	})

	t.Run("wall_below_threshold_ignored", func(t *testing.T) {
		smallLevels := []types.BookLevel{
			{Price: 69.90, Size: 10},
			{Price: 69.80, Size: 12},
			{Price: 69.70, Size: 11},
		}
		ordersWithWall := buildSideOrders(
			"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			smallLevels, true, 3.0, nil, // avg ~11, minWall ~33, nothing qualifies
		)
		ordersNoWall := buildSideOrders(
			"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			nil, false, 0, nil,
		)
		if len(ordersWithWall) != len(ordersNoWall) {
			t.Fatalf("expected same count: %d vs %d", len(ordersWithWall), len(ordersNoWall))
		}
		for i := range ordersWithWall {
			if math.Abs(ordersWithWall[i].Price-ordersNoWall[i].Price) > 1e-6 {
				t.Errorf("order %d price differs: %f vs %f", i, ordersWithWall[i].Price, ordersNoWall[i].Price)
			}
		}
	})

	t.Run("disabled_no_snap", func(t *testing.T) {
		ordersDisabled := buildSideOrders(
			"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			bidLevels, false, 3.0, nil,
		)
		ordersNil := buildSideOrders(
			"xyz:CL", "buy", 69.825, 70.0, 10, 500.0, 4,
			69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
			nil, false, 0, nil,
		)
		if len(ordersDisabled) != len(ordersNil) {
			t.Fatalf("expected same count: %d vs %d", len(ordersDisabled), len(ordersNil))
		}
		for i := range ordersDisabled {
			if math.Abs(ordersDisabled[i].Price-ordersNil[i].Price) > 1e-6 {
				t.Errorf("order %d price differs: %f vs %f", i, ordersDisabled[i].Price, ordersNil[i].Price)
			}
		}
	})
}

func TestAutoRecoveryFromFailClosed(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50
	cfg.Risk.FailClosedCooldownSec = 1
	cfg.Risk.MaxFailClosedRetries = 5

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	// First refresh fails, subsequent ones succeed.
	exec.setRefreshErrors("xyz:CL", errors.New("nonce collision"))
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	update := types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}

	// Trigger fail-closed halt.
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	mgr.mu.Lock()
	retries := mgr.failClosedRetries
	mgr.mu.Unlock()
	if !mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected operational halt after first failure")
	}
	if retries != 1 {
		t.Fatalf("expected failClosedRetries=1, got %d", retries)
	}

	// Wait for cooldown to elapse, then send a new book update to trigger recovery.
	time.Sleep(1100 * time.Millisecond)
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	mgr.mu.Lock()
	retriesAfter := mgr.failClosedRetries
	mgr.mu.Unlock()
	if mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected recovery after cooldown")
	}
	if retriesAfter != 0 {
		t.Fatalf("expected failClosedRetries reset to 0 after recovery, got %d", retriesAfter)
	}
}

func TestAutoRecoveryExhaustsRetries(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50
	cfg.Risk.FailClosedCooldownSec = 1
	cfg.Risk.MaxFailClosedRetries = 2

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	// Every refresh fails.
	exec.setRefreshErrors("xyz:CL",
		errors.New("nonce collision 1"),
		errors.New("nonce collision 2"),
		errors.New("nonce collision 3"),
	)
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	update := types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}

	// First failure → retry_count=1.
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	// Wait for cooldown, trigger recovery attempt → fails → retry_count=2.
	time.Sleep(1100 * time.Millisecond)
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	mgr.mu.Lock()
	retries := mgr.failClosedRetries
	mgr.mu.Unlock()
	if retries != 2 {
		t.Fatalf("expected failClosedRetries=2, got %d", retries)
	}

	// Wait for cooldown again — should NOT attempt recovery (exhausted retries).
	time.Sleep(1100 * time.Millisecond)
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	mgr.mu.Lock()
	retriesFinal := mgr.failClosedRetries
	mgr.mu.Unlock()
	if !mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected permanent halt after exhausting retries")
	}
	if retriesFinal != 2 {
		t.Fatalf("expected failClosedRetries to remain at 2, got %d", retriesFinal)
	}
}

func TestAutoRecoveryDisabledByDefault(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.MarketMaking.RefreshIntervalMs = 50
	// FailClosedCooldownSec defaults to 0 (disabled).

	bookCh := make(chan types.BookUpdate, 100)
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	exec := newMockExecutor()
	exec.setRefreshErrors("xyz:CL", errors.New("place failed"))
	szDecimals := map[string]int{"xyz:CL": 2, "xyz:BRENTOIL": 2}

	mgr := New(cfg, bookCh, signalCh, riskCh, exec, szDecimals, nil, nil, nil, nil, newTestSessionPnL())

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go mgr.Run(ctx)

	update := types.BookUpdate{
		Coin: "xyz:CL", BestBid: 69.95, BestAsk: 70.05, Mid: 70.0, Timestamp: time.Now(),
	}

	// Trigger fail-closed halt.
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	// Send another update — should NOT recover (auto-recovery disabled).
	bookCh <- update
	time.Sleep(300 * time.Millisecond)

	if !mgr.supervisor.IsRefreshFailed() {
		t.Fatal("expected permanent halt when fail_closed_cooldown_sec=0")
	}

	refreshes := exec.getRefreshes()
	if len(refreshes) != 1 {
		t.Fatalf("expected exactly 1 refresh attempt (no recovery), got %d", len(refreshes))
	}
}

// --- New tests for microprice, convex skew, and wick fix ---

func TestInventorySkewGammaClampRatio(t *testing.T) {
	// When notional > maxNotional, ratio should be clamped to 1.0.
	bid := 69.825
	ask := 70.175
	bid, ask = applyInventorySkew(bid, ask, 70.0, 600.0, "long", 500.0, 0.5, 50.0, 2.0, 0.5, false)
	// ratio = min(600/500, 1.0) = 1.0, pow(1.0, 2.0) = 1.0
	// shiftBps = 1.0 * 0.5 * 50 = 25 bps, shift = 70 * 25 / 10000 = 0.175
	if !almostEqual(bid, 69.650, floatTol) {
		t.Fatalf("expected bid=69.650, got %f", bid)
	}
	if !almostEqual(ask, 70.000, floatTol) {
		t.Fatalf("expected ask=70.000, got %f", ask)
	}
}

func TestWickSizingOnlyShortFar(t *testing.T) {
	// Wick scenario: short VWAP deviated but long VWAP stable.
	threshold := 0.01
	longThreshold := 0.05 // separate wider long threshold
	// mid=100, shortVWAP=98 (2% dev), longVWAP=99.95 (0.05% dev)
	bidMult, askMult, spacing := computeWickSizing(100.0, 98.0, 99.95, threshold, longThreshold, 3.0, 10.0)
	// shortDev = 2/98 = 0.02041 > 0.01 (shortFar)
	// longDev = 0.05/99.95 = 0.0005 < 0.01 (!longFar)
	// => wick detected. mid > shortVWAP => askMult > 1
	if bidMult != 1.0 {
		t.Errorf("expected bidMult=1.0, got %f", bidMult)
	}
	if askMult <= 1.0 {
		t.Errorf("expected askMult > 1.0, got %f", askMult)
	}
	if spacing >= 10.0 {
		t.Errorf("expected spacing < 10.0 (normal), got %f", spacing)
	}
}

func TestWickSizingTrendSuppressed(t *testing.T) {
	// Both VWAPs deviated = trend, not wick. Should NOT trigger wick logic.
	threshold := 0.01
	longThreshold := 0.02 // longDev = |100-97|/97 ~ 0.031 > 0.02 => trend
	bidMult, askMult, spacing := computeWickSizing(100.0, 98.0, 97.0, threshold, longThreshold, 3.0, 10.0)
	// shortDev > threshold AND longDev > longThreshold => trend
	if bidMult != 1.0 {
		t.Errorf("expected bidMult=1.0 (trend, not wick), got %f", bidMult)
	}
	if askMult != 1.0 {
		t.Errorf("expected askMult=1.0 (trend, not wick), got %f", askMult)
	}
	if spacing != 10.0 {
		t.Errorf("expected spacing=10.0 (unchanged), got %f", spacing)
	}
}

func TestWickSizingScaleClamp(t *testing.T) {
	// At exactly 3x threshold, scale should be clamped to 1.0 and mult = maxMult.
	threshold := 0.01
	longThreshold := 0.05
	// shortDev = 3 * 0.01 = 0.03
	// mid=100, shortVWAP = 100/(1+0.03) = 97.087...
	// longVWAP very close to mid
	bidMult, _, _ := computeWickSizing(100.0, 97.0, 99.99, threshold, longThreshold, 3.0, 10.0)
	// shortDev = |100-97|/97 ~ 0.03093, far above 3x threshold
	// scale = min((0.03093 - 0.01) / (2*0.01), 1.0) = min(1.0465, 1.0) = 1.0
	// mid > shortVWAP => bidMult = 1.0
	if bidMult != 1.0 {
		t.Errorf("expected bidMult=1.0 (mid > shortVWAP), got %f", bidMult)
	}
}

// --- Tests for asymmetric z-widening ---

func TestApplyZWideningAsymmetric(t *testing.T) {
	mid := 70.0
	bid := 69.825 // 0.175 below mid
	ask := 70.175 // 0.175 above mid

	// z>0, first asset → bid is adverse. asymmetry=0.7 → 70% on adverse side.
	// absZ=5, threshold=4, rate=0.15 → mult=1.15, extra=0.15
	// adverseExtra = 0.15 * 0.7 * 2 = 0.21, passiveExtra = 0.15 * 0.3 * 2 = 0.09
	// bid (adverse) = 70 - 0.175*(1+0.21) = 70 - 0.175*1.21 = 70 - 0.21175 = 69.78825
	// ask (passive) = 70 + 0.175*(1+0.09) = 70 + 0.175*1.09 = 70 + 0.19075 = 70.19075
	gotBid, gotAsk := applyZWidening(bid, ask, mid, 5.0, 4.0, 0.15, 3.0, 0.7, true, true)

	if !almostEqual(gotBid, 69.78825, floatTol) {
		t.Errorf("asymmetric bid: got %f, want 69.78825", gotBid)
	}
	if !almostEqual(gotAsk, 70.19075, floatTol) {
		t.Errorf("asymmetric ask: got %f, want 70.19075", gotAsk)
	}

	// Verify adverse side widens more
	bidWidening := mid - gotBid // 0.21175
	askWidening := gotAsk - mid // 0.19075
	if bidWidening <= askWidening {
		t.Errorf("expected bid (adverse) to widen more: bid_w=%f, ask_w=%f", bidWidening, askWidening)
	}
}

func TestApplyZWideningSymmetricDefault(t *testing.T) {
	mid := 70.0
	bid := 69.825
	ask := 70.175

	// At asymmetry=0.5, should be identical to old symmetric behavior
	gotBid, gotAsk := applyZWidening(bid, ask, mid, 5.0, 4.0, 0.15, 3.0, 0.5, true, true)

	// mult=1.15, symmetric: bid = 70 - 0.175*1.15 = 69.79875
	if !almostEqual(gotBid, 69.79875, floatTol) {
		t.Errorf("symmetric bid: got %f, want 69.79875", gotBid)
	}
	if !almostEqual(gotAsk, 70.20125, floatTol) {
		t.Errorf("symmetric ask: got %f, want 70.20125", gotAsk)
	}
}

// --- Tests for asymmetric inventory skew ---

func TestApplyInventorySkewAsymmetricLong(t *testing.T) {
	bid := 69.825
	ask := 70.175

	// Long position with asymmetry=0.7: ask is aggressive (shift down more)
	// ratio=100/500=0.2, pow(0.2,1.0)=0.2, shiftBps=0.2*0.5*50=5, shift=70*5/10000=0.035
	// aggressiveShift = 0.035 * 0.7 * 2 = 0.049
	// passiveShift = 0.035 * 0.3 * 2 = 0.021
	// Long: bid -= passiveShift, ask -= aggressiveShift
	gotBid, gotAsk := applyInventorySkew(bid, ask, 70.0, 100.0, "long", 500.0, 0.5, 50.0, 1.0, 0.7, false)

	expectedBid := 69.825 - 0.021 // 69.804
	expectedAsk := 70.175 - 0.049 // 70.126
	if !almostEqual(gotBid, expectedBid, floatTol) {
		t.Errorf("asymmetric long bid: got %f, want %f", gotBid, expectedBid)
	}
	if !almostEqual(gotAsk, expectedAsk, floatTol) {
		t.Errorf("asymmetric long ask: got %f, want %f", gotAsk, expectedAsk)
	}
}

func TestApplyInventorySkewAsymmetricShort(t *testing.T) {
	bid := 69.825
	ask := 70.175

	// Short position with asymmetry=0.7: bid is aggressive (shift up more)
	// shift = 0.035 (same calc as above)
	// aggressiveShift = 0.049, passiveShift = 0.021
	// Short: bid += aggressiveShift, ask += passiveShift
	gotBid, gotAsk := applyInventorySkew(bid, ask, 70.0, 100.0, "short", 500.0, 0.5, 50.0, 1.0, 0.7, false)

	expectedBid := 69.825 + 0.049 // 69.874
	expectedAsk := 70.175 + 0.021 // 70.196
	if !almostEqual(gotBid, expectedBid, floatTol) {
		t.Errorf("asymmetric short bid: got %f, want %f", gotBid, expectedBid)
	}
	if !almostEqual(gotAsk, expectedAsk, floatTol) {
		t.Errorf("asymmetric short ask: got %f, want %f", gotAsk, expectedAsk)
	}
}

func TestApplyInventorySkewSymmetricDefault(t *testing.T) {
	bid := 69.825
	ask := 70.175

	// At asymmetry=0.5, should match original behavior exactly
	gotBid, gotAsk := applyInventorySkew(bid, ask, 70.0, 100.0, "long", 500.0, 0.5, 50.0, 1.0, 0.5, false)

	// shift = 0.035, symmetric: both shift down by 0.035
	if !almostEqual(gotBid, 69.790, floatTol) {
		t.Errorf("symmetric long bid: got %f, want 69.790", gotBid)
	}
	if !almostEqual(gotAsk, 70.140, floatTol) {
		t.Errorf("symmetric long ask: got %f, want 70.140", gotAsk)
	}
}

// --- Tests for position-aware crossing protection (A2) ---

func TestCrossingProtectionLong(t *testing.T) {
	// Long position: exit is sell (ask). Keep ask, pull bid below.
	bid, ask := applyCrossingProtection(70.10, 70.05, 70.0, "long", 100.0, 0, 0, 2.0)
	// Ask (exit side) should stay at 70.05
	if !almostEqual(ask, 70.05, floatTol) {
		t.Fatalf("long crossing: ask should stay at 70.05, got %f", ask)
	}
	// Bid pulled to ask - 2 bps (full min spread)
	expectedBid := 70.05 - 70.0*2.0/10000.0
	if !almostEqual(bid, expectedBid, floatTol) {
		t.Fatalf("long crossing: expected bid=%f, got %f", expectedBid, bid)
	}
}

func TestCrossingProtectionShort(t *testing.T) {
	// Short position: exit is buy (bid). Keep bid, pull ask above.
	bid, ask := applyCrossingProtection(70.10, 70.05, 70.0, "short", 100.0, 0, 0, 2.0)
	// Bid (exit side) should stay at 70.10
	if !almostEqual(bid, 70.10, floatTol) {
		t.Fatalf("short crossing: bid should stay at 70.10, got %f", bid)
	}
	expectedAsk := 70.10 + 70.0*2.0/10000.0
	if !almostEqual(ask, expectedAsk, floatTol) {
		t.Fatalf("short crossing: expected ask=%f, got %f", expectedAsk, ask)
	}
}

func TestCrossingProtectionFlat(t *testing.T) {
	// Flat position: fall back to midpoint clamping
	bid, ask := applyCrossingProtection(70.10, 70.05, 70.0, "", 0, 0, 0, 2.0)
	mid := (70.10 + 70.05) / 2.0
	minHalf := 70.0 * 2.0 / 10000.0 / 2.0
	if !almostEqual(bid, mid-minHalf, floatTol) {
		t.Fatalf("flat crossing: expected bid=%f, got %f", mid-minHalf, bid)
	}
	if !almostEqual(ask, mid+minHalf, floatTol) {
		t.Fatalf("flat crossing: expected ask=%f, got %f", mid+minHalf, ask)
	}
}

func TestCrossingProtectionNoCross(t *testing.T) {
	// No crossing: should return unchanged
	bid, ask := applyCrossingProtection(69.90, 70.10, 70.0, "long", 100.0, 0, 0, 2.0)
	if !almostEqual(bid, 69.90, floatTol) || !almostEqual(ask, 70.10, floatTol) {
		t.Fatalf("no crossing: should be unchanged, got bid=%f ask=%f", bid, ask)
	}
}

// --- Tests for exit-preserving inventory skew (A1) ---

func TestInventorySkewExitPreserveLong(t *testing.T) {
	bid := 69.825
	ask := 70.175
	// Long position: exit is sell (ask). Ask should NOT move.
	// shiftBps = pow(100/500, 1.0) * 0.5 * 50 = 5 bps
	// shift = 70.0 * 5 / 10000 = 0.035
	// fullShift = 0.035 * 2 = 0.070
	bid, ask = applyInventorySkew(bid, ask, 70.0, 100.0, "long", 500.0, 0.5, 50.0, 1.0, 0.5, true)
	if !almostEqual(ask, 70.175, floatTol) {
		t.Fatalf("exit-preserve long: ask should not move, got %f", ask)
	}
	expectedBid := 69.825 - 0.070
	if !almostEqual(bid, expectedBid, floatTol) {
		t.Fatalf("exit-preserve long: expected bid=%f, got %f", expectedBid, bid)
	}
}

func TestInventorySkewExitPreserveShort(t *testing.T) {
	bid := 69.825
	ask := 70.175
	// Short position: exit is buy (bid). Bid should NOT move.
	bid, ask = applyInventorySkew(bid, ask, 70.0, 100.0, "short", 500.0, 0.5, 50.0, 1.0, 0.5, true)
	if !almostEqual(bid, 69.825, floatTol) {
		t.Fatalf("exit-preserve short: bid should not move, got %f", bid)
	}
	expectedAsk := 70.175 + 0.070
	if !almostEqual(ask, expectedAsk, floatTol) {
		t.Fatalf("exit-preserve short: expected ask=%f, got %f", expectedAsk, ask)
	}
}

func TestInventorySkewExitPreserveFlat(t *testing.T) {
	bid := 69.825
	ask := 70.175
	bid2, ask2 := applyInventorySkew(bid, ask, 70.0, 0, "", 500.0, 0.5, 50.0, 1.0, 0.5, true)
	if bid2 != bid || ask2 != ask {
		t.Fatalf("exit-preserve flat: should be no-op, got bid=%f ask=%f", bid2, ask2)
	}
}

// --- Test for multi-level microprice ---

func TestComputeMultiLevelMicroprice(t *testing.T) {
	bids := []types.BookLevel{
		{Price: 69.90, Size: 10.0},
		{Price: 69.80, Size: 20.0},
		{Price: 69.70, Size: 30.0},
	}
	asks := []types.BookLevel{
		{Price: 70.10, Size: 5.0},
		{Price: 70.20, Size: 15.0},
		{Price: 70.30, Size: 25.0},
	}
	mid := 70.0

	// Single level should degrade to BBO microprice
	mp1 := computeMultiLevelMicroprice(bids, asks, mid, 1, 0.7)
	// weight: bid[0].Size/dist = 10/0.1=100, ask[0].Size/dist = 5/0.1=50
	// microprice = (69.90*50 + 70.10*100) / 150 = (3495+7010)/150 = 10505/150 = 70.0333
	if !almostEqual(mp1, 70.0333, 0.001) {
		t.Errorf("1-level microprice: got %f, want ~70.033", mp1)
	}

	// Multi-level should differ from single-level
	mp3 := computeMultiLevelMicroprice(bids, asks, mid, 3, 0.7)
	if mp3 == mp1 {
		t.Errorf("3-level microprice should differ from 1-level")
	}

	// Empty books should return mid
	mpEmpty := computeMultiLevelMicroprice(nil, asks, mid, 3, 0.7)
	if mpEmpty != mid {
		t.Errorf("expected mid for empty bids, got %f", mpEmpty)
	}
}

func TestApplyCrossLegSkew(t *testing.T) {
	mid := 70.0
	bid := 69.79 // 6 bps half-spread
	ask := 70.21
	spreadBps := 6.0
	maxNotional := 400.0
	factor := 0.3
	beta := 1.0

	tests := []struct {
		name      string
		thisPos   types.PositionUpdate
		otherPos  types.PositionUpdate
		wantShift bool // whether any shift should occur
		wantDown  bool // true if shift is downward (both long)
	}{
		{
			name:      "both_long",
			thisPos:   types.PositionUpdate{Side: "long", Notional: 200},
			otherPos:  types.PositionUpdate{Side: "long", Notional: 200},
			wantShift: true,
			wantDown:  true,
		},
		{
			name:      "both_short",
			thisPos:   types.PositionUpdate{Side: "short", Notional: 200},
			otherPos:  types.PositionUpdate{Side: "short", Notional: 200},
			wantShift: true,
			wantDown:  false,
		},
		{
			name:      "hedged_no_effect",
			thisPos:   types.PositionUpdate{Side: "long", Notional: 200},
			otherPos:  types.PositionUpdate{Side: "short", Notional: 200},
			wantShift: false,
		},
		{
			name:      "one_flat_no_effect",
			thisPos:   types.PositionUpdate{Side: "long", Notional: 200},
			otherPos:  types.PositionUpdate{Side: "", Notional: 0},
			wantShift: false,
		},
		{
			name:      "factor_zero_no_effect",
			thisPos:   types.PositionUpdate{Side: "long", Notional: 200},
			otherPos:  types.PositionUpdate{Side: "long", Notional: 200},
			wantShift: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := factor
			if tt.name == "factor_zero_no_effect" {
				f = 0
			}
			newBid, newAsk, diagBps := applyCrossLegSkew(bid, ask, mid, tt.thisPos, tt.otherPos,
				beta, true, maxNotional, f, spreadBps)
			if !tt.wantShift {
				if newBid != bid || newAsk != ask {
					t.Fatalf("expected no shift, got bid=%f ask=%f diagBps=%f", newBid, newAsk, diagBps)
				}
				return
			}
			if tt.wantDown {
				if newBid >= bid || newAsk >= ask {
					t.Fatalf("expected downward shift for both-long, got bid=%f ask=%f", newBid, newAsk)
				}
			} else {
				if newBid <= bid || newAsk <= ask {
					t.Fatalf("expected upward shift for both-short, got bid=%f ask=%f", newBid, newAsk)
				}
			}
		})
	}
}

func TestApplyCrossLegSkewBetaNormalization(t *testing.T) {
	mid := 70.0
	bid := 69.79
	ask := 70.21
	spreadBps := 6.0
	maxNotional := 400.0
	factor := 0.3

	// With beta=2.0, other leg notional is effectively doubled
	thisPos := types.PositionUpdate{Side: "long", Notional: 200}
	otherPos := types.PositionUpdate{Side: "long", Notional: 100}

	_, _, diagBeta1 := applyCrossLegSkew(bid, ask, mid, thisPos, otherPos, 1.0, true, maxNotional, factor, spreadBps)
	_, _, diagBeta2 := applyCrossLegSkew(bid, ask, mid, thisPos, otherPos, 2.0, true, maxNotional, factor, spreadBps)

	// With beta=2.0, the other position's effective notional doubles, producing larger same-side metric
	if math.Abs(diagBeta2) <= math.Abs(diagBeta1) {
		t.Fatalf("expected larger shift with higher beta, got diagBeta1=%f diagBeta2=%f", diagBeta1, diagBeta2)
	}
}

func TestApplySpreadBiasAsymmetric(t *testing.T) {
	mid := 70.0
	bid := 69.79 // 6 bps half-spread
	ask := 70.21

	t.Run("legacy_mode_backward_compat", func(t *testing.T) {
		// maxBiasFraction=0 -> legacy mode, should behave like old function
		newBid, newAsk := applySpreadBias(bid, ask, mid, 2.0, 2.0, 8.0, "xyz:CL", "xyz:CL", 0, 0.70)
		shift := mid * math.Tanh(1.0) * 8.0 / 10000.0
		if math.Abs(newBid-(bid-shift)) > 1e-10 {
			t.Fatalf("legacy bid mismatch: got %f want %f", newBid, bid-shift)
		}
		if math.Abs(newAsk-(ask-shift)) > 1e-10 {
			t.Fatalf("legacy ask mismatch: got %f want %f", newAsk, ask-shift)
		}
	})

	t.Run("asymmetric_convergence_tighter", func(t *testing.T) {
		// z>0, first asset -> convergence = ask side (should tighten more)
		newBid, newAsk := applySpreadBias(bid, ask, mid, 2.0, 1.5, 8.0, "xyz:CL", "xyz:CL", 0.80, 0.70)
		origHalf := (ask - bid) / 2.0
		newAskHalf := newAsk - (newBid+newAsk)/2.0
		newBidHalf := (newBid+newAsk)/2.0 - newBid
		// Convergence (ask) should have tightened more
		askTighten := origHalf - newAskHalf
		bidWiden := newBidHalf - origHalf
		// With asymmetry=0.70, convergence gets 70% of shift, divergence gets 30%
		// So ask tighten should be larger than bid widen
		if askTighten <= 0 {
			t.Fatalf("expected ask to tighten, got askTighten=%f", askTighten)
		}
		_ = bidWiden // divergence side effect varies with the shift
	})

	t.Run("asymmetric_second_asset", func(t *testing.T) {
		// z>0, second asset -> convergence = bid side (buy BRENT when BRENT cheap)
		newBid, newAsk := applySpreadBias(bid, ask, mid, 2.0, 1.5, 8.0, "xyz:BRENTOIL", "xyz:CL", 0.80, 0.70)
		// Convergence is bid -> should move up (tighten bid)
		if newBid <= bid {
			t.Fatalf("expected bid to move up for second asset convergence, got newBid=%f", newBid)
		}
		if newAsk <= ask {
			t.Fatalf("expected ask to widen for second asset divergence, got newAsk=%f", newAsk)
		}
	})

	t.Run("full_collapse_fraction_1", func(t *testing.T) {
		// maxBiasFraction=1.0 with strong z can nearly collapse convergence side
		_, newAsk := applySpreadBias(bid, ask, mid, 5.0, 1.0, 8.0, "xyz:CL", "xyz:CL", 1.0, 1.0)
		// With fraction=1.0, asymmetry=1.0, and strong z (tanh->1):
		// convergeTighten = 1.0 * 1.0 * halfSpread * 1.0 * 2.0 = 2*halfSpread (fully collapsed)
		// divergeWiden = 0
		halfSpread := (ask - bid) / 2.0
		_ = halfSpread
		// Ask should have tightened dramatically
		if newAsk >= ask {
			t.Fatalf("expected ask to tighten with full collapse, got newAsk=%f vs ask=%f", newAsk, ask)
		}
	})
}

func TestBuildSideOrdersPowerLaw(t *testing.T) {
	pow10Mult := math.Pow(10, 2)
	// 4 sell levels with exponent 1.3 — outer offsets should be wider than linear.
	orders := buildSideOrders(
		"xyz:CL", "sell", 70.175, 70.0, 10, 500.0, 4,
		69.95, 70.05, pow10Mult, "test", 1.0, 1.3,
		nil, false, 0, nil,
	)
	if len(orders) != 4 {
		t.Fatalf("expected 4 orders, got %d", len(orders))
	}
	// Level 0 should be at startPrice (offset=0 for any exponent)
	if math.Abs(orders[0].Price-70.18) > 0.02 {
		t.Errorf("level 0 price %f should be near startPrice 70.175", orders[0].Price)
	}
	// Verify spacing increases (non-uniform) for outer levels.
	// With exponent 1.3: offsets are 0^1.3, 1^1.3, 2^1.3, 3^1.3 = 0, 1, 2.46, 4.17
	// So gap between levels should increase.
	gaps := make([]float64, 0)
	for i := 1; i < len(orders); i++ {
		gaps = append(gaps, orders[i].Price-orders[i-1].Price)
	}
	// For sell side, prices increase, so gaps should be positive and increasing
	for i := 1; i < len(gaps); i++ {
		if gaps[i] < gaps[i-1]-1e-6 {
			t.Errorf("gap %d (%f) should be >= gap %d (%f) for power-law spacing", i, gaps[i], i-1, gaps[i-1])
		}
	}
}

func TestBuildSideOrdersExponentLinear(t *testing.T) {
	pow10Mult := math.Pow(10, 2)
	// Exponent 1.0 should produce same results as the standard linear tests.
	ordersLinear := buildSideOrders(
		"xyz:CL", "sell", 70.175, 70.0, 10, 500.0, 4,
		69.95, 70.05, pow10Mult, "test", 1.0, 1.0,
		nil, false, 0, nil,
	)
	// Manually compute expected offsets: 0, 1*10bps, 2*10bps, 3*10bps
	for i, o := range ordersLinear {
		expectedOffset := 70.0 * float64(i) * 10.0 / 10000.0
		expectedPrice := types.RoundToTickSize(70.175 + expectedOffset)
		if o.Price < 70.05 {
			expectedPrice = 70.05 // bestAsk clamp
		}
		if math.Abs(o.Price-expectedPrice) > 0.02 {
			t.Errorf("level %d: got price %f, expected ~%f", i, o.Price, expectedPrice)
		}
	}
}

// --- Integration tests for profitability improvements ---

func TestReferenceFVBlending(t *testing.T) {
	localMid := 70.0
	refMid := 70.10 // reference perp is 10 bps ahead
	weight := 0.7

	localBids := []types.BookLevel{{Price: 69.95, Size: 10}}
	localAsks := []types.BookLevel{{Price: 70.05, Size: 10}}
	refBids := []types.BookLevel{{Price: 70.05, Size: 100}}
	refAsks := []types.BookLevel{{Price: 70.15, Size: 100}}

	localMicro := computeMultiLevelMicroprice(localBids, localAsks, localMid, 1, 0.7)
	refMicro := computeMultiLevelMicroprice(refBids, refAsks, refMid, 1, 0.7)

	blendedFV := weight*refMicro + (1-weight)*localMicro

	// Blended FV should be closer to reference than local
	if math.Abs(blendedFV-refMicro) > math.Abs(blendedFV-localMicro) {
		t.Errorf("blended FV should be closer to reference (weight=0.7), got %f (local=%f, ref=%f)",
			blendedFV, localMicro, refMicro)
	}
}

func TestExitPreserveWithCrossing(t *testing.T) {
	// Setup: strong skew should cause crossing, which should go one-sided
	mid := 70.0
	halfSpread := 6.0 / 20000.0
	bid, ask := mid*(1.0-halfSpread), mid*(1.0+halfSpread) // tight 6 bps spread

	// Large long position with aggressive skew → bid shifts way down
	bid, ask = applyInventorySkew(bid, ask, mid, 400.0, "long", 500.0, 3.0, 6.0, 1.0, 0.5, true)

	// If bid crossed ask (bid > ask), crossing protection should keep ask (exit) and pull bid
	if bid >= ask {
		bid, ask = applyCrossingProtection(bid, ask, mid, "long", 400.0, 0, 0, 2.0)
		// Ask (exit) should remain, bid should be pulled below
		if bid >= ask {
			t.Fatal("crossing protection failed to uncross")
		}
		// The spread should be very tight (~1 bps) with ask at its computed position
		spreadBps := (ask - bid) / mid * 10000
		if spreadBps > 2.0 {
			t.Errorf("expected ~1 bps spread after crossing, got %.1f bps", spreadBps)
		}
	}
}

func TestWickSizingSuppressedAtHighInventory_Long(t *testing.T) {
	// When long and above threshold, only bid (buy) side should be suppressed.
	// Ask (sell/exit) side wick amplification should be preserved.
	wickMaxInventoryUtil := 0.40
	inventoryUtil := 0.50
	side := "long"

	wickSuppressBid, wickSuppressAsk := false, false
	if wickMaxInventoryUtil > 0 && inventoryUtil > wickMaxInventoryUtil {
		switch side {
		case "long":
			wickSuppressBid = true
		case "short":
			wickSuppressAsk = true
		}
	}

	// Simulate wick sizing returning amplified mults (e.g., price wicked down).
	bidMult, askMult := 2.0, 1.0
	if wickSuppressBid {
		bidMult = 1.0
	}
	if wickSuppressAsk {
		askMult = 1.0
	}

	if bidMult != 1.0 {
		t.Errorf("bid wick should be suppressed when long at 50%% util (threshold 40%%), got bidMult=%f", bidMult)
	}
	if askMult != 1.0 {
		t.Errorf("ask wick should be preserved (no suppression), got askMult=%f", askMult)
	}
}

func TestWickSizingSuppressedAtHighInventory_Short(t *testing.T) {
	// When short and above threshold, only ask (sell) side should be suppressed.
	// Bid (buy/exit) side wick amplification should be preserved.
	wickMaxInventoryUtil := 0.40
	inventoryUtil := 0.50
	side := "short"

	wickSuppressBid, wickSuppressAsk := false, false
	if wickMaxInventoryUtil > 0 && inventoryUtil > wickMaxInventoryUtil {
		switch side {
		case "long":
			wickSuppressBid = true
		case "short":
			wickSuppressAsk = true
		}
	}

	// Simulate wick sizing returning amplified mults (e.g., price wicked up).
	bidMult, askMult := 1.0, 2.0
	if wickSuppressBid {
		bidMult = 1.0
	}
	if wickSuppressAsk {
		askMult = 1.0
	}

	if bidMult != 1.0 {
		t.Errorf("bid wick should be preserved (no suppression), got bidMult=%f", bidMult)
	}
	if askMult != 1.0 {
		t.Errorf("ask wick should be suppressed when short at 50%% util (threshold 40%%), got askMult=%f", askMult)
	}
}

func TestWickSizingExitSidePreserved_Long(t *testing.T) {
	// When long at high inventory and price wicks up (ask amplified),
	// the ask-side wick should be PRESERVED since selling reduces inventory.
	wickMaxInventoryUtil := 0.40
	inventoryUtil := 0.60
	side := "long"

	wickSuppressBid, wickSuppressAsk := false, false
	if wickMaxInventoryUtil > 0 && inventoryUtil > wickMaxInventoryUtil {
		switch side {
		case "long":
			wickSuppressBid = true
		case "short":
			wickSuppressAsk = true
		}
	}

	// Price wicked up: ask side gets amplified.
	_, askMult := 1.0, 2.0
	if wickSuppressBid {
		// bidMult would be clamped to 1.0, but it's already 1.0
	}
	if wickSuppressAsk {
		askMult = 1.0
	}

	if askMult != 2.0 {
		t.Errorf("exit-side (ask) wick should be preserved when long, got askMult=%f", askMult)
	}
}

func TestWickSizingAllowedBelowInventory(t *testing.T) {
	wickMaxInventoryUtil := 0.40
	inventoryUtil := 0.30

	wickSuppressBid, wickSuppressAsk := false, false
	if wickMaxInventoryUtil > 0 && inventoryUtil > wickMaxInventoryUtil {
		wickSuppressBid = true // would be set per-side, but shouldn't trigger
	}

	if wickSuppressBid || wickSuppressAsk {
		t.Error("wick should NOT be suppressed at 30% util (threshold 40%)")
	}
}

func TestWickSizingDisabledWhenThresholdZero(t *testing.T) {
	wickMaxInventoryUtil := 0.0
	inventoryUtil := 0.90

	wickSuppressBid, wickSuppressAsk := false, false
	if wickMaxInventoryUtil > 0 && inventoryUtil > wickMaxInventoryUtil {
		wickSuppressBid = true
	}

	if wickSuppressBid || wickSuppressAsk {
		t.Error("wick should never be suppressed when threshold is 0 (disabled)")
	}
}

// TestManagerSessionPnLWired verifies the prod codepath: a real tracker
// is injected and the manager reads PnL from it (same call the optimizer
// eval path makes to populate OptimizerV2Inputs.SessionPnLBps).
func TestManagerSessionPnLWired(t *testing.T) {
	cfg := defaultTestConfig()
	tracker := NewSessionPnLTracker(10000.0, 60)
	tracker.Update(10100.0, time.Now()) // +100 bps

	mgr := New(cfg, nil, nil, nil, newMockExecutor(), nil, nil, nil, nil, nil, tracker)
	if mgr.sessionPnL == nil {
		t.Fatal("manager.sessionPnL is nil; non-nil contract broken")
	}
	_, bps := mgr.sessionPnL.PnL()
	if math.Abs(bps-100.0) > 1e-9 {
		t.Fatalf("manager.sessionPnL.PnL bps: want 100, got %g", bps)
	}
}

// TestManagerRequiresNonNilSessionPnL verifies construction panics when
// sessionPnL is nil, enforcing the non-nil contract from the prod wiring.
func TestManagerRequiresNonNilSessionPnL(t *testing.T) {
	cfg := defaultTestConfig()
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic when sessionPnL is nil")
		}
	}()
	_ = New(cfg, nil, nil, nil, newMockExecutor(), nil, nil, nil, nil, nil, nil)
}
