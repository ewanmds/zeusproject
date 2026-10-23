package quoting

import (
	"testing"
	"time"
)

func TestSupervisorStartsNormal(t *testing.T) {
	s := NewSupervisor()
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true for new supervisor")
	}
	if s.IsDegraded() {
		t.Error("expected IsDegraded=false for new supervisor")
	}
	if s.IsRecovery() {
		t.Error("expected IsRecovery=false for new supervisor")
	}
}

func TestSupervisorRiskHaltTransition(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRiskHalt)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after RiskHalt, got %s", s.State())
	}
	if s.CanQuote() {
		t.Error("expected CanQuote=false after RiskHalt")
	}
}

func TestSupervisorRiskResumeTransition(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRiskHalt)
	s.Signal(SignalRiskResume)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after RiskResume, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true after RiskResume")
	}
}

func TestSupervisorStaleBookDegrades(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalBookStale)
	if s.State() != OpCancelOnly {
		t.Errorf("expected OpCancelOnly after BookStale, got %s", s.State())
	}
	if s.CanQuote() {
		t.Error("expected CanQuote=false after BookStale")
	}
}

func TestSupervisorFeedDeadFlattens(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalBookStale)
	s.Signal(SignalFeedDead)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after FeedDead, got %s", s.State())
	}
	if s.CanQuote() {
		t.Error("expected CanQuote=false after FeedDead")
	}
}

func TestSupervisorFeedRecovery(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalBookStale)
	if s.State() != OpCancelOnly {
		t.Errorf("expected OpCancelOnly after BookStale, got %s", s.State())
	}
	s.Signal(SignalFeedRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after FeedRecover, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true after FeedRecover")
	}
}

func TestSupervisorRateLimitCriticalFlattens(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLCritical)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after RLCritical, got %s", s.State())
	}
	if s.CanQuote() {
		t.Error("expected CanQuote=false after RLCritical")
	}
}

func TestSupervisorRateLimitRecovery(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLCritical)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after RLCritical, got %s", s.State())
	}
	s.Signal(SignalRLRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after RLRecover, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true after RLRecover")
	}
}

func TestSupervisorDegradedState(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLDegraded)
	if s.State() != OpDegraded {
		t.Errorf("expected OpDegraded after RLDegraded, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true in Degraded state")
	}
	if !s.IsDegraded() {
		t.Error("expected IsDegraded=true in Degraded state")
	}
	if s.IsRecovery() {
		t.Error("expected IsRecovery=false in Degraded state")
	}
}

func TestSupervisorFailClosedTransition(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRefreshFailed)
	if s.State() != OpCancelOnly {
		t.Errorf("expected OpCancelOnly after RefreshFailed, got %s", s.State())
	}
	if s.CanQuote() {
		t.Error("expected CanQuote=false after RefreshFailed")
	}
}

func TestSupervisorFailClosedRecovery(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRefreshFailed)
	if s.State() != OpCancelOnly {
		t.Errorf("expected OpCancelOnly after RefreshFailed, got %s", s.State())
	}
	s.Signal(SignalRefreshRecovered)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after RefreshRecovered, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true after RefreshRecovered")
	}
}

func TestSupervisorHigherPrioritySignalWins(t *testing.T) {
	s := NewSupervisor()

	// RLDegraded → OpDegraded
	s.Signal(SignalRLDegraded)
	if s.State() != OpDegraded {
		t.Errorf("expected OpDegraded, got %s", s.State())
	}

	// RiskHalt overrides → OpFlatten
	s.Signal(SignalRiskHalt)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten (higher priority), got %s", s.State())
	}

	// RiskResume clears risk halt, but RL degraded still active → OpDegraded
	s.Signal(SignalRiskResume)
	if s.State() != OpDegraded {
		t.Errorf("expected OpDegraded (RL still active), got %s", s.State())
	}

	// RLRecover clears degraded → OpNormal
	s.Signal(SignalRLRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after all cleared, got %s", s.State())
	}
}

func TestSupervisorRecoveryMode(t *testing.T) {
	s := NewSupervisor()

	// Must be risk-halted first for recovery to make sense
	s.Signal(SignalRiskHalt)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after RiskHalt, got %s", s.State())
	}

	// SignalRecovery clears risk halt and sets time-limited recovery
	s.SignalRecovery(30 * time.Second)
	if s.State() != OpRecovery {
		t.Errorf("expected OpRecovery, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Error("expected CanQuote=true in Recovery")
	}
	if !s.IsRecovery() {
		t.Error("expected IsRecovery=true in Recovery")
	}
	if s.IsDegraded() {
		t.Error("expected IsDegraded=false in Recovery")
	}
}

