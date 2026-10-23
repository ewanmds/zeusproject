package main

import (
	"os"
	"time"

	tea "charm.land/bubbletea/v2"
)

type focus int

const (
	focusTable focus = iota
	focusEvents
	focusRawLog
)

type linesMsg []string
type tailerDoneMsg struct{}

type model struct {
	state       *State
	parser      *Parser
	linesCh     <-chan string
	tailer      *Tailer
	width       int
	height      int
	selectedIdx int
	focus       focus
	rawLines    *RingBuffer[string]
	logsDir     string
	picker      *filePicker
	pickerOpen  bool
}

func newModel(linesCh <-chan string, logFile string, fromStart bool, logsDir string) model {
	p := NewParser()
	p.State.Session.LogFile = logFile
	if fromStart {
		p.State.Session.Mode = "static"
	} else {
		p.State.Session.Mode = "live"
	}
	return model{
		state:    p.State,
		parser:   p,
		linesCh:  linesCh,
		rawLines: NewRingBuffer[string](500),
		logsDir:  logsDir,
	}
}

func (m model) Init() tea.Cmd {
	return waitForLines(m.linesCh)
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height

	case tea.KeyPressMsg:
		return m.handleKey(msg)

	case linesMsg:
		for _, line := range msg {
			m.rawLines.Add(line)
			m.parser.ParseLine(line)
		}
		m.state.Session.Mode = "live"
		return m, waitForLines(m.linesCh)

	case tailerDoneMsg:
		m.state.Session.Mode = "static"
		return m, nil

	case switchFileMsg:
		// Stop the old tailer to prevent goroutine/fd leak
		if m.tailer != nil {
			m.tailer.Stop()
		}

		info, err := os.Stat(msg.path)
		isLive := err == nil && time.Since(info.ModTime()) <= 60*time.Second

		m.parser = NewParser()
		m.parser.State.Session.LogFile = msg.path
		m.state = m.parser.State
		m.rawLines = NewRingBuffer[string](500)
		m.selectedIdx = 0

		newLinesCh := make(chan string, 1000)
		m.linesCh = newLinesCh

		// Always read from start to build state
		tailer, err := NewTailer(msg.path, newLinesCh, true)
		if err != nil {
			return m, nil
		}
		m.tailer = tailer
		go tailer.Run()

		if isLive {
			m.state.Session.Mode = "live"
		} else {
			m.state.Session.Mode = "static"
		}

		return m, waitForLines(m.linesCh)
	}

	return m, nil
}

func (m model) handleKey(msg tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	key := msg.String()

	// If picker is open, handle picker keys
	if m.pickerOpen {
		switch key {
		case "esc":
			m.pickerOpen = false
		case "up", "k":
			m.picker.up()
		case "down", "j":
			m.picker.down()
		case "enter":
			path := m.picker.selectedPath()
			if path != "" {
				m.pickerOpen = false
				return m, m.switchFile(path)
			}
		}
		return m, nil
	}

	switch key {
	case "q", "ctrl+c":
		return m, tea.Quit

	case "j", "down":
		if len(m.state.AssetOrder) > 0 {
			m.selectedIdx = (m.selectedIdx + 1) % len(m.state.AssetOrder)
		}

	case "k", "up":
		if len(m.state.AssetOrder) > 0 {
			m.selectedIdx = (m.selectedIdx - 1 + len(m.state.AssetOrder)) % len(m.state.AssetOrder)
		}

	case "l":
		if m.focus == focusRawLog {
			m.focus = focusTable
		} else {
			m.focus = focusRawLog
		}

	case "e":
		if m.focus == focusEvents {
			m.focus = focusTable
		} else {
			m.focus = focusEvents
		}

	case "o":
		m.picker = newFilePicker(m.logsDir)
		m.pickerOpen = true
	}

	return m, nil
}

func (m model) selectedAsset() *AssetState {
	if m.selectedIdx >= len(m.state.AssetOrder) {
		return nil
	}
	coin := m.state.AssetOrder[m.selectedIdx]
	return m.state.Assets[coin]
}

func waitForLines(ch <-chan string) tea.Cmd {
	return func() tea.Msg {
		line, ok := <-ch
		if !ok {
			return tailerDoneMsg{}
		}
		lines := []string{line}
		for {
			select {
			case l, ok := <-ch:
				if !ok {
					return linesMsg(lines)
				}
				lines = append(lines, l)
			default:
				return linesMsg(lines)
			}
		}
	}
}

// Used by file picker (Task 11)
type switchFileMsg struct {
	path string
}

func (m model) switchFile(path string) tea.Cmd {
	return func() tea.Msg {
		return switchFileMsg{path: path}
	}
}
