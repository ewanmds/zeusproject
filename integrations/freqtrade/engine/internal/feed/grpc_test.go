package feed

import (
	"math"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/types"
)

// almostEqual checks that two floats are within epsilon of each other.
func almostEqual(a, b, eps float64) bool {
	return math.Abs(a-b) < eps
}

func TestParseL2BookPrimaryFormat(t *testing.T) {
	// Primary format: "bids"/"asks" as [][]any where each level is []any{px_string, sz_string, count}.
	data := map[string]any{
		"coin": "CL",
		"bids": [][]any{
			{"70.05", "2.5", uint32(3)},
			{"70.00", "1.0", uint32(1)},
			{"69.95", "5.0", uint32(2)},
		},
		"asks": [][]any{
			{"70.10", "3.0", uint32(4)},
			{"70.15", "1.5", uint32(1)},
			{"70.20", "0.5", uint32(1)},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify bid levels.
	if len(update.Bids) != 3 {
		t.Fatalf("expected 3 bid levels, got %d", len(update.Bids))
	}
	if !almostEqual(update.Bids[0].Price, 70.05, 1e-9) {
		t.Errorf("Bids[0].Price = %f, want 70.05", update.Bids[0].Price)
	}
	if !almostEqual(update.Bids[0].Size, 2.5, 1e-9) {
		t.Errorf("Bids[0].Size = %f, want 2.5", update.Bids[0].Size)
	}
	if !almostEqual(update.Bids[2].Price, 69.95, 1e-9) {
		t.Errorf("Bids[2].Price = %f, want 69.95", update.Bids[2].Price)
	}
	if !almostEqual(update.Bids[2].Size, 5.0, 1e-9) {
		t.Errorf("Bids[2].Size = %f, want 5.0", update.Bids[2].Size)
	}

	// Verify ask levels.
	if len(update.Asks) != 3 {
		t.Fatalf("expected 3 ask levels, got %d", len(update.Asks))
	}
	if !almostEqual(update.Asks[0].Price, 70.10, 1e-9) {
		t.Errorf("Asks[0].Price = %f, want 70.10", update.Asks[0].Price)
	}
	if !almostEqual(update.Asks[0].Size, 3.0, 1e-9) {
		t.Errorf("Asks[0].Size = %f, want 3.0", update.Asks[0].Size)
	}

	// BestBid/BestAsk from depth.
	if !almostEqual(update.BestBid, 70.05, 1e-9) {
		t.Errorf("BestBid = %f, want 70.05", update.BestBid)
	}
	if !almostEqual(update.BestAsk, 70.10, 1e-9) {
		t.Errorf("BestAsk = %f, want 70.10", update.BestAsk)
	}
}

func TestParseL2BookFallbackSliceAny(t *testing.T) {
	// Fallback: "bids"/"asks" as []any wrapping []any levels.
	data := map[string]any{
		"coin": "BRENTOIL",
		"bids": []any{
			[]any{"73.50", "1.0", uint32(2)},
			[]any{"73.45", "4.0", uint32(1)},
		},
		"asks": []any{
			[]any{"73.55", "2.0", uint32(1)},
		},
	}

	update, err := parseL2Book("BRENTOIL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(update.Bids) != 2 {
		t.Fatalf("expected 2 bid levels, got %d", len(update.Bids))
	}
	if !almostEqual(update.Bids[0].Price, 73.50, 1e-9) {
		t.Errorf("Bids[0].Price = %f, want 73.50", update.Bids[0].Price)
	}
	if !almostEqual(update.Bids[1].Size, 4.0, 1e-9) {
		t.Errorf("Bids[1].Size = %f, want 4.0", update.Bids[1].Size)
	}

	if len(update.Asks) != 1 {
		t.Fatalf("expected 1 ask level, got %d", len(update.Asks))
	}
	if !almostEqual(update.Asks[0].Price, 73.55, 1e-9) {
		t.Errorf("Asks[0].Price = %f, want 73.55", update.Asks[0].Price)
	}
	if !almostEqual(update.Asks[0].Size, 2.0, 1e-9) {
		t.Errorf("Asks[0].Size = %f, want 2.0", update.Asks[0].Size)
	}
}

func TestParseL2BookLevelsFormat(t *testing.T) {
	// "levels" format: [[bids...], [asks...]] with map levels.
	data := map[string]any{
		"levels": []any{
			// bids
			[]any{
				map[string]any{"px": "70.05", "sz": "1.5", "n": uint32(3)},
				map[string]any{"px": "70.00", "sz": "2.0", "n": uint32(1)},
			},
			// asks
			[]any{
				map[string]any{"px": "70.10", "sz": "0.5", "n": uint32(2)},
				map[string]any{"px": "70.15", "sz": "1.0", "n": uint32(1)},
			},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(update.Bids) != 2 {
		t.Fatalf("expected 2 bid levels, got %d", len(update.Bids))
	}
	if !almostEqual(update.Bids[0].Price, 70.05, 1e-9) {
		t.Errorf("Bids[0].Price = %f, want 70.05", update.Bids[0].Price)
	}
	if !almostEqual(update.Bids[0].Size, 1.5, 1e-9) {
		t.Errorf("Bids[0].Size = %f, want 1.5", update.Bids[0].Size)
	}

	if len(update.Asks) != 2 {
		t.Fatalf("expected 2 ask levels, got %d", len(update.Asks))
	}
	if !almostEqual(update.Asks[1].Price, 70.15, 1e-9) {
		t.Errorf("Asks[1].Price = %f, want 70.15", update.Asks[1].Price)
	}

	if !almostEqual(update.BestBid, 70.05, 1e-9) {
		t.Errorf("BestBid = %f, want 70.05", update.BestBid)
	}
	if !almostEqual(update.BestAsk, 70.10, 1e-9) {
		t.Errorf("BestAsk = %f, want 70.10", update.BestAsk)
	}
}

func TestParseL2BookDataWrapperFormat(t *testing.T) {
	// "data" wrapper with nested "bids"/"asks" as map levels.
	data := map[string]any{
		"data": map[string]any{
			"bids": []any{
				map[string]any{"px": "70.00", "sz": "3.0", "n": uint32(5)},
			},
			"asks": []any{
				map[string]any{"px": "70.10", "sz": "1.0", "n": uint32(2)},
			},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(update.Bids) != 1 {
		t.Fatalf("expected 1 bid level, got %d", len(update.Bids))
	}
	if !almostEqual(update.Bids[0].Price, 70.00, 1e-9) {
		t.Errorf("Bids[0].Price = %f, want 70.00", update.Bids[0].Price)
	}
	if !almostEqual(update.Bids[0].Size, 3.0, 1e-9) {
		t.Errorf("Bids[0].Size = %f, want 3.0", update.Bids[0].Size)
	}

	if len(update.Asks) != 1 {
		t.Fatalf("expected 1 ask level, got %d", len(update.Asks))
	}
	if !almostEqual(update.Asks[0].Price, 70.10, 1e-9) {
		t.Errorf("Asks[0].Price = %f, want 70.10", update.Asks[0].Price)
	}
}

func TestParseL2BookMidFromDepth(t *testing.T) {
	// Verify Mid = (BestBid + BestAsk) / 2.
	data := map[string]any{
		"bids": [][]any{
			{"100.00", "1.0", uint32(1)},
		},
		"asks": [][]any{
			{"100.20", "1.0", uint32(1)},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expectedMid := (100.00 + 100.20) / 2.0
	if !almostEqual(update.Mid, expectedMid, 1e-9) {
		t.Errorf("Mid = %f, want %f", update.Mid, expectedMid)
	}
	if !almostEqual(update.BestBid, 100.00, 1e-9) {
		t.Errorf("BestBid = %f, want 100.00", update.BestBid)
	}
	if !almostEqual(update.BestAsk, 100.20, 1e-9) {
		t.Errorf("BestAsk = %f, want 100.20", update.BestAsk)
	}
}

func TestMicropriceComputation(t *testing.T) {
	// Microprice = (bestBid * askSize + bestAsk * bidSize) / (bidSize + askSize)
	data := map[string]any{
		"bids": [][]any{
			{"100.00", "10.0", uint32(1)}, // big bid
		},
		"asks": [][]any{
			{"100.20", "2.0", uint32(1)}, // small ask
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// microprice = (100.00*2.0 + 100.20*10.0) / (10.0+2.0)
	//            = (200 + 1002) / 12 = 1202 / 12 = 100.1667
	expectedMicro := (100.00*2.0 + 100.20*10.0) / (10.0 + 2.0)
	if !almostEqual(update.Microprice, expectedMicro, 1e-4) {
		t.Errorf("Microprice = %f, want %f", update.Microprice, expectedMicro)
	}
	// With large bid and small ask, microprice should be closer to ask (pulled up).
	if update.Microprice <= update.Mid {
		t.Errorf("expected microprice > mid when bid size >> ask size")
	}
}

func TestMicropriceEqualSizes(t *testing.T) {
	// Equal sizes → microprice = mid.
	data := map[string]any{
		"bids": [][]any{
			{"100.00", "5.0", uint32(1)},
		},
		"asks": [][]any{
			{"100.20", "5.0", uint32(1)},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if !almostEqual(update.Microprice, update.Mid, 1e-9) {
		t.Errorf("expected microprice = mid with equal sizes, got microprice=%f mid=%f",
			update.Microprice, update.Mid)
	}
}

func TestMicropriceZeroSizes(t *testing.T) {
	// Zero sizes → microprice falls back to mid.
	data := map[string]any{
		"bids": [][]any{
			{"100.00", "0", uint32(0)},
		},
		"asks": [][]any{
			{"100.20", "0", uint32(0)},
		},
	}

	update, err := parseL2Book("CL", data)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if !almostEqual(update.Microprice, update.Mid, 1e-9) {
		t.Errorf("expected microprice = mid with zero sizes, got %f", update.Microprice)
	}
}

func TestGRPCFeedRecoversAfterConsecutiveOutliers(t *testing.T) {
	ch := make(chan types.BookUpdate, 10)
	f := &GRPCFeed{
		channels:     []chan types.BookUpdate{ch},
		prevMid:      make(map[string]float64),
		outlierState: make(map[string]outlierState),
	}

	base := map[string]any{
		"bids": [][]any{{"100.00", "1.0", uint32(1)}},
		"asks": [][]any{{"100.20", "1.0", uint32(1)}},
	}
	outlier := map[string]any{
		"bids": [][]any{{"112.00", "1.0", uint32(1)}},
		"asks": [][]any{{"112.20", "1.0", uint32(1)}},
	}

	f.handleL2Book("CL", base)
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("expected baseline book update to be accepted")
	}

	for attempt := 1; attempt < outlierRecoveryRejects; attempt++ {
		f.handleL2Book("CL", outlier)
		select {
		case <-ch:
			t.Fatalf("unexpected outlier acceptance on attempt %d", attempt)
		default:
		}
	}

	f.handleL2Book("CL", outlier)
	select {
	case update := <-ch:
		if !almostEqual(update.Mid, 112.10, 1e-9) {
			t.Fatalf("expected recovered mid=112.10, got %f", update.Mid)
		}
	case <-time.After(time.Second):
		t.Fatal("expected outlier book to be accepted after recovery threshold")
	}
}

func TestGRPCFeedRecoversAfterCooldown(t *testing.T) {
	ch := make(chan types.BookUpdate, 10)
	f := &GRPCFeed{
		channels:     []chan types.BookUpdate{ch},
		prevMid:      make(map[string]float64),
		outlierState: make(map[string]outlierState),
	}

	base := map[string]any{
		"bids": [][]any{{"100.00", "1.0", uint32(1)}},
		"asks": [][]any{{"100.20", "1.0", uint32(1)}},
	}
	outlier := map[string]any{
		"bids": [][]any{{"112.00", "1.0", uint32(1)}},
		"asks": [][]any{{"112.20", "1.0", uint32(1)}},
	}

	f.handleL2Book("CL", base)
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("expected baseline book update to be accepted")
	}

	f.mu.Lock()
	f.outlierState["CL"] = outlierState{
		lastAcceptedAt: time.Now().Add(-outlierRecoveryCooldown - time.Millisecond),
	}
	f.mu.Unlock()

	f.handleL2Book("CL", outlier)
	select {
	case update := <-ch:
		if !almostEqual(update.Mid, 112.10, 1e-9) {
			t.Fatalf("expected recovered mid=112.10, got %f", update.Mid)
		}
	case <-time.After(time.Second):
		t.Fatal("expected outlier book to be accepted after cooldown")
	}
}
