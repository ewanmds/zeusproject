package main

import (
	"strings"
	"testing"
)

func TestParseStartup(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:03:31.659709-04:00","level":"INFO","msg":"starting hyperliquid market maker","mode":"LIVE","assets":["xyz:CL"],"max_drawdown_pct":25,"commit":"3d00bf5"}`)

	if p.State.Session.BotMode != "LIVE" {
		t.Errorf("expected mode LIVE, got %q", p.State.Session.BotMode)
	}
	if p.State.Session.CommitHash != "3d00bf5" {
		t.Errorf("expected commit 3d00bf5, got %q", p.State.Session.CommitHash)
	}
	if len(p.State.Session.Assets) != 1 || p.State.Session.Assets[0] != "xyz:CL" {
		t.Errorf("expected assets [xyz:CL], got %v", p.State.Session.Assets)
	}
	if p.State.Portfolio.MaxDrawdownPct != 25 {
		t.Errorf("expected max_drawdown_pct 25, got %f", p.State.Portfolio.MaxDrawdownPct)
	}
}

func TestParseBootstrapEquity(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:03:34.755396-04:00","level":"INFO","msg":"bootstrapped equity","equity":396.420001}`)
	if p.State.Portfolio.Equity != 396.420001 {
		t.Errorf("expected equity 396.420001, got %f", p.State.Portfolio.Equity)
	}
}

func TestParseStatusBlock(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759972-04:00","level":"INFO","msg":"=== STATUS ===","equity":396.420001,"high_water":"396.42"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760822-04:00","level":"INFO","msg":"  pnl","equity_delta":"0.0000","equity":"396.42","session_pnl":"0.0000","session_pnl_bps":"0.0"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759989-04:00","level":"INFO","msg":"  vwap_short","coin":"xyz:CL","vwap":"112.1896","trades":18,"trade_age_ms":4104}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759998-04:00","level":"INFO","msg":"  vwap_long","coin":"xyz:CL","vwap":"112.1896","trades":18}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760003-04:00","level":"INFO","msg":"  position","coin":"xyz:CL","side":"","size":0,"notional":0,"avg_entry":0}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760011-04:00","level":"INFO","msg":"  orders","coin":"xyz:CL","count":6}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.76004-04:00","level":"INFO","msg":"  quoting","coin":"xyz:CL","book_mid":"112.1800","fair_value":"112.2292","bbo_spread_bps":"1.8","spread_bps":"9.9","skew_bps":"0.00","cross_leg_bps":"0.00","bias_bps":"0.00","imbalance_shift_bps":"3.00","fill_decay_shift_bps":"0.00","wick_bid":"1.00","wick_ask":"1.00","z_wide":"1.00","warmup":true,"recovery":false,"degraded":false,"halted":false,"health_reason":"","pair_reduce_only":false}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760061-04:00","level":"INFO","msg":"  sizing","coin":"xyz:CL","inv_util":"0.0000","entry_size_factor":"1.00","allowed_buy":"40.0","allowed_sell":"40.0","notional_mult":"1.00","tox_buy_ewma":"0.00","tox_sell_ewma":"0.00","tox_buy_mult":"1.00","tox_sell_mult":"1.00"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760076-04:00","level":"INFO","msg":"  adaptive","coin":"xyz:CL","skew_factor":"2.500","vwap_alpha":"0.400","autocorr":"0.000","opt_spread_delta":"0.0","opt_spread_buy":"0.0","opt_spread_sell":"0.0","opt_notional_mult":"1.00","opt_skew_mult":"1.00","opt_alpha_delta":"0.000","opt_imb_shift_mult":"1.00","opt_fill_decay_mult":"1.00","opt_refresh_mult":"1.00","opt_eval":0,"burst_buy_spread":"1.00","burst_sell_spread":"1.00","vol_burst_ratio":"0.00","vol_burst_levels":3,"vol_burst_active":false}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760845-04:00","level":"INFO","msg":"  api_requests","window_sec":30,"total":12,"exchange":2,"info":10,"avg_rps":"0.4"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760853-04:00","level":"INFO","msg":"  fees","total_exchange_fees":"0.0000","total_builder_fees":"0.0000","total_all_fees":"0.0000"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.760862-04:00","level":"INFO","msg":"  rate_limit","remaining":0,"pct":"—","tier":"normal","tte_min":"0.0","depletion":"0.00","ewma_depletion":"0.0000","halted":false,"session_used":2,"session_generated":0,"session_net":0}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.76088-04:00","level":"INFO","msg":"  virt_cycle","headroom":"0.000","volume_rate_usd_min":"0.00","burn_rate_calls_min":"2.40","cum_volume_usd":"0.00"}`)

	s := p.State

	if s.Portfolio.Equity != 396.420001 {
		t.Errorf("equity: got %f", s.Portfolio.Equity)
	}
	if s.Portfolio.HighWater != "396.42" {
		t.Errorf("high_water: got %q", s.Portfolio.HighWater)
	}
	if s.Portfolio.SessionPnL != "0.0000" {
		t.Errorf("session_pnl: got %q", s.Portfolio.SessionPnL)
	}

	a := s.Assets["xyz:CL"]
	if a == nil {
		t.Fatal("expected asset xyz:CL to exist")
	}
	if a.VWAP.ShortVwap != "112.1896" {
		t.Errorf("short vwap: got %q", a.VWAP.ShortVwap)
	}
	if a.VWAP.Trades != 18 {
		t.Errorf("trades: got %d", a.VWAP.Trades)
	}
	if a.Position.Side != "" {
		t.Errorf("position side: got %q", a.Position.Side)
	}
	if a.OrderCount != 6 {
		t.Errorf("orders: got %d", a.OrderCount)
	}
	if a.Quoting.BookMid != "112.1800" {
		t.Errorf("book_mid: got %q", a.Quoting.BookMid)
	}
	if !a.Quoting.Warmup {
		t.Error("expected warmup=true")
	}
	if a.Sizing.AllowedBuy != "40.0" {
		t.Errorf("allowed_buy: got %q", a.Sizing.AllowedBuy)
	}
	if a.Adaptive.OptEval != 0 {
		t.Errorf("opt_eval: got %d", a.Adaptive.OptEval)
	}
	if s.APIRequests.Total != 12 {
		t.Errorf("api total: got %d", s.APIRequests.Total)
	}
	if s.RateLimit.Tier != "normal" {
		t.Errorf("tier: got %q", s.RateLimit.Tier)
	}
	if s.RateLimit.BurnRate != "2.40" {
		t.Errorf("burn_rate: got %q", s.RateLimit.BurnRate)
	}
}

