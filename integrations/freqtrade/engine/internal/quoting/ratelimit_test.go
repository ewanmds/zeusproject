package quoting

import (
	"context"
	"math"
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

func defaultRLConfig() config.RateLimitConfig {
	return config.RateLimitConfig{
		AbsoluteFloor:        300,
		TTECautionMinutes:    60,
		TTEDegradedMinutes:   30,
		TTECriticalMinutes:   15,
		TTERecoveryMinutes:   45,
		EWMAHalfLifeMinutes:  10,
		RemainingPctWarn:     10,
		RemainingPctCritical: 3,
	}
}

func TestRateLimitDisabledObservesWithoutBlocking(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	cfg.RateLimit.Disabled = true
	exec := newMockExecutor()
	sup := NewSupervisor()
	m := &Manager{cfg: cfg, executor: exec, supervisor: sup}
	for _, remaining := range []int{68, 0, -1} {
		m.UpdateRateLimit(remaining, 730496)
		if sup.IsRLCritical() || sup.RLTier() != RLTierNormal {
			t.Fatal("disabled quota guard blocked quoting")
		}
		if m.rateLimitRemaining != remaining {
			t.Fatal("quota telemetry must remain accurate")
		}
	}
	if len(exec.cancels) != 0 || len(exec.closes) != 0 {
		t.Fatal("disabled quota guard must not cancel or flatten")
	}
	// Disabling quota handling must not clear independent risk halts.
	sup.Signal(SignalRiskHalt)
	m.UpdateRateLimit(0, 730496)
	if !sup.riskHalted {
		t.Fatal("independent risk halt was cleared")
	}
}

// fillHistory populates the circular buffer with samples showing a linear depletion.
// startRemaining depletes by ratePerSec over 10 samples at 30s intervals.
func fillHistory(m *Manager, startRemaining int, ratePerSec float64) {
	m.rateLimitHistory = make([]rateLimitSample, 10)
	now := time.Now()
	for i := 0; i < 10; i++ {
		elapsed := float64(i) * 30.0 // 30s apart
		rem := float64(startRemaining) - ratePerSec*elapsed
		m.rateLimitHistory[i] = rateLimitSample{
			remaining: int(rem),
			at:        now.Add(time.Duration(-int64((9-i)*30)) * time.Second),
		}
	}
	m.rateLimitHistoryIdx = 0
	m.rateLimitHistoryFull = true
	m.rateLimitRemaining = m.rateLimitHistory[9].remaining
}

func TestComputeRateLimitTier_Normal(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	fillHistory(m, 10270, 1.0)
	tier, tte, dep := m.computeRateLimitTierLocked()
	if tier != rlTierNormal {
		t.Errorf("expected Normal, got %v (tte=%.0fs, dep=%.2f)", tier, tte, dep)
	}
	if tte < 5000 || math.IsInf(tte, 1) {
		t.Errorf("expected finite TTE > 5000s, got %.0f", tte)
	}
}

func TestComputeRateLimitTier_Caution(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	fillHistory(m, 12000+int(5.0*270), 5.0)
	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60
	if tier != rlTierCaution {
		t.Errorf("expected Caution, got %v (tte=%.1f min)", tier, tteMin)
	}
}

func TestComputeRateLimitTier_Degraded(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	fillHistory(m, 12000+int(10.0*270), 10.0)
	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60
	if tier != rlTierDegraded {
		t.Errorf("expected Degraded, got %v (tte=%.1f min)", tier, tteMin)
	}
}

func TestComputeRateLimitTier_Critical_TTE(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	fillHistory(m, 12000+int(20.0*270), 20.0)
	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60
	if tier != rlTierCritical {
		t.Errorf("expected Critical, got %v (tte=%.1f min)", tier, tteMin)
	}
}

func TestComputeRateLimitTier_Critical_Floor(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	m.rateLimitRemaining = 200
	m.rateLimitHistory = nil
	m.rateLimitHistoryFull = false
	tier, _, _ := m.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Errorf("expected Critical from floor, got %v", tier)
	}
}

