package quoting

import (
	"context"
	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/tradeflow"
	"hyperliquid-market-maker/internal/types"
	"hyperliquid-market-maker/internal/vwap"
	"testing"
	"time"
)

func TestZeusOrderCapUsesQuotePrice(t *testing.T) {
	orders := []types.OrderRequest{{Coin: "xyz:SP500", Side: "buy", Price: 100.1, Size: 1}, {Coin: "xyz:SP500", Side: "sell", Price: 100.2, Size: 1}}
	result := capZeusOrders(orders, types.PositionUpdate{}, 40, 1000)
	if len(result) != 2 {
		t.Fatal(result)
	}
	for _, o := range result {
		if o.Size*o.Price > 40 {
			t.Fatal(o)
		}
	}
	result = capZeusOrders(orders, types.PositionUpdate{Side: "long", Size: 0.2}, 40, 1000)
	for _, o := range result {
		if o.Side == "sell" && (!o.ReduceOnly || o.Size > 0.2) {
			t.Fatal(o)
		}
	}
}

func TestZeusLifetimeQuotaDoesNotPreventBootstrap(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.ZeusMode = true
	cfg.RateLimit = defaultRLConfig()
	mgr := &Manager{cfg: cfg, executor: newMockExecutor(), supervisor: NewSupervisor()}
	// Reproduce the account's shape: large lifetime usage, viable credit.
	mgr.UpdateRateLimit(733, 731870)
	if mgr.supervisor.IsRLCritical() {
		t.Fatal("lifetime cap falsely prevented the first order")
	}
	if got := mgr.rateLimitRemainingPctLocked(733); got != 100 {
		t.Fatal(got)
	}
	// Absolute safety reserve still prevents new exposure.
	mgr.UpdateRateLimit(299, 731870)
	if !mgr.supervisor.IsRLCritical() {
		t.Fatal("absolute API reserve bypassed")
	}
	status, blocked := mgr.ZeusQuoteStatus("xyz:SP500")
	if !blocked || status == "" {
		t.Fatal("API halt missing from dashboard status")
	}
	// A viable credit level can recover; independent risk halts remain.
	mgr.UpdateRateLimit(733, 731870)
	if mgr.supervisor.IsRLCritical() {
		t.Fatal("API guard failed to recover")
	}
	mgr.supervisor.Signal(SignalRiskHalt)
	mgr.UpdateRateLimit(733, 731870)
	if mgr.supervisor.CanQuote() {
		t.Fatal("independent risk halt cleared")
	}
	// The standalone bot keeps its existing percentage policy.
	cfg.ZeusMode = false
	if got := mgr.rateLimitRemainingPctLocked(733); got >= 3 {
		t.Fatal("standalone policy changed")
	}
}

func TestZeusQuotaRetainsExhaustionProtection(t *testing.T) {
	cfg := defaultTestConfig()
	cfg.ZeusMode = true
	cfg.RateLimit = defaultRLConfig()
	mgr := &Manager{cfg: cfg, rlBootSeeded: true, rlBootRemaining: 733, rateLimitCap: 731870}
	fillHistory(mgr, 5000, 15)
	tier, _, _ := mgr.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Fatal("rapid API budget depletion no longer triggers protection")
	}
}