func TestSupervisorRecoveryExpires(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRiskHalt)

	// Set a very short recovery so it expires immediately
	s.SignalRecovery(1 * time.Millisecond)
	time.Sleep(5 * time.Millisecond)

	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after recovery expired, got %s", s.State())
	}
	if s.IsRecovery() {
		t.Error("expected IsRecovery=false after recovery expired")
	}
}

func TestSupervisorExecHaltPerCoin(t *testing.T) {
	s := NewSupervisor()

	// Halt CL
	s.SignalCoinHalt("CL", "reconciliation mismatch")
	if !s.IsCoinHalted("CL") {
		t.Error("expected CL to be halted")
	}
	if s.IsCoinHalted("BRENTOIL") {
		t.Error("expected BRENTOIL to NOT be halted")
	}

	// Per-coin halt should not affect global state
	if s.State() != OpNormal {
		t.Errorf("expected global state OpNormal, got %s", s.State())
	}

	// Resume CL
	s.SignalCoinResume("CL")
	if s.IsCoinHalted("CL") {
		t.Error("expected CL to be resumed")
	}
}

func TestSupervisorOpStateString(t *testing.T) {
	tests := []struct {
		state OpState
		want  string
	}{
		{OpNormal, "Normal"},
		{OpDegraded, "Degraded"},
		{OpRecovery, "Recovery"},
		{OpCancelOnly, "CancelOnly"},
		{OpFlatten, "Flatten"},
	}
	for _, tt := range tests {
		if got := tt.state.String(); got != tt.want {
			t.Errorf("OpState(%d).String() = %q, want %q", tt.state, got, tt.want)
		}
	}
}

func TestSupervisorRLDegradedThenCritical(t *testing.T) {
	s := NewSupervisor()

	// RLDegraded → OpDegraded
	s.Signal(SignalRLDegraded)
	if s.State() != OpDegraded {
		t.Errorf("expected OpDegraded, got %s", s.State())
	}

	// RLCritical should escalate to OpFlatten and clear degraded
	s.Signal(SignalRLCritical)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after RLCritical, got %s", s.State())
	}

	// RLRecover clears both
	s.Signal(SignalRLRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after RLRecover, got %s", s.State())
	}
}

func TestSupervisorRiskHaltClearsRecovery(t *testing.T) {
	s := NewSupervisor()

	// Enter recovery
	s.Signal(SignalRiskHalt)
	s.SignalRecovery(30 * time.Second)
	if s.State() != OpRecovery {
		t.Errorf("expected OpRecovery, got %s", s.State())
	}

	// New risk halt should clear recovery
	s.Signal(SignalRiskHalt)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after new RiskHalt, got %s", s.State())
	}
	if s.IsRecovery() {
		t.Error("expected IsRecovery=false after RiskHalt clears recovery")
	}
}

func TestSupervisorMultipleFeedSignals(t *testing.T) {
	s := NewSupervisor()

	// FeedDead without BookStale first should still flatten
	s.Signal(SignalFeedDead)
	if s.State() != OpFlatten {
		t.Errorf("expected OpFlatten after FeedDead, got %s", s.State())
	}

	// FeedRecover should clear both feedDead and bookStale
	s.Signal(SignalFeedRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after FeedRecover, got %s", s.State())
	}
}

func TestSupervisorRecoveryUntil(t *testing.T) {
	s := NewSupervisor()

	// No recovery — returns zero time.
	if !s.RecoveryUntil().IsZero() {
		t.Error("expected zero RecoveryUntil before any recovery signal")
	}

	// Signal recovery with 30-second duration.
	s.SignalRecovery(30 * time.Second)
	ru := s.RecoveryUntil()
	if ru.IsZero() {
		t.Fatal("expected non-zero RecoveryUntil after SignalRecovery")
	}
	if time.Until(ru) < 29*time.Second || time.Until(ru) > 31*time.Second {
		t.Errorf("RecoveryUntil should be ~30s in the future, got %v", time.Until(ru))
	}

	// Risk halt clears recovery.
	s.Signal(SignalRiskHalt)
	if !s.RecoveryUntil().IsZero() {
		t.Error("expected zero RecoveryUntil after RiskHalt clears recovery")
	}
}

func TestSupervisorRecoveryUntilNilSafe(t *testing.T) {
	var s *Supervisor
	if !s.RecoveryUntil().IsZero() {
		t.Error("nil supervisor RecoveryUntil should return zero time")
	}
}

func TestSupervisorRLCautionState(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLCaution)
	if s.State() != OpDegraded {
		t.Errorf("expected OpDegraded after RLCaution, got %v", s.State())
	}
	if !s.IsRLCaution() {
		t.Error("IsRLCaution should be true")
	}
	if s.IsRLDegraded() {
		t.Error("IsRLDegraded should be false when only caution is active")
	}
}