func TestComputeRateLimitTier_Recovering(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	fillHistory(m, 10000, -2.0)
	tier, tte, dep := m.computeRateLimitTierLocked()
	if tier != rlTierNormal {
		t.Errorf("expected Normal when recovering, got %v", tier)
	}
	if !math.IsInf(tte, 1) {
		t.Errorf("expected +Inf TTE when recovering, got %.0f", tte)
	}
	if dep >= 0 {
		t.Errorf("expected negative depletion when recovering, got %.2f", dep)
	}
}

func TestComputeRateLimitTier_InsufficientHistory(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	m.rateLimitRemaining = 5000
	m.rateLimitHistory = make([]rateLimitSample, 10)
	m.rateLimitHistoryFull = false
	tier, tte, _ := m.computeRateLimitTierLocked()
	if tier != rlTierNormal {
		t.Errorf("expected Normal with insufficient history, got %v", tier)
	}
	if !math.IsInf(tte, 1) {
		t.Errorf("expected +Inf TTE with insufficient history, got %.0f", tte)
	}
}

func TestRateLimitTierString(t *testing.T) {
	tests := []struct {
		tier rateLimitTier
		want string
	}{
		{rlTierNormal, "normal"},
		{rlTierCaution, "caution"},
		{rlTierDegraded, "degraded"},
		{rlTierCritical, "critical"},
	}
	for _, tt := range tests {
		if got := tt.tier.String(); got != tt.want {
			t.Errorf("rateLimitTier(%d).String() = %q, want %q", tt.tier, got, tt.want)
		}
	}
}

func TestRateLimitShutdown(t *testing.T) {
	exec := newMockExecutor()
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{"xyz:CL", "xyz:BRENTOIL"},
		},
		RateLimit: defaultRLConfig(),
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		executor:   exec,
		supervisor: sup,
	}
	// RL critical is now set by UpdateRateLimit via supervisor signal before
	// calling rateLimitShutdown, so simulate that here.
	m.rateLimitShutdown(100, 300, 5.0)
	exec.mu.Lock()
	defer exec.mu.Unlock()
	if len(exec.cancels) != 2 {
		t.Errorf("expected 2 cancel calls, got %d", len(exec.cancels))
	}
	if len(exec.closes) != 0 {
		t.Errorf("expected 0 close calls (no positions), got %d", len(exec.closes))
	}
	if !m.supervisor.IsRLCritical() {
		t.Error("expected RL critical=true after shutdown")
	}
}

func TestRateLimitShutdownWithPositions(t *testing.T) {
	exec := newMockExecutor()
	exec.pos["xyz:CL"] = types.PositionUpdate{Coin: "xyz:CL", Size: 1.5}
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{"xyz:CL"},
		},
		RateLimit: defaultRLConfig(),
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		executor:   exec,
		supervisor: sup,
	}
	// RL critical is now set by UpdateRateLimit via supervisor signal before
	// calling rateLimitShutdown, so simulate that here.
	m.rateLimitShutdown(100, 300, 5.0)
	exec.mu.Lock()
	defer exec.mu.Unlock()
	if len(exec.cancels) != 1 {
		t.Errorf("expected 1 cancel call, got %d", len(exec.cancels))
	}
	if len(exec.closes) != 1 {
		t.Errorf("expected 1 close call for position, got %d", len(exec.closes))
	}
}

func TestRateLimitRecover(t *testing.T) {
	cfg := &config.Config{
		RateLimit: defaultRLConfig(),
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		supervisor: sup,
		startedAt:  time.Now().Add(-1 * time.Hour),
	}
	oldStartedAt := m.startedAt
	m.rateLimitRecover(5000, 3600, -2.0)
	m.mu.Lock()
	defer m.mu.Unlock()
	// Note: rateLimitRecover no longer clears RL critical directly;
	// the caller (UpdateRateLimit) signals SignalRLRecover before calling.
	if !m.startedAt.After(oldStartedAt) {
		t.Error("expected startedAt to be reset (re-trigger warmup)")
	}
}

