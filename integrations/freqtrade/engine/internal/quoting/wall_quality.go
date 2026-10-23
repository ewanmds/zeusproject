package quoting

import (
	"math"
	"sync"
	"time"
)

type wallQualitySide struct {
	score         float64
	seeded        bool
	disabledUntil time.Time
	// P7 hysteresis state.
	enabled      bool
	enabledSince time.Time
	pendingState string // "enable" or "disable"; empty = no pending transition
	pendingSince time.Time
}

// WallQualityTracker disables wall-fronting for a side after snapped fills
// produce poor downstream markouts.
type WallQualityTracker struct {
	alpha             float64
	minScore          float64
	disableFor        time.Duration
	hysteresis        time.Duration
	reenableThreshold float64
	sides             map[string]*wallQualitySide
	mu                sync.Mutex
}

// WallQualityTransition describes a state flip emitted from RecordMarkout so
// callers (which have coin context) can log it.
type WallQualityTransition struct {
	Side    string
	From    string // "enabled" or "disabled"
	To      string
	Score   float64
	HeldSec float64
}

// NewWallQualityTracker constructs a tracker. `hysteresis` and
// `reenableThresholdBps` are P7 hysteresis knobs; zero values preserve the
// pre-P7 immediate-flip behavior once `disableFor` expires. The threshold is
// given in markout-bps space and mapped to the internal [0,1] score space via
// the same formula used for observations: score = 0.5 + bps/10.
func NewWallQualityTracker(halfLife time.Duration, minScore float64, disableFor time.Duration, hysteresis time.Duration, reenableThresholdBps float64) *WallQualityTracker {
	hl := halfLife.Seconds()
	if hl <= 0 {
		hl = 300
	}
	if minScore <= 0 {
		minScore = 0.35
	}
	if disableFor <= 0 {
		disableFor = 120 * time.Second
	}
	if hysteresis < 0 {
		hysteresis = 0
	}
	if reenableThresholdBps < 0 {
		reenableThresholdBps = 0
	}
	reenableScore := 0.0
	if reenableThresholdBps > 0 {
		reenableScore = 0.5 + reenableThresholdBps/10.0
		if reenableScore > 1 {
			reenableScore = 1
		}
	}
	return &WallQualityTracker{
		alpha:             1 - math.Exp(-1.0/hl),
		minScore:          minScore,
		disableFor:        disableFor,
		hysteresis:        hysteresis,
		reenableThreshold: reenableScore,
		sides: map[string]*wallQualitySide{
			"buy":  {enabled: true},
			"sell": {enabled: true},
		},
	}
}

func (t *WallQualityTracker) SideEnabled(side string, now time.Time) bool {
	t.mu.Lock()
	defer t.mu.Unlock()

	s, ok := t.sides[side]
	if !ok {
		return true
	}
	if t.hysteresisActive() {
		return s.enabled
	}
	return s.disabledUntil.IsZero() || !now.Before(s.disabledUntil)
}

func (t *WallQualityTracker) hysteresisActive() bool {
	return t.hysteresis > 0 && t.reenableThreshold > 0
}

// RecordMarkout records a completed markout in bps. A 0 bps wall fill maps to
// a neutral score of 0.5; materially adverse markouts push the score below the
// configured floor and temporarily disable snapping on that side.
//
// When hysteresis is configured (hysteresis>0 and reenableThreshold>0), state
// transitions require the trigger condition to persist for `hysteresis`
// duration continuously; transient moves reset the pending timer. Returns a
// non-nil transition struct when the side actually flips.
func (t *WallQualityTracker) RecordMarkout(side string, markoutBps float64, now time.Time) (score float64, disabledUntil time.Time, disabled bool, transition *WallQualityTransition) {
	t.mu.Lock()
	defer t.mu.Unlock()

	s, ok := t.sides[side]
	if !ok {
		s = &wallQualitySide{enabled: true, enabledSince: now}
		t.sides[side] = s
	}

	observation := 0.5 + markoutBps/10.0
	if observation < 0 {
		observation = 0
	}
	if observation > 1 {
		observation = 1
	}
	if !s.seeded {
		s.score = observation
		s.seeded = true
	} else {
		s.score = s.score*(1-t.alpha) + observation*t.alpha
	}

	if !t.hysteresisActive() {
		if s.score < t.minScore {
			s.disabledUntil = now.Add(t.disableFor)
			disabled = true
		}
		return s.score, s.disabledUntil, disabled, nil
	}

	// Hysteresis-gated state machine.
	if s.enabledSince.IsZero() {
		s.enabledSince = now
	}

	if s.enabled {
		// Pending disable when score drops below minScore.
		if s.score < t.minScore {
			if s.pendingState != "disable" {
				s.pendingState = "disable"
				s.pendingSince = now
			} else if now.Sub(s.pendingSince) >= t.hysteresis {
				held := now.Sub(s.enabledSince).Seconds()
				s.enabled = false
				s.disabledUntil = now.Add(t.disableFor)
				s.pendingState = ""
				s.enabledSince = now
				disabled = true
				transition = &WallQualityTransition{Side: side, From: "enabled", To: "disabled", Score: s.score, HeldSec: held}
			}
		} else if s.pendingState == "disable" {
			s.pendingState = ""
		}
	} else {
		// Pending enable when score rises to the re-enable threshold and the
		// minimum disable window has passed.
		disableWindowElapsed := s.disabledUntil.IsZero() || !now.Before(s.disabledUntil)
		if s.score >= t.reenableThreshold && disableWindowElapsed {
			if s.pendingState != "enable" {
				s.pendingState = "enable"
				s.pendingSince = now
			} else if now.Sub(s.pendingSince) >= t.hysteresis {
				held := now.Sub(s.enabledSince).Seconds()
				s.enabled = true
				s.disabledUntil = time.Time{}
				s.pendingState = ""
				s.enabledSince = now
				transition = &WallQualityTransition{Side: side, From: "disabled", To: "enabled", Score: s.score, HeldSec: held}
			}
		} else if s.pendingState == "enable" {
			s.pendingState = ""
		}
		disabled = !s.enabled
	}
	return s.score, s.disabledUntil, disabled, transition
}

func (t *WallQualityTracker) Score(side string) float64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	if s, ok := t.sides[side]; ok && s.seeded {
		return s.score
	}
	return 0.5
}
