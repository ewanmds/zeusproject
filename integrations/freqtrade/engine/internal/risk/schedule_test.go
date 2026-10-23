package risk

import (
	"testing"
	"time"

	"hyperliquid-market-maker/internal/config"
)

func mustNY(t *testing.T) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		t.Fatalf("load NY: %v", err)
	}
	return loc
}

func TestTradingSchedule_BasicMatching(t *testing.T) {
	sched, err := NewTradingSchedule([]config.TradingScheduleWindowConfig{
		{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.3, Label: "cme_open"},
	})
	if err != nil {
		t.Fatalf("NewTradingSchedule: %v", err)
	}
	ny := mustNY(t)

	cases := []struct {
		name     string
		t        time.Time
		wantMult float64
		wantLbl  string
	}{
		{"before window", time.Date(2026, 4, 13, 8, 59, 0, 0, ny), 1.0, ""},
		{"at start", time.Date(2026, 4, 13, 9, 0, 0, 0, ny), 0.3, "cme_open"},
		{"mid window", time.Date(2026, 4, 13, 9, 30, 0, 0, ny), 0.3, "cme_open"},
		{"at end (exclusive)", time.Date(2026, 4, 13, 10, 0, 0, 0, ny), 1.0, ""},
		{"after window", time.Date(2026, 4, 13, 10, 5, 0, 0, ny), 1.0, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m, l := sched.NotionalMultiplier(tc.t)
			if m != tc.wantMult || l != tc.wantLbl {
				t.Errorf("got (%g, %q), want (%g, %q)", m, l, tc.wantMult, tc.wantLbl)
			}
		})
	}
}

func TestTradingSchedule_TimezoneHandling(t *testing.T) {
	sched, err := NewTradingSchedule([]config.TradingScheduleWindowConfig{
		{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "ny_morning"},
	})
	if err != nil {
		t.Fatalf("NewTradingSchedule: %v", err)
	}
	ny := mustNY(t)
	// 09:30 NY == 13:30 or 14:30 UTC depending on DST; convert via Location.
	nyTime := time.Date(2026, 4, 13, 9, 30, 0, 0, ny)
	utcTime := nyTime.UTC()
	mult, label := sched.NotionalMultiplier(utcTime)
	if mult != 0.5 || label != "ny_morning" {
		t.Errorf("UTC-passed time should still match NY window: got (%g, %q)", mult, label)
	}
}

func TestTradingSchedule_OverlappingMinWins(t *testing.T) {
	sched, err := NewTradingSchedule([]config.TradingScheduleWindowConfig{
		{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.3, Label: "loose"},
		{Start: "09:15", End: "09:45", Tz: "America/New_York", NotionalMultiplier: 0.2, Label: "strict"},
	})
	if err != nil {
		t.Fatalf("NewTradingSchedule: %v", err)
	}
	ny := mustNY(t)
	at := time.Date(2026, 4, 13, 9, 30, 0, 0, ny)
	mult, label := sched.NotionalMultiplier(at)
	if mult != 0.2 || label != "strict" {
		t.Errorf("overlap should pick min: got (%g, %q), want (0.2, strict)", mult, label)
	}

	// Outside the stricter inner window, the looser outer one should apply.
	at2 := time.Date(2026, 4, 13, 9, 50, 0, 0, ny)
	mult2, label2 := sched.NotionalMultiplier(at2)
	if mult2 != 0.3 || label2 != "loose" {
		t.Errorf("outside inner, outer applies: got (%g, %q), want (0.3, loose)", mult2, label2)
	}
}

func TestTradingSchedule_LabelOnNoMatch(t *testing.T) {
	sched, err := NewTradingSchedule([]config.TradingScheduleWindowConfig{
		{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.3, Label: "cme_open"},
	})
	if err != nil {
		t.Fatalf("NewTradingSchedule: %v", err)
	}
	ny := mustNY(t)
	mult, label := sched.NotionalMultiplier(time.Date(2026, 4, 13, 14, 0, 0, 0, ny))
	if mult != 1.0 || label != "" {
		t.Errorf("no-match should be (1.0, \"\"): got (%g, %q)", mult, label)
	}
}

func TestTradingSchedule_InvalidConfig(t *testing.T) {
	cases := []struct {
		name string
		cfg  config.TradingScheduleWindowConfig
	}{
		{"bad HH:MM start", config.TradingScheduleWindowConfig{Start: "9-00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "x"}},
		{"hour out of range", config.TradingScheduleWindowConfig{Start: "25:00", End: "26:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "x"}},
		{"minute out of range", config.TradingScheduleWindowConfig{Start: "09:60", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "x"}},
		{"bad tz", config.TradingScheduleWindowConfig{Start: "09:00", End: "10:00", Tz: "Nowhere/Nothing", NotionalMultiplier: 0.5, Label: "x"}},
		{"empty tz", config.TradingScheduleWindowConfig{Start: "09:00", End: "10:00", Tz: "", NotionalMultiplier: 0.5, Label: "x"}},
		{"mult > 1", config.TradingScheduleWindowConfig{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: 1.5, Label: "x"}},
		{"mult < 0", config.TradingScheduleWindowConfig{Start: "09:00", End: "10:00", Tz: "America/New_York", NotionalMultiplier: -0.1, Label: "x"}},
		{"start == end", config.TradingScheduleWindowConfig{Start: "09:00", End: "09:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "x"}},
		{"start > end", config.TradingScheduleWindowConfig{Start: "10:00", End: "09:00", Tz: "America/New_York", NotionalMultiplier: 0.5, Label: "x"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewTradingSchedule([]config.TradingScheduleWindowConfig{tc.cfg}); err == nil {
				t.Errorf("expected error for %s, got nil", tc.name)
			}
		})
	}
}

func TestTradingSchedule_NilReceiver(t *testing.T) {
	var s *TradingSchedule
	mult, label := s.NotionalMultiplier(time.Now())
	if mult != 1.0 || label != "" {
		t.Errorf("nil receiver should return (1.0, \"\"): got (%g, %q)", mult, label)
	}
}

func TestTradingSchedule_Empty(t *testing.T) {
	sched, err := NewTradingSchedule(nil)
	if err != nil {
		t.Fatalf("NewTradingSchedule(nil): %v", err)
	}
	mult, label := sched.NotionalMultiplier(time.Now())
	if mult != 1.0 || label != "" {
		t.Errorf("empty schedule should return (1.0, \"\"): got (%g, %q)", mult, label)
	}
}
