package quoting

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"math"
	"strings"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
)

func TestShouldSkipKnobUpdate_BelowDeadbandSkips(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 1.0
	if !shouldSkipKnobUpdate("spreadDeltaBuy", 0.5, cfg) {
		t.Errorf("expected skip when |delta|=0.5 < deadband=1.0")
	}
	if !shouldSkipKnobUpdate("spreadDeltaBuy", -0.5, cfg) {
		t.Errorf("expected skip for negative delta below deadband")
	}
}

func TestShouldSkipKnobUpdate_AboveDeadbandApplies(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 1.0
	if shouldSkipKnobUpdate("spreadDeltaBuy", 2.0, cfg) {
		t.Errorf("expected NO skip when |delta|=2.0 > deadband=1.0")
	}
}

func TestShouldSkipKnobUpdate_ZeroDeadbandAlwaysApplies(t *testing.T) {
	cfg := defaultV2Config()
	// All deadbands left at 0 — every delta should apply.
	for _, knob := range []string{"spreadDeltaBuy", "spreadDeltaSell", "skewMult", "notionalMult", "alphaDelta"} {
		if shouldSkipKnobUpdate(knob, 1e-12, cfg) {
			t.Errorf("knob %s: expected no skip with zero deadband", knob)
		}
		if shouldSkipKnobUpdate(knob, 100, cfg) {
			t.Errorf("knob %s: expected no skip with zero deadband", knob)
		}
	}
}

func TestShouldSkipKnobUpdate_PerKnobMapping(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 0.5
	cfg.SkewDeltaDeadband = 0.1
	cfg.NotionalDeltaDeadband = 0.05

	if !shouldSkipKnobUpdate("skewMult", 0.05, cfg) {
		t.Errorf("expected skewMult skip below 0.1 deadband")
	}
	if shouldSkipKnobUpdate("skewMult", 0.2, cfg) {
		t.Errorf("expected skewMult apply above 0.1 deadband")
	}
	if !shouldSkipKnobUpdate("notionalMult", 0.02, cfg) {
		t.Errorf("expected notionalMult skip below 0.05 deadband")
	}
	if shouldSkipKnobUpdate("notionalMult", 0.10, cfg) {
		t.Errorf("expected notionalMult apply above 0.05 deadband")
	}
	// Unmapped knobs (alphaDelta, imbalanceShiftMult, ...) have no deadband.
	if shouldSkipKnobUpdate("alphaDelta", 1e-12, cfg) {
		t.Errorf("alphaDelta should never skip (no deadband configured for it)")
	}
}

// TestOptimizerV2_DeadbandSkipsKnobWrite drives approach() directly and
// verifies that small proposed updates are skipped, leaving o.current unchanged.
func TestOptimizerV2_DeadbandSkipsKnobWrite(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 1.0
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 100 // confidence = 1.0

	// approach() computes proposedDelta = lr * conf * newVelocity, where
	// newVelocity = momentum*velocity + (1-momentum)*direction. With initial
	// velocity 0, momentum 0.8, lr 0.1, conf 1.0, direction = target - current:
	// proposed = 0.1 * 1.0 * (0.0*0.8 + 0.2*direction) = 0.02 * direction.
	// To get |proposed| < 1.0 we need |direction| < 50. Use a small target.
	target := 0.5 // proposed = 0.02 * 0.5 = 0.01 bps, well below 1.0 deadband
	prev := opt.current["spreadDeltaBuy"]
	moved := opt.approach("spreadDeltaBuy", target)
	if moved {
		t.Errorf("approach should report NOT moved when delta below deadband")
	}
	if opt.current["spreadDeltaBuy"] != prev {
		t.Errorf("current value changed despite deadband skip: prev=%g now=%g",
			prev, opt.current["spreadDeltaBuy"])
	}
	if len(opt.pendingDeadbandSkips) != 1 || opt.pendingDeadbandSkips[0] != "spreadDeltaBuy" {
		t.Errorf("expected pendingDeadbandSkips=[spreadDeltaBuy], got %v", opt.pendingDeadbandSkips)
	}
}

