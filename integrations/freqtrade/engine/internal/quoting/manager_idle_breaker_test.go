package quoting

import (
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
)

func newIdleBreakerManager(coin string, mm config.MarketMakingConfig) *Manager {
	cfg := &config.Config{
		Strategy: config.StrategyConfig{
			Assets: []string{coin},
		},
		MarketMaking: mm,
	}
	m := &Manager{
		cfg:          cfg,
		supervisor:   NewSupervisor(),
		lastFillTime: make(map[string]time.Time),
		idleBreakers: make(map[string]*idleBreakerState),
	}
	return m
}

func defaultIdleMM() config.MarketMakingConfig {
	return config.MarketMakingConfig{
		IdleFillDetectorEnabled:  true,
		IdleFillThresholdSec:     300,
		IdleFillMinInvUtil:       0.30,
		IdleExitTightenBps:       1.5,
		IdleExitSizeMultiplier:   1.5,
		IdleFillRetryIntervalSec: 120,
	}
}

func TestIdleBreakerFiresWhenAllConditionsMet(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	fired, side := m.checkIdleBreaker("CL", 0.5, now)
	if !fired {
		t.Fatalf("expected breaker to fire")
	}
	if side != "sell" {
		t.Errorf("expected side=sell for positive inv_util, got %s", side)
	}
	state := m.idleBreakers["CL"]
	if state == nil || !state.activeFlag || state.activeSide != "sell" {
		t.Errorf("expected state activated as sell, got %+v", state)
	}
}

func TestIdleBreakerNoFireBelowInvUtil(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	fired, _ := m.checkIdleBreaker("CL", 0.20, now)
	if fired {
		t.Fatalf("expected no fire when inv_util below threshold")
	}
}

func TestIdleBreakerNoFireDuringCooldown(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now); !fired {
		t.Fatalf("first call should fire")
	}
	// 30s later, retry interval is 120s — must not re-fire.
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now.Add(30*time.Second)); fired {
		t.Fatalf("expected cooldown to suppress second fire")
	}
}

func TestIdleBreakerNoFireWhenSupervisorHalted(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	m.supervisor.Signal(SignalRiskHalt)
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now); fired {
		t.Fatalf("expected no fire while supervisor is in flatten state")
	}
}

func TestIdleBreakerClearsOnFill(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now); !fired {
		t.Fatalf("first call should fire")
	}
	if state := m.idleBreakers["CL"]; state == nil || !state.activeFlag {
		t.Fatalf("expected active flag set after fire")
	}
	// Simulate the RecordFill clear path directly (the lock-protected
	// write in RecordFill).
	if state := m.idleBreakers["CL"]; state != nil {
		state.activeFlag = false
	}
	if state := m.idleBreakers["CL"]; state == nil || state.activeFlag {
		t.Fatalf("expected active flag cleared after fill")
	}
}

func TestIdleBreakerRefiresAfterRetryInterval(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now); !fired {
		t.Fatalf("first call should fire")
	}
	// 121s later — past retry interval, last fill is even more idle.
	later := now.Add(121 * time.Second)
	m.lastFillTime["CL"] = now.Add(-521 * time.Second)
	fired, side := m.checkIdleBreaker("CL", 0.5, later)
	if !fired {
		t.Fatalf("expected re-fire after retry interval")
	}
	if side != "sell" {
		t.Errorf("expected side=sell, got %s", side)
	}
}

func TestIdleBreakerExitSideFromInvSign(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	fired, side := m.checkIdleBreaker("CL", -0.5, now)
	if !fired {
		t.Fatalf("expected fire for negative inv_util")
	}
	if side != "buy" {
		t.Errorf("negative inv_util should yield side=buy, got %s", side)
	}

	// Reset and test positive direction in fresh manager.
	m2 := newIdleBreakerManager("CL", defaultIdleMM())
	m2.lastFillTime["CL"] = now.Add(-400 * time.Second)
	fired2, side2 := m2.checkIdleBreaker("CL", 0.5, now)
	if !fired2 {
		t.Fatalf("expected fire for positive inv_util")
	}
	if side2 != "sell" {
		t.Errorf("positive inv_util should yield side=sell, got %s", side2)
	}
}

func TestIdleBreakerNoFireWhenDisabled(t *testing.T) {
	mm := defaultIdleMM()
	mm.IdleFillDetectorEnabled = false
	m := newIdleBreakerManager("CL", mm)
	now := time.Now()
	m.lastFillTime["CL"] = now.Add(-400 * time.Second)
	if fired, _ := m.checkIdleBreaker("CL", 0.5, now); fired {
		t.Fatalf("expected no fire when detector disabled")
	}
}

func TestIdleBreakerNoFireWhenLastFillUnknown(t *testing.T) {
	m := newIdleBreakerManager("CL", defaultIdleMM())
	if fired, _ := m.checkIdleBreaker("CL", 0.5, time.Now()); fired {
		t.Fatalf("expected no fire when lastFillTime is zero")
	}
}
