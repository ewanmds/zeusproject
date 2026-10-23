package main

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"
)

func (m model) View() tea.View {
	if m.width == 0 || m.height == 0 {
		v := tea.NewView("Initializing...")
		v.AltScreen = true
		return v
	}

	// File picker modal takes over the screen
	if m.pickerOpen && m.picker != nil {
		s := renderFilePicker(m.picker, m.width, m.height)
		v := tea.NewView(s)
		v.AltScreen = true
		return v
	}

	var b strings.Builder

	overview := renderOverview(m.state, m.width)
	b.WriteString(overview)

	b.WriteString(strings.Repeat("─", m.width) + "\n")
	table := renderAssetTable(m.state, m.selectedIdx, m.width)
	b.WriteString(table)
	b.WriteString(strings.Repeat("─", m.width) + "\n")

	usedLines := strings.Count(b.String(), "\n")
	remainingHeight := m.height - usedLines - 2
	if remainingHeight < 4 {
		remainingHeight = 4
	}

	if m.focus == focusRawLog {
		rawLog := renderRawLog(m.rawLines, remainingHeight)
		b.WriteString(rawLog)
	} else {
		detailWidth := m.width*6/10 - 1
		eventsWidth := m.width - detailWidth - 3

		detail := renderDetailPanel(m.state, m.selectedAsset(), detailWidth)
		events := renderEventsPanel(m.state, remainingHeight-2)

		detailLines := strings.Split(detail, "\n")
		eventLines := strings.Split(events, "\n")

		for i := 0; i < remainingHeight; i++ {
			left := ""
			if i < len(detailLines) {
				left = detailLines[i]
			}
			right := ""
			if i < len(eventLines) {
				right = eventLines[i]
			}

			leftWidth := lipgloss.Width(left)
			if leftWidth < detailWidth {
				left += strings.Repeat(" ", detailWidth-leftWidth)
			}

			if lipgloss.Width(right) > eventsWidth {
				right = ansi.Truncate(right, eventsWidth, "")
			}

			b.WriteString(left + " │ " + right + "\n")
		}
	}

	statusBar := renderStatusBar(m)
	b.WriteString(statusBar)

	v := tea.NewView(b.String())
	v.AltScreen = true
	return v
}

func renderRawLog(rawLines *RingBuffer[string], maxLines int) string {
	var b strings.Builder
	b.WriteString(styleSectionHeader.Render("Raw Log (l=toggle)") + "\n")

	lines := rawLines.Items()
	limit := len(lines)
	if limit > maxLines-1 {
		limit = maxLines - 1
	}

	for _, line := range lines[:limit] {
		b.WriteString("  " + line + "\n")
	}
	return b.String()
}

func renderStatusBar(m model) string {
	file := m.state.Session.LogFile
	mode := m.state.Session.Mode
	skip := m.state.UnmatchedCount

	modeStr := renderModeIndicator(mode)

	hints := "j/k=asset  l=raw  e=events  o=open  q=quit"
	skipStr := ""
	if skip > 0 {
		skipStr = fmt.Sprintf("  skip:%d", skip)
	}

	return styleStatusBar.Render(fmt.Sprintf("  %s │ %s%s │ %s", file, modeStr, skipStr, hints))
}
