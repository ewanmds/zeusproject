package main

import (
	"fmt"
	"strings"
)

func renderEventsPanel(s *State, maxLines int) string {
	var b strings.Builder

	b.WriteString(styleSectionHeader.Render("Events") + "\n")

	events := s.Events.Items()
	if len(events) == 0 {
		b.WriteString(styleLabel.Render("  (no events yet)\n"))
		return b.String()
	}

	limit := len(events)
	if limit > maxLines {
		limit = maxLines
	}

	for _, e := range events[:limit] {
		ts := e.Time.Format("15:04:05")
		coinTag := ""
		if e.Coin != "" {
			coinTag = " " + e.Coin
		}

		var style func(strs ...string) string
		switch e.Type {
		case EventError:
			style = styleEventError.Render
		case EventWarn:
			style = styleEventWarn.Render
		case EventFill:
			switch e.Side {
			case "buy":
				style = styleEventFillBuy.Render
			case "sell":
				style = styleEventFillSell.Render
			default:
				style = styleEventFill.Render
			}
		case EventOptimizer:
			style = styleEventDim.Render
		case EventOptimizerDeadband:
			style = styleEventDim.Render
		case EventBurst:
			style = styleEventBurst.Render
		case EventRisk:
			style = styleEventRisk.Render
		case EventRecovery:
			style = styleEventRecovery.Render
		case EventTrendGuard:
			style = styleEventBurst.Render // orange — same as burst (transient state change)
		case EventFlowGate:
			style = styleEventBurst.Render // orange — same as burst (transient state change)
		case EventRegimeGate:
			style = styleEventBurst.Render // orange — same as burst (transient state change)
		case EventIdleFillBreaker:
			style = styleEventBurst.Render // orange — transient one-shot tighten
		case EventMarkoutPersistExit:
			style = styleEventBurst.Render // orange — exit accelerator firing (V32.2)
		case EventPriceShock:
			style = styleEventRisk.Render
		case EventReconMismatch:
			style = styleEventWarn.Render
		case EventSessionStopLoss:
			style = styleEventRisk.Render
		case EventShockChain:
			style = styleEventRisk.Render // cascade = treat as risk event (red)
		case EventPostMortem:
			style = styleEventFill.Render
		default:
			style = styleEventDim.Render
		}

		line := fmt.Sprintf("  %s%s %s", ts, coinTag, e.Summary)
		// Colorize the (net $X.XXXX) portion green when positive.
		if e.Type == EventFill && e.NetPnL > 0 {
			if idx := strings.Index(line, "(net "); idx != -1 {
				end := strings.Index(line[idx:], ")")
				if end != -1 {
					before := line[:idx]
					netPart := line[idx : idx+end+1]
					after := line[idx+end+1:]
					b.WriteString(style(before) + styleValuePositive.Render(netPart) + style(after) + "\n")
					continue
				}
			}
		}
		b.WriteString(style(line) + "\n")
	}

	return b.String()
}
