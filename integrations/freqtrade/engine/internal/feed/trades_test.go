package feed

import (
	"testing"
	"time"
)

func TestParseTrade(t *testing.T) {
	tests := []struct {
		name     string
		data     map[string]any
		wantCoin string
		wantPx   float64
		wantSz   float64
		wantSide string
		wantErr  bool
	}{
		{
			name: "string fields",
			data: map[string]any{
				"coin": "CL",
				"px":   "70.05",
				"sz":   "2.5",
				"side": "B",
			},
			wantCoin: "CL",
			wantPx:   70.05,
			wantSz:   2.5,
			wantSide: "buy",
		},
		{
			name: "float fields",
			data: map[string]any{
				"coin": "BRENTOIL",
				"px":   73.20,
				"sz":   1.0,
				"side": "A",
			},
			wantCoin: "BRENTOIL",
			wantPx:   73.20,
			wantSz:   1.0,
			wantSide: "sell",
		},
		{
			name: "missing coin",
			data: map[string]any{
				"px":   "70.0",
				"sz":   "1.0",
				"side": "B",
			},
			wantErr: true,
		},
		{
			name: "zero price",
			data: map[string]any{
				"coin": "CL",
				"px":   "0",
				"sz":   "1.0",
				"side": "B",
			},
			wantErr: true,
		},
		{
			name: "zero size",
			data: map[string]any{
				"coin": "CL",
				"px":   "70.0",
				"sz":   "0",
				"side": "B",
			},
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			trade, err := parseTrade(tc.data)
			if tc.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if trade.Coin != tc.wantCoin {
				t.Errorf("Coin = %q, want %q", trade.Coin, tc.wantCoin)
			}
			if !almostEqual(trade.Price, tc.wantPx, 1e-9) {
				t.Errorf("Price = %f, want %f", trade.Price, tc.wantPx)
			}
			if !almostEqual(trade.Size, tc.wantSz, 1e-9) {
				t.Errorf("Size = %f, want %f", trade.Size, tc.wantSz)
			}
			if trade.Side != tc.wantSide {
				t.Errorf("Side = %q, want %q", trade.Side, tc.wantSide)
			}
			if trade.Timestamp.IsZero() {
				t.Error("Timestamp should not be zero")
			}
			_ = time.Now()
		})
	}
}
