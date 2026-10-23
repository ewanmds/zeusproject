package feed

import (
	"testing"

	"hyperliquid-market-maker/internal/types"
)

func TestGRPCTradeHandleDexQualifiedCoin(t *testing.T) {
	ch := make(chan types.TradeUpdate, 10)
	tf := &GRPCTradeFeed{
		channels:  []chan types.TradeUpdate{ch},
		baseToDex: map[string]string{"NVDA": "xyz:NVDA", "MU": "xyz:MU"},
	}

	// gRPC delivers trades with dex-qualified coin names.
	data := map[string]any{
		"coin":          "xyz:NVDA",
		"px":            "120.50",
		"sz":            "5.0",
		"side":          "B",
		"_block_number": float64(938707017),
		"_timestamp":    "2026-03-29T04:34:40.339773960",
		"_user":         "0x1e1feb0f648f5de8a76796f6796e0ff9f1feeb32",
	}

	tf.handleTrade(data)

	if len(ch) != 1 {
		t.Fatalf("expected 1 trade, got %d", len(ch))
	}

	trade := <-ch
	// Dex-qualified coin should pass through unchanged (not in baseToDex map).
	if trade.Coin != "xyz:NVDA" {
		t.Errorf("Coin = %q, want %q", trade.Coin, "xyz:NVDA")
	}
	if !almostEqual(trade.Price, 120.50, 1e-9) {
		t.Errorf("Price = %f, want 120.50", trade.Price)
	}
	if !almostEqual(trade.Size, 5.0, 1e-9) {
		t.Errorf("Size = %f, want 5.0", trade.Size)
	}
	if trade.Side != "buy" {
		t.Errorf("Side = %q, want %q", trade.Side, "buy")
	}
}

func TestGRPCTradeHandleBareCoinRemapping(t *testing.T) {
	ch := make(chan types.TradeUpdate, 10)
	tf := &GRPCTradeFeed{
		channels:  []chan types.TradeUpdate{ch},
		baseToDex: map[string]string{"NVDA": "xyz:NVDA"},
	}

	// If a bare coin name arrives, baseToDex should remap it.
	data := map[string]any{
		"coin": "NVDA",
		"px":   "121.00",
		"sz":   "3.0",
		"side": "A",
	}

	tf.handleTrade(data)

	if len(ch) != 1 {
		t.Fatalf("expected 1 trade, got %d", len(ch))
	}

	trade := <-ch
	if trade.Coin != "xyz:NVDA" {
		t.Errorf("Coin = %q, want %q (should be remapped via baseToDex)", trade.Coin, "xyz:NVDA")
	}
	if trade.Side != "sell" {
		t.Errorf("Side = %q, want %q", trade.Side, "sell")
	}
}

func TestGRPCTradeHandleInvalidData(t *testing.T) {
	ch := make(chan types.TradeUpdate, 10)
	tf := &GRPCTradeFeed{
		channels:  []chan types.TradeUpdate{ch},
		baseToDex: map[string]string{"NVDA": "xyz:NVDA"},
	}

	// Missing coin field — should be silently dropped.
	tf.handleTrade(map[string]any{
		"px":   "120.00",
		"sz":   "1.0",
		"side": "B",
	})

	// Zero price — should be silently dropped.
	tf.handleTrade(map[string]any{
		"coin": "xyz:NVDA",
		"px":   "0",
		"sz":   "1.0",
		"side": "B",
	})

	// Empty map — should be silently dropped.
	tf.handleTrade(map[string]any{})

	if len(ch) != 0 {
		t.Errorf("expected 0 trades from invalid data, got %d", len(ch))
	}
}

func TestGRPCTradeFanOutToMultipleChannels(t *testing.T) {
	ch1 := make(chan types.TradeUpdate, 10)
	ch2 := make(chan types.TradeUpdate, 10)
	tf := &GRPCTradeFeed{
		channels:  []chan types.TradeUpdate{ch1, ch2},
		baseToDex: map[string]string{"MU": "xyz:MU"},
	}

	data := map[string]any{
		"coin": "xyz:MU",
		"px":   "85.00",
		"sz":   "10.0",
		"side": "B",
	}

	tf.handleTrade(data)

	if len(ch1) != 1 {
		t.Fatalf("ch1: expected 1 trade, got %d", len(ch1))
	}
	if len(ch2) != 1 {
		t.Fatalf("ch2: expected 1 trade, got %d", len(ch2))
	}

	trade1 := <-ch1
	trade2 := <-ch2
	if trade1.Coin != "xyz:MU" || trade2.Coin != "xyz:MU" {
		t.Errorf("both channels should receive xyz:MU trade")
	}
}

func TestGRPCTradeIgnoresExtraFields(t *testing.T) {
	ch := make(chan types.TradeUpdate, 10)
	tf := &GRPCTradeFeed{
		channels:  []chan types.TradeUpdate{ch},
		baseToDex: map[string]string{"NVDA": "xyz:NVDA"},
	}

	// gRPC trade events include extra fields that should be ignored by parseTrade.
	data := map[string]any{
		"coin":          "xyz:NVDA",
		"px":            "119.75",
		"sz":            "2.0",
		"side":          "B",
		"_block_number": float64(938707020),
		"_timestamp":    "2026-03-29T04:35:00.000000000",
		"_user":         "0xdeadbeef",
		"hash":          "0xabc123",
		"tid":           float64(999),
	}

	tf.handleTrade(data)

	if len(ch) != 1 {
		t.Fatalf("expected 1 trade, got %d", len(ch))
	}

	trade := <-ch
	if !almostEqual(trade.Price, 119.75, 1e-9) {
		t.Errorf("Price = %f, want 119.75", trade.Price)
	}
	if !almostEqual(trade.Size, 2.0, 1e-9) {
		t.Errorf("Size = %f, want 2.0", trade.Size)
	}
}

func TestParseTradeExtractsCrossedField(t *testing.T) {
	tests := []struct {
		name string
		data map[string]any
		want bool
	}{
		{
			name: "crossed true (bool) = taker",
			data: map[string]any{
				"coin":    "xyz:CL",
				"px":      "68.50",
				"sz":      "10.0",
				"side":    "B",
				"crossed": true,
			},
			want: true,
		},
		{
			name: "crossed false (bool) = maker",
			data: map[string]any{
				"coin":    "xyz:CL",
				"px":      "68.50",
				"sz":      "10.0",
				"side":    "A",
				"crossed": false,
			},
			want: false,
		},
		{
			name: "crossed true (string)",
			data: map[string]any{
				"coin":    "xyz:CL",
				"px":      "68.50",
				"sz":      "10.0",
				"side":    "B",
				"crossed": "true",
			},
			want: true,
		},
		{
			name: "crossed false (string)",
			data: map[string]any{
				"coin":    "xyz:CL",
				"px":      "68.50",
				"sz":      "10.0",
				"side":    "B",
				"crossed": "false",
			},
			want: false,
		},
		{
			name: "crossed field missing defaults to false",
			data: map[string]any{
				"coin": "xyz:CL",
				"px":   "68.50",
				"sz":   "10.0",
				"side": "B",
			},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			trade, err := parseTrade(tt.data)
			if err != nil {
				t.Fatalf("parseTrade() error: %v", err)
			}
			if trade.Crossed != tt.want {
				t.Errorf("Crossed = %v, want %v", trade.Crossed, tt.want)
			}
		})
	}
}