func TestRateLimitTierHysteresis(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	sup := NewSupervisor()
	m := &Manager{cfg: cfg, supervisor: sup}

	// Set current tier to Degraded via supervisor signal.
	sup.Signal(SignalRLDegraded)

	// TTE = 35 min: raw would be Caution, but hysteresis holds at Degraded.
	fillHistory(m, 21000+int(10.0*270), 10.0)
	m.rateLimitRemaining = 21000
	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60
	if tier != rlTierDegraded {
		t.Errorf("hysteresis: expected Degraded to hold at TTE=%.1f min, got %v", tteMin, tier)
	}

	// TTE = 70 min (exceeds caution=60): should de-escalate to Normal.
	fillHistory(m, 42000+int(10.0*270), 10.0)
	m.rateLimitRemaining = 42000
	tier, tte, _ = m.computeRateLimitTierLocked()
	tteMin = tte / 60
	if tier != rlTierNormal {
		t.Errorf("hysteresis: expected Normal at TTE=%.1f min, got %v", tteMin, tier)
	}
}

// TestCriticalRecoveryViaUpdateRateLimit verifies that UpdateRateLimit can
// recover from Critical tier when rate limits replenish. This is the
// integration test that caught the dead-recovery bug where hysteresis
// unconditionally held tier at Critical, making the recovery check dead code.
func TestCriticalRecoveryViaUpdateRateLimit(t *testing.T) {
	exec := newMockExecutor()
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{"xyz:CL"},
		},
		RateLimit: defaultRLConfig(), // recovery = 45 min, floor = 300, pct_critical = 3%
		MarketMaking: config.MarketMakingConfig{
			RefreshIntervalMs: 15000,
		},
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		executor:   exec,
		supervisor: sup,
		startedAt:  time.Now().Add(-1 * time.Hour),
	}

	// Feed healthy rate limit data: remaining = 5000 with negative depletion
	// (recovering), which produces TTE = +Inf (safe). Recovery requires
	// TTE > 45 min AND remaining > 2 × floor (600) AND pct >= pct_critical.
	fillHistory(m, 4460, -2.0) // -2.0 = recovering at 2 units/sec
	m.rateLimitRemaining = 5000

	// Before: halted.
	if !m.supervisor.IsRLCritical() {
		t.Fatal("precondition: expected RL critical=true")
	}
	if m.supervisor.RLTier() != RLTierCritical {
		t.Fatalf("precondition: expected Critical tier, got %v", m.supervisor.RLTier())
	}

	// UpdateRateLimit with remaining/cap = 5000/10000 = 50% (well above 3% threshold).
	// Should detect recovery conditions and clear the halt.
	m.UpdateRateLimit(5000, 10000)

	if m.supervisor.IsRLCritical() {
		t.Error("expected RL critical=false after UpdateRateLimit with healthy rate limits")
	}
	if m.supervisor.RLTier() == RLTierCritical {
		t.Errorf("expected tier to de-escalate from Critical, got %v", m.supervisor.RLTier())
	}
}

// TestPctCriticalRecoveryBlocked verifies that recovery from Critical tier is
// blocked when the remaining/cap percentage is still below the critical threshold.
// This prevents the oscillation bug where pct-triggered Critical would recover
// (because TTE was healthy) and then immediately re-trigger on the next poll.
func TestPctCriticalRecoveryBlocked(t *testing.T) {
	exec := newMockExecutor()
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{"xyz:CL"},
		},
		RateLimit: defaultRLConfig(), // pct_critical = 3%, recovery = 45 min, floor = 300
		MarketMaking: config.MarketMakingConfig{
			RefreshIntervalMs: 15000,
		},
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		executor:   exec,
		supervisor: sup,
		startedAt:  time.Now().Add(-1 * time.Hour),
	}

	// remaining = 5000, cap = 270000. Pct = 1.85% < 3% (below pct_critical).
	// TTE is +Inf (negative depletion = recovering), remaining > 2×floor.
	// Without the pct guard, this would recover and then immediately re-trigger.
	fillHistory(m, 4460, -2.0)
	m.rateLimitRemaining = 5000

	if !m.supervisor.IsRLCritical() {
		t.Fatal("precondition: expected RL critical=true")
	}

	// UpdateRateLimit with pct = 5000/270000 = 1.85% < 3%.
	// Recovery should be BLOCKED because pct is still below critical threshold.
	m.UpdateRateLimit(5000, 270000)

	if !m.supervisor.IsRLCritical() {
		t.Error("expected RL critical to remain true when pct (1.85%) < pct_critical (3%)")
	}

	// Now simulate credits recovering above the pct threshold.
	// remaining = 9000, cap = 270000. Pct = 3.33% > 3%.
	fillHistory(m, 8460, -2.0)
	m.rateLimitRemaining = 9000
	m.UpdateRateLimit(9000, 270000)

	if m.supervisor.IsRLCritical() {
		t.Error("expected RL critical=false after pct recovered above threshold (3.33% > 3%)")
	}
}

