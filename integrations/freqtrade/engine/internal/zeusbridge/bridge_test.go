package zeusbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestConfirmedFillsOnlyDedupAndCPM(t *testing.T) {
	s := Configure("test", "xyz:SP500")
	defer func() { active = nil }()
	out := &bytes.Buffer{}
	s.out = out
	raw := map[string]any{"coin": "SP500", "tid": 42, "time": s.start, "px": "100", "sz": "2", "fee": "0.04", "closedPnl": "0.02", "feeToken": "USDC"}
	RecordFill(raw)
	raw["coin"] = "xyz:SP500"
	RecordFill(raw)
	raw["tid"] = 43
	raw["time"] = s.start - 1
	RecordFill(raw)
	raw["time"] = s.start
	raw["closedPnl"] = "bad"
	RecordFill(raw)
	raw["closedPnl"] = "0.02"
	raw["feeToken"] = "BTC"
	RecordFill(raw)
	// Synthetic callbacks have no exchange tid and are not accepted.
	delete(raw, "tid")
	RecordFill(raw)
	RecordAction("order", 2)
	RecordAction("batchModify", 1)
	RecordAction("cancel", 2)
	RecordAction("updateLeverage", 1)
	s.Emit()
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got["fills"] != float64(1) || got["volume"] != float64(200) || got["cost_per_1m"] != float64(100) {
		t.Fatalf("wrong accounting: %v", got)
	}
	if got["order_placements"] != float64(2) || got["order_modifications"] != float64(1) || got["order_cancellations"] != float64(2) || got["order_requests"] != float64(3) {
		t.Fatal(got)
	}
}
func TestEmptyCPMUnknownPosition(t *testing.T) {
	s := Configure("test", "xyz:SP500")
	defer func() { active = nil }()
	out := &bytes.Buffer{}
	s.out = out
	s.Emit()
	var got map[string]any
	_ = json.Unmarshal(out.Bytes(), &got)
	if got["cost_per_1m"] != nil || got["position_known"] != false {
		t.Fatal(got)
	}
}

func TestIncompleteAccountingNeverPublishesCPM(t *testing.T) {
	s := Configure("test", "xyz:SP500")
	defer func() { active = nil }()
	out := &bytes.Buffer{}
	s.out = out
	s.Update(map[string]any{"volume": 100.0, "fees": 1.0, "rpnl": 0.0, "accounting_complete": false})
	s.Emit()
	var got map[string]any
	_ = json.Unmarshal(out.Bytes(), &got)
	if got["cost_per_1m"] != nil {
		t.Fatal("CPM published from incomplete closing accounting")
	}
}

// lastJSONSnapshot keeps the last emitted snapshot, since a State.Emit call
// appends exactly one JSON object per stream line and ErrorHandler.Handle
// emits on its own.
func lastJSONSnapshot(t *testing.T, out *bytes.Buffer) map[string]any {
	t.Helper()
	dec := json.NewDecoder(out)
	var got map[string]any
	for {
		var next map[string]any
		err := dec.Decode(&next)
		if err != nil {
			break
		}
		got = next
	}
	if got == nil {
		t.Fatal("no snapshot emitted")
	}
	return got
}

func TestErrorHandlerAppendsAttrsAndCapsLength(t *testing.T) {
	s := Configure("test", "xyz:SP500")
	defer func() { active = nil }()
	out := &bytes.Buffer{}
	s.out = out
	h := ErrorHandler{State: s}

	record := slog.NewRecord(time.Now(), slog.LevelError, "quote manager entering fail-closed halt", 0)
	record.AddAttrs(slog.String("reason", "quote_refresh_failure"))
	record.Add(slog.Any("error", context.DeadlineExceeded))
	record.AddAttrs(slog.Int("retry_count", 2))
	if err := h.Handle(context.Background(), record); err != nil {
		t.Fatal(err)
	}

	shown := lastJSONSnapshot(t, out)["error"].(string)
	for _, want := range []string{
		"quote manager entering fail-closed halt",
		"reason=quote_refresh_failure",
		"retry_count=2",
	} {
		if !strings.Contains(shown, want) {
			t.Fatalf("banner missing %q: %q", want, shown)
		}
	}

	// A verbose attr value must not flood the Zeus banner.
	verbose := strings.Repeat("x", 400)
	rec := slog.NewRecord(time.Now(), slog.LevelError, "op failed", 0)
	rec.AddAttrs(slog.String("error", verbose))
	if err := h.Handle(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	shown = lastJSONSnapshot(t, out)["error"].(string)
	if runes := len([]rune(shown)); runes > maxZeusErrorLen {
		t.Fatalf("banner not capped: %d runes", runes)
	}
	if !strings.HasPrefix(shown, "op failed") {
		t.Fatalf("unexpected banner: %q", shown)
	}
}
