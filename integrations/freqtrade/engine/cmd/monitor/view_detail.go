package main

import (
	"fmt"
	"math"
	"strconv"
	"strings"
)

func renderDetailPanel(s *State, a *AssetState, width int) string {
	if a == nil {
		return styleLabel.Render("  Select an asset")
	}

	var b strings.Builder

	b.WriteString(styleSectionHeader.Render("Quoting") + "\n")
	b.WriteString(fmt.Sprintf("  mid=%s  fv=%s  bbo=%sbps\n",
		val(a.Quoting.BookMid), val(a.Quoting.FairValue), val(a.Quoting.BBOSpread)))
	b.WriteString(fmt.Sprintf("  spread=%sbps  skew=%s  bias=%s  imb=%sbps\n",
		val(a.Quoting.Spread), val(a.Quoting.Skew), val(a.Quoting.Bias), val(a.Quoting.ImbalanceShift)))
	b.WriteString(fmt.Sprintf("  fill_decay=%sbps  wick_bid=%s  wick_ask=%s  z_wide=%s\n",
		val(a.Quoting.FillDecayShift), val(a.Quoting.WickBid), val(a.Quoting.WickAsk), val(a.Quoting.ZWide)))

	flags := renderFlags(a)
	if flags != "" {
		b.WriteString(fmt.Sprintf("  %s\n", flags))
	}

	b.WriteString(styleSectionHeader.Render("Sizing") + "\n")
	b.WriteString(fmt.Sprintf("  inv_util=%s  entry_factor=%s  buy=%s  sell=%s\n",
		val(a.Sizing.InvUtil), val(a.Sizing.EntryFactor), val(a.Sizing.AllowedBuy), val(a.Sizing.AllowedSell)))
	b.WriteString(fmt.Sprintf("  notional_mult=%s  schedule_mult=%s  tox_buy=%s  tox_sell=%s\n",
		val(a.Sizing.NotionalMult), val(a.Sizing.ScheduleMult), val(a.Sizing.ToxBuyEwma), val(a.Sizing.ToxSellEwma)))
	b.WriteString(fmt.Sprintf("  tox_buy_mult=%s  tox_sell_mult=%s\n",
		val(a.Sizing.ToxBuyMult), val(a.Sizing.ToxSellMult)))
	tgStr := "off"
	if a.Sizing.TrendGuardActive {
		tgStr = styleValueWarn.Render(fmt.Sprintf("ON suppress=%s dev=%s%%", val(a.Sizing.TrendGuardSuppPct), val(a.Sizing.TrendGuardDevPct)))
	}
	fgStr := "off"
	if a.Sizing.FlowGateActive {
		fgStr = styleValueWarn.Render(fmt.Sprintf("ON factor=%s", val(a.Sizing.FlowGateFactor)))
	}
	psStr := "off"
	if a.Sizing.PriceShockActive {
		psStr = styleValueNegative.Render(fmt.Sprintf("ON %s %sbps", val(a.Sizing.PriceShockSide), val(a.Sizing.PriceShockBps)))
	}
	wickStr := "none"
	if a.Sizing.WickSuppBid || a.Sizing.WickSuppAsk {
		wickStr = fmt.Sprintf("bid=%v ask=%v", a.Sizing.WickSuppBid, a.Sizing.WickSuppAsk)
	}
	b.WriteString(fmt.Sprintf("  trend_guard: %s  flow_gate: %s\n", tgStr, fgStr))
	b.WriteString(fmt.Sprintf("  price_shock: %s  wick_supp: %s\n", psStr, wickStr))
	// Side-presence: % of quote cycles where a side produced zero orders
	// (sustained >0% means stacked suppression is collapsing one side).
	if a.Sizing.PresenceCycles > 0 {
		presenceStr := fmt.Sprintf("bid=%s%% ask=%s%% both=%s%% (n=%d)",
			val(a.Sizing.BidAbsentPct), val(a.Sizing.AskAbsentPct),
			val(a.Sizing.BothAbsentPct), a.Sizing.PresenceCycles)
		bidOut, _ := strconv.ParseFloat(a.Sizing.BidAbsentPct, 64)
		askOut, _ := strconv.ParseFloat(a.Sizing.AskAbsentPct, 64)
		if bidOut >= 25.0 || askOut >= 25.0 {
			presenceStr = styleValueWarn.Render(presenceStr)
		}
		b.WriteString(fmt.Sprintf("  side_absent: %s\n", presenceStr))
	}

	b.WriteString(styleSectionHeader.Render("VWAP") + "\n")
	b.WriteString(fmt.Sprintf("  short=%s  long=%s  trades=%d  age=%sms\n",
		val(a.VWAP.ShortVwap), val(a.VWAP.LongVwap), a.VWAP.Trades, val(a.VWAP.TradeAge)))

	markouts := a.Markouts.Items()
	b.WriteString(styleSectionHeader.Render(fmt.Sprintf("Markout (last %d)", len(markouts))) + "\n")
	if len(markouts) == 0 {
		b.WriteString("  (no fills yet)\n")
	} else {
		b.WriteString(fmt.Sprintf("  %-5s %8s %8s %8s %8s %8s\n", "side", "size", "T500ms", "T1s", "T2s", "T5s"))
		limit := len(markouts)
		if limit > 8 {
			limit = 8
		}
		for _, m := range markouts[:limit] {
			b.WriteString(fmt.Sprintf("  %-5s %8s %8s %8s %8s %8s\n",
				m.Side, m.Size, m.T500ms, m.T1s, m.T2s, m.T5s))
		}
	}

	b.WriteString(styleSectionHeader.Render("Optimizer") + "\n")
	b.WriteString(fmt.Sprintf("  eval=%d  policy=%s  utility=%s  ready=%v\n",
		a.Optimizer.Eval, val2(a.Optimizer.Policy, "—"), val2(a.Optimizer.Utility, "—"), a.Optimizer.Ready))
	fourthDimLabel := a.Optimizer.FourthDim
	if fourthDimLabel == "" {
		fourthDimLabel = "budget"
	}
	b.WriteString(fmt.Sprintf("  tox=%.2f  opp=%.2f  inv=%.2f  %s=%.2f\n",
		a.Optimizer.Tox, a.Optimizer.Opp, a.Optimizer.Inv, fourthDimLabel, a.Optimizer.Budget))

	b.WriteString(styleSectionHeader.Render("Adaptive") + "\n")
	alphaDisplay := val(a.Adaptive.VwapAlpha)
	if base, err := strconv.ParseFloat(a.Adaptive.VwapAlpha, 64); err == nil && s.Portfolio.EffectiveVWAPAlpha > 0 {
		if math.Abs(base-s.Portfolio.EffectiveVWAPAlpha) > 0.01 {
			alphaDisplay = fmt.Sprintf("%.2f (eff: %.2f)", base, s.Portfolio.EffectiveVWAPAlpha)
		}
	}
	b.WriteString(fmt.Sprintf("  skew=%s  alpha=%s  autocorr=%s\n",
		val(a.Adaptive.SkewFactor), alphaDisplay, val(a.Adaptive.Autocorr)))
	b.WriteString(fmt.Sprintf("  opt: spread=%s/%s  notional=%s  skew=%s\n",
		val(a.Adaptive.OptSpreadBuy), val(a.Adaptive.OptSpreadSell),
		val(a.Adaptive.OptNotionalMult), val(a.Adaptive.OptSkewMult)))
	b.WriteString(fmt.Sprintf("  opt: imb=%s  decay=%s  refresh=%s  (eval %d)\n",
		val(a.Adaptive.OptImbShiftMult), val(a.Adaptive.OptFillDecayMult),
		val(a.Adaptive.OptRefreshMult), a.Adaptive.OptEval))

	burstStr := "inactive"
	if a.Adaptive.VolBurstActive {
		burstStr = styleValueWarn.Render(fmt.Sprintf("active (ratio=%s, levels=%d)", val(a.Adaptive.VolBurstRatio), a.Adaptive.VolBurstLevels))
	}
	b.WriteString(fmt.Sprintf("  burst: %s  spread_buy=%s  spread_sell=%s\n",
		burstStr, val(a.Adaptive.BurstBuySpread), val(a.Adaptive.BurstSellSpread)))

	// V31/V32 trend-bias mechanism diagnostics: show per-status firing counts
	// plus latest FV shift so a dormant mechanism is visible at a glance.
	tm := a.TrendMech
	if tm.FVShiftFired > 0 || tm.AlphaBoostFired > 0 || tm.EntryAccelFired > 0 ||
		tm.FVShiftBps != "" || tm.CompositeBuyBps != "" || tm.CompositeSellBps != "" {
		b.WriteString(fmt.Sprintf("  trend: fv_shift=%sbps fired: fv=%d alpha=%d entry=%d  mkt_buy=%s sell=%s\n",
			val(tm.FVShiftBps), tm.FVShiftFired, tm.AlphaBoostFired, tm.EntryAccelFired,
			val(tm.CompositeBuyBps), val(tm.CompositeSellBps)))
	}

	return b.String()
}

func renderFlags(a *AssetState) string {
	var flags []string
	if a.Quoting.Warmup {
		flags = append(flags, styleValueWarn.Render("WARMUP"))
	}
	if a.Quoting.Recovery {
		flags = append(flags, styleValueWarn.Render("RECOVERY"))
	}
	if a.Quoting.Degraded {
		flags = append(flags, styleValueWarn.Render("DEGRADED"))
	}
	if a.Quoting.Halted {
		flags = append(flags, styleValueNegative.Render("HALTED"))
	}
	if a.Quoting.PairReduceOnly {
		flags = append(flags, styleValueWarn.Render("PAIR-REDUCE"))
	}
	if a.Sizing.TrendGuardActive {
		flags = append(flags, styleValueWarn.Render("TREND-GUARD"))
	}
	if a.Sizing.FlowGateActive {
		flags = append(flags, styleValueWarn.Render("FLOW-GATE"))
	}
	if a.Sizing.PriceShockActive {
		flags = append(flags, styleValueNegative.Render("PRICE-SHOCK"))
	}
	if len(flags) == 0 {
		return ""
	}
	return strings.Join(flags, "  ")
}

func val(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func val2(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}
