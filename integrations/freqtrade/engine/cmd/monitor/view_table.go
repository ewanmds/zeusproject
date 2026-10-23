package main

import (
	"fmt"
	"strings"
)

func renderAssetTable(s *State, selectedIdx int, width int) string {
	if len(s.Assets) == 0 {
		return styleLabel.Render("  No assets yet...\n")
	}

	var b strings.Builder

	header := fmt.Sprintf(" %-10s %-6s %9s %6s %8s %10s  %-10s",
		"Asset", "Side", "Notional", "Inv%", "Spread", "FV", "Health")
	b.WriteString(styleTableHeader.Render(header) + "\n")

	for i, coin := range s.AssetOrder {
		a, ok := s.Assets[coin]
		if !ok {
			continue
		}

		marker := " "
		rowStyle := styleTableRow
		if i == selectedIdx {
			marker = ">"
			rowStyle = styleTableSelected
		}

		side := "—"
		if a.Position.Side != "" {
			side = a.Position.Side
		}

		notional := fmt.Sprintf("%.1f", a.Position.Notional)

		invUtil := "0.0%"
		if a.Sizing.InvUtil != "" && a.Sizing.InvUtil != "0.0000" {
			var f float64
			fmt.Sscanf(a.Sizing.InvUtil, "%f", &f)
			invUtil = fmt.Sprintf("%.1f%%", f*100)
		}

		spread := "—"
		if a.Quoting.Spread != "" {
			spread = a.Quoting.Spread
		}

		fv := "—"
		if a.Quoting.FairValue != "" {
			fv = a.Quoting.FairValue
		}

		health := renderHealthDot(a)

		row := fmt.Sprintf("%s%-10s %-6s %9s %6s %8s %10s  %s",
			marker, coin, side, notional, invUtil, spread, fv, health)
		b.WriteString(rowStyle.Render(row) + "\n")
	}

	return b.String()
}

func renderHealthDot(a *AssetState) string {
	if a.Quoting.Halted {
		return styleDotHalted.Render("●") + " halted"
	}
	if a.Quoting.Degraded {
		return styleDotDegraded.Render("●") + " degraded"
	}
	if a.Quoting.Warmup {
		return styleDotDegraded.Render("●") + " warmup"
	}
	if a.Quoting.Recovery {
		return styleDotDegraded.Render("●") + " recovery"
	}
	if a.Sizing.PriceShockActive {
		return styleDotHalted.Render("●") + " " + styleValueNegative.Render("shock")
	}
	if a.Sizing.TrendGuardActive {
		return styleDotDegraded.Render("●") + " " + styleValueWarn.Render("tguard")
	}
	if a.Sizing.FlowGateActive {
		return styleDotDegraded.Render("●") + " " + styleValueWarn.Render("fgate")
	}
	if a.Adaptive.VolBurstActive {
		return styleDotNormal.Render("●") + " " + styleValuePositive.Render("burst")
	}
	return styleDotNormal.Render("●") + " normal"
}
