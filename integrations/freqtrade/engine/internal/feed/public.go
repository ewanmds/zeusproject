package feed

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/types"
)

// PublicFeed uses native Hyperliquid subscriptions, one per exact asset name.
// A separate instance handles books and trades so either stream can reconnect.
type PublicFeed struct {
	endpoint     string
	coins        map[string]bool
	book         *GRPCFeed // reuse existing parsing, crossed-book and outlier guards
	trades       chan types.TradeUpdate
	reconnect    chan<- struct{}
	ctx          context.Context
	cancel       context.CancelFunc
	mu           sync.Mutex
	conn         *websocket.Conn
	wg           sync.WaitGroup
	seen         map[string]time.Time
	connectedAt  time.Time
	serverOffset time.Duration
}

func NewPublicFeed(coins []string, books []chan types.BookUpdate, trades chan types.TradeUpdate, reconnect chan<- struct{}) *PublicFeed {
	ctx, cancel := context.WithCancel(context.Background())
	f := &PublicFeed{endpoint: "wss://api.hyperliquid.xyz/ws", coins: map[string]bool{}, trades: trades, reconnect: reconnect, ctx: ctx, cancel: cancel, seen: map[string]time.Time{}}
	for _, coin := range coins {
		f.coins[coin] = true
	}
	if len(books) > 0 {
		f.book = &GRPCFeed{channels: books, prevMid: map[string]float64{}, outlierState: map[string]outlierState{}}
	}
	return f
}

func (f *PublicFeed) connect() (*websocket.Conn, error) {
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second, Proxy: http.ProxyFromEnvironment}
	c, response, err := dialer.DialContext(f.ctx, f.endpoint, nil)
	if err != nil {
		return nil, err
	}
	// Use the handshake server clock for freshness checks; Windows clocks can
	// drift. Local monotonic receipt times still drive downstream feed guards.
	if serverTime, err := http.ParseTime(response.Header.Get("Date")); err == nil {
		f.serverOffset = serverTime.Sub(time.Now())
	}
	f.mu.Lock()
	if f.ctx.Err() != nil {
		f.mu.Unlock()
		c.Close()
		return nil, f.ctx.Err()
	}
	f.conn = c
	f.mu.Unlock()
	for coin := range f.coins {
		kind := "l2Book"
		if f.trades != nil {
			kind = "trades"
		}
		if err := c.WriteJSON(map[string]any{"method": "subscribe", "subscription": map[string]any{"type": kind, "coin": coin}}); err != nil {
			c.Close()
			return nil, err
		}
	}
	f.connectedAt = time.Now().Add(f.serverOffset)
	if f.book != nil {
		f.book.mu.Lock()
		f.book.prevMid = map[string]float64{}
		f.book.outlierState = map[string]outlierState{}
		f.book.mu.Unlock()
	}
	return c, nil
}

func (f *PublicFeed) Start() error {
	c, err := f.connect()
	if err != nil {
		return err
	}
	f.wg.Add(1)
	go func() {
		defer f.wg.Done()
		backoff := time.Second
		for {
			err := f.consume(c)
			c.Close()
			if f.ctx.Err() != nil {
				return
			}
			slog.Warn("public Hyperliquid stream disconnected", "error", err)
			for {
				select {
				case <-f.ctx.Done():
					return
				case <-time.After(backoff):
				}
				c, err = f.connect()
				if err == nil {
					backoff = time.Second
					break
				}
				if backoff < 16*time.Second {
					backoff *= 2
				}
			}
			select {
			case f.reconnect <- struct{}{}:
			default:
			}
			slog.Info("public Hyperliquid stream reconnected")
		}
	}()
	return nil
}

func (f *PublicFeed) Stop() {
	f.cancel()
	f.mu.Lock()
	if f.conn != nil {
		f.conn.Close()
	}
	f.mu.Unlock()
	f.wg.Wait()
}