// TestPctCriticalGracePeriod verifies that the percentage-based critical floor
// is skipped during the startup grace period, allowing accounts with low
// remaining at boot to operate until a depletion rate is established.
func TestPctCriticalGracePeriod(t *testing.T) {
	rlCfg := defaultRLConfig()
	rlCfg.PctCriticalGraceSec = 120
	cfg := &config.Config{RateLimit: rlCfg}
	m := &Manager{cfg: cfg}

	// remaining = 5000, cap = 270000 → 1.85% < 3% (pct_critical).
	// Without grace period, this would be Critical.
	m.rateLimitRemaining = 5000
	m.rateLimitCap = 270000
	m.rateLimitHistoryFull = false

	// Case 1: Within grace period — should NOT trigger pct-based critical.
	m.startedAt = time.Now().Add(-10 * time.Second) // 10s ago, well within 120s grace
	tier, _, _ := m.computeRateLimitTierLocked()
	if tier == rlTierCritical {
		t.Error("expected non-critical during grace period, got Critical")
	}

	// Case 2: After grace period — should trigger pct-based critical.
	m.startedAt = time.Now().Add(-130 * time.Second) // 130s ago, past 120s grace
	tier, _, _ = m.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Errorf("expected Critical after grace period expired, got %v", tier)
	}

	// Case 3: Absolute floor is always enforced regardless of grace period.
	m.startedAt = time.Now().Add(-10 * time.Second) // within grace
	m.rateLimitRemaining = 200                      // below absolute floor (300)
	tier, _, _ = m.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Error("expected Critical from absolute floor even during grace period")
	}
}

// TestCriticalHysteresisHoldsWhenStillDepleting verifies that Critical tier
// is held (not de-escalated) when TTE is below recovery threshold.
func TestCriticalHysteresisHoldsWhenStillDepleting(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{cfg: cfg, supervisor: sup}

	// TTE = ~10 min (below recovery=45 and even below critical=15): hold at Critical.
	fillHistory(m, 12000+int(20.0*270), 20.0)
	m.rateLimitRemaining = 12000

	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60
	if tier != rlTierCritical {
		t.Errorf("hysteresis: expected Critical to hold at TTE=%.1f min, got %v", tteMin, tier)
	}
}

// TestFillRequoteBlockedByRLHalted verifies that fill-triggered requotes
// are suppressed when the rate limit circuit breaker is in Critical halt.
func TestFillRequoteBlockedByRLHalted(t *testing.T) {
	exec := newMockExecutor()
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{"xyz:CL"},
		},
		RateLimit: defaultRLConfig(),
		MarketMaking: config.MarketMakingConfig{
			RefreshIntervalMs: 15000,
		},
	}
	sup := NewSupervisor()
	sup.Signal(SignalRLCritical)
	m := &Manager{
		cfg:        cfg,
		executor:   exec,
		supervisor: sup,
	}
	m.maybeRefreshFromFill(context.Background(), "xyz:CL")

	// Should have returned early without setting fillTriggered.
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.fillTriggered {
		t.Error("expected fillTriggered=false when RL critical is true")
	}
}

