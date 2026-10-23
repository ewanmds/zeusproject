package main

import "charm.land/lipgloss/v2"

var (
	colorGreen  = lipgloss.Color("#00FF87")
	colorRed    = lipgloss.Color("#FF5F5F")
	colorYellow = lipgloss.Color("#FFFF00")
	colorCyan   = lipgloss.Color("#00FFFF")
	colorDim    = lipgloss.Color("#666666")
	colorWhite  = lipgloss.Color("#FFFFFF")
	colorBorder = lipgloss.Color("#444444")

	styleHeader = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorWhite)

	styleLabel = lipgloss.NewStyle().
			Foreground(colorDim)

	styleValuePositive = lipgloss.NewStyle().
				Foreground(colorGreen)

	styleValueNegative = lipgloss.NewStyle().
				Foreground(colorRed)

	styleValueNeutral = lipgloss.NewStyle().
				Foreground(colorWhite)

	styleValueWarn = lipgloss.NewStyle().
			Foreground(colorYellow)

	styleDotNormal   = lipgloss.NewStyle().Foreground(colorGreen)
	styleDotDegraded = lipgloss.NewStyle().Foreground(colorYellow)
	styleDotHalted   = lipgloss.NewStyle().Foreground(colorRed)

	styleTableHeader = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorDim).
				BorderBottom(true).
				BorderStyle(lipgloss.NormalBorder()).
				BorderForeground(colorBorder)

	styleTableSelected = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorWhite)

	styleTableRow = lipgloss.NewStyle().
			Foreground(colorDim)

	styleEventFill      = lipgloss.NewStyle().Foreground(colorCyan)
	styleEventFillBuy   = lipgloss.NewStyle().Foreground(colorGreen)
	styleEventFillSell  = lipgloss.NewStyle().Foreground(colorRed)
	styleEventWarn      = lipgloss.NewStyle().Foreground(colorYellow)
	styleEventError     = lipgloss.NewStyle().Foreground(colorRed).Bold(true)
	styleEventDim   = lipgloss.NewStyle().Foreground(colorDim)
	styleEventBurst = lipgloss.NewStyle().Foreground(lipgloss.Color("#FF8C00")) // orange
	styleEventRisk      = lipgloss.NewStyle().Foreground(colorRed).Bold(true)
	styleEventRecovery  = lipgloss.NewStyle().Foreground(colorGreen)

	styleStatusBar = lipgloss.NewStyle().
			Foreground(colorDim)

	styleLiveIndicator = lipgloss.NewStyle().
				Foreground(colorGreen).
				Bold(true)

	styleStaticIndicator = lipgloss.NewStyle().
				Foreground(colorDim)

	styleSectionHeader = lipgloss.NewStyle().
				Bold(true).
				Foreground(colorCyan).
				MarginTop(1)
)
