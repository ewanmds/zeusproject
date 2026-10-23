package quoting

import (
	"testing"
	"time"
)

// markoutBpsForScore inverts the observation mapping used inside RecordMarkout.
// observation = 0.5 + markoutBps/10, clamped to [0,1]. To drive the steady-state
// score to target `s`, feed markoutBps = (s-0.5)*10.
func markoutBpsForScore(target float64) float64 {
	return (target - 0.5) * 10
}

// drive feeds markoutBpsForScore until score converges near target, then returns.
func drive(t *testing.T, tr *WallQualityTracker, side string, target float64, now time.Time) time.Time {
	t.Helper()
	m := markoutBpsForScore(target)
	for i := 0; i < 500; i++ {
		tr.RecordMarkout(side, m, now)
	}
	return now
}

func TestWallQuality_ZeroDefaults_PreservesPreP7Behavior(t *testing.T) {
	// hysteresis=0 and reenableThreshold=0 => legacy behavior.
	tr := NewWallQualityTracker(5*time.Second, 0.5, 2*time.Second, 0, 0)
	now := time.Unix(1_000_000, 0)
	// Push score well below min -> disable.
	for i := 0; i < 200; i++ {
		now = now.Add(100 * time.Millisecond)
		tr.RecordMarkout("buy", -5.0, now)
	}
	if tr.SideEnabled("buy", now) {
		t.Fatalf("expected side disabled after adverse markouts")
	}
	// Advance past disable window -> immediately enabled again.
	now = now.Add(3 * time.Second)
	if !tr.SideEnabled("buy", now) {
		t.Fatalf("expected side re-enabled after disable window")
	}
}

func TestWallQuality_HysteresisPreventsShortToggle(t *testing.T) {
	// minScore=0.5, reenable=0.7, hysteresis=30s.
	tr := NewWallQualityTracker(3*time.Second, 0.5, 2*time.Second, 30*time.Second, 2.0)
	now := time.Unix(2_000_000, 0)
	start := now
	// Drive adverse markouts; score crosses minScore quickly but flip should
	// only occur after hysteresis (30s) of continuous sub-minScore condition.
	flipped := false
	flipAt := time.Duration(0)
	for i := 0; i < 500 && !flipped; i++ {
		_, _, _, tr2 := tr.RecordMarkout("buy", -5.0, now)
		if tr2 != nil && tr2.To == "disabled" {
			flipAt = now.Sub(start)
			flipped = true
			break
		}
		now = now.Add(100 * time.Millisecond)
	}
	if !flipped {
		t.Fatalf("expected flip to disabled within simulated window")
	}
	// Must take at least ~30s from first sub-threshold observation.
	// The score needs a few samples to cross minScore, so allow a small grace
	// above the raw hysteresis window.
	if flipAt < 29*time.Second {
		t.Fatalf("flipped too early: %v (expected >= 30s hysteresis)", flipAt)
	}
}

func TestWallQuality_AsymmetricThreshold_ScoreBelowReenableBlocks(t *testing.T) {
	tr := NewWallQualityTracker(3*time.Second, 0.5, 1*time.Second, 10*time.Second, 2.0)
	now := time.Unix(3_000_000, 0)
	// Force into disabled state.
	for i := 0; i < 400; i++ {
		tr.RecordMarkout("buy", -5.0, now)
		now = now.Add(200 * time.Millisecond)
	}
	// Keep driving until actually flipped.
	for i := 0; i < 200; i++ {
		_, _, _, _ = tr.RecordMarkout("buy", -5.0, now)
		now = now.Add(1 * time.Second)
		if !tr.SideEnabled("buy", now) {
			break
		}
	}
	if tr.SideEnabled("buy", now) {
		t.Fatalf("expected disabled after sustained adverse markouts")
	}
	// Advance past disable window.
	now = now.Add(5 * time.Second)
	// Now drive score to 0.55: above minScore(0.5) but below reenable(0.7).
	// It should NEVER re-enable.
	target := markoutBpsForScore(0.55)
	for i := 0; i < 300; i++ {
		tr.RecordMarkout("buy", target, now)
		now = now.Add(200 * time.Millisecond)
	}
	if tr.SideEnabled("buy", now) {
		t.Fatalf("expected still disabled; score below reenable threshold")
	}
}

func TestWallQuality_PendingResetOnScoreDrop(t *testing.T) {
	tr := NewWallQualityTracker(3*time.Second, 0.5, 1*time.Second, 30*time.Second, 2.0)
	now := time.Unix(4_000_000, 0)
	// Force disabled.
	for i := 0; i < 400; i++ {
		tr.RecordMarkout("buy", -5.0, now)
		now = now.Add(200 * time.Millisecond)
	}
	if tr.SideEnabled("buy", now) {
		t.Fatalf("expected disabled")
	}
	// Advance past disable window.
	now = now.Add(2 * time.Second)
	// Drive score to 0.8 for 20s (pending enable, not yet flipped).
	highTarget := markoutBpsForScore(0.8)
	for i := 0; i < 100; i++ {
		tr.RecordMarkout("buy", highTarget, now)
		now = now.Add(200 * time.Millisecond)
	}
	if tr.SideEnabled("buy", now) {
		t.Fatalf("should not have re-enabled yet (pending < hysteresis)")
	}
	// Drop score below reenable threshold for a while (pending should reset).
	lowTarget := markoutBpsForScore(0.55)
	for i := 0; i < 50; i++ {
		tr.RecordMarkout("buy", lowTarget, now)
		now = now.Add(200 * time.Millisecond)
	}
	// Now continue high target; pending should restart, not accumulate.
	for i := 0; i < 100; i++ {
		tr.RecordMarkout("buy", highTarget, now)
		now = now.Add(200 * time.Millisecond)
	}
	// Elapsed time since initial pending: ~50s, but pending was reset midway.
	// Only ~20s of continuous high-score accumulated -> still disabled.
	if tr.SideEnabled("buy", now) {
		t.Fatalf("expected still disabled; pending timer should have reset")
	}
}

func TestWallQuality_TransitionEventPopulated(t *testing.T) {
	tr := NewWallQualityTracker(3*time.Second, 0.5, 1*time.Second, 5*time.Second, 2.0)
	now := time.Unix(5_000_000, 0)
	var flipToDisabled *WallQualityTransition
	for i := 0; i < 500 && flipToDisabled == nil; i++ {
		_, _, _, tr2 := tr.RecordMarkout("sell", -5.0, now)
		if tr2 != nil && tr2.To == "disabled" {
			flipToDisabled = tr2
		}
		now = now.Add(200 * time.Millisecond)
	}
	if flipToDisabled == nil {
		t.Fatalf("expected a disable transition")
	}
	if flipToDisabled.From != "enabled" || flipToDisabled.To != "disabled" {
		t.Fatalf("bad from/to: %+v", flipToDisabled)
	}
	if flipToDisabled.Side != "sell" {
		t.Fatalf("bad side: %s", flipToDisabled.Side)
	}
	if flipToDisabled.HeldSec <= 0 {
		t.Fatalf("held_sec should be > 0, got %f", flipToDisabled.HeldSec)
	}
}