func TestOptimizerV2_DeadbandAppliesAboveThreshold(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 0.001 // tiny so even small deltas apply
	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 100

	prev := opt.current["spreadDeltaBuy"]
	moved := opt.approach("spreadDeltaBuy", 5.0)
	if !moved {
		t.Errorf("approach should report moved when delta above deadband")
	}
	if math.Abs(opt.current["spreadDeltaBuy"]-prev) < 1e-9 {
		t.Errorf("current value should have changed; prev=%g now=%g",
			prev, opt.current["spreadDeltaBuy"])
	}
	if len(opt.pendingDeadbandSkips) != 0 {
		t.Errorf("expected no pending deadband skips, got %v", opt.pendingDeadbandSkips)
	}
}

// TestOptimizerV2_DeadbandRateLimit verifies the 30s/coin emit cap.
func TestOptimizerV2_DeadbandRateLimit(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 1.0

	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)

	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)

	// Simulate 20 rapid evals where a knob is skipped each time.
	for i := 0; i < 20; i++ {
		opt.pendingDeadbandSkips = []string{"spreadDeltaBuy"}
		opt.pendingDeadbandMaxAbs = 0.5
		opt.maybeLogDeadbandSkip()
	}

	count := strings.Count(buf.String(), `"msg":"optimizer deadband skip"`)
	if count != 1 {
		t.Errorf("expected exactly 1 deadband-skip log within 30s, got %d", count)
	}
}

// TestOptimizerV2_DeadbandRateLimitReleasesAfter30s manipulates lastDeadbandLog
// to simulate a 30s gap and verifies a second emission fires.
func TestOptimizerV2_DeadbandRateLimitReleasesAfter30s(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 1.0

	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)

	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.pendingDeadbandSkips = []string{"spreadDeltaBuy"}
	opt.pendingDeadbandMaxAbs = 0.5
	opt.maybeLogDeadbandSkip()
	first := strings.Count(buf.String(), `"msg":"optimizer deadband skip"`)
	if first != 1 {
		t.Fatalf("expected initial emission, got %d", first)
	}

	// Second call within 30s should be suppressed.
	opt.pendingDeadbandSkips = []string{"spreadDeltaBuy"}
	opt.pendingDeadbandMaxAbs = 0.5
	opt.maybeLogDeadbandSkip()
	if c := strings.Count(buf.String(), `"msg":"optimizer deadband skip"`); c != 1 {
		t.Errorf("expected suppression within 30s; count=%d", c)
	}

	// Rewind the rate-limit timestamp by 31s and trigger again.
	opt.lastDeadbandLog = time.Now().Add(-31 * time.Second)
	opt.pendingDeadbandSkips = []string{"spreadDeltaBuy"}
	opt.pendingDeadbandMaxAbs = 0.5
	opt.maybeLogDeadbandSkip()
	if c := strings.Count(buf.String(), `"msg":"optimizer deadband skip"`); c != 2 {
		t.Errorf("expected second emission after 30s gap; count=%d", c)
	}
}

// TestOptimizerV2_DeadbandLogMaxDeltaIsLargestAbs ensures max_delta_abs reports
// the largest absolute proposed delta across all skipped knobs in an eval.
func TestOptimizerV2_DeadbandLogMaxDeltaIsLargestAbs(t *testing.T) {
	cfg := defaultV2Config()
	cfg.SpreadDeltaDeadbandBps = 100.0

	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)

	opt := NewOptimizerV2(cfg, "TEST", 3.0, 0.4)
	opt.totalFills = 100

	// Manually drive approach with two different proposed magnitudes by setting
	// distinct targets. proposed = 0.02 * direction with default cfg.
	opt.approach("spreadDeltaBuy", 5.0)  // proposed ~0.10
	opt.approach("spreadDeltaSell", 8.0) // proposed ~0.16
	if opt.pendingDeadbandMaxAbs < 0.15 || opt.pendingDeadbandMaxAbs > 0.17 {
		t.Errorf("expected pendingDeadbandMaxAbs ~0.16, got %g", opt.pendingDeadbandMaxAbs)
	}
}

// TestDeadbandConfigJSON ensures the new fields round-trip via JSON for the
// startup config dump.
func TestDeadbandConfigJSON(t *testing.T) {
	cfg := config.OptimizerV2Config{
		Enabled:                true,
		SpreadDeltaDeadbandBps: 0.1,
		SkewDeltaDeadband:      0.02,
		NotionalDeltaDeadband:  0.05,
	}
	b, err := json.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}
	s := string(b)
	for _, want := range []string{"SpreadDeltaDeadbandBps", "SkewDeltaDeadband", "NotionalDeltaDeadband"} {
		if !strings.Contains(s, want) {
			t.Errorf("expected %s in JSON output: %s", want, s)
		}
	}
}