// TestEWMAFallback verifies that when the short window sees zero depletion
// but the EWMA detects slow secular drain, the EWMA is used as a fallback
// for TTE calculation instead of returning +Inf.
func TestEWMAFallback(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}

	// Short window shows zero depletion (remaining stable over 90s).
	fillHistory(m, 7440, 0)

	// But EWMA has detected slow drain: 0.14 req/s over a long period.
	m.rlEWMASeeded = true
	m.rlEWMADepletion = 0.14 // ~8.5 per minute — the v22 session's actual drain rate.

	tier, tte, dep := m.computeRateLimitTierLocked()
	tteMin := tte / 60.0

	// With EWMA depletion of 0.14 and remaining 7440:
	// TTE = 7440 / 0.14 = 53143s = 886 min.
	// This is > 60 min (caution), so tier should be Normal.
	if tier != rlTierNormal {
		t.Errorf("expected Normal (TTE=%.0f min from EWMA fallback), got %v", tteMin, tier)
	}
	if math.IsInf(tte, 1) {
		t.Error("expected finite TTE from EWMA fallback, got +Inf")
	}
	if dep <= 0 {
		t.Errorf("expected positive depletion from EWMA fallback, got %.4f", dep)
	}
	// Verify TTE is in the right ballpark (~53143s).
	if tte < 50000 || tte > 60000 {
		t.Errorf("expected TTE ~53143s from EWMA fallback, got %.0f", tte)
	}
}

// TestEWMAFallbackTriggersCaution verifies that EWMA-derived depletion can
// escalate to Caution tier when the drain rate is high enough.
func TestEWMAFallbackTriggersCaution(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}

	// Short window shows zero depletion.
	fillHistory(m, 5000, 0)

	// EWMA detects significant drain: 2 req/s.
	// TTE = 5000 / 2.0 = 2500s = 41.7 min → below caution (60) but above degraded (30).
	m.rlEWMASeeded = true
	m.rlEWMADepletion = 2.0

	tier, tte, _ := m.computeRateLimitTierLocked()
	tteMin := tte / 60.0

	if tier != rlTierCaution {
		t.Errorf("expected Caution from EWMA fallback (TTE=%.1f min), got %v", tteMin, tier)
	}
}

// TestEWMANoFallbackWhenBothSafe verifies that when both the short window
// and EWMA show zero/negative depletion, we return Normal with +Inf TTE.
func TestEWMANoFallbackWhenBothSafe(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}

	// Short window: recovering (negative depletion).
	fillHistory(m, 10000, -1.0)

	// EWMA also shows recovery.
	m.rlEWMASeeded = true
	m.rlEWMADepletion = -0.5

	tier, tte, _ := m.computeRateLimitTierLocked()
	if tier != rlTierNormal {
		t.Errorf("expected Normal when both short and EWMA are safe, got %v", tier)
	}
	if !math.IsInf(tte, 1) {
		t.Errorf("expected +Inf TTE when both safe, got %.0f", tte)
	}
}

// TestPctCriticalFloor verifies that the percentage-based Critical floor
// triggers when remaining/cap drops below the configured threshold.
func TestPctCriticalFloor(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()} // remaining_pct_critical = 3%
	m := &Manager{cfg: cfg}

	// remaining = 4000, cap = 163000. Pct = 2.45% < 3% → Critical.
	m.rateLimitRemaining = 4000
	m.rateLimitCap = 163000

	// Short window shows zero depletion (looks safe).
	fillHistory(m, 4000, 0)

	tier, _, _ := m.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Errorf("expected Critical from pct floor (2.45%% < 3%%), got %v", tier)
	}
}

// TestPctCriticalDoesNotFireAboveThreshold verifies the percentage check
// does not trigger when remaining is above the threshold.
func TestPctCriticalDoesNotFireAboveThreshold(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()} // remaining_pct_critical = 3%
	m := &Manager{cfg: cfg}

	// remaining = 7000, cap = 163000. Pct = 4.29% > 3% → no pct trigger.
	m.rateLimitRemaining = 7000
	m.rateLimitCap = 163000

	// Short window shows zero depletion → Normal.
	fillHistory(m, 7000, 0)

	tier, tte, _ := m.computeRateLimitTierLocked()
	if tier == rlTierCritical {
		t.Errorf("expected non-Critical (pct 4.29%% > 3%%), got %v", tier)
	}
	_ = tte
}

