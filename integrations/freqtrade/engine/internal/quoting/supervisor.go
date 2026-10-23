package quoting

import (
	"fmt"
	"log/slog"
	"sync"
	"time"
)

// externalPhase tracks the observable shock-chain external-halt phase for
// edge-detection logging. Distinct from OpState because OpFlatten/OpRecovery
// are shared with other non-external causes (risk halt, drawdown recovery).
type externalPhase int

const (
	extPhaseNormal externalPhase = iota
	extPhaseHalt
	extPhaseRecovery
)

// OpState represents the operational state of the trading system.
// States are resolved by priority: Flatten > CancelOnly > Recovery > Degraded > Normal.
type OpState int

const (
	OpNormal     OpState = iota // Normal quoting
	OpDegraded                  // Quote with wider spreads / reduced notional
	OpRecovery                  // Time-limited post-drawdown recovery
	OpCancelOnly                // Cancel all orders, no new quotes
	OpFlatten                   // Cancel all orders + close all positions
)

func (s OpState) String() string {
	switch s {
	case OpNormal:
		return "Normal"
	case OpDegraded:
		return "Degraded"
	case OpRecovery:
		return "Recovery"
	case OpCancelOnly:
		return "CancelOnly"
	case OpFlatten:
		return "Flatten"
	default:
		return fmt.Sprintf("OpState(%d)", s)
	}
}

// Signal represents an event that can change the Supervisor's operational state.
type Signal int

const (
	SignalRiskHalt         Signal = iota // Sets riskHalted=true, clears recovery
	SignalRiskResume                     // Clears riskHalted
	SignalRLCritical                     // Sets rlCritical=true, clears rlDegraded and rlCaution
	SignalRLDegraded                     // Sets rlDegraded=true, clears rlCaution and rlCritical
	SignalRLCaution                      // Sets rlCaution=true, clears rlDegraded and rlCritical
	SignalRLRecover                      // Clears all RL flags
	SignalBookStale                      // Sets bookStale=true
	SignalFeedDead                       // Sets feedDead=true
	SignalFeedRecover                    // Clears both bookStale and feedDead
	SignalRefreshFailed                  // Sets refreshFailed=true
	SignalRefreshRecovered               // Clears refreshFailed
)

// RLTier represents the rate-limit circuit breaker tier as exposed by the Supervisor.
type RLTier int

const (
	RLTierNormal   RLTier = iota // TTE > caution threshold
	RLTierCaution                // TTE between degraded and caution thresholds
	RLTierDegraded               // TTE between critical and degraded thresholds
	RLTierCritical               // TTE < critical threshold or remaining < floor
)

func (t RLTier) String() string {
	switch t {
	case RLTierNormal:
		return "normal"
	case RLTierCaution:
		return "caution"
	case RLTierDegraded:
		return "degraded"
	case RLTierCritical:
		return "critical"
	default:
		return fmt.Sprintf("RLTier(%d)", t)
	}
}

// Supervisor centralizes all operational state transitions. It resolves
// multiple independent condition flags into a single authoritative OpState.
// All methods are safe for concurrent use.
type Supervisor struct {
	mu sync.Mutex

	// Flatten-level conditions
	riskHalted bool
	rlCritical bool
	feedDead   bool

	// CancelOnly-level conditions
	bookStale     bool
	refreshFailed bool

	// Rate-limit degraded
	rlDegraded bool
	rlCaution  bool

	// Recovery (time-limited)
	recoveryUntil time.Time

	// External halt + recovery deadlines (e.g. shock-chain cascade).
	// V30: independent of the risk manager's halt/resume lifecycle so that
	// RiskResume (emitted when e.g. a flatten clears drawdown) cannot
	// prematurely end an in-progress shock-chain pause. Both fields are
	// absolute deadlines; stateLocked() checks them before risk state.
	externalHaltUntil     time.Time
	externalRecoveryUntil time.Time

	// Last-observed external phase, for edge-detection logging in
	// CheckExternalTransitions(). Transitions here drive the "shock chain
	// pause expired" log events the monitor parses.
	lastExtPhase externalPhase

	// Per-coin halts (from executor reconciliation)
	coinHalts map[string]string // coin → reason
}

// NewSupervisor creates a Supervisor in OpNormal state.
func NewSupervisor() *Supervisor {
	return &Supervisor{
		coinHalts: make(map[string]string),
	}
}

