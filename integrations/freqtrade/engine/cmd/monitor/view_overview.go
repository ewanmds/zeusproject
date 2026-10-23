package main

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"charm.land/lipgloss/v2"
)

func renderOverview(s *State, width int) string {
	if width < 40 {
		return ""
	}

	var b strings.Builder

	// Title bar
	title := styleHeader.Render("HyperMM Monitor")
	fileInfo := s.Session.LogFile
	modeIndicator := renderModeIndicator(s.Session.Mode)
	gap := width - lipgloss.Width(title) - lipgloss.Width(fileInfo) - lipgloss.Width(modeIndicator) - 4
	if gap < 1 {
		gap = 1
	}
	b.WriteString(title + strings.Repeat(" ", gap) + fileInfo + "  " + modeIndicator + "\n")
	b.WriteString(strings.Repeat("─", width) + "\n")

	// Money block — use live MTM-adjusted equity so the display tracks book moves
	// between STATUS log lines instead of stalling for up to status_interval_sec.
	liveEquity := s.LiveEquity()
	equityStr := fmt.Sprintf("$%.2f", liveEquity)
	// Net change from start equity.
	deltaStr := ""
	if s.Portfolio.StartEquity > 0 && liveEquity > 0 {
		delta := liveEquity - s.Portfolio.StartEquity
		deltaStyle := styleValueNeutral
		sign := ""
		if delta > 0 {
			sign = "+"
			deltaStyle = styleValuePositive
		} else if delta < 0 {
			deltaStyle = styleValueNegative
		}
		deltaStr = " " + deltaStyle.Render(fmt.Sprintf("(%s$%.2f)", sign, delta))
	}
	hwStr := s.Portfolio.HighWater
	if hwStr == "" {
		hwStr = "—"
	} else {
		hwStr = "$" + hwStr
	}
	lwStr := "—"
	if s.Portfolio.LowWater > 0 {
		lwStr = fmt.Sprintf("$%.2f", s.Portfolio.LowWater)
	}
	floorStr := fmt.Sprintf("$%.0f", s.Portfolio.EquityFloor)

	startEqStr := "—"
	if s.Portfolio.StartEquity > 0 {
		startEqStr = fmt.Sprintf("$%.2f", s.Portfolio.StartEquity)
	}

	rangeStr := fmt.Sprintf("%s – %s", lwStr, hwStr)

	b.WriteString(fmt.Sprintf("  %s %s%s  %s %s       %s %s    %s %s\n",
		styleLabel.Render("EQUITY"), styleValueNeutral.Render(equityStr), deltaStr,
		styleLabel.Render("START"), styleValueNeutral.Render(startEqStr),
		styleLabel.Render("RANGE"), styleValueNeutral.Render(rangeStr),
		styleLabel.Render("FLOOR"), styleValueNeutral.Render(floorStr),
	))

	pnlStyle := styleValueNeutral
	pnl := s.Portfolio.SessionPnL
	if pnl != "" && pnl != "0.0000" {
		if strings.HasPrefix(pnl, "-") {
			pnlStyle = styleValueNegative
		} else {
			pnlStyle = styleValuePositive
			pnl = "+" + pnl
		}
	}
	pnlBps := s.Portfolio.SessionPnLBps
	if pnlBps == "" {
		pnlBps = "0.0"
	}
	pnlBpsDisplay := pnlBps + "bps"
	sessionBpsVal, _ := strconv.ParseFloat(pnlBps, 64)
	if s.Portfolio.WindowPnLBps != 0 && sessionBpsVal != 0 {
		pnlBpsDisplay = fmt.Sprintf("%sbps (60m: %+.1f)", pnlBps, s.Portfolio.WindowPnLBps)
	}

	drawdownPct := "0.0"
	if liveEquity > 0 && s.Portfolio.HighWater != "" {
		hw, _ := strconv.ParseFloat(s.Portfolio.HighWater, 64)
		if hw > 0 {
			dd := (1.0 - liveEquity/hw) * 100
			if dd < 0 {
				dd = 0
			}
			drawdownPct = fmt.Sprintf("%.1f", dd)
		}
	}

	ddStyle := styleValueNeutral
	ddVal, _ := strconv.ParseFloat(drawdownPct, 64)
	if ddVal > s.Portfolio.MaxDrawdownPct*0.5 {
		ddStyle = styleValueWarn
	}

	b.WriteString(fmt.Sprintf("  %s %s (%s)          %s %s / %.0f%%\n",
		styleLabel.Render("SESSION"), pnlStyle.Render(pnl), pnlBpsDisplay,
		styleLabel.Render("DRAWDOWN"), ddStyle.Render(drawdownPct+"%"), s.Portfolio.MaxDrawdownPct,
	))

	fees := s.Portfolio.TotalAllFees
	if fees == "" {
		fees = "0.0000"
	}
	exchFees := s.Portfolio.TotalExchangeFees
	if exchFees == "" {
		exchFees = "0.0000"
	}
	builderFees := s.Portfolio.TotalBuilderFees
	if builderFees == "" {
		builderFees = "0.0000"
	}
	b.WriteString(fmt.Sprintf("  %s %s (exch: %s  builder: %s)\n",
		styleLabel.Render("FEES"), styleValueNeutral.Render(fees), exchFees, builderFees,
	))

	b.WriteString(strings.Repeat("─", width) + "\n")

	// Activity block
	totalExposure := 0.0
	activeCount := 0
	for _, coin := range s.AssetOrder {
		a := s.Assets[coin]
		totalExposure += a.Position.Notional
		if a.OrderCount > 0 || a.Position.Notional > 0 {
			activeCount++
		}
	}
	maxExp := s.Portfolio.MaxExposure
	if maxExp == 0 {
		maxExp = totalExposure
	}

	totalVolume := s.Fills.BuyNotional + s.Fills.SellNotional
	b.WriteString(fmt.Sprintf("  %s %.1f / %.0f max    %s %d (%d buy, %d sell)    %s $%.0f\n",
		styleLabel.Render("EXPOSURE"), totalExposure, maxExp,
		styleLabel.Render("FILLS"), s.Fills.Total, s.Fills.Buy, s.Fills.Sell,
		styleLabel.Render("VOLUME"), totalVolume,
	))

	totalAssets := len(s.Assets)
	if len(s.Session.Assets) > totalAssets {
		totalAssets = len(s.Session.Assets)
	}

	markoutStr := "—"
	var markoutSum float64
	var markoutCount int
	for _, coin := range s.AssetOrder {
		a := s.Assets[coin]
		for _, m := range a.Markouts.Items() {
			v, err := strconv.ParseFloat(m.T5s, 64)
			if err == nil {
				markoutSum += v
				markoutCount++
			}
			if markoutCount >= 10 {
				break
			}
		}
	}
	if markoutCount > 0 {
		avg := markoutSum / float64(markoutCount)
		markoutStr = fmt.Sprintf("%+.2fbps (last %d)", avg, markoutCount)
	}

	b.WriteString(fmt.Sprintf("  %s %d/%d active         %s %s\n",
		styleLabel.Render("ASSETS"), activeCount, totalAssets,
		styleLabel.Render("MARKOUT"), markoutStr,
	))

	// Health block
	rlPct := s.RateLimit.Pct
	if rlPct == "" {
		rlPct = "—"
	}
	rps := s.APIRequests.AvgRPS
	if rps == "" {
		rps = "—"
	}
	burn := s.RateLimit.BurnRate
	if burn == "" {
		burn = "—"
	}

	rlStyle := styleValueNeutral
	pctVal, _ := strconv.ParseFloat(rlPct, 64)
	if pctVal > 0 && pctVal < 20 {
		rlStyle = styleValueWarn
	}

	netStr := "—"
	if s.RateLimit.StartRemaining > 0 {
		delta := s.RateLimit.Remaining - s.RateLimit.StartRemaining
		sign := "+"
		if delta < 0 {
			sign = ""
		}
		netStr = fmt.Sprintf("%s%d", sign, delta)
	}

	etaStr := "—"
	if s.RateLimit.StartRemaining > 0 && s.RateLimit.Remaining > 0 && !s.Session.StartTime.IsZero() {
		elapsed := s.LastUpdate.Sub(s.Session.StartTime).Minutes()
		drained := float64(s.RateLimit.StartRemaining - s.RateLimit.Remaining)
		if elapsed > 1 && drained > 0 {
			drainPerMin := drained / elapsed
			minLeft := float64(s.RateLimit.Remaining) / drainPerMin
			if minLeft >= 60 {
				etaStr = fmt.Sprintf("%.1fh", minLeft/60)
			} else {
				etaStr = fmt.Sprintf("%.0fm", minLeft)
			}
		}
	}

	b.WriteString(fmt.Sprintf("  %s %s  start %d  net %s   %s rps   burn %s/min   eta %s\n",
		styleLabel.Render("API"),
		rlStyle.Render(fmt.Sprintf("%d remaining (%s%%)", s.RateLimit.Remaining, rlPct)),
		s.RateLimit.StartRemaining, netStr,
		rps, burn, etaStr,
	))

	// TTE + session accounting + virt_cycle stats
	tteStr := s.RateLimit.TteMin
	if tteStr == "" || tteStr == "0.0" {
		tteStr = "—"
	} else if tteStr != "inf" {
		if tteVal, err := strconv.ParseFloat(tteStr, 64); err == nil {
			if tteVal >= 60 {
				tteStr = fmt.Sprintf("%.1fh", tteVal/60)
			} else {
				tteStr = fmt.Sprintf("%.0fm", tteVal)
			}
		}
	}
	depletion := s.RateLimit.Depletion
	if depletion == "" {
		depletion = "—"
	}
	headroom := s.RateLimit.Headroom
	if headroom == "" {
		headroom = "—"
	}
	volRate := s.RateLimit.VolumeRateUSD
	if volRate == "" {
		volRate = "—"
	}
	cumVol := s.RateLimit.CumVolumeUSD
	if cumVol == "" {
		cumVol = "—"
	}

	b.WriteString(fmt.Sprintf("        tte %s   used %d  gen %d  net %d    depletion %s    headroom %s    vol $%s/min ($%s cum)\n",
		tteStr,
		s.RateLimit.SessionUsed, s.RateLimit.SessionGenerated, s.RateLimit.SessionNet,
		depletion, headroom, volRate, cumVol,
	))

	uptimeStr := "—"
	startStr := "—"
	nowStr := "—"
	if !s.Session.StartTime.IsZero() {
		dur := time.Since(s.Session.StartTime)
		if !s.LastUpdate.IsZero() {
			dur = s.LastUpdate.Sub(s.Session.StartTime)
		}
		uptimeStr = formatDuration(dur)
		startStr = s.Session.StartTime.Format("15:04")
		if !s.LastUpdate.IsZero() {
			nowStr = s.LastUpdate.Format("15:04")
		}
	}

	normalCount, degradedCount, haltedCount := countHealthStates(s)
	healthSummary := ""
	if normalCount > 0 {
		healthSummary += styleDotNormal.Render("●") + fmt.Sprintf(" %d normal", normalCount)
	}
	if degradedCount > 0 {
		if healthSummary != "" {
			healthSummary += "  "
		}
		healthSummary += styleDotDegraded.Render("●") + fmt.Sprintf(" %d deg", degradedCount)
	}
	if haltedCount > 0 {
		if healthSummary != "" {
			healthSummary += "  "
		}
		healthSummary += styleDotHalted.Render("●") + fmt.Sprintf(" %d halt", haltedCount)
	}
	if healthSummary == "" {
		healthSummary = "—"
	}

	stopStr := ""
	if s.Portfolio.SessionStopActive {
		stopStr = "   " + styleEventRisk.Render("⛔ SESSION STOP")
	} else if s.Portfolio.SessionStopReason == "recovery" {
		stopStr = "   " + styleEventWarn.Render("⚠ STOP RECOVERY")
	}

	chainStr := ""
	if s.Portfolio.ShockChainActive {
		if s.Portfolio.ShockChainReason == "recovery" {
			chainStr = "   " + styleEventWarn.Render("⚠ CASCADE RECOVERY")
		} else {
			chainStr = "   " + styleEventRisk.Render("⛔ CASCADE PAUSE")
		}
	}

	scheduleStr := ""
	if s.Portfolio.ScheduleActive {
		label := s.Portfolio.ScheduleLabel
		if label == "" {
			label = "window"
		}
		scheduleStr = "   " + styleEventDim.Render(fmt.Sprintf("🕒 %s ×%.2f", label, s.Portfolio.ScheduleMultiplier))
	}

	regimeStr := ""
	if s.Portfolio.RegimeGateActive {
		arrow := "↑"
		if s.Portfolio.RegimeDir == -1 {
			arrow = "↓"
		}
		regimeStr = "   " + styleEventBurst.Render(fmt.Sprintf("REGIME GATE %s", arrow))
	}

	b.WriteString(fmt.Sprintf("  %s %s (%s → %s)    %s %s%s%s%s%s\n",
		styleLabel.Render("UPTIME"), uptimeStr, startStr, nowStr,
		styleLabel.Render("STATUS"), healthSummary, stopStr, chainStr, scheduleStr, regimeStr,
	))

	return b.String()
}

func renderModeIndicator(mode string) string {
	if mode == "live" {
		return styleLiveIndicator.Render("● LIVE")
	}
	return styleStaticIndicator.Render("○ STATIC")
}

func countHealthStates(s *State) (normal, degraded, halted int) {
	for _, coin := range s.AssetOrder {
		a := s.Assets[coin]
		if a.Quoting.Halted {
			halted++
		} else if a.Quoting.Degraded || a.Quoting.Warmup || a.Quoting.Recovery {
			degraded++
		} else {
			normal++
		}
	}
	return
}

func formatDuration(d time.Duration) string {
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	if d < time.Hour {
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	h := int(d.Hours())
	m := int(d.Minutes()) % 60
	return fmt.Sprintf("%dh%dm", h, m)
}