// TestPctCriticalUsesEWMAForTTE verifies that when pct Critical fires,
// the returned TTE uses EWMA depletion if available.
func TestPctCriticalUsesEWMAForTTE(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}

	m.rateLimitRemaining = 4000
	m.rateLimitCap = 163000
	m.rlEWMASeeded = true
	m.rlEWMADepletion = 0.14

	// No history needed — pct floor fires before history check.
	m.rateLimitHistoryFull = false

	tier, tte, dep := m.computeRateLimitTierLocked()
	if tier != rlTierCritical {
		t.Errorf("expected Critical from pct floor, got %v", tier)
	}
	// TTE = 4000 / 0.14 = 28571s = 476 min.
	if tte < 25000 || tte > 32000 {
		t.Errorf("expected TTE ~28571s from EWMA in pct-Critical, got %.0f", tte)
	}
	if dep != 0.14 {
		t.Errorf("expected depletion=0.14 from EWMA, got %.4f", dep)
	}
}

// TestEWMAUpdatesCorrectly verifies that the EWMA depletion tracker
// produces reasonable values after multiple UpdateRateLimit calls.
func TestEWMAUpdatesCorrectly(t *testing.T) {
	cfg := &config.Config{
		RateLimit: defaultRLConfig(),
		MarketMaking: config.MarketMakingConfig{
			RefreshIntervalMs: 15000,
		},
	}
	m := &Manager{cfg: cfg}

	// Simulate 60 updates at 10s intervals, draining 1 token per update.
	// That's 0.1 tokens/sec drain rate.
	start := 10000
	for i := 0; i < 60; i++ {
		remaining := start - i
		if i > 0 {
			m.rlEWMALastAt = time.Now().Add(-10 * time.Second)
		}
		m.UpdateRateLimit(remaining, 163000)
		// Advance the EWMA clock manually since UpdateRateLimit uses time.Now().
		// We need to hack the timestamps for a deterministic test.
	}

	m.mu.Lock()
	ewma := m.rlEWMADepletion
	seeded := m.rlEWMASeeded
	m.mu.Unlock()

	if !seeded {
		t.Fatal("expected EWMA to be seeded after 60 updates")
	}
	// The EWMA should be approximately 0.1 req/s (1 token per 10s).
	// Allow some tolerance since time.Now() intervals may not be exactly 10s.
	// The EWMA converges slowly with a 10-minute half-life, so after 60 updates
	// (very short real time in tests), it won't fully converge. But it should be
	// positive (detecting drain).
	if ewma <= 0 {
		t.Errorf("expected positive EWMA depletion after drain, got %.6f", ewma)
	}
}

// TestUpdateRateLimitPctWarnLogging verifies the pct-warn log fires.
func TestUpdateRateLimitPctWarnLogging(t *testing.T) {
	cfg := &config.Config{
		RateLimit: defaultRLConfig(), // warn = 10%, critical = 3%
		MarketMaking: config.MarketMakingConfig{
			RefreshIntervalMs: 15000,
		},
	}
	m := &Manager{cfg: cfg}

	// First call with plenty of headroom — no warn.
	m.UpdateRateLimit(20000, 163000) // 12.3%
	m.mu.Lock()
	warned := m.rlPctWarned
	m.mu.Unlock()
	if warned {
		t.Error("expected no pct warn at 12.3% remaining")
	}

	// Second call drops below 10% — warn should fire.
	m.UpdateRateLimit(15000, 163000) // 9.2%
	m.mu.Lock()
	warned = m.rlPctWarned
	m.mu.Unlock()
	if !warned {
		t.Error("expected pct warn at 9.2% remaining")
	}

	// Remains below — should not re-warn (rlPctWarned stays true).
	m.UpdateRateLimit(14000, 163000) // 8.6%
	m.mu.Lock()
	warned = m.rlPctWarned
	m.mu.Unlock()
	if !warned {
		t.Error("expected pct warn to stay set")
	}

	// Recovers above 10% — warn flag should reset.
	m.UpdateRateLimit(20000, 163000) // 12.3%
	m.mu.Lock()
	warned = m.rlPctWarned
	m.mu.Unlock()
	if warned {
		t.Error("expected pct warn to reset after recovery above threshold")
	}
}

