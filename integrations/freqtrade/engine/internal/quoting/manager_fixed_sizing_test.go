package quoting

import (
	"hyperliquid-market-maker/internal/types"
	"testing"
	"time"
)

func TestFixedSizingContinuesAfterBuyAndSellFills(t *testing.T) {
	const coin = "xyz:SP500"
	cfg := defaultTestConfig()
	cfg.Strategy.Assets = []string{coin}
	cfg.MarketMaking.QuoteNotional = 40
	cfg.MarketMaking.AdaptiveNotionalEnabled = false
	cfg.MarketMaking.AdaptiveNotionalHalfLifeSec = 60
	cfg.MarketMaking.AdaptiveNotionalTargetPerMin = 0
	cfg.MarketMaking.AdaptiveNotionalMin = 0
	cfg.MarketMaking.AdaptiveNotionalMax = 0
	mgr := New(cfg, nil, nil, nil, newMockExecutor(), map[string]int{coin: 3}, nil, nil, nil, nil, newTestSessionPnL())
	book := types.BookUpdate{Coin: coin, BestBid: 7646.5, BestAsk: 7646.7, Mid: 7646.6, Timestamp: time.Now(), Bids: []types.BookLevel{{Price: 7646.5, Size: 1}}, Asks: []types.BookLevel{{Price: 7646.7, Size: 1}}}
	for i, side := range []string{"", "buy", "sell", "buy", "sell"} {
		if side != "" {
			mgr.fillRates[coin].RecordFillSide(side, 38.23)
		}
		orders := mgr.computeOrdersForCoin(coin, book, types.SpreadSignal{}, types.PositionUpdate{Coin: coin}, map[string]sideExposure{coin: {}}, 100, 160, coinQuotingHealth{}, pairReduceState{})
		if len(orders) != 2 {
			t.Fatalf("stage %d after %q fill: expected both quotes, got %+v", i, side, orders)
		}
		for _, o := range orders {
			if o.Size <= 0 || o.Price*o.Size < 10 || o.Price*o.Size > 40.1 {
				t.Fatalf("stage %d invalid fixed-size quote %+v", i, o)
			}
		}
	}
}
