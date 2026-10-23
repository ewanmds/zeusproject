package risk

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"hyperliquid-market-maker/internal/config"
)

type scheduleWindow struct {
	startMin int
	endMin   int
	mult     float64
	label    string
	loc      *time.Location
}

// TradingSchedule evaluates a list of local-time windows and returns a
// [0, 1] notional multiplier. Unmatched windows → 1.0. Overlapping matches
// take the minimum multiplier.
type TradingSchedule struct {
	windows []scheduleWindow
}

// NewTradingSchedule builds a TradingSchedule from config, validating each
// window. Returns a non-nil *TradingSchedule on success, or an error on the
// first invalid entry.
func NewTradingSchedule(cfg []config.TradingScheduleWindowConfig) (*TradingSchedule, error) {
	windows := make([]scheduleWindow, 0, len(cfg))
	for i, w := range cfg {
		startMin, err := parseHHMM(w.Start)
		if err != nil {
			return nil, fmt.Errorf("trading_schedule[%d]: start %q: %w", i, w.Start, err)
		}
		endMin, err := parseHHMM(w.End)
		if err != nil {
			return nil, fmt.Errorf("trading_schedule[%d]: end %q: %w", i, w.End, err)
		}
		if startMin >= endMin {
			return nil, fmt.Errorf("trading_schedule[%d]: start %q must be before end %q (same calendar day)", i, w.Start, w.End)
		}
		if w.NotionalMultiplier < 0 || w.NotionalMultiplier > 1 {
			return nil, fmt.Errorf("trading_schedule[%d]: notional_multiplier must be in [0, 1], got %g", i, w.NotionalMultiplier)
		}
		if w.Tz == "" {
			return nil, fmt.Errorf("trading_schedule[%d]: tz must be set (IANA zone, e.g. America/New_York)", i)
		}
		loc, err := time.LoadLocation(w.Tz)
		if err != nil {
			return nil, fmt.Errorf("trading_schedule[%d]: tz %q: %w", i, w.Tz, err)
		}
		windows = append(windows, scheduleWindow{
			startMin: startMin,
			endMin:   endMin,
			mult:     w.NotionalMultiplier,
			label:    w.Label,
			loc:      loc,
		})
	}
	return &TradingSchedule{windows: windows}, nil
}

// NotionalMultiplier returns the most conservative (minimum) multiplier from
// all windows that match now, plus the label of that window. When no window
// matches (or s is nil), returns (1.0, "").
func (s *TradingSchedule) NotionalMultiplier(now time.Time) (float64, string) {
	if s == nil || len(s.windows) == 0 {
		return 1.0, ""
	}
	result := 1.0
	resultLabel := ""
	for _, w := range s.windows {
		local := now.In(w.loc)
		minsNow := local.Hour()*60 + local.Minute()
		if minsNow >= w.startMin && minsNow < w.endMin {
			if w.mult < result {
				result = w.mult
				resultLabel = w.label
			}
		}
	}
	return result, resultLabel
}

// Windows returns a read-only summary of configured windows for startup logging.
func (s *TradingSchedule) Windows() []string {
	if s == nil {
		return nil
	}
	out := make([]string, 0, len(s.windows))
	for _, w := range s.windows {
		out = append(out, fmt.Sprintf("%s %s-%s %s x%.2f",
			w.label,
			formatHHMM(w.startMin),
			formatHHMM(w.endMin),
			w.loc.String(),
			w.mult,
		))
	}
	return out
}

func parseHHMM(s string) (int, error) {
	parts := strings.Split(s, ":")
	if len(parts) != 2 {
		return 0, fmt.Errorf("must be HH:MM")
	}
	h, err := strconv.Atoi(parts[0])
	if err != nil || h < 0 || h > 23 {
		return 0, fmt.Errorf("hour must be 00-23")
	}
	m, err := strconv.Atoi(parts[1])
	if err != nil || m < 0 || m > 59 {
		return 0, fmt.Errorf("minute must be 00-59")
	}
	return h*60 + m, nil
}

func formatHHMM(mins int) string {
	return fmt.Sprintf("%02d:%02d", mins/60, mins%60)
}