// fillHistoryProduction populates the circular buffer with 15 samples at 10s intervals,
// matching the production polling rate. This verifies that the buffer is large enough
// for the 120s minimum elapsed threshold.
func fillHistoryProduction(m *Manager, startRemaining int, ratePerSec float64) {
	m.rateLimitHistory = make([]rateLimitSample, 15)
	now := time.Now()
	for i := 0; i < 15; i++ {
		elapsed := float64(i) * 10.0 // 10s apart (production polling interval)
		rem := float64(startRemaining) - ratePerSec*elapsed
		m.rateLimitHistory[i] = rateLimitSample{
			remaining: int(rem),
			at:        now.Add(time.Duration(-int64((14-i)*10)) * time.Second),
		}
	}
	m.rateLimitHistoryIdx = 0
	m.rateLimitHistoryFull = true
	m.rateLimitRemaining = m.rateLimitHistory[14].remaining
}

// TestComputeRateLimitTier_ProductionPolling verifies that TTE is finite
// at the production 10s polling interval with the 15-sample buffer.
// This was a bug: with a 10-sample buffer, elapsed = 90s < 120s threshold,
// causing TTE to always be +Inf.
func TestComputeRateLimitTier_ProductionPolling(t *testing.T) {
	cfg := &config.Config{RateLimit: defaultRLConfig()}
	m := &Manager{cfg: cfg}
	// 15 samples × 10s = 140s elapsed (> 120s threshold).
	// 10000 remaining, draining at 1.0/s → TTE = 10000s ≈ 167 min → Normal tier.
	fillHistoryProduction(m, 10140, 1.0)
	tier, tte, dep := m.computeRateLimitTierLocked()
	if tier != rlTierNormal {
		t.Errorf("expected Normal, got %v (tte=%.0fs, dep=%.2f)", tier, tte, dep)
	}
	if math.IsInf(tte, 1) {
		t.Errorf("expected finite TTE at production polling rate, got +Inf (buffer too small for 120s threshold?)")
	}
	if tte < 5000 {
		t.Errorf("expected TTE > 5000s, got %.0f", tte)
	}
	if dep < 0.5 || dep > 1.5 {
		t.Errorf("expected depletion ~1.0/s, got %.2f", dep)
	}
}