func TestParseQuotingSkewFieldNames(t *testing.T) {
	// v32.1+: field renamed from skew_bps to skew_entry_bps; parser must accept both
	// so old logs still render in the monitor.
	cases := []struct {
		name string
		line string
		want string
	}{
		{
			name: "new field name skew_entry_bps",
			line: `{"time":"2026-04-16T09:00:00-04:00","level":"INFO","msg":"  quoting","coin":"xyz:CL","skew_entry_bps":"12.34"}`,
			want: "12.34",
		},
		{
			name: "legacy field name skew_bps",
			line: `{"time":"2026-04-16T09:00:00-04:00","level":"INFO","msg":"  quoting","coin":"xyz:CL","skew_bps":"7.89"}`,
			want: "7.89",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := NewParser()
			p.ParseLine(tc.line)
			got := p.State.Assets["xyz:CL"].Quoting.Skew
			if got != tc.want {
				t.Errorf("skew: got %q, want %q", got, tc.want)
			}
		})
	}
}

func TestParseFillEvent(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:11:56.020805-04:00","level":"INFO","msg":"fill poller: new fill","coin":"xyz:CL","side":"buy","size":0.896,"price":112.2,"oid":370753611161,"fee":0.003015,"builder_fee":0}`)
	p.ParseLine(`{"time":"2026-04-04T21:11:56.021000-04:00","level":"INFO","msg":"fill detail","coin":"xyz:CL","side":"buy","size":0.896,"price":112.2,"fair_value":"112.2500","capture_bps":"4.4","notional":"100.53","fee":"0.0030","wick_mult":"1.00"}`)

	if p.State.Fills.Total != 1 || p.State.Fills.Buy != 1 || p.State.Fills.Sell != 0 {
		t.Errorf("fill counts: got %+v", p.State.Fills)
	}
	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventFill {
		t.Errorf("expected EventFill, got %d", events[0].Type)
	}
	if events[0].Coin != "xyz:CL" {
		t.Errorf("expected coin xyz:CL, got %q", events[0].Coin)
	}
}