// Signal processes a state transition signal. Each signal sets or clears
// the appropriate condition flags. Nil-receiver safe (no-op if s is nil).
func (s *Supervisor) Signal(sig Signal) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	switch sig {
	case SignalRiskHalt:
		s.riskHalted = true
		s.recoveryUntil = time.Time{} // clear recovery
	case SignalRiskResume:
		s.riskHalted = false
	case SignalRLCritical:
		s.rlCritical = true
		s.rlDegraded = false
		s.rlCaution = false
	case SignalRLDegraded:
		s.rlDegraded = true
		s.rlCritical = false
		s.rlCaution = false
	case SignalRLCaution:
		s.rlCaution = true
		s.rlDegraded = false
		s.rlCritical = false
	case SignalRLRecover:
		s.rlCritical = false
		s.rlDegraded = false
		s.rlCaution = false
	case SignalBookStale:
		s.bookStale = true
	case SignalFeedDead:
		s.feedDead = true
	case SignalFeedRecover:
		s.bookStale = false
		s.feedDead = false
	case SignalRefreshFailed:
		s.refreshFailed = true
	case SignalRefreshRecovered:
		s.refreshFailed = false
	}
}

// SignalRecovery clears the risk halt and enters time-limited recovery mode.
// Nil-receiver safe (no-op if s is nil).
func (s *Supervisor) SignalRecovery(duration time.Duration) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.riskHalted = false
	s.recoveryUntil = time.Now().Add(duration)
}

// SignalExternalHalt schedules an external halt (e.g. shock-chain cascade)
// for `haltDur`, followed by a recovery window of `recoveryDur`. Both are
// stored as absolute deadlines; state resolution consults them directly so
// no timer goroutine is required. The external deadlines are NOT cleared by
// SignalRiskResume or SignalRiskHalt — the caller that scheduled them owns
// their lifecycle. A second call overwrites both deadlines. Pass
// `recoveryDur = 0` to skip the recovery phase (resume directly to Normal
// after halt expires). Nil-receiver safe.
func (s *Supervisor) SignalExternalHalt(haltDur, recoveryDur time.Duration) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	if haltDur > 0 {
		s.externalHaltUntil = now.Add(haltDur)
	} else {
		s.externalHaltUntil = time.Time{}
	}
	if recoveryDur > 0 {
		// Recovery starts when halt ends, even when halt duration is 0.
		recoveryStart := now
		if haltDur > 0 {
			recoveryStart = s.externalHaltUntil
		}
		s.externalRecoveryUntil = recoveryStart.Add(recoveryDur)
	} else {
		s.externalRecoveryUntil = time.Time{}
	}
}

// ExternalHaltUntil returns the current external halt deadline (zero value
// if not scheduled). Useful for tests and diagnostics. Nil-receiver safe.
func (s *Supervisor) ExternalHaltUntil() time.Time {
	if s == nil {
		return time.Time{}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.externalHaltUntil
}

// ExternalRecoveryUntil returns the current external recovery deadline
// (zero value if not scheduled). Nil-receiver safe.
func (s *Supervisor) ExternalRecoveryUntil() time.Time {
	if s == nil {
		return time.Time{}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.externalRecoveryUntil
}

// CheckExternalTransitions detects edges in the external-halt / recovery
// state machine and emits the "shock chain pause expired" log events the
// monitor parser consumes. The supervisor uses timestamp deadlines (not
// goroutines) to transition between halt → recovery → normal, so without
// this poller nothing ever logs the transitions and operational monitors
// latch on the initial cascade event forever. Caller should invoke this
// periodically (a few Hz is sufficient). Nil-receiver safe.
//
// Transitions logged:
//   - halt → recovery: "shock chain pause expired, entering recovery"
//   - halt → normal   (no recovery window): "shock chain pause expired, resuming normal"
//   - recovery → normal: "shock chain pause expired, resuming normal"
//
// halt → halt (rescheduled via a second SignalExternalHalt) is not logged
// here; the caller is expected to log the rescheduling at the call site.
func (s *Supervisor) CheckExternalTransitions() {
	if s == nil {
		return
	}
	s.mu.Lock()
	now := time.Now()
	var cur externalPhase
	switch {
	case !s.externalHaltUntil.IsZero() && now.Before(s.externalHaltUntil):
		cur = extPhaseHalt
	case !s.externalRecoveryUntil.IsZero() && now.Before(s.externalRecoveryUntil):
		cur = extPhaseRecovery
	default:
		cur = extPhaseNormal
	}
	prev := s.lastExtPhase
	if cur == prev {
		s.mu.Unlock()
		return
	}
	s.lastExtPhase = cur
	recUntil := s.externalRecoveryUntil
	s.mu.Unlock()

	switch {
	case prev == extPhaseHalt && cur == extPhaseRecovery:
		recSec := time.Until(recUntil).Seconds()
		if recSec < 0 {
			recSec = 0
		}
		slog.Warn("shock chain pause expired, entering recovery",
			"recovery_sec", recSec,
		)
	case prev == extPhaseHalt && cur == extPhaseNormal,
		prev == extPhaseRecovery && cur == extPhaseNormal:
		slog.Info("shock chain pause expired, resuming normal")
	}
}

// SignalCoinHalt marks a specific coin as halted (e.g., from executor reconciliation).
// Nil-receiver safe (no-op if s is nil).
func (s *Supervisor) SignalCoinHalt(coin, reason string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.coinHalts[coin] = reason
}

// SignalCoinResume clears a per-coin halt.
// Nil-receiver safe (no-op if s is nil).
func (s *Supervisor) SignalCoinResume(coin string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.coinHalts, coin)
}

// IsCoinHalted returns true if the given coin is individually halted.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsCoinHalted(coin string) bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, halted := s.coinHalts[coin]
	return halted
}

