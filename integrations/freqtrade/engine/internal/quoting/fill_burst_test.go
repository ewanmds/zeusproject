package quoting

import (
	"testing"
	"time"
)

func TestFillBurstNoActivation(t *testing.T) {
	// 2 fills in window with threshold=3 should not activate burst
	tracker := NewFillBurstTracker(5.0, 3, 2.0, 0.3, 15.0)
	tracker.RecordFill("buy")
	tracker.RecordFill("buy")

	buyMult, sellMult := tracker.SpreadMultiplier()
	if buyMult != 1.0 {
		t.Errorf("expected buy spread mult 1.0 (no burst), got %f", buyMult)
	}
	if sellMult != 1.0 {
		t.Errorf("expected sell spread mult 1.0 (no burst), got %f", sellMult)
	}

	buySF, sellSF := tracker.SizeFactor()
	if buySF != 1.0 {
		t.Errorf("expected buy size factor 1.0 (no burst), got %f", buySF)
	}
	if sellSF != 1.0 {
		t.Errorf("expected sell size factor 1.0 (no burst), got %f", sellSF)
	}
}

func TestFillBurstActivates(t *testing.T) {
	// 3 fills in window with threshold=3 should activate burst
	tracker := NewFillBurstTracker(5.0, 3, 2.0, 0.3, 15.0)
	tracker.RecordFill("buy")
	tracker.RecordFill("buy")
	tracker.RecordFill("buy")

	buyMult, _ := tracker.SpreadMultiplier()
	if buyMult <= 1.0 {
		t.Errorf("expected buy spread mult > 1.0 after burst, got %f", buyMult)
	}
	if buyMult > 2.0 {
		t.Errorf("expected buy spread mult <= 2.0, got %f", buyMult)
	}

	buySF, _ := tracker.SizeFactor()
	if buySF >= 1.0 {
		t.Errorf("expected buy size factor < 1.0 after burst, got %f", buySF)
	}
	if buySF < 0.3 {
		t.Errorf("expected buy size factor >= 0.3, got %f", buySF)
	}
}

func TestFillBurstDecays(t *testing.T) {
	// Activate burst, then check that after some time the multiplier has decayed
	tracker := NewFillBurstTracker(5.0, 3, 2.0, 0.3, 0.5) // 500ms half-life for test
	tracker.RecordFill("sell")
	tracker.RecordFill("sell")
	tracker.RecordFill("sell")

	_, sellMultPeak := tracker.SpreadMultiplier()
	if sellMultPeak <= 1.0 {
		t.Fatalf("expected sell spread mult > 1.0 at peak, got %f", sellMultPeak)
	}

	// Wait for several half-lives (3s = 6 half-lives, decay ~= 0.016)
	time.Sleep(3 * time.Second)

	_, sellMultAfter := tracker.SpreadMultiplier()
	if sellMultAfter >= sellMultPeak {
		t.Errorf("expected sell spread mult to decay, peak=%f after=%f", sellMultPeak, sellMultAfter)
	}
	// After 6 half-lives, should be very close to 1.0
	if sellMultAfter > 1.05 {
		t.Errorf("expected sell spread mult near 1.0 after 6 half-lives, got %f", sellMultAfter)
	}

	// Size factor should also have recovered toward 1.0
	_, sellSFAfter := tracker.SizeFactor()
	if sellSFAfter < 0.95 {
		t.Errorf("expected sell size factor near 1.0 after 6 half-lives, got %f", sellSFAfter)
	}
}

func TestFillBurstPerSide(t *testing.T) {
	// Buy burst should only affect buy side, not sell
	tracker := NewFillBurstTracker(5.0, 3, 2.0, 0.3, 15.0)
	tracker.RecordFill("buy")
	tracker.RecordFill("buy")
	tracker.RecordFill("buy")

	buyMult, sellMult := tracker.SpreadMultiplier()
	if buyMult <= 1.0 {
		t.Errorf("expected buy spread mult > 1.0 after buy burst, got %f", buyMult)
	}
	if sellMult != 1.0 {
		t.Errorf("expected sell spread mult 1.0 (no sell burst), got %f", sellMult)
	}

	buySF, sellSF := tracker.SizeFactor()
	if buySF >= 1.0 {
		t.Errorf("expected buy size factor < 1.0 after buy burst, got %f", buySF)
	}
	if sellSF != 1.0 {
		t.Errorf("expected sell size factor 1.0 (no sell burst), got %f", sellSF)
	}
}

func TestFillBurstPrunesOldFills(t *testing.T) {
	// Fills outside window should not count toward threshold
	tracker := NewFillBurstTracker(0.1, 3, 2.0, 0.3, 15.0) // 100ms window

	tracker.RecordFill("buy")
	tracker.RecordFill("buy")

	// Wait for fills to expire
	time.Sleep(150 * time.Millisecond)

	// This third fill is within the window, but the first two are expired
	tracker.RecordFill("buy")

	buyMult, _ := tracker.SpreadMultiplier()
	if buyMult != 1.0 {
		t.Errorf("expected buy spread mult 1.0 (old fills pruned), got %f", buyMult)
	}

	buySF, _ := tracker.SizeFactor()
	if buySF != 1.0 {
		t.Errorf("expected buy size factor 1.0 (old fills pruned), got %f", buySF)
	}
}
