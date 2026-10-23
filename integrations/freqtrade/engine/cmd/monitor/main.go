package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	tea "charm.land/bubbletea/v2"
)

func main() {
	var logsDir string
	flag.StringVar(&logsDir, "dir", "logs", "directory to scan for log files")
	flag.Parse()

	logFile := ""
	if flag.NArg() > 0 {
		logFile = flag.Arg(0)
	} else {
		var err error
		logFile, err = findLatestLog(logsDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			fmt.Fprintf(os.Stderr, "usage: monitor [path/to/logfile.txt]\n")
			os.Exit(1)
		}
	}

	info, err := os.Stat(logFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot stat %s: %v\n", logFile, err)
		os.Exit(1)
	}
	isLive := time.Since(info.ModTime()) <= 60*time.Second

	// Always read from start to build up state, then tail for new lines
	linesCh := make(chan string, 1000)
	tailer, err := NewTailer(logFile, linesCh, true)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	go tailer.Run()
	defer tailer.Stop()

	m := newModel(linesCh, logFile, !isLive, logsDir)
	m.tailer = tailer
	p := tea.NewProgram(m)
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func findLatestLog(dir string) (string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "", fmt.Errorf("cannot read %s: %w", dir, err)
	}

	type fileEntry struct {
		path    string
		modTime time.Time
	}

	var files []fileEntry
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if filepath.Ext(e.Name()) != ".txt" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		files = append(files, fileEntry{
			path:    filepath.Join(dir, e.Name()),
			modTime: info.ModTime(),
		})
	}

	if len(files) == 0 {
		return "", fmt.Errorf("no .txt files found in %s", dir)
	}

	sort.Slice(files, func(i, j int) bool {
		return files[i].modTime.After(files[j].modTime)
	})

	return files[0].path, nil
}
