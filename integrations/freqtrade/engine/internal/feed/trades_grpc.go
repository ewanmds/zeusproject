package feed

import (
	"fmt"
	"log/slog"
	"time"

	"hyperliquid-market-maker/internal/types"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

// GRPCTradeFeed streams public trades via QuickNode gRPC and fans out to channels.
// This replaces the WebSocket-based TradeFeed for HIP-3 assets where the public
// WS endpoint rejects trade subscriptions (1006 close within 300ms).
type GRPCTradeFeed struct {
	stream    *hyperliquid.GRPCStream
	channels  []chan types.TradeUpdate
	baseToDex map[string]string // e.g. "NVDA" → "xyz:NVDA"
}

// NewGRPCTradeFeed creates a gRPC-based trade feed for the given coins.
// endpoint is the QuickNode gRPC URL.
// coins are dex-qualified names (e.g. "xyz:NVDA").
// reconnectCh (optional) receives a signal on each successful reconnect.
func NewGRPCTradeFeed(endpoint string, coins []string, reconnectCh chan<- struct{}, channels ...chan types.TradeUpdate) *GRPCTradeFeed {
	baseToDex := make(map[string]string, len(coins))
	for _, coin := range coins {
		_, base := types.ParseAsset(coin)
		baseToDex[base] = coin
	}

	tf := &GRPCTradeFeed{
		channels:  channels,
		baseToDex: baseToDex,
	}

	grpcConfig := &hyperliquid.GRPCStreamConfig{
		Secure:       true,
		Reconnect:    true,
		MaxReconnect: 0, // unlimited
		OnError: func(err error) {
			slog.Error("[TRADES-GRPC] stream error", "error", err)
		},
		OnClose: func() {
			slog.Warn("[TRADES-GRPC] stream closed")
		},
		OnConnect: func() {
			slog.Info("[TRADES-GRPC] stream connected")
		},
		OnReconnect: func(attempt int) {
			slog.Info("[TRADES-GRPC] stream reconnected", "attempt", attempt)
			if reconnectCh != nil {
				select {
				case reconnectCh <- struct{}{}:
				default:
				}
			}
		},
	}

	stream := hyperliquid.NewGRPCStream(endpoint, grpcConfig)
	tf.stream = stream

	// Subscribe to trades for all coins. gRPC server-side filtering handles
	// multi-coin subscriptions correctly (unlike the WS SDK path).
	stream.Trades(coins, func(data map[string]any) {
		tf.handleTrade(data)
	})

	return tf
}

// Start begins the gRPC stream (non-blocking).
func (tf *GRPCTradeFeed) Start() error {
	return tf.stream.Start()
}

// Stop shuts down the gRPC stream.
func (tf *GRPCTradeFeed) Stop() {
	tf.stream.Stop()
}

func (tf *GRPCTradeFeed) handleTrade(data map[string]any) {
	trade, err := parseTrade(data)
	if err != nil {
		slog.Debug("[TRADES-GRPC] parse error", "error", err)
		return
	}

	// gRPC delivers dex-qualified coins (e.g. "xyz:NVDA"), but fall back to
	// baseToDex remapping if bare names arrive.
	if dexName, ok := tf.baseToDex[trade.Coin]; ok {
		trade.Coin = dexName
	}

	for _, ch := range tf.channels {
		select {
		case ch <- trade:
		default:
			select {
			case <-ch:
			default:
			}
			select {
			case ch <- trade:
			default:
			}
		}
	}
}

func parseTrade(data map[string]any) (types.TradeUpdate, error) {
	coin, ok := data["coin"]
	if !ok {
		return types.TradeUpdate{}, fmt.Errorf("missing coin field")
	}
	coinStr, ok := coin.(string)
	if !ok {
		return types.TradeUpdate{}, fmt.Errorf("coin is not a string")
	}

	px, ok := parseFloat(data["px"])
	if !ok {
		return types.TradeUpdate{}, fmt.Errorf("parse px: unsupported type %T", data["px"])
	}
	sz, ok := parseFloat(data["sz"])
	if !ok {
		return types.TradeUpdate{}, fmt.Errorf("parse sz: unsupported type %T", data["sz"])
	}

	if px <= 0 {
		return types.TradeUpdate{}, fmt.Errorf("price must be positive, got %f", px)
	}
	if sz <= 0 {
		return types.TradeUpdate{}, fmt.Errorf("size must be positive, got %f", sz)
	}

	side := "buy"
	if s, ok := data["side"]; ok {
		switch s {
		case "A", "ask", "sell":
			side = "sell"
		case "B", "bid", "buy":
			side = "buy"
		}
	}

	// Extract crossed field: true = taker (aggressor), false = maker.
	// Only taker trades represent directional market pressure.
	var crossed bool
	if c, ok := data["crossed"]; ok {
		switch v := c.(type) {
		case bool:
			crossed = v
		case string:
			crossed = v == "true"
		}
	}

	return types.TradeUpdate{
		Coin:      coinStr,
		Price:     px,
		Size:      sz,
		Side:      side,
		Crossed:   crossed,
		Timestamp: time.Now(),
	}, nil
}