func TestParseMarkout(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:05:46.908719-04:00","level":"INFO","msg":"markout","coin":"xyz:CL","side":"buy","fill_px":"112.2200","mid_at_fill":"112.2300","size":"0.6450","T500ms_bps":"0.89","T1s_bps":"0.89","T2s_bps":"0.45","T5s_bps":"0.45"}`)

	a := p.State.Assets["xyz:CL"]
	if a == nil {
		t.Fatal("expected asset xyz:CL to exist")
	}
	markouts := a.Markouts.Items()
	if len(markouts) != 1 {
		t.Fatalf("expected 1 markout, got %d", len(markouts))
	}
	if markouts[0].Side != "buy" || markouts[0].T500ms != "0.89" {
		t.Errorf("markout: got %+v", markouts[0])
	}
}

func TestParseWarnEvent(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:12:05.86725-04:00","level":"WARN","msg":"equity poll: median filter rejected outlier","raw":"375.66","median":"396.56","previous":"396.56","drift_pct":"-5.3"}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventWarn {
		t.Errorf("expected EventWarn, got %d", events[0].Type)
	}
}

func TestParseErrorEvent(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T00:35:18.168018-04:00","level":"ERROR","msg":"reconciliation: hard drift detected, halting coin quoting","coin":"ETH","delta_notional":"39.76"}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventError || events[0].Coin != "ETH" {
		t.Errorf("expected EventError for ETH, got %+v", events[0])
	}
}

func TestParseReconZeusSoftCorrection(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T00:35:18.168018-04:00","level":"INFO","msg":"reconciliation: drift within Zeus order-size cap, treating as soft correction","coin":"io:ANTH","delta_notional":"100.00","order_cap":"100.00"}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventWarn || events[0].Coin != "io:ANTH" {
		t.Errorf("expected EventWarn for io:ANTH, got %+v", events[0])
	}
}

func TestParseOptimizerState(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759699-04:00","level":"INFO","msg":"[OptimizerV2] state","coin":"xyz:CL","eval":1,"tox":0,"tox_markout":0,"tox_burst":0,"tox_emergency":0,"opp":0.26,"opp_fill_rate":0.5,"opp_autocorr":0,"opp_vwap_dev":0.21,"opp_spread_bbo":0.23,"inv":0.24,"inv_pos_util":0,"inv_ctrl":0.81,"inv_duration":0,"budget":0.5}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759759-04:00","level":"INFO","msg":"[OptimizerV2] policy","coin":"xyz:CL","eval":1,"fired":"imb_sat_reduce","tgt_spread_buy":"0.0000","tgt_spread_sell":"0.0000","tgt_notional":"1.0000","tgt_skew":"1.0000","tgt_alpha":"0.0000","tgt_fill_decay":"1.0000","tgt_refresh":"1.0000","tgt_imb_shift":"0.7000"}`)
	p.ParseLine(`{"time":"2026-04-04T21:04:05.759796-04:00","level":"INFO","msg":"[OptimizerV2] utility","coin":"xyz:CL","eval":1,"utility":"0.0000","avg_fill_rate":"1.000","composite_markout":"0.00","history_len":1,"window":5,"ready":false}`)

	a := p.State.Assets["xyz:CL"]
	if a == nil {
		t.Fatal("expected asset xyz:CL")
	}
	if a.Optimizer.Eval != 1 {
		t.Errorf("eval: got %d", a.Optimizer.Eval)
	}
	if a.Optimizer.Policy != "imb_sat_reduce" {
		t.Errorf("policy: got %q", a.Optimizer.Policy)
	}
	if a.Optimizer.Utility != "0.0000" {
		t.Errorf("utility: got %q", a.Optimizer.Utility)
	}
	if a.Optimizer.Ready {
		t.Error("expected ready=false")
	}
	if a.Optimizer.Opp != 0.26 {
		t.Errorf("opp: got %f", a.Optimizer.Opp)
	}
}

func TestParseUnmatchedLine(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:03:32.83115-04:00","level":"INFO","msg":"market metadata","asset":"xyz:CL","szDecimals":3,"source":"hip3"}`)
	if p.State.UnmatchedCount != 1 {
		t.Errorf("expected unmatched count 1, got %d", p.State.UnmatchedCount)
	}
}

