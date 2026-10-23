package quoting

import (
	"math"
	"sync"
	"testing"
	"time"
)

func TestSessionPnLTracker_InitialPnLZero(t *testing.T) {
	tr := NewSessionPnLTracker(10000.0, 60)
	abs, bps := tr.PnL()
	if abs != 0 || bps != 0 {
		t.Fatalf("expected zero PnL before Update, got abs=%g bps=%g", abs, bps)
	}
	if w := tr.WindowPnLBps(time.Now()); w != 0 {
		t.Fatalf("expected zero WindowPnLBps before Update, got %g", w)
	}
}

func TestSessionPnLTracker_PositivePnL(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 60)
	tr.Update(10100.0, start.Add(30*time.Second))
	abs, bps := tr.PnL()
	if math.Abs(abs-100.0) > 1e-9 {
		t.Fatalf("abs PnL: want 100, got %g", abs)
	}
	if math.Abs(bps-100.0) > 1e-9 {
		t.Fatalf("bps PnL: want 100, got %g", bps)
	}
}

func TestSessionPnLTracker_NegativePnL(t *testing.T) {
	tr := NewSessionPnLTracker(10000.0, 60)
	tr.Update(9950.0, time.Now())
	abs, bps := tr.PnL()
	if math.Abs(abs-(-50.0)) > 1e-9 {
		t.Fatalf("abs PnL: want -50, got %g", abs)
	}
	if math.Abs(bps-(-50.0)) > 1e-9 {
		t.Fatalf("bps PnL: want -50, got %g", bps)
	}
}

func TestSessionPnLTracker_WindowPnLBpsInsideWindow(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 60) // 60 minute window

	// Sample 1: at t=10min, equity=10050 (50 bps gain so far).
	tr.Update(10050.0, start.Add(10*time.Minute))
	// Sample 2: at t=30min, equity=10100.
	tr.Update(10100.0, start.Add(30*time.Minute))
	// Sample 3 (current): at t=40min, equity=10200 (200 bps gain since start).
	now := start.Add(40 * time.Minute)
	tr.Update(10200.0, now)

	// Window is 60min. Window-start equity should be the oldest sample inside
	// the window — the one at t=10min (10050) — since now (t=40min) - 60min = -20min,
	// all samples are inside the window.
	// WindowPnLBps = (10200-10050)/10050 * 10000 ≈ 149.25
	want := (10200.0 - 10050.0) / 10050.0 * 10000.0
	got := tr.WindowPnLBps(now)
	if math.Abs(got-want) > 1e-6 {
		t.Fatalf("WindowPnLBps: want %g, got %g", want, got)
	}
}

func TestSessionPnLTracker_WindowPnLBpsPrunesOldSamples(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 10) // 10 minute window

	// Samples outside the 10min window relative to "now".
	tr.Update(10050.0, start.Add(1*time.Minute)) // way out of window
	tr.Update(10100.0, start.Add(2*time.Minute)) // way out of window
	now := start.Add(60 * time.Minute)
	tr.Update(10150.0, now) // inside (this is "now")

	// "now" is t=60min, window-start = t=50min. The first two samples
	// (t=1min, t=2min) are outside the window and must be pruned.
	// Only sample at t=60min remains => WindowPnLBps = 0 (start == current).
	got := tr.WindowPnLBps(now)
	if got != 0 {
		t.Fatalf("expected WindowPnLBps=0 after pruning all but current sample, got %g", got)
	}
}

// TestSessionPnLTracker_WindowStalenessAfterHalt verifies that WindowPnLBps
// prunes against the caller-provided wall clock, not the frozen lastUpdate.
// A halted Update path must not cause the rolling window to report stale data.
func TestSessionPnLTracker_WindowStalenessAfterHalt(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 10) // 10 minute window

	// Put some samples in the window.
	tr.Update(10050.0, start.Add(1*time.Minute))
	tr.Update(10100.0, start.Add(2*time.Minute))

	// Sanity: at t=3min everything is in-window and we see non-zero delta.
	want := (10100.0 - 10050.0) / 10050.0 * 10000.0
	if got := tr.WindowPnLBps(start.Add(3 * time.Minute)); math.Abs(got-want) > 1e-6 {
		t.Fatalf("precondition: WindowPnLBps at t=3min: want %g, got %g", want, got)
	}

	// Now simulate halt: no more Update calls. lastUpdate stays at t=2min.
	// Caller's wall clock advances past the 10min window (to t=1hr).
	// All samples must be pruned (wall clock based), so window reports 0.
	if got := tr.WindowPnLBps(start.Add(1 * time.Hour)); got != 0 {
		t.Fatalf("post-halt staleness: expected WindowPnLBps=0 after window expired, got %g", got)
	}
}

