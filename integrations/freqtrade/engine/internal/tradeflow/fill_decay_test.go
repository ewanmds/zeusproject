package tradeflow

import (
	"math"
	"testing"
	"time"
)

func TestNoShiftWithoutFills(t *testing.T) {
	fd := NewFillDecay()
	shift := fd.CurrentShiftBps()
	if shift != 0 {
		t.Fatalf("expected 0 shift, got %f", shift)
	}
}

func TestBuyShiftsDown(t *testing.T) {
	fd := NewFillDecay()
	fd.RecordFill("buy", 2.0, time.Hour) // long half-life so minimal decay
	shift := fd.CurrentShiftBps()
	if shift >= 0 {
		t.Fatalf("expected negative shift from buy fill, got %f", shift)
	}
	if math.Abs(shift+2.0) > 0.1 {
		t.Fatalf("expected shift ~-2.0, got %f", shift)
	}
}

func TestSellShiftsUp(t *testing.T) {
	fd := NewFillDecay()
	fd.RecordFill("sell", 2.0, time.Hour)
	shift := fd.CurrentShiftBps()
	if shift <= 0 {
		t.Fatalf("expected positive shift from sell fill, got %f", shift)
	}
	if math.Abs(shift-2.0) > 0.1 {
		t.Fatalf("expected shift ~2.0, got %f", shift)
	}
}

func TestExponentialDecay(t *testing.T) {
	fd := NewFillDecay()
	halfLife := 50 * time.Millisecond
	fd.RecordFill("buy", 4.0, halfLife)

	// Wait one half-life
	time.Sleep(55 * time.Millisecond)
	shift := fd.CurrentShiftBps()
	// Should be roughly half: -4.0 * 0.5 = -2.0
	if math.Abs(shift+2.0) > 0.5 {
		t.Fatalf("expected shift ~-2.0 after one half-life, got %f", shift)
	}
}

func TestStackingFills(t *testing.T) {
	fd := NewFillDecay()
	fd.RecordFill("buy", 2.0, time.Hour)
	fd.RecordFill("buy", 3.0, time.Hour)
	shift := fd.CurrentShiftBps()
	if math.Abs(shift+5.0) > 0.1 {
		t.Fatalf("expected shift ~-5.0 from stacked buys, got %f", shift)
	}
}

func TestPruning(t *testing.T) {
	fd := NewFillDecay()
	halfLife := 10 * time.Millisecond
	fd.RecordFill("buy", 2.0, halfLife)

	// Wait for decay to be negligible (many half-lives)
	time.Sleep(200 * time.Millisecond)
	shift := fd.CurrentShiftBps()
	if math.Abs(shift) > 0.01 {
		t.Fatalf("expected shift ~0 after many half-lives, got %f", shift)
	}

	// Entries should be pruned
	fd.mu.Lock()
	count := len(fd.entries)
	fd.mu.Unlock()
	if count != 0 {
		t.Fatalf("expected 0 entries after pruning, got %d", count)
	}
}
