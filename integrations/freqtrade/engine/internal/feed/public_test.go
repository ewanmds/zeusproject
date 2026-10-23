package feed

import (
	"fmt"
	"hyperliquid-market-maker/internal/types"
	"testing"
	"time"
)

func TestPublicFeedNamespacesDedupAndStale(t *testing.T) {
	trades := make(chan types.TradeUpdate, 10)
	f := NewPublicFeed([]string{"xyz:SNDK", "io:SNDK"}, nil, trades, nil)
	f.connectedAt = time.Now().Add(-time.Second)
	now := time.Now().UnixMilli()
	message := func(coin string, stamp int64) []byte {
		return []byte(fmt.Sprintf(`{"channel":"trades","data":[{"coin":%q,"px":"100","sz":"2","side":"A","time":%d,"tid":42}]}`, coin, stamp))
	}
	for _, coin := range []string{"xyz:SNDK", "io:SNDK", "xyz:SNDK", "SNDK"} {
		if err := f.handle(message(coin, now)); err != nil {
			t.Fatal(err)
		}
	}
	f.handle(message("xyz:SNDK", now-60000))
	if len(trades) != 2 {
		t.Fatalf("expected two namespaced unique trades, got %d", len(trades))
	}
	for i := 0; i < 2; i++ {
		trade := <-trades
		if trade.Side != "sell" || !trade.Crossed || trade.Timestamp.UnixMilli() != now {
			t.Fatalf("bad aggressor trade: %+v", trade)
		}
	}
}

func TestPublicFeedBookGuards(t *testing.T) {
	books := make(chan types.BookUpdate, 10)
	f := NewPublicFeed([]string{"xyz:CL"}, []chan types.BookUpdate{books}, nil, nil)
	message := func(stamp int64, bid, ask string) []byte {
		return []byte(fmt.Sprintf(`{"channel":"l2Book","data":{"coin":"xyz:CL","time":%d,"levels":[[{"px":%q,"sz":"2","n":1}],[{"px":%q,"sz":"3","n":1}]]}}`, stamp, bid, ask))
	}
	f.handle(message(time.Now().Add(-time.Minute).UnixMilli(), "100", "101"))
	f.handle(message(time.Now().UnixMilli(), "102", "101"))
	f.handle(message(time.Now().UnixMilli(), "100", "101"))
	if len(books) != 1 {
		t.Fatalf("expected only fresh uncrossed book, got %d", len(books))
	}
}

func TestPublicTradeClockDrift(t *testing.T) {
	trades := make(chan types.TradeUpdate, 1)
	f := NewPublicFeed([]string{"xyz:CL"}, nil, trades, nil)
	f.serverOffset = 15 * time.Second
	f.connectedAt = time.Now().Add(f.serverOffset - time.Second)
	stamp := time.Now().Add(f.serverOffset).UnixMilli()
	msg := []byte(fmt.Sprintf(`{"channel":"trades","data":[{"coin":"xyz:CL","px":"100","sz":"1","side":"B","time":%d,"tid":1}]}`, stamp))
	if err := f.handle(msg); err != nil {
		t.Fatal(err)
	}
	select {
	case trade := <-trades:
		if age := time.Since(trade.Timestamp); age < -time.Second || age > time.Second {
			t.Fatalf("trade was not mapped to local clock: %v", age)
		}
	default:
		t.Fatal("fresh server trade rejected because local clock drifted")
	}
}