func TestParsePositionFlip(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T21:07:14.474341-04:00","level":"INFO","msg":"fill caused position sign flip","coin":"xyz:CL","old_side":"long","new_side":"short"}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventPositionFlip {
		t.Errorf("expected EventPositionFlip, got %d", events[0].Type)
	}
}

func TestParseRecon(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-04T00:13:39.850822-04:00","level":"WARN","msg":"reconciliation: order count mismatch, clearing tracking","coin":"xyz:BRENTOIL","tracked":10,"exchange":9}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	// WARN-level recon gets classified as EventWarn
	if events[0].Type != EventWarn {
		t.Errorf("expected EventWarn for WARN-level recon, got %d", events[0].Type)
	}
}

func TestParseReconMismatchDiagnostic(t *testing.T) {
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-13T10:00:00Z","level":"INFO","msg":"reconciliation mismatch diagnostic","coin":"xyz:CL","tracked_count":3,"exchange_count":2,"only_local":[100,101],"only_exchange":[200],"tracked_ages_ms":[1000,1000,1000],"exchange_ages_ms":[0,0],"generation":5,"generation_age_ms":1500}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventReconMismatch {
		t.Errorf("expected EventReconMismatch, got %d", events[0].Type)
	}
	if events[0].Coin != "xyz:CL" {
		t.Errorf("coin = %q, want xyz:CL", events[0].Coin)
	}
	if !strings.Contains(events[0].Summary, "tracked=3") || !strings.Contains(events[0].Summary, "exch=2") {
		t.Errorf("summary missing fields: %q", events[0].Summary)
	}
	if !strings.Contains(events[0].Summary, "only_local=2") || !strings.Contains(events[0].Summary, "only_exch=1") {
		t.Errorf("summary missing diff counts: %q", events[0].Summary)
	}
}

func TestParseReconMismatchDiagnosticWithEnrichedFields(t *testing.T) {
	p := NewParser()
	// v22 P10 fixup adds only_local_sides, only_local_prices, exchange_fetch_ok.
	// Parser must tolerate their presence without erroring.
	p.ParseLine(`{"time":"2026-04-13T10:00:00Z","level":"INFO","msg":"reconciliation mismatch diagnostic","coin":"xyz:CL","tracked_count":3,"exchange_count":2,"exchange_fetch_ok":true,"only_local":[100,101],"only_local_sides":["buy","sell"],"only_local_prices":[100.5,101.5],"only_exchange":[200],"tracked_ages_ms":[1000,1000,1000],"exchange_ages_ms":[0,0],"generation":5,"generation_age_ms":1500}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if events[0].Type != EventReconMismatch {
		t.Errorf("expected EventReconMismatch, got %d", events[0].Type)
	}
	if !strings.Contains(events[0].Summary, "only_local=2") {
		t.Errorf("summary missing diff counts: %q", events[0].Summary)
	}
}

func TestParseReconMismatchDiagnosticWithDriftBps(t *testing.T) {
	// V27: recon diagnostic now emits book_mid + only_local_drift_bps[].
	// Monitor surfaces max |drift| in the event summary.
	p := NewParser()
	p.ParseLine(`{"time":"2026-04-13T10:00:00Z","level":"INFO","msg":"reconciliation mismatch diagnostic","coin":"xyz:CL","tracked_count":2,"exchange_count":0,"only_local":[100,101],"only_local_drift_bps":[-12.5,3.2],"book_mid":89.5}`)

	events := p.State.Events.Items()
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	if !strings.Contains(events[0].Summary, "max_drift=12.5bps") {
		t.Errorf("summary missing max_drift: %q", events[0].Summary)
	}
}

func TestParseInvalidJSON(t *testing.T) {
	p := NewParser()
	p.ParseLine("this is not json")
	if p.State.UnmatchedCount != 1 {
		t.Errorf("expected unmatched count 1, got %d", p.State.UnmatchedCount)
	}
}
