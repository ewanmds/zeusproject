package main

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Parser processes JSON log lines into State.
type Parser struct {
	State *State
}

// NewParser creates a Parser with an initialized State.
func NewParser() *Parser {
	return &Parser{State: NewState()}
}

// ParseLine parses a single JSON log line and updates State.
func (p *Parser) ParseLine(line string) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &raw); err != nil {
		p.State.UnmatchedCount++
		return
	}

	msg := jsonString(raw, "msg")
	level := jsonString(raw, "level")
	ts := parseTime(jsonString(raw, "time"))
	coin := jsonString(raw, "coin")

	p.State.LastUpdate = ts

	switch msg {
	// --- STATUS block ---
	case "=== STATUS ===":
		eq := jsonFloat(raw, "equity")
		p.State.Portfolio.Equity = eq
		p.State.Portfolio.StatusEquity = eq
		p.State.Portfolio.HighWater = jsonString(raw, "high_water")
		if eq > 0 && (p.State.Portfolio.LowWater == 0 || eq < p.State.Portfolio.LowWater) {
			p.State.Portfolio.LowWater = eq
		}

	case "  pnl":
		p.State.Portfolio.SessionPnL = jsonString(raw, "session_pnl")
		p.State.Portfolio.SessionPnLBps = jsonString(raw, "session_pnl_bps")
		p.State.Portfolio.WindowPnLBps = jsonFloat(raw, "window_pnl_bps")
		p.State.Portfolio.EquityDelta = jsonString(raw, "equity_delta")

	case "  position":
		a := p.State.GetOrCreateAsset(coin)
		a.Position.Side = jsonString(raw, "side")
		a.Position.Size = jsonFloat(raw, "size")
		a.Position.Notional = jsonFloat(raw, "notional")
		// Snapshot signed position size (negative for short) for live MTM.
		signed := a.Position.Size
		if a.Position.Side == "short" || a.Position.Side == "sell" {
			signed = -signed
		}
		a.StatusPosSize = signed

	case "  orders":
		a := p.State.GetOrCreateAsset(coin)
		a.OrderCount = jsonInt(raw, "count")

	case "  quoting":
		a := p.State.GetOrCreateAsset(coin)
		// Snapshot book_mid as MTM baseline for live equity computation.
		if mid, err := strconv.ParseFloat(jsonString(raw, "book_mid"), 64); err == nil {
			a.StatusBookMid = mid
		}
		a.Quoting = QuotingState{
			BookMid:        jsonString(raw, "book_mid"),
			FairValue:      jsonString(raw, "fair_value"),
			BBOSpread:      jsonString(raw, "bbo_spread_bps"),
			Spread:         jsonString(raw, "spread_bps"),
			Skew:           jsonStringFallback(raw, "skew_entry_bps", "skew_bps"),
			Bias:           jsonString(raw, "bias_bps"),
			ImbalanceShift: jsonString(raw, "imbalance_shift_bps"),
			FillDecayShift: jsonString(raw, "fill_decay_shift_bps"),
			WickBid:        jsonString(raw, "wick_bid"),
			WickAsk:        jsonString(raw, "wick_ask"),
			ZWide:          jsonString(raw, "z_wide"),
			Warmup:         jsonBool(raw, "warmup"),
			Recovery:       jsonBool(raw, "recovery"),
			Degraded:       jsonBool(raw, "degraded"),
			Halted:         jsonBool(raw, "halted"),
			PairReduceOnly: jsonBool(raw, "pair_reduce_only"),
		}

	case "  sizing":
		a := p.State.GetOrCreateAsset(coin)
		a.Sizing = SizingState{
			InvUtil:           jsonString(raw, "inv_util"),
			EntryFactor:       jsonString(raw, "entry_size_factor"),
			AllowedBuy:        jsonString(raw, "allowed_buy"),
			AllowedSell:       jsonString(raw, "allowed_sell"),
			NotionalMult:      jsonString(raw, "notional_mult"),
			ScheduleMult:      jsonString(raw, "schedule_mult"),
			ToxBuyEwma:        jsonString(raw, "tox_buy_ewma"),
			ToxSellEwma:       jsonString(raw, "tox_sell_ewma"),
			ToxBuyMult:        jsonString(raw, "tox_buy_mult"),
			ToxSellMult:       jsonString(raw, "tox_sell_mult"),
			TrendGuardActive:  jsonBool(raw, "trend_guard"),
			TrendGuardSuppPct: jsonString(raw, "tg_suppress"),
			TrendGuardDevPct:  jsonString(raw, "tg_dev_pct"),
			FlowGateActive:    jsonBool(raw, "flow_gate"),
			FlowGateFactor:    jsonString(raw, "fg_factor"),
			PriceShockActive:  jsonBool(raw, "price_shock"),
			PriceShockSide:    jsonString(raw, "price_shock_side"),
			PriceShockBps:     jsonString(raw, "price_shock_bps"),
			WickSuppBid:       jsonBool(raw, "wick_supp_bid"),
			WickSuppAsk:       jsonBool(raw, "wick_supp_ask"),
			BidAbsentPct:      jsonString(raw, "bid_absent_pct"),
			AskAbsentPct:      jsonString(raw, "ask_absent_pct"),
			BothAbsentPct:     jsonString(raw, "both_absent_pct"),
			PresenceCycles:    jsonInt(raw, "presence_cycles"),
		}
		// Emit events on trend guard activation/deactivation transitions.
		if a.Sizing.TrendGuardActive && !a.PrevTrendGuard {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventTrendGuard,
				Summary: fmt.Sprintf("trend guard ON dev=%s%% suppress=%s", a.Sizing.TrendGuardDevPct, a.Sizing.TrendGuardSuppPct),
			})
		} else if !a.Sizing.TrendGuardActive && a.PrevTrendGuard {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventTrendGuard,
				Summary: "trend guard OFF",
			})
		}
		a.PrevTrendGuard = a.Sizing.TrendGuardActive
		// Emit events on flow gate activation/deactivation transitions.
		if a.Sizing.FlowGateActive && !a.PrevFlowGate {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventFlowGate,
				Summary: fmt.Sprintf("flow gate ON factor=%s", a.Sizing.FlowGateFactor),
			})
		} else if !a.Sizing.FlowGateActive && a.PrevFlowGate {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventFlowGate,
				Summary: "flow gate OFF",
			})
		}
		a.PrevFlowGate = a.Sizing.FlowGateActive
		if a.Sizing.PriceShockActive && !a.PrevPriceShock {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventPriceShock,
				Summary: fmt.Sprintf("price shock ON side=%s move=%sbps", a.Sizing.PriceShockSide, a.Sizing.PriceShockBps),
			})
		} else if !a.Sizing.PriceShockActive && a.PrevPriceShock {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventPriceShock,
				Summary: "price shock OFF",
			})
		}
		a.PrevPriceShock = a.Sizing.PriceShockActive

	case "  adaptive":
		a := p.State.GetOrCreateAsset(coin)
		a.Adaptive = AdaptiveState{
			SkewFactor:       jsonString(raw, "skew_factor"),
			VwapAlpha:        jsonString(raw, "vwap_alpha"),
			Autocorr:         jsonString(raw, "autocorr"),
			OptSpreadBuy:     jsonString(raw, "opt_spread_buy"),
			OptSpreadSell:    jsonString(raw, "opt_spread_sell"),
			OptNotionalMult:  jsonString(raw, "opt_notional_mult"),
			OptSkewMult:      jsonString(raw, "opt_skew_mult"),
			OptAlphaDelta:    jsonString(raw, "opt_alpha_delta"),
			OptImbShiftMult:  jsonString(raw, "opt_imb_shift_mult"),
			OptFillDecayMult: jsonString(raw, "opt_fill_decay_mult"),
			OptRefreshMult:   jsonString(raw, "opt_refresh_mult"),
			OptEval:          jsonInt(raw, "opt_eval"),
			BurstBuySpread:   jsonString(raw, "burst_buy_spread"),
			BurstSellSpread:  jsonString(raw, "burst_sell_spread"),
			VolBurstRatio:    jsonString(raw, "vol_burst_ratio"),
			VolBurstLevels:   jsonInt(raw, "vol_burst_levels"),
			VolBurstActive:   jsonBool(raw, "vol_burst_active"),
		}
		// Emit event on burst activation/deactivation transitions.
		prevBurst := a.PrevBurstActive
		nowBurst := a.Adaptive.VolBurstActive
		if nowBurst && !prevBurst {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventBurst,
				Summary: fmt.Sprintf("burst ON ratio=%s levels=%d", a.Adaptive.VolBurstRatio, a.Adaptive.VolBurstLevels),
			})
		} else if !nowBurst && prevBurst {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventBurst,
				Summary: "burst OFF",
			})
		}
		a.PrevBurstActive = nowBurst

	case "  vwap_short":
		a := p.State.GetOrCreateAsset(coin)
		a.VWAP.ShortVwap = jsonString(raw, "vwap")
		a.VWAP.Trades = jsonInt(raw, "trades")
		a.VWAP.TradeAge = jsonString(raw, "trade_age_ms")

	case "  vwap_long":
		a := p.State.GetOrCreateAsset(coin)
		a.VWAP.LongVwap = jsonString(raw, "vwap")

	case "  markout":
		// Status block markout summary (different from standalone markout event).
		// V30 part 5 added per-side composite EWMA. V32 added trend-mechanism
		// firing counters so dormant V31 mechanisms surface immediately.
		a := p.State.GetOrCreateAsset(coin)
		a.TrendMech.CompositeBuyBps = jsonString(raw, "buy_composite_bps")
		a.TrendMech.CompositeSellBps = jsonString(raw, "sell_composite_bps")
		a.TrendMech.FVShiftFired = jsonInt(raw, "trend_fv_shift_fired")
		a.TrendMech.AlphaBoostFired = jsonInt(raw, "alpha_boost_fired")
		a.TrendMech.EntryAccelFired = jsonInt(raw, "entry_accel_fired")

	case "  markout_ewma_snapshot":
		// V30 part 6: per-status per-asset horizon EWMA snapshot. Overlaps
		// with the "  markout" status line (same composite values, expanded
		// per-horizon + per-side fill counts). Acknowledged here so the line
		// does not fall through to UnmatchedCount; TUI surfacing can be added
		// later if a dedicated panel is needed.

	case "  fills":
		// Per-asset per-status window summary of buy/sell counts, notional,
		// fees, and avg capture bps. Already tracked per-fill via the
		// "fill detail" / "fill poller: new fill" events (which feed
		// p.State.Fills + Events). Acknowledged here to avoid UnmatchedCount;
		// a dedicated per-asset window panel can be added later if desired.

	case "  rate_limit_allocator":
		// Per-asset ROI scores from the multi-asset rate-limit allocator
		// (Smart Layer 5). No-op when `rate_limit_allocator_enabled: false`.
		// Acknowledged here to avoid UnmatchedCount; surfacing can be added
		// later if a multi-asset budget panel is needed.

	case "  order":
		// Verbose-only per-order diagnostic line
		// (emitted when logging.verbose: true). Per-order resting state is
		// not surfaced in the TUI; counts come from "  orders".

	case "  vwap windows":
		// One-shot startup line listing per-asset VWAP window sizes. Not
		// useful once the run is live; acknowledged to avoid UnmatchedCount.

	case "  api_requests":
		p.State.APIRequests = APIRequestsState{
			WindowSec: jsonInt(raw, "window_sec"),
			Total:     jsonInt(raw, "total"),
			Exchange:  jsonInt(raw, "exchange"),
			Info:      jsonInt(raw, "info"),
			AvgRPS:    jsonString(raw, "avg_rps"),
		}

	case "  rate_limit":
		p.State.RateLimit.Remaining = jsonInt(raw, "remaining")
		p.State.RateLimit.Pct = jsonString(raw, "pct")
		p.State.RateLimit.Tier = jsonString(raw, "tier")
		p.State.RateLimit.Halted = jsonBool(raw, "halted")
		p.State.RateLimit.SessionUsed = jsonInt(raw, "session_used")
		p.State.RateLimit.SessionGenerated = jsonInt(raw, "session_generated")
		p.State.RateLimit.SessionNet = jsonInt(raw, "session_net")
		p.State.RateLimit.Depletion = jsonString(raw, "depletion")
		p.State.RateLimit.TteMin = jsonString(raw, "tte_min")

	case "  virt_cycle":
		p.State.RateLimit.Headroom = jsonString(raw, "headroom")
		p.State.RateLimit.BurnRate = jsonString(raw, "burn_rate_calls_min")
		p.State.RateLimit.VolumeRateUSD = jsonString(raw, "volume_rate_usd_min")
		p.State.RateLimit.CumVolumeUSD = jsonString(raw, "cum_volume_usd")

	case "rate limit status":
		p.State.RateLimit.StartRemaining = jsonInt(raw, "remaining")

	case "  fees":
		p.State.Portfolio.TotalExchangeFees = jsonString(raw, "total_exchange_fees")
		p.State.Portfolio.TotalBuilderFees = jsonString(raw, "total_builder_fees")
		p.State.Portfolio.TotalAllFees = jsonString(raw, "total_all_fees")

	// --- Non-status events ---
	case "markout":
		a := p.State.GetOrCreateAsset(coin)
		a.Markouts.Add(Markout{
			Side:   jsonString(raw, "side"),
			Size:   jsonString(raw, "size"),
			T500ms: jsonString(raw, "T500ms_bps"),
			T1s:    jsonString(raw, "T1s_bps"),
			T2s:    jsonString(raw, "T2s_bps"),
			T5s:    jsonString(raw, "T5s_bps"),
		})

	case "fill poller: new fill":
		side := jsonString(raw, "side")
		p.State.Fills.Total++
		if side == "buy" {
			p.State.Fills.Buy++
		} else {
			p.State.Fills.Sell++
		}

	case "fill detail":
		side := jsonString(raw, "side")
		size := jsonStringRaw(raw, "size")
		price := jsonStringRaw(raw, "price")
		notional := jsonString(raw, "notional")
		capture := jsonString(raw, "capture_bps")
		fee := jsonString(raw, "fee")
		realizedPnL := jsonString(raw, "realized_pnl")
		netPnL := jsonString(raw, "net_pnl")
		wickMult := jsonString(raw, "wick_mult")
		// Track notional volume.
		if nv, err := strconv.ParseFloat(notional, 64); err == nil {
			if side == "buy" {
				p.State.Fills.BuyNotional += nv
			} else {
				p.State.Fills.SellNotional += nv
			}
		}
		// Update latest fill price for live mid fallback.
		if px, err := strconv.ParseFloat(price, 64); err == nil && px > 0 {
			a := p.State.GetOrCreateAsset(coin)
			a.LastFillPrice = px
			a.LastMidUpdate = ts
		}
		summary := fmt.Sprintf("%s %s @ %s ($%s)  capture %sbps", side, size, price, notional, capture)
		if feeVal, err := strconv.ParseFloat(fee, 64); err == nil && feeVal > 0 {
			summary += fmt.Sprintf("  fee $%.4f", feeVal)
		}
		// Show realized PnL when the fill closed part of a position.
		var parsedNetPnL float64
		if rpnl, err := strconv.ParseFloat(realizedPnL, 64); err == nil && rpnl != 0 {
			summary += fmt.Sprintf("  pnl $%.4f", rpnl)
			if npnl, err2 := strconv.ParseFloat(netPnL, 64); err2 == nil {
				summary += fmt.Sprintf(" (net $%.4f)", npnl)
				parsedNetPnL = npnl
			}
		}
		if wickMult != "" && wickMult != "1.00" {
			summary += fmt.Sprintf("  wick %sx", wickMult)
		}
		if capVal, err := strconv.ParseFloat(capture, 64); err == nil && capVal >= 10 {
			summary += " \U0001F525" // fire emoji
		}
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventFill,
			Side:    side,
			NetPnL:  parsedNetPnL,
			Summary: summary,
		})

	case "fill-triggered requote":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRequote,
			Summary: msg,
		})

	case "fill caused position sign flip":
		oldSide := jsonString(raw, "old_side")
		newSide := jsonString(raw, "new_side")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventPositionFlip,
			Summary: fmt.Sprintf("%s -> %s", oldSide, newSide),
		})

	case "starting hyperliquid market maker":
		p.State.Session.StartTime = ts
		p.State.Session.BotMode = jsonString(raw, "mode")
		p.State.Session.CommitHash = jsonString(raw, "commit")
		p.State.Portfolio.MaxDrawdownPct = jsonFloat(raw, "max_drawdown_pct")
		// Parse assets array
		if v, ok := raw["assets"]; ok {
			var assets []string
			if err := json.Unmarshal(v, &assets); err == nil {
				p.State.Session.Assets = assets
			}
		}

	case "bootstrapped equity":
		eq := jsonFloat(raw, "equity")
		p.State.Portfolio.Equity = eq
		p.State.Portfolio.StartEquity = eq

	case "asset config: risk":
		floor := jsonFloat(raw, "absolute_equity_floor")
		if floor > 0 {
			p.State.Portfolio.EquityFloor = floor
		}
		aggExp := jsonFloat(raw, "max_aggregate_exposure")
		if aggExp > p.State.Portfolio.MaxExposure {
			p.State.Portfolio.MaxExposure = aggExp
		}
		spreadExp := jsonFloat(raw, "max_spread_exposure")
		if spreadExp > p.State.Portfolio.MaxExposure && aggExp == 0 {
			p.State.Portfolio.MaxExposure = spreadExp
		}

	// --- Optimizer ---
	case "[OptimizerV2] state":
		a := p.State.GetOrCreateAsset(coin)
		a.Optimizer.Eval = jsonInt(raw, "eval")
		a.Optimizer.Tox = jsonFloat(raw, "tox")
		a.Optimizer.Opp = jsonFloat(raw, "opp")
		a.Optimizer.Inv = jsonFloat(raw, "inv")
		a.Optimizer.Budget = jsonFloat(raw, "budget")
		if label := jsonString(raw, "fourth_dim_label"); label != "" {
			a.Optimizer.FourthDim = label
		}

	case "[OptimizerV2] policy":
		a := p.State.GetOrCreateAsset(coin)
		a.Optimizer.Eval = jsonInt(raw, "eval")
		fired := jsonString(raw, "fired")
		a.Optimizer.Policy = fired
		if fired != "" && fired != "none" {
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventOptimizer,
				Summary: fmt.Sprintf("policy: %s", fired),
			})
		}

	case "[OptimizerV2] utility":
		a := p.State.GetOrCreateAsset(coin)
		a.Optimizer.Eval = jsonInt(raw, "eval")
		a.Optimizer.Utility = jsonString(raw, "utility")
		a.Optimizer.Ready = jsonBool(raw, "ready")

	case "[OptimizerV2] graduated rollback":
		rate := jsonString(raw, "rate")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizer,
			Summary: fmt.Sprintf("rollback rate=%s", rate),
		})

	case "[OptimizerV2] emergency ENTER":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizer,
			Summary: fmt.Sprintf("EMERGENCY tox=%.2f", jsonFloat(raw, "toxicity")),
		})

	case "[OptimizerV2] emergency EXIT":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizer,
			Summary: fmt.Sprintf("emergency exit tox=%.2f", jsonFloat(raw, "toxicity")),
		})

	case "[OptimizerV2] emergency widen":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizer,
			Summary: "emergency widen",
		})

	case "[OptimizerV2] rollback floor":
		// V32.2: dynamic floor relaxation event — only emitted when the
		// session-PnL-driven relaxation is non-zero, so each occurrence is
		// signal that the floor moved off its base value.
		base := jsonString(raw, "base_floor")
		eff := jsonString(raw, "effective_floor")
		relax := jsonString(raw, "relaxation_bps")
		pnl := jsonString(raw, "session_pnl_bps")
		breached := jsonBool(raw, "breached")
		summary := fmt.Sprintf("floor %s→%s (relax=%s pnl=%s breached=%v)", base, eff, relax, pnl, breached)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizer,
			Summary: summary,
		})

	case "optimizer deadband skip":
		maxDelta := jsonFloat(raw, "max_delta_abs")
		summary := fmt.Sprintf("deadband skip max_delta_abs=%.3f", maxDelta)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventOptimizerDeadband,
			Summary: summary,
		})

	// --- Risk actions (critical) ---
	case "risk action", "risk action (forced)":
		action := jsonString(raw, "action")
		previous := jsonString(raw, "previous")
		evType := EventRisk
		if action == "RESUME" || action == "RESUME_FROM_DRAWDOWN" {
			evType = EventRecovery
		}
		summary := fmt.Sprintf("%s (was %s)", action, previous)
		if msg == "risk action (forced)" {
			summary = "FORCED " + summary
		}
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    evType,
			Summary: summary,
		})

	// --- P3: Session stop-loss ---
	case "session stop-loss triggered":
		bps := jsonFloat(raw, "session_pnl_bps")
		elapsed := jsonFloat(raw, "elapsed_sec")
		p.State.Portfolio.SessionStopActive = true
		p.State.Portfolio.SessionStopReason = "triggered"
		summary := fmt.Sprintf("STOP LOSS pnl=%.1fbps elapsed=%.0fs", bps, elapsed)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventSessionStopLoss,
			Summary: summary,
		})
	case "session stop-loss pause expired":
		p.State.Portfolio.SessionStopActive = false
		p.State.Portfolio.SessionStopReason = "recovery"
		pause := jsonFloat(raw, "pause_sec")
		recDur := jsonFloat(raw, "recovery_duration_sec")
		summary := fmt.Sprintf("pause expired (%.0fs), recovery %.0fs", pause, recDur)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventSessionStopLoss,
			Summary: summary,
		})
	case "session stop-loss reset":
		p.State.Portfolio.SessionStopReason = ""
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventSessionStopLoss,
			Summary: "tracker reset",
		})

	// --- V30: Shock-chain cascade detection ---
	case "shock chain threshold crossed — flatten + pause":
		count := jsonFloat(raw, "shock_count")
		threshold := jsonFloat(raw, "threshold")
		windowSec := jsonFloat(raw, "window_sec")
		pauseSec := jsonFloat(raw, "pause_sec")
		recoverySec := jsonFloat(raw, "recovery_sec")
		moveBps := jsonString(raw, "trigger_move_bps")
		p.State.Portfolio.ShockChainActive = true
		p.State.Portfolio.ShockChainReason = "paused"
		summary := fmt.Sprintf("CASCADE %.0fshocks/%.0fs move=%s pause=%.0fs rec=%.0fs",
			count, windowSec, moveBps, pauseSec, recoverySec)
		_ = threshold
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventShockChain,
			Summary: summary,
		})
	case "shock chain pause expired, entering recovery":
		p.State.Portfolio.ShockChainReason = "recovery"
		recDur := jsonFloat(raw, "recovery_sec")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventShockChain,
			Summary: fmt.Sprintf("pause expired, recovery %.0fs", recDur),
		})
	case "shock chain pause expired, resuming normal":
		p.State.Portfolio.ShockChainActive = false
		p.State.Portfolio.ShockChainReason = ""
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventShockChain,
			Summary: "pause expired, resumed",
		})
	case "shock chain count":
		count := jsonFloat(raw, "count")
		threshold := jsonFloat(raw, "threshold")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventShockChain,
			Summary: fmt.Sprintf("chain count %.0f/%.0f", count, threshold),
		})

	// --- V30: Fractional IOC on favorable shock (execution events) ---
	case "favorable shock IOC executed":
		direction := jsonString(raw, "direction")
		moveBps := jsonString(raw, "move_bps")
		frac := jsonFloat(raw, "frac")
		filled := jsonFloat(raw, "filled_size")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventPriceShock,
			Summary: fmt.Sprintf("IOC %s move=%s frac=%.2f filled=%.4f", direction, moveBps, frac, filled),
		})
	case "reduce position IOC completed":
		target := jsonFloat(raw, "target_size")
		filled := jsonFloat(raw, "filled_size")
		reasonStr := jsonString(raw, "reason")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventPriceShock,
			Summary: fmt.Sprintf("reduce-IOC target=%.4f filled=%.4f (%s)", target, filled, reasonStr),
		})

	case "idle-fill breaker fired":
		side := jsonString(raw, "side")
		invUtil := jsonFloat(raw, "inv_util")
		idleSec := jsonFloat(raw, "idle_sec")
		tighten := jsonFloat(raw, "tighten_bps")
		summary := fmt.Sprintf("idle breaker %s inv=%.2f idle=%.0fs tighten=%.2fbps", side, invUtil, idleSec, tighten)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Side:    side,
			Type:    EventIdleFillBreaker,
			Summary: summary,
		})

	case "markout persistence exit fired":
		// V32.2: active position killer for drift-shaped adversity. Fires
		// when composite markout sits below threshold for the configured
		// duration AND inv_util is above the floor.
		side := jsonString(raw, "side")
		invUtil := jsonFloat(raw, "inv_util")
		composite := jsonFloat(raw, "composite_markout_bps")
		threshold := jsonFloat(raw, "threshold_bps")
		persistSec := jsonFloat(raw, "persist_sec")
		tighten := jsonFloat(raw, "tighten_bps")
		summary := fmt.Sprintf("markout-persist exit %s inv=%.2f cm=%.2f<%.2f persist=%.0fs tighten=%.2fbps",
			side, invUtil, composite, threshold, persistSec, tighten)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Side:    side,
			Type:    EventMarkoutPersistExit,
			Summary: summary,
		})

	case "quantile normalizer warm":
		subdim := jsonString(raw, "subdim")
		summary := fmt.Sprintf("quantile warm: %s", subdim)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventQuantileNormalizer,
			Summary: summary,
		})

	case "quantile normalizer snapshot":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventQuantileNormalizer,
			Summary: "quantile snapshot",
		})

	case "per-side winrate gate":
		side := jsonString(raw, "side")
		wr := jsonFloat(raw, "winrate")
		pf := jsonFloat(raw, "pf")
		samples := jsonFloat(raw, "samples")
		widen := jsonFloat(raw, "widen_bps")
		summary := fmt.Sprintf("winrate gate %s wr=%.2f pf=%.2f n=%.0f widen=%.1fbps", side, wr, pf, samples, widen)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Side:    side,
			Type:    EventPerSideGate,
			Summary: summary,
		})

	case "per-side winrate gate cleared":
		side := jsonString(raw, "side")
		wr := jsonFloat(raw, "winrate")
		pf := jsonFloat(raw, "pf")
		samples := jsonFloat(raw, "samples")
		summary := fmt.Sprintf("winrate gate %s CLEARED wr=%.2f pf=%.2f n=%.0f", side, wr, pf, samples)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Side:    side,
			Type:    EventPerSideGate,
			Summary: summary,
		})

	// --- Position closes (critical) ---
	case "position closed", "position closed via passive order", "position closed via limit IOC":
		reason := jsonString(raw, "reason")
		summary := fmt.Sprintf("position closed: %s", reason)
		if msg == "position closed via passive order" {
			summary = fmt.Sprintf("position closed (passive): %s", reason)
		} else if msg == "position closed via limit IOC" {
			slip := jsonString(raw, "slippage_multiplier")
			summary = fmt.Sprintf("position closed (IOC slip=%s): %s", slip, reason)
		}
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRisk,
			Summary: summary,
		})

	// --- Rate limit (critical) ---
	case "rate limit tier changed":
		oldTier := jsonString(raw, "old_tier")
		newTier := jsonString(raw, "new_tier")
		remaining := jsonStringRaw(raw, "remaining")
		tte := jsonString(raw, "tte_minutes")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("rate limit: %s -> %s (remaining=%s, tte=%sm)", oldTier, newTier, remaining, tte),
		})

	case "rate limit recovered from critical, entering warmup":
		remaining := jsonStringRaw(raw, "remaining")
		tte := jsonString(raw, "tte_minutes")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: fmt.Sprintf("rate limit recovered from critical (remaining=%s, tte=%sm)", remaining, tte),
		})

	case "rate limit CRITICAL: cancelling all orders and closing positions":
		remaining := jsonStringRaw(raw, "remaining")
		trigger := jsonString(raw, "trigger")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRisk,
			Summary: fmt.Sprintf("RATE LIMIT CRITICAL: remaining=%s trigger=%s", remaining, trigger),
		})

	// --- Risk resume / recovery (high) ---
	case "quoting resumed (risk, non-drawdown)":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "quoting resumed (non-drawdown)",
		})

	case "quoting resumed (risk, drawdown recovery)":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "quoting resumed (drawdown recovery)",
		})

	case "entering recovery mode after drawdown resume":
		dur := jsonStringRaw(raw, "duration_sec")
		notionalPct := jsonFloat(raw, "notional_pct")
		spreadMult := jsonFloat(raw, "spread_mult")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: fmt.Sprintf("recovery mode: %ss duration, %.0f%% notional, %.1fx spread", dur, notionalPct*100, spreadMult),
		})

	// --- Reconciliation (high) ---
	case "reconciliation: inferred synthetic fill":
		side := jsonString(raw, "side")
		size := jsonStringRaw(raw, "size")
		price := jsonStringRaw(raw, "price")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("synthetic fill: %s %s @ %s", side, size, price),
		})

	case "reconciliation: position mismatch, updating":
		localSide := jsonString(raw, "local_side")
		localSize := jsonString(raw, "local_size")
		chainSide := jsonString(raw, "chain_side")
		chainSize := jsonString(raw, "chain_size")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("recon mismatch: local %s/%s -> chain %s/%s", localSide, localSize, chainSide, chainSize),
		})

	case "reconciliation: local position not on chain, clearing":
		localSide := jsonString(raw, "local_side")
		localSize := jsonString(raw, "local_size")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("recon: local %s/%s not on chain, clearing", localSide, localSize),
		})

	case "reconciliation: consecutive soft mismatches exceeded threshold, cancelling orders":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: "recon: soft mismatch threshold exceeded, cancelling",
		})

	case "reconciliation: drift within Zeus order-size cap, treating as soft correction":
		delta := jsonFloat(raw, "delta_notional")
		orderCap := jsonFloat(raw, "order_cap")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("recon: zeus soft drift %.2f (cap %.2f)", delta, orderCap),
		})

	case "reconciliation mismatch diagnostic":
		tracked := jsonInt(raw, "tracked_count")
		exchange := jsonInt(raw, "exchange_count")
		onlyLocal := jsonIntArray(raw, "only_local")
		onlyExchange := jsonIntArray(raw, "only_exchange")
		driftBps := jsonFloatArray(raw, "only_local_drift_bps") // V27: stale-order hypothesis
		maxAbsDrift := 0.0
		for _, d := range driftBps {
			if d > maxAbsDrift {
				maxAbsDrift = d
			} else if -d > maxAbsDrift {
				maxAbsDrift = -d
			}
		}
		summary := fmt.Sprintf("recon mismatch diag tracked=%d exch=%d only_local=%d only_exch=%d max_drift=%.1fbps",
			tracked, exchange, len(onlyLocal), len(onlyExchange), maxAbsDrift)
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventReconMismatch,
			Summary: summary,
		})

	// --- Health state (high) ---
	case "health halt auto-recovered after 2 consecutive healthy reconciliations":
		reason := jsonString(raw, "previous_reason")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: fmt.Sprintf("health auto-recovered (was: %s)", reason),
		})

	case "health recovered: resuming quotes":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "health recovered: resuming quotes",
		})

	case "health no longer degraded":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "health no longer degraded",
		})

	case "health halt: suppressing quotes":
		reason := jsonString(raw, "reason")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRisk,
			Summary: fmt.Sprintf("health halt: %s", reason),
		})

	case "health degraded: widening spreads":
		reason := jsonString(raw, "reason")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("health degraded: %s", reason),
		})

	// --- Auto-recovery (high) ---
	case "attempting auto-recovery from fail-closed halt":
		retries := jsonStringRaw(raw, "retry_count")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("auto-recovery attempt #%s", retries),
		})

	case "auto-recovery successful, resetting fail-closed counter":
		prev := jsonStringRaw(raw, "previous_retries")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: fmt.Sprintf("auto-recovery successful (after %s retries)", prev),
		})

	// --- Feed recovery (medium) ---
	case "book data fresh again, resuming quoting":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "book data fresh, resuming",
		})

	case "feed recovered from death":
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRecovery,
			Summary: "feed recovered from death",
		})

	// --- Enhanced warn messages (structured attributes) ---
	case "equity below floor, awaiting confirmation":
		equity := jsonFloat(raw, "smoothed_equity")
		floor := jsonFloat(raw, "floor")
		breach := jsonStringRaw(raw, "breach_count")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRisk,
			Summary: fmt.Sprintf("equity below floor: %.2f < %.0f (breach #%s)", equity, floor, breach),
		})

	case "position notional at/above limit":
		notional := jsonFloat(raw, "notional")
		limit := jsonFloat(raw, "limit")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("position at limit: $%.1f / $%.0f", notional, limit),
		})

	case "spread exposure at/above per-asset limit":
		notional := jsonFloat(raw, "notional")
		limit := jsonFloat(raw, "limit")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("spread exposure at limit: $%.1f / $%.0f", notional, limit),
		})

	case "aggregate spread exposure at/above limit":
		totalNotional := jsonFloat(raw, "total_notional")
		limit := jsonFloat(raw, "limit")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("aggregate exposure at limit: $%.1f / $%.0f", totalNotional, limit),
		})

	case "fair value diverged from book mid, clamping to mid":
		fv := jsonString(raw, "fair_value")
		mid := jsonString(raw, "mid")
		devPct := jsonString(raw, "dev_pct")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("FV clamped: fv=%s mid=%s dev=%s%%", fv, mid, devPct),
		})

	case "book data stale, cancelling orders and suppressing quotes":
		age := jsonString(raw, "age_ms")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("book stale (%sms), suppressing quotes", age),
		})

	case "regime gate activated":
		dir := jsonInt(raw, "dir")
		p.State.Portfolio.RegimeGateActive = true
		p.State.Portfolio.RegimeDir = dir
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRegimeGate,
			Summary: fmt.Sprintf("REGIME GATE ON dir=%+d", dir),
		})

	case "regime gate deactivated":
		p.State.Portfolio.RegimeGateActive = false
		p.State.Portfolio.RegimeDir = 0
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventRegimeGate,
			Summary: "regime gate off",
		})

	case "regime gate heartbeat":
		dir := jsonInt(raw, "dir")
		p.State.Portfolio.RegimeGateActive = true
		p.State.Portfolio.RegimeDir = dir

	case "price shock guard activated":
		side := jsonString(raw, "entry_side")
		move := jsonString(raw, "move_bps")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventPriceShock,
			Summary: fmt.Sprintf("price shock side=%s move=%sbps", side, move),
		})

	case "wall_quality state":
		side := jsonString(raw, "side")
		from := jsonString(raw, "from")
		to := jsonString(raw, "to")
		score := jsonFloat(raw, "score")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWallQualityState,
			Side:    side,
			Summary: fmt.Sprintf("wall_quality %s->%s score=%.2f", from, to, score),
		})

	case "schedule window entered":
		label := jsonString(raw, "label")
		mult := jsonFloat(raw, "multiplier")
		p.State.Portfolio.ScheduleActive = true
		p.State.Portfolio.ScheduleLabel = label
		p.State.Portfolio.ScheduleMultiplier = mult
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventScheduleWindow,
			Summary: fmt.Sprintf("schedule enter %s x%.2f", label, mult),
		})

	case "schedule window exited":
		label := jsonString(raw, "label")
		p.State.Portfolio.ScheduleActive = false
		p.State.Portfolio.ScheduleLabel = ""
		p.State.Portfolio.ScheduleMultiplier = 1.0
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventScheduleWindow,
			Summary: fmt.Sprintf("schedule exit %s", label),
		})

	case "vwap_alpha drift fade":
		base := jsonFloat(raw, "base_alpha")
		effective := jsonFloat(raw, "effective_alpha")
		divergence := jsonFloat(raw, "divergence")
		p.State.Portfolio.EffectiveVWAPAlpha = effective
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventVWAPAlphaFade,
			Summary: fmt.Sprintf("vwap_alpha fade %.2f->%.2f div=%.2f", base, effective, divergence),
		})

	case "quote attribution":
		// V31/V32 diagnostic quote breakdown. Capture trend_fv_shift_bps so the
		// Adaptive panel can show whether the trend FV drift mechanism is
		// actually biasing FV or sitting at zero. Other attribution fields
		// overlap with existing status-line state.
		a := p.State.GetOrCreateAsset(coin)
		a.TrendMech.FVShiftBps = jsonString(raw, "trend_fv_shift_bps")

	case "post-mortem written":
		path := jsonString(raw, "path")
		reason := jsonString(raw, "reason")
		sizeBytes := jsonInt(raw, "size_bytes")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Type:    EventPostMortem,
			Summary: fmt.Sprintf("post-mortem %s %s (%d bytes)", reason, path, sizeBytes),
		})

	case "suppressing fill position update during quote halt":
		side := jsonString(raw, "side")
		size := jsonString(raw, "size")
		p.State.Events.Add(Event{
			Time:    ts,
			Level:   level,
			Coin:    coin,
			Type:    EventWarn,
			Summary: fmt.Sprintf("fill suppressed during halt: %s %s", side, size),
		})

	default:
		// Wall-front snap prefix check
		if strings.HasPrefix(msg, "wall-front snap") {
			a := p.State.GetOrCreateAsset(coin)
			snapPx := jsonFloat(raw, "snap_price")
			side := jsonString(raw, "side")
			if snapPx > 0 {
				switch side {
				case "buy":
					a.LastBuyWall = snapPx
				case "sell":
					a.LastSellWall = snapPx
				}
				a.LastMidUpdate = ts
			}
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventWallSnap,
				Summary: msg,
			})
			return
		}

		// Level-based fallbacks
		switch level {
		case "ERROR":
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventError,
				Summary: msg,
			})
		case "WARN":
			p.State.Events.Add(Event{
				Time:    ts,
				Level:   level,
				Coin:    coin,
				Type:    EventWarn,
				Summary: msg,
			})
		default:
			p.State.UnmatchedCount++
		}
	}
}