func TestSupervisorRLCautionThenDegraded(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLCaution)
	s.Signal(SignalRLDegraded)
	if !s.IsRLDegraded() {
		t.Error("IsRLDegraded should be true after degraded signal")
	}
	if s.IsRLCaution() {
		t.Error("IsRLCaution should be false after degraded replaces caution")
	}
}

func TestSupervisorRLRecoverClearsCaution(t *testing.T) {
	s := NewSupervisor()
	s.Signal(SignalRLCaution)
	s.Signal(SignalRLRecover)
	if s.State() != OpNormal {
		t.Errorf("expected OpNormal after RLRecover, got %v", s.State())
	}
}

func TestSupervisorRLTier(t *testing.T) {
	s := NewSupervisor()
	if s.RLTier() != RLTierNormal {
		t.Errorf("expected RLTierNormal, got %v", s.RLTier())
	}
	s.Signal(SignalRLCaution)
	if s.RLTier() != RLTierCaution {
		t.Errorf("expected RLTierCaution, got %v", s.RLTier())
	}
	s.Signal(SignalRLDegraded)
	if s.RLTier() != RLTierDegraded {
		t.Errorf("expected RLTierDegraded, got %v", s.RLTier())
	}
	s.Signal(SignalRLCritical)
	if s.RLTier() != RLTierCritical {
		t.Errorf("expected RLTierCritical, got %v", s.RLTier())
	}
	s.Signal(SignalRLRecover)
	if s.RLTier() != RLTierNormal {
		t.Errorf("expected RLTierNormal after recover, got %v", s.RLTier())
	}
}

func TestSupervisorRLTierNilSafe(t *testing.T) {
	var s *Supervisor
	if s.RLTier() != RLTierNormal {
		t.Error("nil supervisor RLTier should return RLTierNormal")
	}
	if s.IsRLCaution() {
		t.Error("nil supervisor IsRLCaution should return false")
	}
	if s.IsRLDegraded() {
		t.Error("nil supervisor IsRLDegraded should return false")
	}
}

func TestSupervisorRLTierString(t *testing.T) {
	tests := []struct {
		tier RLTier
		want string
	}{
		{RLTierNormal, "normal"},
		{RLTierCaution, "caution"},
		{RLTierDegraded, "degraded"},
		{RLTierCritical, "critical"},
	}
	for _, tt := range tests {
		if got := tt.tier.String(); got != tt.want {
			t.Errorf("RLTier(%d).String() = %q, want %q", tt.tier, got, tt.want)
		}
	}
}

func TestSupervisorCoinHaltResumIdempotent(t *testing.T) {
	s := NewSupervisor()

	// Resuming a coin that was never halted is a no-op
	s.SignalCoinResume("CL")
	if s.IsCoinHalted("CL") {
		t.Error("expected CL not halted after no-op resume")
	}

	// Halting same coin twice should be fine
	s.SignalCoinHalt("CL", "reason1")
	s.SignalCoinHalt("CL", "reason2")
	if !s.IsCoinHalted("CL") {
		t.Error("expected CL halted after double halt")
	}

	// Single resume should clear it
	s.SignalCoinResume("CL")
	if s.IsCoinHalted("CL") {
		t.Error("expected CL not halted after resume")
	}
}

// V30: external halt deadline outranks RiskHalt/RiskResume so shock-chain
// pauses are not cleared by a concurrent drawdown-recovery risk resume.

func TestSupervisorExternalHaltBasics(t *testing.T) {
	s := NewSupervisor()
	s.SignalExternalHalt(50*time.Millisecond, 100*time.Millisecond)

	if s.State() != OpFlatten {
		t.Fatalf("expected OpFlatten during external halt, got %s", s.State())
	}
	if s.CanQuote() {
		t.Fatalf("expected CanQuote=false during external halt")
	}

	time.Sleep(80 * time.Millisecond) // past halt, into recovery
	if s.State() != OpRecovery {
		t.Fatalf("expected OpRecovery after halt expired, got %s", s.State())
	}
	if !s.CanQuote() {
		t.Fatalf("expected CanQuote=true during recovery")
	}

	time.Sleep(100 * time.Millisecond) // past recovery
	if s.State() != OpNormal {
		t.Fatalf("expected OpNormal after recovery expired, got %s", s.State())
	}
}