func TestSessionPnLTracker_WindowPnLBpsAcrossWindowBoundary(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 10) // 10 minute window

	// Sample inside window (t=55min, equity=10100).
	tr.Update(10100.0, start.Add(55*time.Minute))
	// Current sample at t=60min, equity=10200.
	now := start.Add(60 * time.Minute)
	tr.Update(10200.0, now)

	// Window start = t=50min; oldest in-window sample is at t=55min (10100).
	// Window bps = (10200-10100)/10100 * 10000 ≈ 99.0099
	want := (10200.0 - 10100.0) / 10100.0 * 10000.0
	got := tr.WindowPnLBps(now)
	if math.Abs(got-want) > 1e-6 {
		t.Fatalf("WindowPnLBps: want %g, got %g", want, got)
	}
}

func TestSessionPnLTracker_Reset(t *testing.T) {
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 60)
	tr.Update(10500.0, start.Add(5*time.Minute))
	abs, bps := tr.PnL()
	if abs == 0 || bps == 0 {
		t.Fatalf("precondition: expected nonzero PnL after Update, got abs=%g bps=%g", abs, bps)
	}

	tr.Reset(11000.0, start.Add(10*time.Minute))
	// PnL after Reset should be zero (start equity == current).
	abs2, bps2 := tr.PnL()
	if abs2 != 0 || bps2 != 0 {
		t.Fatalf("expected zero PnL after Reset, got abs=%g bps=%g", abs2, bps2)
	}
	if w := tr.WindowPnLBps(start.Add(10 * time.Minute)); w != 0 {
		t.Fatalf("expected zero WindowPnLBps after Reset, got %g", w)
	}
	// Update after Reset should reflect new baseline (11000).
	tr.Update(11110.0, start.Add(11*time.Minute))
	_, bps3 := tr.PnL()
	want := 110.0 / 11000.0 * 10000.0
	if math.Abs(bps3-want) > 1e-6 {
		t.Fatalf("post-Reset PnL bps: want %g, got %g", want, bps3)
	}
}

func TestSessionPnLTracker_ElapsedSec(t *testing.T) {
	start := time.Now()
	tr := NewSessionPnLTracker(10000.0, 60)
	time.Sleep(20 * time.Millisecond)
	el := tr.ElapsedSec()
	if el <= 0 {
		t.Fatalf("ElapsedSec should be positive, got %g", el)
	}
	if el > time.Since(start).Seconds()+0.1 {
		t.Fatalf("ElapsedSec too large: %g", el)
	}
}

func TestSessionPnLTracker_DefaultWindowOnZero(t *testing.T) {
	// When windowMinutes <= 0, default to 60. We can't directly inspect the
	// window, but we can verify the tracker doesn't reject the call and
	// behaves like a 60min window (samples within an hour included).
	start := time.Date(2026, 4, 13, 12, 0, 0, 0, time.UTC)
	tr := NewSessionPnLTracker(10000.0, 0)
	tr.Update(10050.0, start.Add(20*time.Minute))
	now := start.Add(40 * time.Minute)
	tr.Update(10150.0, now)
	// Both samples within the default 60min window.
	want := (10150.0 - 10050.0) / 10050.0 * 10000.0
	got := tr.WindowPnLBps(now)
	if math.Abs(got-want) > 1e-6 {
		t.Fatalf("default window: want WindowPnLBps=%g, got %g", want, got)
	}
}

func TestSessionPnLTracker_ZeroStartEquity(t *testing.T) {
	// Defensive: divide-by-zero protection when start equity is 0.
	tr := NewSessionPnLTracker(0, 60)
	tr.Update(100.0, time.Now())
	_, bps := tr.PnL()
	if math.IsNaN(bps) || math.IsInf(bps, 0) {
		t.Fatalf("expected finite bps when startEquity=0, got %g", bps)
	}
}

func TestSessionPnLTracker_Concurrent(t *testing.T) {
	// Race exerciser. Run with `go test -race`.
	tr := NewSessionPnLTracker(10000.0, 60)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			now := time.Now()
			for j := 0; ; j++ {
				select {
				case <-stop:
					return
				default:
				}
				tr.Update(10000.0+float64(j%500), now.Add(time.Duration(j)*time.Second))
			}
		}()
	}
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				_, _ = tr.PnL()
				_ = tr.WindowPnLBps(time.Now())
				_ = tr.ElapsedSec()
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		now := time.Now()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			if i%100 == 0 {
				tr.Reset(10000.0, now.Add(time.Duration(i)*time.Millisecond))
			}
		}
	}()

	time.Sleep(50 * time.Millisecond)
	close(stop)
	wg.Wait()
}
