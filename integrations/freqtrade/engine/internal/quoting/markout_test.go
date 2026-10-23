package quoting

import (
	"math"
	"testing"
	"time"
)

func TestMarkoutTracker_NoPending(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)

	ewma := mt.EWMA()
	if ewma.Count != 0 {
		t.Fatalf("expected count 0, got %d", ewma.Count)
	}
	for i, v := range ewma.Values {
		if v != 0 {
			t.Fatalf("expected EWMA[%d] == 0, got %f", i, v)
		}
	}
	if mt.PendingCount() != 0 {
		t.Fatalf("expected 0 pending, got %d", mt.PendingCount())
	}
}

func TestMarkoutTracker_BuyFavorable(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	// Buy fill at 100.0, mid at fill is 100.0.
	mt.RecordFillAt(100.0, 100.0, "buy", 1.0, now)

	// Mid moves to 100.10 after 6 seconds (all windows complete).
	results := mt.OnBookUpdate(100.10, now.Add(6*time.Second))
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}

	// markout = (100.10 - 100.0) / 100.0 * 10000 * +1 = +10 bps
	for i, m := range results[0].Markouts {
		expected := 10.0
		if math.Abs(m-expected) > 0.01 {
			t.Errorf("window %d: expected ~%.2f bps, got %.2f", i, expected, m)
		}
	}
}

func TestMarkoutTracker_SellAdverse(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	// Sell fill at 100.0, mid at fill is 100.0.
	mt.RecordFillAt(100.0, 100.0, "sell", 1.0, now)

	// Mid moves UP to 100.10 after 6 seconds — adverse for a sell.
	results := mt.OnBookUpdate(100.10, now.Add(6*time.Second))
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}

	// markout = (100.10 - 100.0) / 100.0 * 10000 * -1 = -10 bps (adverse)
	for i, m := range results[0].Markouts {
		expected := -10.0
		if math.Abs(m-expected) > 0.01 {
			t.Errorf("window %d: expected ~%.2f bps, got %.2f", i, expected, m)
		}
	}
}

func TestMarkoutTracker_AllWindowsComplete(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	mt.RecordFillAt(50.0, 50.0, "buy", 2.0, now)

	// After 500ms: first window completes, mid at 50.05.
	results := mt.OnBookUpdate(50.05, now.Add(500*time.Millisecond))
	if len(results) != 0 {
		t.Fatalf("expected 0 results after 500ms, got %d", len(results))
	}
	if mt.PendingCount() != 1 {
		t.Fatalf("expected 1 pending, got %d", mt.PendingCount())
	}

	// After 1s: second window completes, mid at 50.08.
	results = mt.OnBookUpdate(50.08, now.Add(1*time.Second))
	if len(results) != 0 {
		t.Fatalf("expected 0 results after 1s, got %d", len(results))
	}

	// After 2s: third window completes, mid at 50.10.
	results = mt.OnBookUpdate(50.10, now.Add(2*time.Second))
	if len(results) != 0 {
		t.Fatalf("expected 0 results after 2s, got %d", len(results))
	}

	// After 5s: all windows complete, mid at 50.15.
	results = mt.OnBookUpdate(50.15, now.Add(5*time.Second))
	if len(results) != 1 {
		t.Fatalf("expected 1 result after 5s, got %d", len(results))
	}
	if mt.PendingCount() != 0 {
		t.Fatalf("expected 0 pending after all complete, got %d", mt.PendingCount())
	}

	// Verify markouts at each window.
	// Window 0 (500ms): (50.05-50)/50*10000*1 = 10 bps
	// Window 1 (1s):    (50.08-50)/50*10000*1 = 16 bps
	// Window 2 (2s):    (50.10-50)/50*10000*1 = 20 bps
	// Window 3 (5s):    (50.15-50)/50*10000*1 = 30 bps
	expected := [4]float64{10.0, 16.0, 20.0, 30.0}
	for i, e := range expected {
		if math.Abs(results[0].Markouts[i]-e) > 0.01 {
			t.Errorf("window %d: expected %.2f bps, got %.2f", i, e, results[0].Markouts[i])
		}
	}
}

func TestMarkoutTracker_PartialCompletion(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	mt.RecordFillAt(100.0, 100.0, "buy", 1.0, now)

	// After 1.5s: windows 0 (500ms) and 1 (1s) should complete, but not 2 (2s) or 3 (5s).
	results := mt.OnBookUpdate(100.05, now.Add(1500*time.Millisecond))
	if len(results) != 0 {
		t.Fatalf("expected 0 results (only partial), got %d", len(results))
	}
	if mt.PendingCount() != 1 {
		t.Fatalf("expected 1 pending, got %d", mt.PendingCount())
	}

	// EWMA count should still be 0 (no fill fully completed yet).
	ewma := mt.EWMA()
	if ewma.Count != 0 {
		t.Fatalf("expected EWMA count 0 during partial, got %d", ewma.Count)
	}
}

func TestMarkoutTracker_EWMAConverges(t *testing.T) {
	mt := NewMarkoutTracker(5 * time.Second) // short half-life for faster convergence
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	// Record 20 fills, all buys at 100.0, all completing with mid at 100.05 (+5 bps).
	for i := 0; i < 20; i++ {
		fillTime := now.Add(time.Duration(i*10) * time.Second)
		mt.RecordFillAt(100.0, 100.0, "buy", 1.0, fillTime)
		// Complete all windows 6s later.
		mt.OnBookUpdate(100.05, fillTime.Add(6*time.Second))
	}

	ewma := mt.EWMA()
	if ewma.Count != 20 {
		t.Fatalf("expected count 20, got %d", ewma.Count)
	}

	// With constant +5 bps markout, EWMA should converge close to 5.0.
	for i, v := range ewma.Values {
		if math.Abs(v-5.0) > 0.5 {
			t.Errorf("EWMA[%d] = %.4f, expected ~5.0", i, v)
		}
	}
}

func TestMarkoutTracker_PendingCount(t *testing.T) {
	mt := NewMarkoutTracker(300 * time.Second)
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	if mt.PendingCount() != 0 {
		t.Fatalf("expected 0 pending, got %d", mt.PendingCount())
	}

	mt.RecordFillAt(100.0, 100.0, "buy", 1.0, now)
	if mt.PendingCount() != 1 {
		t.Fatalf("expected 1 pending, got %d", mt.PendingCount())
	}

	mt.RecordFillAt(101.0, 101.0, "sell", 2.0, now.Add(10*time.Second))
	if mt.PendingCount() != 2 {
		t.Fatalf("expected 2 pending, got %d", mt.PendingCount())
	}

	// Complete the first fill (all windows) after 6s from its fill time.
	// Second fill is only 6-10 = -4s old at this point — not completed.
	mt.OnBookUpdate(100.10, now.Add(6*time.Second))
	if mt.PendingCount() != 1 {
		t.Fatalf("expected 1 pending after first complete, got %d", mt.PendingCount())
	}

	// Complete the second fill after 16s from now (6s from fill at +10s).
	mt.OnBookUpdate(100.10, now.Add(16*time.Second))
	if mt.PendingCount() != 0 {
		t.Fatalf("expected 0 pending after all complete, got %d", mt.PendingCount())
	}
}