// --- JSON helpers ---

func jsonString(raw map[string]json.RawMessage, key string) string {
	v, ok := raw[key]
	if !ok {
		return ""
	}
	var s string
	if err := json.Unmarshal(v, &s); err == nil {
		return s
	}
	// Fallback: trim quotes from raw value (handles numbers serialized as JSON)
	return strings.Trim(string(v), "\"")
}

// jsonStringFallback tries the primary key first, then fallback keys in order.
// Returns the first non-empty value, or "" if none match. Used for field renames
// where older logs still use the previous key name.
func jsonStringFallback(raw map[string]json.RawMessage, primary string, fallbacks ...string) string {
	if s := jsonString(raw, primary); s != "" {
		return s
	}
	for _, k := range fallbacks {
		if s := jsonString(raw, k); s != "" {
			return s
		}
	}
	return ""
}

// jsonStringRaw returns the raw JSON value as a string, handling both
// quoted strings and unquoted numbers.
func jsonStringRaw(raw map[string]json.RawMessage, key string) string {
	v, ok := raw[key]
	if !ok {
		return ""
	}
	return strings.Trim(string(v), "\"")
}

func jsonFloat(raw map[string]json.RawMessage, key string) float64 {
	v, ok := raw[key]
	if !ok {
		return 0
	}
	var f float64
	if err := json.Unmarshal(v, &f); err != nil {
		return 0
	}
	return f
}

func jsonInt(raw map[string]json.RawMessage, key string) int {
	v, ok := raw[key]
	if !ok {
		return 0
	}
	var i int
	if err := json.Unmarshal(v, &i); err != nil {
		return 0
	}
	return i
}

func jsonIntArray(raw map[string]json.RawMessage, key string) []int64 {
	v, ok := raw[key]
	if !ok {
		return nil
	}
	var a []int64
	if err := json.Unmarshal(v, &a); err != nil {
		return nil
	}
	return a
}

func jsonFloatArray(raw map[string]json.RawMessage, key string) []float64 {
	v, ok := raw[key]
	if !ok {
		return nil
	}
	var a []float64
	if err := json.Unmarshal(v, &a); err != nil {
		return nil
	}
	return a
}

func jsonBool(raw map[string]json.RawMessage, key string) bool {
	v, ok := raw[key]
	if !ok {
		return false
	}
	var b bool
	if err := json.Unmarshal(v, &b); err != nil {
		return false
	}
	return b
}

func parseTime(s string) time.Time {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}
	}
	return t
}