func (f *PublicFeed) consume(c *websocket.Conn) error {
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(20 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-f.ctx.Done():
				return
			case <-ticker.C:
				c.SetWriteDeadline(time.Now().Add(5 * time.Second))
				if err := c.WriteJSON(map[string]string{"method": "ping"}); err != nil {
					c.Close()
					return
				}
			}
		}
	}()
	for {
		c.SetReadDeadline(time.Now().Add(60 * time.Second))
		_, raw, err := c.ReadMessage()
		if err != nil {
			return err
		}
		if err := f.handle(raw); err != nil {
			return err
		}
	}
}

func (f *PublicFeed) handle(raw []byte) error {
	var envelope struct {
		Channel string          `json:"channel"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return err
	}
	if envelope.Channel == "error" {
		return fmt.Errorf("subscription rejected: %s", envelope.Data)
	}
	now := time.Now().Add(f.serverOffset)
	if envelope.Channel == "l2Book" && f.book != nil {
		var data map[string]any
		if err := json.Unmarshal(envelope.Data, &data); err != nil {
			return err
		}
		coin, _ := data["coin"].(string)
		stamp, _ := data["time"].(float64)
		age := now.Sub(time.UnixMilli(int64(stamp)))
		slog.Debug("public book received", "coin", coin, "exchange_time", stamp, "age_ms", age.Milliseconds(), "configured", f.coins[coin])
		if f.coins[coin] && stamp > 0 && age < 5*time.Second && age > -5*time.Second {
			f.book.handleL2Book(coin, data)
		}
	}
	if envelope.Channel == "trades" && f.trades != nil {
		var rows []map[string]any
		if err := json.Unmarshal(envelope.Data, &rows); err != nil {
			return err
		}
		for key, expiry := range f.seen {
			if now.After(expiry) {
				delete(f.seen, key)
			}
		}
		for _, data := range rows {
			coin, _ := data["coin"].(string)
			stamp, _ := data["time"].(float64)
			ts := time.UnixMilli(int64(stamp))
			if !f.coins[coin] || ts.Before(f.connectedAt) || now.Sub(ts) > 15*time.Second || ts.After(now.Add(5*time.Second)) {
				continue
			}
			id, ok := data["tid"]
			if !ok {
				continue
			}
			key := fmt.Sprint(coin, ":", stamp, ":", id)
			if _, exists := f.seen[key]; exists {
				continue
			}
			trade, err := parseTrade(data)
			if err != nil {
				continue
			}
			// Public trades encode the aggressor side, unlike paired user fills.
			trade.Crossed = true
			trade.Timestamp = ts.Add(-f.serverOffset)
			f.seen[key] = now.Add(2 * time.Minute)
			select {
			case f.trades <- trade:
			default:
				slog.Warn("public trade channel full", "coin", coin)
			}
		}
	}
	return nil
}

// PublicMarkets fetches only configured perpetual DEXes from the native info API.
func PublicMarkets(endpoint string, assets []string) (*hyperliquid.Markets, error) {
	markets := &hyperliquid.Markets{HIP3: map[string][]hyperliquid.Market{}}
	dexes := map[string]bool{}
	for _, coin := range assets {
		dex, _ := types.ParseAsset(coin)
		dexes[dex] = true
	}
	client := &http.Client{Timeout: 15 * time.Second}
	for dex := range dexes {
		body, _ := json.Marshal(map[string]string{"type": "meta", "dex": dex})
		resp, err := client.Post(endpoint+"/info", "application/json", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		var meta struct {
			Universe []struct {
				Name       string `json:"name"`
				SzDecimals int    `json:"szDecimals"`
			} `json:"universe"`
		}
		err = json.NewDecoder(resp.Body).Decode(&meta)
		resp.Body.Close()
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("metadata HTTP %d", resp.StatusCode)
		}
		for _, m := range meta.Universe {
			name := strings.TrimPrefix(m.Name, dex+":")
			market := hyperliquid.Market{Name: name, SzDecimals: m.SzDecimals}
			if dex == "" {
				markets.Perps = append(markets.Perps, market)
			} else {
				markets.HIP3[dex] = append(markets.HIP3[dex], market)
			}
		}
	}
	return markets, nil
}
