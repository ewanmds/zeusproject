package feed

import (
	"hyperliquid-market-maker/internal/zeusbridge"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"hyperliquid-market-maker/internal/types"

	"github.com/gorilla/websocket"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

// WSFeed wraps the SDK WebSocket stream for UserFills and OrderUpdates.
type WSFeed struct {
	stream *hyperliquid.Stream
	fillCh chan<- types.FillEvent
}

// NewWSFeed creates a WebSocket feed for user events.
// endpoint is the QuickNode URL. address is the user's Hyperliquid address.
// reconnectCh (optional) receives a signal on each successful reconnect so
// consumers can trigger immediate reconciliation instead of waiting for a timer.
func NewWSFeed(endpoint string, address string, fillCh chan<- types.FillEvent, reconnectCh chan<- struct{}) *WSFeed {
	f := &WSFeed{
		fillCh: fillCh,
	}

	wsConfig := &hyperliquid.StreamConfig{
		Reconnect:    true,
		MaxReconnect: 0,                // unlimited
		PingInterval: 20 * time.Second, // aggressive ping to prevent server-side timeouts
		OnError: func(err error) {
			slog.Error("WebSocket stream error", "error", err)
			// After a server-initiated session expiry ("close 1000: Expired"),
			// delay before reconnect to let the server clean up the old session.
			// Without this, rapid reconnects trigger a cascade of 3-5 close-1006
			// disconnects over ~35-50 seconds before stabilizing.
			if websocket.IsCloseError(err, websocket.CloseNormalClosure) &&
				strings.Contains(err.Error(), "Expired") {
				slog.Info("WebSocket session expired, delaying reconnect to avoid 1006 cascade",
					"delay", "5s")
				time.Sleep(5 * time.Second)
			}
		},
		OnClose: func() {
			slog.Warn("WebSocket stream closed")
		},
		OnOpen: func() {
			slog.Info("WebSocket stream connected")
		},
		OnReconnect: func(attempt int) {
			slog.Warn("WebSocket stream reconnecting", "attempt", attempt)
			if reconnectCh != nil {
				select {
				case reconnectCh <- struct{}{}:
				default:
				}
			}
		},
	}

	stream := hyperliquid.NewStream(endpoint, wsConfig)
	f.stream = stream

	// Subscribe to user fills.
	stream.UserFills(address, func(data map[string]any) {
		f.handleUserFills(data)
	})

	// Subscribe to order updates (for logging only).
	stream.OrderUpdates(address, func(data map[string]any) {
		f.handleOrderUpdates(data)
	})

	return f
}

// Start begins the WebSocket stream.
func (f *WSFeed) Start() error {
	return f.stream.Start()
}

// Stop shuts down the WebSocket stream.
func (f *WSFeed) Stop() {
	f.stream.Stop()
}

func (f *WSFeed) handleUserFills(data map[string]any) {
	// Parse fills from the callback data.
	// Expected structures:
	// 1. {"data": {"fills": [{"coin":..., "side":..., "px":..., "sz":..., "oid":...}]}}
	// 2. Top-level {"fills": [...]}
	var fills []any

	if d, ok := data["data"].(map[string]any); ok {
		if fl, ok := d["fills"].([]any); ok {
			fills = fl
		}
	} else if fl, ok := data["fills"].([]any); ok {
		fills = fl
	}

	for _, fill := range fills {
		fm, ok := fill.(map[string]any)
		if !ok {
			continue
		}

		zeusbridge.RecordFill(fm)
		event := types.FillEvent{}

		if coin, ok := fm["coin"].(string); ok {
			event.Coin = coin
		}

		if side, ok := fm["side"].(string); ok {
			switch side {
			case "B", "Buy", "buy":
				event.Side = "buy"
			case "A", "Ask", "ask", "S", "Sell", "sell":
				event.Side = "sell"
			default:
				event.Side = side
				slog.Warn("WS fill: unrecognized side value", "side", side, "coin", event.Coin, "oid", event.OID)
			}
		}

		if px, ok := fm["px"].(string); ok {
			event.Price, _ = strconv.ParseFloat(px, 64)
		}

		if sz, ok := fm["sz"].(string); ok {
			event.Size, _ = strconv.ParseFloat(sz, 64)
		}

		if oid, ok := fm["oid"].(float64); ok {
			event.OID = int64(oid)
		}

		if fee, ok := fm["fee"].(string); ok {
			event.Fee, _ = strconv.ParseFloat(fee, 64)
		}

		if builderFee, ok := fm["builderFee"].(string); ok {
			event.BuilderFee, _ = strconv.ParseFloat(builderFee, 64)
		}

		slog.Debug("ws fill received",
			"coin", event.Coin,
			"side", event.Side,
			"size", event.Size,
			"price", event.Price,
			"oid", event.OID,
			"fee", event.Fee,
			"builder_fee", event.BuilderFee,
		)

		select {
		case f.fillCh <- event:
		default:
			// Non-blocking send failed; spawn goroutine to avoid stalling the WS read loop.
			slog.Warn("fill channel full, sending in background goroutine",
				"coin", event.Coin, "side", event.Side)
			go func(e types.FillEvent) {
				select {
				case f.fillCh <- e:
				case <-time.After(5 * time.Second):
					slog.Error("FATAL: fill channel blocked for 5s, fill lost",
						"coin", e.Coin, "side", e.Side, "price", e.Price, "size", e.Size)
				}
			}(event)
		}
	}
}

func (f *WSFeed) handleOrderUpdates(data map[string]any) {
	slog.Debug("order update received", "data", data)
}