func TestSupervisorExternalHaltNotClearedByRiskResume(t *testing.T) {
	s := NewSupervisor()
	s.SignalExternalHalt(200*time.Millisecond, 0)

	if s.State() != OpFlatten {
		t.Fatalf("expected OpFlatten after external halt, got %s", s.State())
	}

	// Simulate the race: risk manager emits RiskResume during the pause.
	// External halt must persist.
	s.Signal(SignalRiskResume)
	if s.State() != OpFlatten {
		t.Fatalf("RiskResume must NOT clear external halt; got %s", s.State())
	}

	// A full RiskHalt can coexist with external halt (both say "flatten"),
	// and RiskResume still must not clear the external halt.
	s.Signal(SignalRiskHalt)
	s.Signal(SignalRiskResume)
	if s.State() != OpFlatten {
		t.Fatalf("after RiskHalt+RiskResume external halt must persist; got %s", s.State())
	}

	time.Sleep(220 * time.Millisecond)
	if s.State() != OpNormal {
		t.Fatalf("after external halt expired, expected OpNormal; got %s", s.State())
	}
}

func TestSupervisorExternalHaltZeroDurationDisables(t *testing.T) {
	s := NewSupervisor()
	// Set with zero values should clear external deadlines.
	s.SignalExternalHalt(0, 0)
	if !s.ExternalHaltUntil().IsZero() {
		t.Fatalf("expected zero ExternalHaltUntil when haltDur=0")
	}
	if !s.ExternalRecoveryUntil().IsZero() {
		t.Fatalf("expected zero ExternalRecoveryUntil when recoveryDur=0")
	}
	if s.State() != OpNormal {
		t.Fatalf("expected OpNormal with zero deadlines, got %s", s.State())
	}
}

func TestSupervisorExternalHaltOverwritesPrior(t *testing.T) {
	s := NewSupervisor()
	s.SignalExternalHalt(1*time.Hour, 1*time.Hour)
	first := s.ExternalHaltUntil()

	// A second call overwrites with a much shorter halt.
	s.SignalExternalHalt(50*time.Millisecond, 50*time.Millisecond)
	second := s.ExternalHaltUntil()

	if !second.Before(first) {
		t.Fatalf("second SignalExternalHalt should overwrite to shorter deadline")
	}

	time.Sleep(150 * time.Millisecond)
	if s.State() != OpNormal {
		t.Fatalf("expected OpNormal after short deadlines elapsed, got %s", s.State())
	}
}

func TestSupervisorExternalHaltNilSafe(t *testing.T) {
	var s *Supervisor
	s.SignalExternalHalt(time.Second, time.Second) // must not panic
	if got := s.ExternalHaltUntil(); !got.IsZero() {
		t.Fatalf("nil ExternalHaltUntil must return zero, got %v", got)
	}
	if got := s.ExternalRecoveryUntil(); !got.IsZero() {
		t.Fatalf("nil ExternalRecoveryUntil must return zero, got %v", got)
	}
	s.CheckExternalTransitions() // must not panic
}

// TestSupervisorCheckExternalTransitions verifies that the phase tracker
// observes each external-halt phase in order. We can't easily assert log
// output without a log capture, but we can verify the internal lastExtPhase
// field advances correctly, which is the invariant the logs depend on.
func TestSupervisorCheckExternalTransitions(t *testing.T) {
	s := NewSupervisor()

	// Normal → Normal: no transition.
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseNormal {
		t.Fatalf("initial phase = %v, want extPhaseNormal", s.lastExtPhase)
	}

	// Schedule halt + recovery. After first poll, phase should be Halt.
	s.SignalExternalHalt(50*time.Millisecond, 50*time.Millisecond)
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseHalt {
		t.Fatalf("after SignalExternalHalt poll, phase = %v, want extPhaseHalt", s.lastExtPhase)
	}

	// Wait for halt to expire, poll should transition to Recovery.
	time.Sleep(70 * time.Millisecond)
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseRecovery {
		t.Fatalf("after halt expiry, phase = %v, want extPhaseRecovery", s.lastExtPhase)
	}

	// Wait for recovery to expire, poll should transition to Normal.
	time.Sleep(70 * time.Millisecond)
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseNormal {
		t.Fatalf("after recovery expiry, phase = %v, want extPhaseNormal", s.lastExtPhase)
	}
}

// TestSupervisorCheckExternalTransitions_NoRecoveryWindow verifies the
// halt→normal edge (when recovery duration is 0) is detected correctly.
func TestSupervisorCheckExternalTransitions_NoRecoveryWindow(t *testing.T) {
	s := NewSupervisor()
	s.SignalExternalHalt(50*time.Millisecond, 0)
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseHalt {
		t.Fatalf("phase = %v, want extPhaseHalt", s.lastExtPhase)
	}
	time.Sleep(70 * time.Millisecond)
	s.CheckExternalTransitions()
	if s.lastExtPhase != extPhaseNormal {
		t.Fatalf("after halt expiry (no recovery), phase = %v, want extPhaseNormal", s.lastExtPhase)
	}
}
