package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestIntegration_ParseRealLogFile(t *testing.T) {
	logData := strings.Join([]string{
		`{"time":"2026-04-04T21:03:31.659709-04:00","level":"INFO","msg":"starting hyperliquid market maker","mode":"LIVE","assets":["xyz:CL"],"max_drawdown_pct":25,"commit":"3d00bf5"}`,
		`{"time":"2026-04-04T21:03:34.755396-04:00","level":"INFO","msg":"bootstrapped equity","equity":396.42}`,
		`{"time":"2026-04-04T21:04:05.759972-04:00","level":"INFO","msg":"=== STATUS ===","equity":396.42,"high_water":"396.42"}`,
		`{"time":"2026-04-04T21:04:05.760822-04:00","level":"INFO","msg":"  pnl","equity_delta":"0.0000","equity":"396.42","session_pnl":"0.0000","session_pnl_bps":"0.0"}`,
		`{"time":"2026-04-04T21:04:05.759989-04:00","level":"INFO","msg":"  vwap_short","coin":"xyz:CL","vwap":"112.19","trades":18,"trade_age_ms":4104}`,
		`{"time":"2026-04-04T21:04:05.759998-04:00","level":"INFO","msg":"  vwap_long","coin":"xyz:CL","vwap":"112.19","trades":18}`,
		`{"time":"2026-04-04T21:04:05.760003-04:00","level":"INFO","msg":"  position","coin":"xyz:CL","side":"long","size":0.645,"notional":72.3,"avg_entry":112.09}`,
		`{"time":"2026-04-04T21:04:05.760011-04:00","level":"INFO","msg":"  orders","coin":"xyz:CL","count":6}`,
		`{"time":"2026-04-04T21:04:05.76004-04:00","level":"INFO","msg":"  quoting","coin":"xyz:CL","book_mid":"112.18","fair_value":"112.22","bbo_spread_bps":"1.8","spread_bps":"6.9","skew_bps":"0.00","cross_leg_bps":"0.00","bias_bps":"0.00","imbalance_shift_bps":"3.00","fill_decay_shift_bps":"0.00","wick_bid":"1.00","wick_ask":"1.00","z_wide":"1.00","warmup":false,"recovery":false,"degraded":false,"halted":false,"health_reason":"","pair_reduce_only":false}`,
		`{"time":"2026-04-04T21:04:05.760061-04:00","level":"INFO","msg":"  sizing","coin":"xyz:CL","inv_util":"0.0900","entry_size_factor":"1.00","allowed_buy":"400.0","allowed_sell":"399.4","notional_mult":"1.00","tox_buy_ewma":"0.00","tox_sell_ewma":"0.00","tox_buy_mult":"1.00","tox_sell_mult":"1.00"}`,
		`{"time":"2026-04-04T21:04:05.760076-04:00","level":"INFO","msg":"  adaptive","coin":"xyz:CL","skew_factor":"2.500","vwap_alpha":"0.400","autocorr":"0.000","opt_spread_delta":"0.0","opt_spread_buy":"0.0","opt_spread_sell":"0.0","opt_notional_mult":"1.00","opt_skew_mult":"1.00","opt_alpha_delta":"0.000","opt_imb_shift_mult":"1.00","opt_fill_decay_mult":"1.00","opt_refresh_mult":"1.00","opt_eval":5,"burst_buy_spread":"1.00","burst_sell_spread":"1.00","vol_burst_ratio":"0.00","vol_burst_levels":3,"vol_burst_active":false}`,
		`{"time":"2026-04-04T21:04:05.760845-04:00","level":"INFO","msg":"  api_requests","window_sec":30,"total":12,"exchange":2,"info":10,"avg_rps":"0.4"}`,
		`{"time":"2026-04-04T21:04:05.760853-04:00","level":"INFO","msg":"  fees","total_exchange_fees":"0.0380","total_builder_fees":"0.0032","total_all_fees":"0.0412"}`,
		`{"time":"2026-04-04T21:04:05.760862-04:00","level":"INFO","msg":"  rate_limit","remaining":16701,"pct":"6.2","tier":"normal","tte_min":"inf","depletion":"0.00","ewma_depletion":"0.0000","halted":false,"session_used":5,"session_generated":0,"session_net":0}`,
		`{"time":"2026-04-04T21:04:05.76088-04:00","level":"INFO","msg":"  virt_cycle","headroom":"0.000","volume_rate_usd_min":"0.00","burn_rate_calls_min":"5.33","cum_volume_usd":"0.00"}`,
		`{"time":"2026-04-04T21:05:46.908719-04:00","level":"INFO","msg":"markout","coin":"xyz:CL","side":"buy","fill_px":"112.2200","mid_at_fill":"112.2300","size":"0.6450","T500ms_bps":"0.89","T1s_bps":"0.89","T2s_bps":"0.45","T5s_bps":"0.45"}`,
		`{"time":"2026-04-04T21:11:56.020805-04:00","level":"INFO","msg":"fill poller: new fill","coin":"xyz:CL","side":"buy","size":0.896,"price":112.2,"oid":370753611161,"fee":0.003015,"builder_fee":0}`,
		`{"time":"2026-04-04T21:11:56.021000-04:00","level":"INFO","msg":"fill detail","coin":"xyz:CL","side":"buy","size":0.896,"price":112.2,"fair_value":"112.2500","capture_bps":"4.4","notional":"100.53","fee":"0.0030","wick_mult":"1.00"}`,
		`{"time":"2026-04-04T21:12:05.86725-04:00","level":"WARN","msg":"equity poll: median filter rejected outlier","raw":"375.66","median":"396.56","previous":"396.56","drift_pct":"-5.3"}`,
	}, "\n") + "\n"

	dir := t.TempDir()
	path := filepath.Join(dir, "test.log")
	if err := os.WriteFile(path, []byte(logData), 0644); err != nil {
		t.Fatal(err)
	}

	linesCh := make(chan string, 1000)
	tail, err := NewTailer(path, linesCh, true)
	if err != nil {
		t.Fatal(err)
	}
	go tail.Run()

	parser := NewParser()
	timeout := time.After(3 * time.Second)
	linesRead := 0

	for linesRead < 19 {
		select {
		case line := <-linesCh:
			parser.ParseLine(line)
			linesRead++
		case <-timeout:
			t.Fatalf("timeout after reading %d lines", linesRead)
		}
	}
	tail.Stop()

	s := parser.State

	if s.Session.BotMode != "LIVE" {
		t.Errorf("bot mode: got %q", s.Session.BotMode)
	}
	if s.Session.CommitHash != "3d00bf5" {
		t.Errorf("commit: got %q", s.Session.CommitHash)
	}
	if s.Portfolio.Equity != 396.42 {
		t.Errorf("equity: got %f", s.Portfolio.Equity)
	}
	if s.Portfolio.TotalAllFees != "0.0412" {
		t.Errorf("fees: got %q", s.Portfolio.TotalAllFees)
	}

	a := s.Assets["xyz:CL"]
	if a == nil {
		t.Fatal("expected xyz:CL")
	}
	if a.Position.Side != "long" || a.Position.Notional != 72.3 {
		t.Errorf("position: got side=%q notional=%f", a.Position.Side, a.Position.Notional)
	}
	if a.OrderCount != 6 {
		t.Errorf("orders: got %d", a.OrderCount)
	}
	if a.Markouts.Len() != 1 {
		t.Errorf("markouts: got %d", a.Markouts.Len())
	}
	if a.Adaptive.OptEval != 5 {
		t.Errorf("opt_eval: got %d", a.Adaptive.OptEval)
	}

	events := s.Events.Items()
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	if events[0].Type != EventWarn {
		t.Errorf("event[0]: expected WARN, got type %d", events[0].Type)
	}
	if events[1].Type != EventFill {
		t.Errorf("event[1]: expected Fill, got type %d", events[1].Type)
	}

	if s.Fills.Total != 1 || s.Fills.Buy != 1 {
		t.Errorf("fills: got %+v", s.Fills)
	}
	if s.RateLimit.Remaining != 16701 {
		t.Errorf("rate_limit remaining: got %d", s.RateLimit.Remaining)
	}
	if s.APIRequests.Total != 12 {
		t.Errorf("api total: got %d", s.APIRequests.Total)
	}
}