func TestZeusSignalGatesKeepBothSidesButHardHealthStillBlocks(t *testing.T) {
	for _, scenario := range []string{"trend", "flow", "regime", "shock", "optimizer"} {
		t.Run(scenario, func(t *testing.T) {
			cfg := defaultTestConfig()
			no := false
			cfg.SignalPausesEnabled = &no
			short := vwap.NewCalculator(time.Minute)
			long := vwap.NewCalculator(time.Hour)
			for i := 0; i < 30; i++ {
				short.AddTrade(70+float64(i)*0.01, 10, time.Now())
				long.AddTrade(100, 10, time.Now())
			}
			flow := tradeflow.NewCalculator(time.Minute, 1)
			flow.AddTrade(100, "sell", time.Now())
			mgr := New(cfg, nil, nil, nil, newMockExecutor(), map[string]int{"xyz:CL": 3}, map[string]*vwap.Calculator{"xyz:CL": short}, map[string]*vwap.Calculator{"xyz:CL": long}, map[string]tradeflow.ImbalanceCalculator{"xyz:CL": flow}, nil, newTestSessionPnL())
			switch scenario {
			case "trend":
				cfg.MarketMaking.TrendGuardEnabled = true
				cfg.MarketMaking.TrendGuardThresholdPct = 0.01
				cfg.MarketMaking.TrendGuardSaturationPct = 0.1
				cfg.MarketMaking.TrendGuardMaxSuppress = 1
				cfg.MarketMaking.TrendGuardFreezeThreshold = 0.1
			case "flow":
				cfg.MarketMaking.FlowGateEnabled = true
				cfg.MarketMaking.FlowGateThreshold = 0.1
				cfg.MarketMaking.FlowGateMaxSuppress = 1
			case "regime":
				cfg.MarketMaking.RegimeGateEnabled = true
				cfg.MarketMaking.RegimeGateMode = "exit_only"
				cfg.MarketMaking.RegimeGateMaxSuppression = 1
				cfg.MarketMaking.RegimeGateMinDurationSec = 1
				rd := NewRegimeDetector(20)
				fillTrendingWindow(rd, 30)
				rd.pendingSince = time.Now().Add(-time.Minute)
				mgr.regimes["xyz:CL"] = rd
			case "shock":
				cfg.MarketMaking.PriceShockFreezeEntry = true
				tracker := NewPriceShockTracker(time.Second, time.Minute, 1)
				tracker.active = PriceShockSnapshot{Active: true, Direction: "down", EntrySide: "buy", ActiveUntil: time.Now().Add(time.Minute)}
				mgr.priceShockTrackers["xyz:CL"] = tracker
			case "optimizer":
				overlay := neutralOverlay()
				overlay.NotionalMult = 0
				mgr.optimizersV2["xyz:CL"] = &OptimizerV2{cfg: config.OptimizerV2Config{Enabled: true}, overlay: overlay}
			}
			book := types.BookUpdate{Coin: "xyz:CL", Mid: 70, BestBid: 69.99, BestAsk: 70.01}
			pos := types.PositionUpdate{Coin: "xyz:CL", Side: "long", Size: 0.5, Notional: 35, AvgEntry: 70}
			orders := mgr.computeOrdersForCoin("xyz:CL", book, types.SpreadSignal{}, pos, map[string]sideExposure{}, 500, 1000, coinQuotingHealth{}, pairReduceState{})
			buy, sell := false, false
			for _, order := range orders {
				buy = buy || order.Side == "buy"
				sell = sell || order.Side == "sell"
			}
			if !buy || !sell {
				t.Fatalf("signal removed a side: %v", orders)
			}
			blocked := mgr.computeOrdersForCoin("xyz:CL", book, types.SpreadSignal{}, pos, map[string]sideExposure{}, 500, 1000, coinQuotingHealth{Halted: true, Reason: "position_mismatch"}, pairReduceState{})
			if len(blocked) != 0 {
				t.Fatal("hard execution guard bypassed")
			}
		})
	}
}
func TestSignalPausePolicyDefaultsAndShockChain(t *testing.T) {
	cfg := defaultTestConfig()
	if !cfg.SignalPauses() {
		t.Fatal("standalone default changed")
	}
	no := false
	cfg.SignalPausesEnabled = &no
	mgr := New(cfg, make(chan types.BookUpdate, 1), make(chan types.SpreadSignal, 1), make(chan types.RiskAction, 1), newMockExecutor(), map[string]int{}, nil, nil, nil, nil, newTestSessionPnL())
	mgr.triggerShockChainEscalation(context.Background(), "xyz:CL", 5, PriceShockSnapshot{})
	if !mgr.supervisor.CanQuote() {
		t.Fatal("signal policy allowed shock-chain pause")
	}
	// Hard risk halt is independent of signal policy.
	mgr.supervisor.Signal(SignalRiskHalt)
	if mgr.supervisor.CanQuote() {
		t.Fatal("risk halt bypassed")
	}
}
