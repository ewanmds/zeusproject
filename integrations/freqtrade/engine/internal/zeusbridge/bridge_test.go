package zeusbridge

import (
	"bytes"
	"encoding/json"
	"testing"
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
