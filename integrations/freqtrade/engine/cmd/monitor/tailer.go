package main

import (
	"bufio"
	"io"
	"os"
	"strings"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Tailer watches a log file and emits new lines to a channel as they are appended.
type Tailer struct {
	path      string
	lines     chan<- string
	fromStart bool
	stop      chan struct{}
}

// NewTailer creates a Tailer for the given file path. If fromStart is true, the
// file is read from the beginning; otherwise reading starts at the current end of
// file so only future appends are delivered.
func NewTailer(path string, lines chan<- string, fromStart bool) (*Tailer, error) {
	if _, err := os.Stat(path); err != nil {
		return nil, err
	}
	return &Tailer{
		path:      path,
		lines:     lines,
		fromStart: fromStart,
		stop:      make(chan struct{}),
	}, nil
}

// Stop signals the tailer to exit its Run loop.
func (t *Tailer) Stop() {
	select {
	case <-t.stop:
	default:
		close(t.stop)
	}
}

// Run watches the file and sends new lines to t.lines. It returns when Stop is
// called or the stop channel is closed. Intended to be run in a goroutine.
func (t *Tailer) Run() {
	f, err := os.Open(t.path)
	if err != nil {
		return
	}
	defer f.Close()

	if !t.fromStart {
		f.Seek(0, io.SeekEnd) //nolint:errcheck
	}

	reader := bufio.NewReader(f)

	// Set up fsnotify for efficient file change detection. Fall back to
	// polling-only when watcher creation or Add fails.
	var watchEvents <-chan fsnotify.Event
	watcher, err := fsnotify.NewWatcher()
	if err == nil {
		if addErr := watcher.Add(t.path); addErr == nil {
			watchEvents = watcher.Events
		} else {
			watcher.Close()
			watcher = nil
		}
	}
	if watcher != nil {
		defer watcher.Close()
	}

	// Drain any lines already present (fromStart) or nothing (seeked to end).
	t.readLines(reader, f)

	pollTicker := time.NewTicker(200 * time.Millisecond)
	defer pollTicker.Stop()

	for {
		// Non-blocking stop check before blocking select.
		select {
		case <-t.stop:
			return
		default:
		}

		select {
		case <-t.stop:
			return

		case _, ok := <-watchEvents:
			if !ok {
				// Watcher closed; fall back to poll-only.
				watchEvents = nil
				continue
			}
			t.readLines(reader, f)

		case <-pollTicker.C:
			// Detect log rotation / truncation: if file is smaller than our
			// current position, seek back to the start.
			info, statErr := os.Stat(t.path)
			if statErr == nil {
				pos, _ := f.Seek(0, io.SeekCurrent)
				if info.Size() < pos {
					f.Seek(0, io.SeekStart) //nolint:errcheck
					reader.Reset(f)
				}
			}
			t.readLines(reader, f)
		}
	}
}

// readLines drains all complete lines currently available from reader, sending
// each trimmed line to t.lines. Stops on any read error (including io.EOF when
// no full line is yet available) or when t.stop is closed.
func (t *Tailer) readLines(reader *bufio.Reader, _ *os.File) {
	for {
		line, err := reader.ReadString('\n')
		line = strings.TrimRight(line, "\n\r")
		if line != "" {
			select {
			case t.lines <- line:
			case <-t.stop:
				return
			}
		}
		if err != nil {
			return
		}
	}
}
