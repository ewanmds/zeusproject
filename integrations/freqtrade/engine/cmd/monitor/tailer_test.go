package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestTailer_ReadNewLines(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.log")

	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString(`{"time":"2026-01-01T00:00:00Z","level":"INFO","msg":"line1"}` + "\n")
	f.WriteString(`{"time":"2026-01-01T00:00:01Z","level":"INFO","msg":"line2"}` + "\n")
	f.Close()

	lines := make(chan string, 100)
	tail, err := NewTailer(path, lines, false) // fromStart=false -> seek to end
	if err != nil {
		t.Fatal(err)
	}
	defer tail.Stop()

	go tail.Run()
	time.Sleep(100 * time.Millisecond)

	// Append new lines
	f, _ = os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0644)
	f.WriteString(`{"time":"2026-01-01T00:00:02Z","level":"INFO","msg":"line3"}` + "\n")
	f.Close()

	select {
	case line := <-lines:
		if line != `{"time":"2026-01-01T00:00:02Z","level":"INFO","msg":"line3"}` {
			t.Errorf("unexpected line: %q", line)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for line")
	}
}

func TestTailer_ReadFromStart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.log")

	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString("line1\n")
	f.WriteString("line2\n")
	f.Close()

	lines := make(chan string, 100)
	tail, err := NewTailer(path, lines, true) // fromStart=true
	if err != nil {
		t.Fatal(err)
	}
	defer tail.Stop()

	go tail.Run()

	var received []string
	timeout := time.After(2 * time.Second)
	for len(received) < 2 {
		select {
		case line := <-lines:
			received = append(received, line)
		case <-timeout:
			t.Fatalf("timeout, received %d lines: %v", len(received), received)
		}
	}

	if received[0] != "line1" || received[1] != "line2" {
		t.Errorf("unexpected lines: %v", received)
	}
}