// TestTTEFiniteViaUpdateRateLimit exercises the actual production code path:
// calls UpdateRateLimit at 10s intervals (simulated via timestamp manipulation)
// and verifies computeRateLimitTierLocked returns a finite TTE once the buffer fills.
// This guards against the buffer size being reduced below the minimum needed for
// the 120s elapsed threshold.
func TestTTEFiniteViaUpdateRateLimit(t *testing.T) {
	cfg := &config.Config{
		RateLimit:    defaultRLConfig(),
		MarketMaking: config.MarketMakingConfig{RefreshIntervalMs: 15000},
	}
	m := &Manager{cfg: cfg}

	// Simulate 20 UpdateRateLimit calls at 10s intervals, draining 1 token/call.
	// After enough calls, the buffer should be full and TTE should be finite.
	start := 50000
	cap := 192000
	for i := 0; i < 20; i++ {
		remaining := start - i
		m.UpdateRateLimit(remaining, cap)

		// Hack timestamps to simulate 10s intervals (UpdateRateLimit uses time.Now()).
		m.mu.Lock()
		if m.rateLimitHistoryFull || m.rateLimitHistoryIdx > 0 {
			// Rewrite the last-written sample's timestamp.
			lastIdx := (m.rateLimitHistoryIdx - 1 + len(m.rateLimitHistory)) % len(m.rateLimitHistory)
			m.rateLimitHistory[lastIdx].at = time.Now().Add(time.Duration(-int64((19-i)*10)) * time.Second)
		}
		// Also fix EWMA timestamps for consistency.
		m.rlEWMALastAt = time.Now().Add(time.Duration(-int64((19-i)*10)) * time.Second)
		m.mu.Unlock()
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.rateLimitHistoryFull {
		t.Fatal("expected history buffer to be full after 20 updates")
	}

	tier, tte, dep := m.computeRateLimitTierLocked()
	if math.IsInf(tte, 1) {
		// This is the exact regression we're guarding against.
		bufLen := len(m.rateLimitHistory)
		oldest := m.rateLimitHistoryIdx
		newest := (m.rateLimitHistoryIdx - 1 + bufLen) % bufLen
		elapsed := m.rateLimitHistory[newest].at.Sub(m.rateLimitHistory[oldest].at).Seconds()
		t.Errorf("TTE is +Inf via UpdateRateLimit path: buffer_size=%d, elapsed=%.0fs (need >=120s). "+
			"Buffer is too small for the 120s threshold at 10s polling.",
			bufLen, elapsed)
	}
	if tier != rlTierNormal {
		t.Errorf("expected Normal tier, got %v (tte=%.0fs, dep=%.4f)", tier, tte, dep)
	}
}

// TestSessionAccounting verifies the session_used / session_generated / session_net
// fields returned by RateLimitStatus.
func TestSessionAccounting(t *testing.T) {
	cfg := &config.Config{
		RateLimit:    defaultRLConfig(),
		MarketMaking: config.MarketMakingConfig{RefreshIntervalMs: 15000},
	}
	sup := NewSupervisor()
	m := &Manager{cfg: cfg, supervisor: sup}

	// Boot: 10000 remaining, cap 192000.
	m.UpdateRateLimit(10000, 192000)

	// After some activity: 9950 remaining, cap grew to 192020 (volume regen).
	m.UpdateRateLimit(9950, 192020)

	// Caller says bot sent 70 exchange requests this session.
	var sessionExchangeReqs int64 = 70
	_, _, _, _, _, _, _, sessUsed, sessGen, sessNet := m.RateLimitStatus(sessionExchangeReqs)

	// session_net = boot(10000) - current(9950) = 50 (net bucket drain).
	if sessNet != 50 {
		t.Errorf("expected session_net=50, got %d", sessNet)
	}
	// session_used = 70 (passed in from caller).
	if sessUsed != 70 {
		t.Errorf("expected session_used=70, got %d", sessUsed)
	}
	// session_generated = cap_delta = 192020 - 192000 = 20 (volume regen).
	if sessGen != 20 {
		t.Errorf("expected session_generated=20, got %d", sessGen)
	}
}

// TestSessionAccountingNoCapGrowth verifies that session_generated=0 when
// cap doesn't change (no volume-based regeneration occurred).
func TestSessionAccountingNoCapGrowth(t *testing.T) {
	cfg := &config.Config{
		RateLimit:    defaultRLConfig(),
		MarketMaking: config.MarketMakingConfig{RefreshIntervalMs: 15000},
	}
	sup := NewSupervisor()
	m := &Manager{cfg: cfg, supervisor: sup}

	m.UpdateRateLimit(10000, 192000)
	// Bucket drained by 100, cap unchanged — no volume regen.
	m.UpdateRateLimit(9900, 192000)

	_, _, _, _, _, _, _, sessUsed, sessGen, sessNet := m.RateLimitStatus(30)
	if sessNet != 100 {
		t.Errorf("expected session_net=100, got %d", sessNet)
	}
	if sessUsed != 30 {
		t.Errorf("expected session_used=30, got %d", sessUsed)
	}
	// No cap growth = 0 generated.
	if sessGen != 0 {
		t.Errorf("expected session_generated=0, got %d", sessGen)
	}
}