// State resolves all active conditions into a single OpState by priority:
// Flatten > CancelOnly > Recovery > Degraded > Normal.
// Nil-receiver safe (returns OpNormal if s is nil).
func (s *Supervisor) State() OpState {
	if s == nil {
		return OpNormal
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stateLocked()
}

// stateLocked resolves state without acquiring the lock. Caller must hold s.mu.
func (s *Supervisor) stateLocked() OpState {
	now := time.Now()

	// Priority 0: External halt (shock-chain). Outranks even RiskHalt so that
	// a RiskResume racing with the external pause cannot prematurely clear it.
	if !s.externalHaltUntil.IsZero() && now.Before(s.externalHaltUntil) {
		return OpFlatten
	}

	// Priority 1: Flatten
	if s.riskHalted || s.feedDead || s.rlCritical {
		return OpFlatten
	}

	// Priority 2: CancelOnly
	if s.bookStale || s.refreshFailed {
		return OpCancelOnly
	}

	// Priority 3: Recovery (time-limited) — external recovery window takes
	// precedence over the risk-manager recovery deadline so the shock-chain
	// recovery window isn't truncated by a shorter risk-manager recovery.
	if !s.externalRecoveryUntil.IsZero() && now.Before(s.externalRecoveryUntil) {
		return OpRecovery
	}
	if !s.recoveryUntil.IsZero() && now.Before(s.recoveryUntil) {
		return OpRecovery
	}

	// Priority 4: Degraded
	if s.rlDegraded || s.rlCaution {
		return OpDegraded
	}

	// Priority 5: Normal
	return OpNormal
}

// CanQuote returns true if the system is in a state that allows quoting
// (Normal, Degraded, or Recovery).
// Nil-receiver safe (returns true if s is nil).
func (s *Supervisor) CanQuote() bool {
	if s == nil {
		return true
	}
	st := s.State()
	return st == OpNormal || st == OpDegraded || st == OpRecovery
}

// IsDegraded returns true if the current state is OpDegraded.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsDegraded() bool {
	if s == nil {
		return false
	}
	return s.State() == OpDegraded
}

// IsRecovery returns true if the current state is OpRecovery.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRecovery() bool {
	if s == nil {
		return false
	}
	return s.State() == OpRecovery
}

// IsRiskHalted returns true if the risk manager has halted the system.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRiskHalted() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.riskHalted
}

// IsRefreshFailed returns true if the system is in fail-closed state.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRefreshFailed() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.refreshFailed
}

// IsRLCritical returns true if the rate limit circuit breaker is in Critical.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRLCritical() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rlCritical
}

// IsRLDegraded returns true if the rate limit circuit breaker is in Degraded
// (not Caution, not Critical). Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRLDegraded() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rlDegraded
}

// IsRLCaution returns true if the rate limit circuit breaker is in Caution
// (not Degraded, not Critical). Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsRLCaution() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rlCaution
}

// RLTier returns the current rate-limit tier. Nil-receiver safe (returns RLTierNormal).
func (s *Supervisor) RLTier() RLTier {
	if s == nil {
		return RLTierNormal
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rlCritical {
		return RLTierCritical
	}
	if s.rlDegraded {
		return RLTierDegraded
	}
	if s.rlCaution {
		return RLTierCaution
	}
	return RLTierNormal
}

// IsBookStale returns true if book data is stale.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsBookStale() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.bookStale
}

// RecoveryUntil returns the time when recovery mode expires.
// Returns zero time if not in recovery.
// Nil-receiver safe (returns zero time if s is nil).
func (s *Supervisor) RecoveryUntil() time.Time {
	if s == nil {
		return time.Time{}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.recoveryUntil
}

// IsFeedDead returns true if the feed death condition is active.
// Nil-receiver safe (returns false if s is nil).
func (s *Supervisor) IsFeedDead() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.feedDead
}
