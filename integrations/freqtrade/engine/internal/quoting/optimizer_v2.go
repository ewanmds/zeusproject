package quoting

import (
	"fmt"
	"log/slog"
	"math"
	"sort"
	"strings"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/config"
)

// OptimizerV2Inputs are the signals fed into the v2 optimizer each evaluation.
type OptimizerV2Inputs struct {
	WorstMarkoutBps     float64 // min(buy-side, sell-side) T1s markout EWMA
	BuyMarkoutBps       float64 // buy-side T1s markout EWMA
	SellMarkoutBps      float64 // sell-side T1s markout EWMA
	CompositeMarkoutBps float64 // weighted blend of T500ms, T1s, T2s, T5s
	BaselineBps         float64 // baseline markout expectation (e.g. half-spread - fees)
	FillRateRatio       float64 // actual $/min / target $/min
	BuyFillRateRatio    float64 // buy-side fill rate ratio
	SellFillRateRatio   float64 // sell-side fill rate ratio
	FillSamples         int     // fills in rolling window
	BuyFillSamples      int     // buy-side fills in rolling window
	SellFillSamples     int     // sell-side fills in rolling window
	FillBurstActive     bool    // true when fill burst tracker has active burst
	EmergencyCount      int     // consecutive emergency widen triggers
	InventoryUtil       float64 // |pos notional| / max pos notional [0,1]
	InventorySide       string  // "buy" or "sell" indicating position direction
	ControllerAdj       float64 // inventory controller skew factor (clamped to [ControllerSkewMin, ControllerSkewMax])
	ControllerSkewMin   float64 // inventory_controller_skew_min — normalization floor for inv_ctrl sub-dim
	ControllerSkewMax   float64 // inventory_controller_skew_max — normalization ceiling for inv_ctrl sub-dim
	SameSideDurationS   float64 // seconds at same-side inventory
	Autocorr            float64 // lag-1 autocorrelation
	VolRatio            float64 // realized vol bps / base spread bps
	VWAPDeviation       float64 // |mid - VWAP| / mid
	WickThreshold       float64 // wick deviation threshold from config
	SpreadBBORatio      float64 // our spread / BBO spread
	BudgetPressure      float64 // [0,1] from VirtCycleEngine
	RLDegraded          bool    // rate-limit allocator degraded
	RLCritical          bool    // rate-limit allocator critical
	Halted              bool    // risk halt active

	// Cross-asset signals (ported from v1 Feature 7)
	PeerInventoryUtil float64 // other leg's inventory utilization
	PeerSameDirection bool    // true if both legs have same-side position
	PairReduceActive  bool    // true if pair-reduce mode is active

	// Trade flow inputs (ported from v1 Feature 8)
	ImbalanceSaturationPct float64 // fraction of recent imbalance reads at ±cap

	// Session PnL (P0 stub; consumed by P3/P8/P9).
	SessionPnLBps float64

	// Position-level unrealized PnL in bps of equity (P8). Sign reflects
	// direction: positive when the position is in profit, negative in loss.
	PositionUnrealizedBps float64

	// P6: Per-side win rate / profit factor distribution-shape signal.
	// Win rate = fraction of fills with positive 5s markout; profit factor =
	// sum(gains) / sum(|losses|). When per_side_winrate_enabled is true and
	// both sides have >= window samples, the optimizer widens the side whose
	// win rate or profit factor falls below threshold.
	PerSideWinrateEnabled        bool
	PerSideWinrateWindow         int
	PerSideWinrateThreshold      float64
	PerSideProfitFactorThreshold float64
	PerSideWinrateWidenBps       float64
	BuyWinRate                   float64
	SellWinRate                  float64
	BuyProfitFactor              float64
	SellProfitFactor             float64
	BuyPnLSamples                int
	SellPnLSamples               int
}

// stateVector represents the 4D state space of the optimizer. Sub-dimension
// values (ToxMarkout, ToxEmergency) are retained so the emergency gate can fire
// on a saturated sub-dim even when the combined Toxicity is below its threshold
// (P0-2 fix from v18 eval 503: tox_emergency=0.33 but emergency:false because
// combined tox peaked at 0.60 < 0.9).
type stateVector struct {
	Toxicity        float64 // [0, 1] — how adverse are fills
	Opportunity     float64 // [0, 1] — how profitable is the market
	InventoryStress float64 // [0, 1] — how much inventory pressure
	BudgetPressure  float64 // [0, 1] — 4th-axis stress; semantics depend on cfg.FourthDimension ("budget_pressure" default; "session_pnl" when enabled)

	ToxMarkout   float64 // [0, 1] — markout-EWMA-derived toxicity sub-dim
	ToxEmergency float64 // [0, 1] — emergency-count toxicity sub-dim
}

// defaultSessionPnLEdges is the default bucket edge list used when the
// config does not explicitly set SessionPnLBucketEdgesBps.
var defaultSessionPnLEdges = []float64{-20, -10, 10, 20}

// defaultFourthDimDefensiveFloor is the default defensive-bucket threshold
// applied when cfg.FourthDimDefensiveFloor is unset (0) and FourthDimension
// is "session_pnl".
const defaultFourthDimDefensiveFloor = 0.75

// mapSessionPnLToStress bucketizes a session PnL (bps) into [0, 1] stress.
// Edges must have length 4 in strictly ascending order:
//
//	bps <  edges[0]           -> 1.00 (very adverse)
//	edges[0] <= bps < edges[1] -> 0.75 (adverse)
//	edges[1] <= bps <= edges[2] -> 0.50 (neutral)
//	edges[2] <  bps <= edges[3] -> 0.25 (good)
//	bps >  edges[3]           -> 0.00 (very good)
func mapSessionPnLToStress(bps float64, edges []float64) float64 {
	if len(edges) != 4 {
		edges = defaultSessionPnLEdges
	}
	switch {
	case bps < edges[0]:
		return 1.0
	case bps < edges[1]:
		return 0.75
	case bps <= edges[2]:
		return 0.50
	case bps <= edges[3]:
		return 0.25
	default:
		return 0.0
	}
}

// fourthDimLabel returns the active fourth-dim label, with empty treated as
// "budget_pressure" for log clarity.
func fourthDimLabel(cfg config.OptimizerV2Config) string {
	if cfg.FourthDimension == "" {
		return "budget_pressure"
	}
	return cfg.FourthDimension
}

// OptimizerV2 is a unified, utility-driven controller that coordinates
// all quoting knobs through a 4D state model and policy table.
type OptimizerV2 struct {
	cfg        config.OptimizerV2Config
	coin       string
	baseSpread float64
	baseAlpha  float64

	current  map[string]float64 // current knob values
	velocity map[string]float64 // momentum state
	shadow   map[string]float64 // frozen at session start (for rollback)

	utilityHistory   []float64
	lastAllNegative  bool // V30 part 5: cached for IsAllNegative() accessor
	overlay          OptimizerOverlay

	// Per-knob selective rollback tracking
	perKnobTargetSnapshot map[string]float64 // target at last significant change
	perKnobMarkout        map[string]float64 // composite markout when snapshot taken
	lastCompositeMarkout  float64            // current markout for rollback comparison

	// Directional cooldown after rollback
	rollbackDirCooldown map[string]time.Time // "knob:+"/"knob:-" -> cooldown expiry
	lastApproachDir     map[string]float64   // sign of last approach direction per knob

	totalFills    int
	evalCount     int
	prevEmergency bool
	prevState     *stateVector // previous state for transition detection

	// P4: per-coin rate-limited "deadband skip" log emission and per-eval
	// scratch state populated by approach() and consumed by Evaluate().
	lastDeadbandLog       time.Time
	pendingDeadbandSkips  []string
	pendingDeadbandMaxAbs float64

	// P6: edge-triggered per-side winrate gate logging. Key is side ("buy" /
	// "sell"). Log emits only when the per-side active state transitions or
	// once every 5 minutes while continuously active. The fired-slice label
	// emission is unchanged (still per-eval), only the slog.Info is throttled.
	perSideWinrateActive  map[string]bool
	perSideWinrateLastLog map[string]time.Time

	// P14: empirical-quantile sub-dimension normalizers. nil when
	// SubdimNormalization != "empirical_quantile".
	fillRateNorm         *quantileNormalizer
	autocorrNorm         *quantileNormalizer
	vwapDeviationNorm    *quantileNormalizer
	spreadBBORatioNorm   *quantileNormalizer
	sameSideDurationNorm *quantileNormalizer
	// v23: tox markout quantile normalizer. Observes -worstMk (larger =
	// more adverse). Replaces baseline-divisor saturation that pinned
	// tox_markout=1.0 above -baseline bps, destroying the "moderate" vs
	// "catastrophic" gradient. Linear fallback retained until warm.
	markoutNorm          *quantileNormalizer
	lastQuantileSnapshot time.Time

	mu sync.Mutex
}

// deadbandFor returns the deadband threshold for a knob; 0 disables.
func deadbandFor(knob string, cfg config.OptimizerV2Config) float64 {
	switch knob {
	case "spreadDeltaBuy", "spreadDeltaSell":
		return cfg.SpreadDeltaDeadbandBps
	case "skewMult":
		return cfg.SkewDeltaDeadband
	case "notionalMult":
		return cfg.NotionalDeltaDeadband
	default:
		return 0
	}
}

// shouldSkipKnobUpdate is a pure helper: returns true when |delta| is below
// the configured per-knob deadband (and a deadband is configured).
func shouldSkipKnobUpdate(knob string, delta float64, cfg config.OptimizerV2Config) bool {
	db := deadbandFor(knob, cfg)
	if db <= 0 {
		return false
	}
	return math.Abs(delta) < db
}

// v2Knobs defines the canonical knob names for optimizer v2.
var v2Knobs = []string{
	"spreadDeltaBuy", "spreadDeltaSell",
	"notionalMult", "skewMult", "alphaDelta",
	"imbalanceShiftMult", "fillDecayMult", "refreshMult",
}

// NewOptimizerV2 creates a new v2 optimizer with neutral initial state.
func NewOptimizerV2(cfg config.OptimizerV2Config, coin string, baseSpread, baseAlpha float64) *OptimizerV2 {
	current := make(map[string]float64, len(v2Knobs))
	velocity := make(map[string]float64, len(v2Knobs))
	shadow := make(map[string]float64, len(v2Knobs))

	// Initialize to neutral values.
	for _, k := range v2Knobs {
		switch k {
		case "notionalMult", "skewMult", "imbalanceShiftMult", "fillDecayMult", "refreshMult":
			current[k] = 1.0
			shadow[k] = 1.0
		default:
			// spreadDeltaBuy, spreadDeltaSell, alphaDelta default to 0.
			current[k] = 0.0
			shadow[k] = 0.0
		}
		velocity[k] = 0.0
	}

	o := &OptimizerV2{
		cfg:                   cfg,
		coin:                  coin,
		baseSpread:            baseSpread,
		baseAlpha:             baseAlpha,
		current:               current,
		velocity:              velocity,
		shadow:                shadow,
		perKnobTargetSnapshot: make(map[string]float64, len(v2Knobs)),
		perKnobMarkout:        make(map[string]float64, len(v2Knobs)),
		rollbackDirCooldown:   make(map[string]time.Time),
		lastApproachDir:       make(map[string]float64, len(v2Knobs)),
		perSideWinrateActive:  make(map[string]bool, 2),
		perSideWinrateLastLog: make(map[string]time.Time, 2),
		overlay: OptimizerOverlay{
			NotionalMult:       1.0,
			SkewMult:           1.0,
			ImbalanceShiftMult: 1.0,
			FillDecayMult:      1.0,
			RefreshMult:        1.0,
		},
	}
	if cfg.SubdimNormalization == "empirical_quantile" {
		window := cfg.SubdimQuantileWindowEvals
		if window <= 0 {
			window = 500
		}
		low := cfg.SubdimQuantileLowPct
		if low <= 0 {
			low = 5
		}
		high := cfg.SubdimQuantileHighPct
		if high <= 0 {
			high = 95
		}
		o.fillRateNorm = newQuantileNormalizer(window, low, high)
		o.autocorrNorm = newQuantileNormalizer(window, low, high)
		o.vwapDeviationNorm = newQuantileNormalizer(window, low, high)
		o.spreadBBORatioNorm = newQuantileNormalizer(window, low, high)
		o.sameSideDurationNorm = newQuantileNormalizer(window, low, high)
		o.markoutNorm = newQuantileNormalizer(window, low, high)
	}
	return o
}

// Overlay returns the current overlay. Returns neutral if disabled or shadow mode. Thread-safe.
func (o *OptimizerV2) Overlay() OptimizerOverlay {
	o.mu.Lock()
	defer o.mu.Unlock()
	if !o.cfg.Enabled || o.cfg.ShadowMode {
		return neutralOverlay()
	}
	return o.overlay
}

// RecordFill increments the total fill counter.
func (o *OptimizerV2) RecordFill() {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.totalFills++
}

// Snapshot returns a structured snapshot of the optimizer's current state
// suitable for inclusion in a post-mortem dump. Thread-safe.
func (o *OptimizerV2) Snapshot() map[string]any {
	o.mu.Lock()
	defer o.mu.Unlock()
	knobs := make(map[string]float64, len(o.current))
	for k, v := range o.current {
		knobs[k] = v
	}
	out := map[string]any{
		"coin":           o.coin,
		"eval_count":     o.evalCount,
		"total_fills":    o.totalFills,
		"prev_emergency": o.prevEmergency,
		"confidence":     o.confidence(),
		"knobs":          knobs,
		"overlay": map[string]float64{
			"spread_delta":         o.overlay.SpreadDelta,
			"spread_delta_buy":     o.overlay.SpreadDeltaBuy,
			"spread_delta_sell":    o.overlay.SpreadDeltaSell,
			"notional_mult":        o.overlay.NotionalMult,
			"skew_mult":            o.overlay.SkewMult,
			"alpha_delta":          o.overlay.AlphaDelta,
			"imbalance_shift_mult": o.overlay.ImbalanceShiftMult,
			"fill_decay_mult":      o.overlay.FillDecayMult,
			"refresh_mult":         o.overlay.RefreshMult,
		},
	}
	if o.prevState != nil {
		out["state"] = map[string]float64{
			"toxicity":         o.prevState.Toxicity,
			"opportunity":      o.prevState.Opportunity,
			"inventory_stress": o.prevState.InventoryStress,
			"budget_pressure":  o.prevState.BudgetPressure,
			"tox_markout":      o.prevState.ToxMarkout,
			"tox_emergency":    o.prevState.ToxEmergency,
		}
	}
	return out
}

// EvalCount returns the number of evaluations performed.
func (o *OptimizerV2) EvalCount() int {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.evalCount
}

// EmergencyActive returns true when the previous evaluation entered the
// emergency path. Thread-safe.
func (o *OptimizerV2) EmergencyActive() bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.prevEmergency
}

// IsAllNegative returns true when the optimizer's last self-evaluation found
// every utility in the rollback window to be negative. Used by the burst-eval
// throttle (V30 part 5) to extend the minimum interval during bad regimes.
func (o *OptimizerV2) IsAllNegative() bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.lastAllNegative
}

// confidence returns a warmup factor in [0, 1] based on fill count vs minimum.
// Called from within locked context.
func (o *OptimizerV2) confidence() float64 {
	if o.cfg.MinFillSamples <= 0 {
		return 1.0
	}
	c := float64(o.totalFills) / float64(o.cfg.MinFillSamples)
	if c > 1.0 {
		return 1.0
	}
	return c
}

// computeState maps raw inputs to a 4D state vector [0, 1] per dimension.
func (o *OptimizerV2) computeState(inputs OptimizerV2Inputs) stateVector {
	w := o.cfg.ToxicityWeights

	// --- Toxicity ---
	// toxMarkout maps the worse-of (T1s WorstMarkout, composite T500ms/1s/2s/5s)
	// to [0, 1]. Using the multi-horizon minimum closes the v21 blind spot where
	// T1s looked benign but T5s was -1.05 bps — fills were picked off by
	// directional flow within seconds of the initial print. With composite
	// folded in, Toxicity rises when any horizon degrades, so the Toxicity<Low
	// gate on opp_tighten naturally blocks tightening into toxic flow.
	// Gradient: positive markout (good fills) scales linearly baseline→0 as
	// toxMarkout 0→1; negative markout scales symmetrically, -baseline → 1.0.
	worstMk := inputs.WorstMarkoutBps
	if inputs.CompositeMarkoutBps < worstMk {
		worstMk = inputs.CompositeMarkoutBps
	}
	var toxMarkout float64
	// Blind-signal gate: before any fills exist, CompositeMarkoutBps defaults to 0
	// while the baseline is nonzero, producing toxMarkout = 1 - 0/baseline = 1.0
	// and tripping the emergency ToxMarkout > 0.95 bypass at startup. Require at
	// least one fill sample before this signal contributes.
	if inputs.FillSamples > 0 {
		if inputs.BaselineBps != 0 {
			toxMarkout = clamp(1.0-worstMk/inputs.BaselineBps, 0, 1)
			// For negative markouts, the formula above saturates at 1.0 (no gradient).
			// Use symmetric scaling: -baseline → 1.0, giving the optimizer a gradient
			// to distinguish mild adverse selection (-1 bps) from severe (-5 bps).
			if worstMk < 0 {
				toxMarkout = clamp(-worstMk/inputs.BaselineBps, 0, 1)
			}
		} else {
			// No baseline: derive from markout sign. Negative markout = toxic.
			if worstMk < 0 {
				toxMarkout = clamp(-worstMk/5.0, 0, 1) // scale: -5 bps -> 1.0
			}
		}
		// v23: empirical-quantile normalization of -worstMk (larger = more
		// adverse). Restores gradient above -baseline where the linear path
		// saturates at 1.0 (can't distinguish -8 bps from -30 bps). Falls
		// back to the linear value above until the normalizer warms up.
		if o.cfg.SubdimNormalization == "empirical_quantile" {
			toxMarkout = o.applyQuantile(o.markoutNorm, "markout", -worstMk, toxMarkout)
		}
	}
	var toxBurst float64
	if inputs.FillBurstActive {
		toxBurst = 1.0
	}
	toxEmergency := clamp(float64(inputs.EmergencyCount)/3.0, 0, 1)

	toxicity := clamp(
		w.MarkoutEWMA*toxMarkout+w.FillBurst*toxBurst+w.EmergencyCount*toxEmergency,
		0, 1,
	)

	// --- Opportunity ---
	ow := o.cfg.OpportunityWeights
	fillRate := inputs.FillRateRatio
	if inputs.BuyFillRateRatio > 0 && inputs.SellFillRateRatio > 0 {
		fillRate = math.Min(inputs.BuyFillRateRatio, inputs.SellFillRateRatio)
	}
	fillRateLinear := clamp(fillRate/2.0, 0, 1)
	autocorrLinear := clamp(math.Abs(inputs.Autocorr)*5.0, 0, 1)
	var vwapDevLinear float64
	if inputs.WickThreshold > 0 {
		vwapDevLinear = clamp(inputs.VWAPDeviation/inputs.WickThreshold, 0, 1)
	}
	spreadBBOLinear := clamp((inputs.SpreadBBORatio-1.0)/9.0, 0, 1)

	fillRateNorm := fillRateLinear
	autocorrSignal := autocorrLinear
	vwapDevNorm := vwapDevLinear
	spreadBBONorm := spreadBBOLinear
	if o.cfg.SubdimNormalization == "empirical_quantile" {
		fillRateNorm = o.applyQuantile(o.fillRateNorm, "fill_rate", fillRate, fillRateLinear)
		autocorrSignal = o.applyQuantile(o.autocorrNorm, "autocorr", math.Abs(inputs.Autocorr), autocorrLinear)
		vwapDevNorm = o.applyQuantile(o.vwapDeviationNorm, "vwap_deviation", inputs.VWAPDeviation, vwapDevLinear)
		spreadBBONorm = o.applyQuantile(o.spreadBBORatioNorm, "spread_bbo_ratio", inputs.SpreadBBORatio, spreadBBOLinear)
	}

	opportunity := clamp(
		ow.FillRateRatio*fillRateNorm+ow.RegimeSignal*autocorrSignal+
			ow.VWAPDeviation*vwapDevNorm+ow.SpreadBBORatio*spreadBBONorm,
		0, 1,
	)

	// --- Inventory Stress ---
	iw := o.cfg.InventoryStressWeights
	posUtil := clamp(inputs.InventoryUtil, 0, 1)
	// controllerNorm maps ControllerAdj (a skew factor clamped to
	// [ControllerSkewMin, ControllerSkewMax]) into [0, 1]. When bounds are
	// provided, map across the actual configured range. Otherwise fall back to
	// the legacy [0.3, 3.0] mapping. The v18 log analysis found that the old
	// mapping saturated inv_ctrl at 1.0 on 93 % of evals — widening the
	// controller's output range (12→18 in v18) did not help because the
	// optimizer normalization floor (3.0) was still below most observed
	// values. P0-3: normalize over the configured range.
	var controllerNorm float64
	if inputs.ControllerSkewMax > inputs.ControllerSkewMin && inputs.ControllerSkewMin >= 0 {
		controllerNorm = clamp(
			(inputs.ControllerAdj-inputs.ControllerSkewMin)/
				(inputs.ControllerSkewMax-inputs.ControllerSkewMin),
			0, 1,
		)
	} else {
		controllerNorm = clamp((inputs.ControllerAdj-0.3)/(3.0-0.3), 0, 1)
	}
	durationLinear := clamp(inputs.SameSideDurationS/600.0, 0, 1)
	durationNorm := durationLinear
	if o.cfg.SubdimNormalization == "empirical_quantile" {
		durationNorm = o.applyQuantile(o.sameSideDurationNorm, "same_side_duration", inputs.SameSideDurationS, durationLinear)
	}

	inventoryStress := clamp(
		iw.PositionUtil*posUtil+iw.ControllerAdj*controllerNorm+iw.SameSideDuration*durationNorm,
		0, 1,
	)

	// --- Budget Pressure (or Session PnL stress when FourthDimension="session_pnl") ---
	// The BudgetPressure field holds the 4th-axis stress value; its semantics
	// depend on cfg.FourthDimension (default "budget_pressure", preserves v22
	// behavior bit-exactly).
	var bp float64
	switch o.cfg.FourthDimension {
	case "session_pnl":
		bp = mapSessionPnLToStress(inputs.SessionPnLBps, o.cfg.SessionPnLBucketEdgesBps)
	default:
		bp = clamp(inputs.BudgetPressure, 0, 1)
		if inputs.RLCritical {
			bp = 1.0
		} else if inputs.RLDegraded {
			bp = math.Max(0.8, bp)
		}
	}

	sv := stateVector{
		Toxicity:        toxicity,
		Opportunity:     opportunity,
		InventoryStress: inventoryStress,
		BudgetPressure:  bp,
		ToxMarkout:      toxMarkout,
		ToxEmergency:    toxEmergency,
	}

	slog.Info("[OptimizerV2] state",
		"coin", o.coin,
		"eval", o.evalCount,
		"tox", sv.Toxicity,
		"tox_markout", toxMarkout,
		"tox_burst", toxBurst,
		"tox_emergency", toxEmergency,
		"opp", sv.Opportunity,
		"opp_fill_rate", fillRateNorm,
		"opp_autocorr", autocorrSignal,
		"opp_vwap_dev", vwapDevNorm,
		"opp_spread_bbo", spreadBBONorm,
		"inv", sv.InventoryStress,
		"inv_pos_util", posUtil,
		"inv_ctrl", controllerNorm,
		"inv_duration", durationNorm,
		"budget", sv.BudgetPressure,
		"fourth_dim_label", fourthDimLabel(o.cfg),
	)

	o.maybeLogQuantileSnapshot()

	return sv
}

// applyQuantile observes v, then normalizes using the empirical p_low/p_high
// of the rolling window. Falls back to the provided linear value before
// warmup. Emits a one-shot INFO "quantile normalizer warm" log the first
// time the normalizer becomes ready.
func (o *OptimizerV2) applyQuantile(q *quantileNormalizer, subdim string, v, linearFallback float64) float64 {
	if q == nil {
		return linearFallback
	}
	q.Observe(v)
	n := q.Normalize(v)
	if n < 0 {
		return linearFallback
	}
	if q.transitionedToWarm() {
		slog.Info("quantile normalizer warm",
			"coin", o.coin,
			"subdim", subdim,
			"samples", q.count,
		)
	}
	return n
}

// maybeLogQuantileSnapshot emits a periodic (5min per coin) INFO log with
// p_low/p_50/p_high and sample counts for every active sub-dim normalizer.
func (o *OptimizerV2) maybeLogQuantileSnapshot() {
	if o.cfg.SubdimNormalization != "empirical_quantile" {
		return
	}
	now := time.Now()
	if !o.lastQuantileSnapshot.IsZero() && now.Sub(o.lastQuantileSnapshot) < 5*time.Minute {
		return
	}
	o.lastQuantileSnapshot = now

	frLo, frMid, frHi, frN := snapshotOrZero(o.fillRateNorm)
	acLo, acMid, acHi, acN := snapshotOrZero(o.autocorrNorm)
	vdLo, vdMid, vdHi, vdN := snapshotOrZero(o.vwapDeviationNorm)
	sbLo, sbMid, sbHi, sbN := snapshotOrZero(o.spreadBBORatioNorm)
	dLo, dMid, dHi, dN := snapshotOrZero(o.sameSideDurationNorm)
	mkLo, mkMid, mkHi, mkN := snapshotOrZero(o.markoutNorm)

	slog.Info("quantile normalizer snapshot",
		"coin", o.coin,
		"fill_rate_p5", frLo, "fill_rate_p50", frMid, "fill_rate_p95", frHi, "fill_rate_samples", frN,
		"autocorr_p5", acLo, "autocorr_p50", acMid, "autocorr_p95", acHi, "autocorr_samples", acN,
		"vwap_deviation_p5", vdLo, "vwap_deviation_p50", vdMid, "vwap_deviation_p95", vdHi, "vwap_deviation_samples", vdN,
		"spread_bbo_ratio_p5", sbLo, "spread_bbo_ratio_p50", sbMid, "spread_bbo_ratio_p95", sbHi, "spread_bbo_ratio_samples", sbN,
		"same_side_duration_p5", dLo, "same_side_duration_p50", dMid, "same_side_duration_p95", dHi, "same_side_duration_samples", dN,
		"markout_p5", mkLo, "markout_p50", mkMid, "markout_p95", mkHi, "markout_samples", mkN,
	)
}

func snapshotOrZero(q *quantileNormalizer) (float64, float64, float64, int) {
	if q == nil {
		return 0, 0, 0, 0
	}
	return q.Snapshot()
}

// policyTargets computes target values for each knob given the current state.
func (o *OptimizerV2) policyTargets(state stateVector, inputs OptimizerV2Inputs) map[string]float64 {
	targets := make(map[string]float64, len(v2Knobs))
	pt := o.cfg.PolicyThresholds

	// Track which conditions fired for logging.
	var fired []string

	// --- Spread targets ---
	var spreadBase float64
	// Deferred exit-side-only tighten from opp_tighten gate (applied after
	// the per-side split so it doesn't affect the entry side).
	var oppTightenExitAmount float64

	// High toxicity + low opportunity: widen.
	if state.Toxicity > pt.ToxicityHigh && state.Opportunity < pt.OpportunityLow {
		spreadBase += o.baseSpread * state.Toxicity * 0.5
		fired = append(fired, "tox_widen")
	}
	// Low toxicity + high opportunity: tighten.
	//
	// Additional belt-and-suspenders gate on CompositeMarkoutBps:
	// computeState now folds composite markout into Toxicity (so this branch
	// typically won't fire when T5s is adverse), but we still damp here on raw
	// composite markout in case baseline is misconfigured or the composite
	// signal leads the EWMA. Damping scales tightening linearly from full
	// effect (markout >= 0) to zero (markout <= -baseline), and skips entirely
	// below -0.25*baseline. Avoids the v21 pattern where spread_bbo_ratio
	// drove Opportunity near 1.0 on thin BBO while T5s bled -1.05 bps.
	if state.Toxicity < pt.ToxicityLow && state.Opportunity > pt.OpportunityHigh {
		markoutHealth := 1.0
		if inputs.BaselineBps > 0 {
			markoutHealth = clamp(1.0+inputs.CompositeMarkoutBps/inputs.BaselineBps, 0, 1)
		}
		if markoutHealth > 0.25 {
			blockOnInv := pt.OppTightenInvUtilGate > 0 &&
				math.Abs(inputs.InventoryUtil) > pt.OppTightenInvUtilGate &&
				inputs.PositionUnrealizedBps < 0
			blockOnPnL := pt.OppTightenSessionPnLFloorBps < 0 &&
				inputs.SessionPnLBps < pt.OppTightenSessionPnLFloorBps
			tightenAmount := o.baseSpread * state.Opportunity * 0.3 * markoutHealth
			if blockOnInv || blockOnPnL {
				if blockOnInv {
					fired = append(fired, "opp_tighten_blocked_inv")
				}
				if blockOnPnL {
					fired = append(fired, "opp_tighten_blocked_pnl")
				}
				// Asymmetric: when the gate fires but we're carrying inventory,
				// still tighten the exit side. Clears bad inventory faster
				// without inviting adverse flow on the entry side. When flat,
				// preserve the original full block (no inventory to clear).
				const invFlatThreshold = 0.05
				if math.Abs(inputs.InventoryUtil) > invFlatThreshold && inputs.InventorySide != "" {
					oppTightenExitAmount = tightenAmount
					fired = append(fired, "opp_tighten_exit_only")
				}
			} else {
				spreadBase -= tightenAmount
				fired = append(fired, "opp_tighten")
			}
		}
	}
	// High budget pressure: widen.
	if state.BudgetPressure > pt.BudgetHigh {
		spreadBase += o.baseSpread * (state.BudgetPressure - pt.BudgetMedium) * 0.3
		fired = append(fired, "budget_widen")
	}
	// Regime: mean-reversion tightens, trending widens.
	// V31: regime_widen applies asymmetrically when regime_widen_entry_mult != 1.0.
	var regimeWidenBuy, regimeWidenSell float64
	if inputs.Autocorr < -pt.AutocorrThreshold {
		spreadBase -= math.Abs(inputs.Autocorr) * 0.15 * o.baseSpread
		fired = append(fired, "regime_tighten")
	} else if inputs.Autocorr > pt.AutocorrThreshold {
		regimeAmount := inputs.Autocorr * 0.15 * o.baseSpread
		entryMult := pt.RegimeWidenEntryMult
		if entryMult == 0 {
			entryMult = 1.0
		}
		exitMult := 2.0 - entryMult
		if inputs.InventorySide == "buy" {
			// Long: entry=buy, exit=sell
			regimeWidenBuy = regimeAmount * entryMult
			regimeWidenSell = regimeAmount * exitMult
		} else if inputs.InventorySide == "sell" {
			// Short: entry=sell, exit=buy
			regimeWidenSell = regimeAmount * entryMult
			regimeWidenBuy = regimeAmount * exitMult
		} else {
			regimeWidenBuy = regimeAmount
			regimeWidenSell = regimeAmount
		}
		spreadBase += regimeAmount
		fired = append(fired, "regime_widen")
	}

	// Per-side toxicity asymmetry: shift the more toxic side.
	var buyTox, sellTox float64
	if inputs.BaselineBps != 0 {
		buyTox = clamp(1.0-inputs.BuyMarkoutBps/inputs.BaselineBps, 0, 1)
		sellTox = clamp(1.0-inputs.SellMarkoutBps/inputs.BaselineBps, 0, 1)
	}
	toxShift := (buyTox - sellTox) * o.baseSpread * 0.2

	// P5: defensive-floor clamp. When the 4th dim uses session_pnl and the
	// state is in the defensive bucket, do not allow the policy to NET-tighten
	// the base spread below baseline. Per-side tox shift is applied on top.
	if o.cfg.FourthDimension == "session_pnl" {
		floor := o.cfg.FourthDimDefensiveFloor
		if floor == 0 {
			floor = defaultFourthDimDefensiveFloor
		}
		if state.BudgetPressure >= floor && spreadBase < 0 {
			spreadBase = 0
			fired = append(fired, "fourth_dim_defensive_clamp")
		}
	}

	// Apply regime asymmetry: replace the symmetric regimeAmount in spreadBase
	// with per-side amounts. Subtract the symmetric component (already in
	// spreadBase) and add the per-side one.
	baseBuy := spreadBase + toxShift
	baseSell := spreadBase - toxShift
	if regimeWidenBuy != 0 || regimeWidenSell != 0 {
		regimeSymmetric := (regimeWidenBuy + regimeWidenSell) / 2
		baseBuy += regimeWidenBuy - regimeSymmetric
		baseSell += regimeWidenSell - regimeSymmetric
	}
	targets["spreadDeltaBuy"] = baseBuy
	targets["spreadDeltaSell"] = baseSell

	// Apply deferred exit-side-only tighten from the opp_tighten gate.
	// InventorySide "buy" = long → exit is sell; "sell" = short → exit is buy.
	if oppTightenExitAmount > 0 {
		switch inputs.InventorySide {
		case "buy":
			targets["spreadDeltaSell"] -= oppTightenExitAmount
		case "sell":
			targets["spreadDeltaBuy"] -= oppTightenExitAmount
		}
	}

	// Per-side fill rate asymmetry: tighten the underfilling side.
	if inputs.BuyFillRateRatio > 0 && inputs.SellFillRateRatio > 0 {
		fillDelta := inputs.BuyFillRateRatio - inputs.SellFillRateRatio
		if math.Abs(fillDelta) > pt.FillRateDelta {
			fillShift := clamp(fillDelta, -1, 1) * o.baseSpread * 0.15
			targets["spreadDeltaBuy"] -= fillShift // tighten underfilling side
			targets["spreadDeltaSell"] += fillShift
			fired = append(fired, "fill_rate_asym")
		}
	}

	// P6: Per-side win rate / profit factor gate. Widens the adverse side when
	// either signal falls below threshold and both sides have enough samples.
	//
	// Log emission is edge-triggered: slog.Info fires only on transitions
	// (inactive→active, or active→inactive) and heartbeats (once per 5min per
	// coin+side while continuously active). The `fired` slice label is still
	// appended every eval (unchanged) so attribution logging elsewhere is
	// preserved — only the slog.Info is rate-limited.
	if inputs.PerSideWinrateEnabled &&
		inputs.BuyPnLSamples >= inputs.PerSideWinrateWindow &&
		inputs.SellPnLSamples >= inputs.PerSideWinrateWindow {
		now := time.Now()
		buyActive := inputs.BuyWinRate < inputs.PerSideWinrateThreshold ||
			inputs.BuyProfitFactor < inputs.PerSideProfitFactorThreshold
		if buyActive {
			targets["spreadDeltaBuy"] += inputs.PerSideWinrateWidenBps
			fired = append(fired, "per_side_winrate_buy")
			prev := o.perSideWinrateActive["buy"]
			last := o.perSideWinrateLastLog["buy"]
			if !prev || last.IsZero() || now.Sub(last) >= 5*time.Minute {
				slog.Info("per-side winrate gate",
					"coin", o.coin,
					"side", "buy",
					"winrate", inputs.BuyWinRate,
					"pf", inputs.BuyProfitFactor,
					"samples", inputs.BuyPnLSamples,
					"widen_bps", inputs.PerSideWinrateWidenBps,
				)
				o.perSideWinrateLastLog["buy"] = now
			}
		} else if o.perSideWinrateActive["buy"] {
			// Transition active → inactive.
			slog.Info("per-side winrate gate cleared",
				"coin", o.coin,
				"side", "buy",
				"winrate", inputs.BuyWinRate,
				"pf", inputs.BuyProfitFactor,
				"samples", inputs.BuyPnLSamples,
			)
		}
		o.perSideWinrateActive["buy"] = buyActive

		sellActive := inputs.SellWinRate < inputs.PerSideWinrateThreshold ||
			inputs.SellProfitFactor < inputs.PerSideProfitFactorThreshold
		if sellActive {
			targets["spreadDeltaSell"] += inputs.PerSideWinrateWidenBps
			fired = append(fired, "per_side_winrate_sell")
			prev := o.perSideWinrateActive["sell"]
			last := o.perSideWinrateLastLog["sell"]
			if !prev || last.IsZero() || now.Sub(last) >= 5*time.Minute {
				slog.Info("per-side winrate gate",
					"coin", o.coin,
					"side", "sell",
					"winrate", inputs.SellWinRate,
					"pf", inputs.SellProfitFactor,
					"samples", inputs.SellPnLSamples,
					"widen_bps", inputs.PerSideWinrateWidenBps,
				)
				o.perSideWinrateLastLog["sell"] = now
			}
		} else if o.perSideWinrateActive["sell"] {
			slog.Info("per-side winrate gate cleared",
				"coin", o.coin,
				"side", "sell",
				"winrate", inputs.SellWinRate,
				"pf", inputs.SellProfitFactor,
				"samples", inputs.SellPnLSamples,
			)
		}
		o.perSideWinrateActive["sell"] = sellActive
	}

	// --- Notional target ---
	// Toxic flow takes precedence over opportunity — never increase size during adverse selection.
	notional := 1.0
	if state.Toxicity > pt.ToxicityMedium {
		notional = 1.0 - state.Toxicity*0.4
		fired = append(fired, "not_tox_reduce")
	} else if state.Opportunity > pt.OpportunityHigh && state.InventoryStress < pt.InventoryLow {
		notional = 1.0 + state.Opportunity*0.3
		fired = append(fired, "not_opp_increase")
	}
	// Cross-asset: reduce notional when pair-reduce is active with inventory pressure.
	if inputs.PairReduceActive && inputs.InventoryUtil > pt.PeerInventoryMin {
		notional *= 0.7
		fired = append(fired, "not_pair_reduce")
	}
	targets["notionalMult"] = notional

	// --- Skew target ---
	skew := 1.0
	if state.InventoryStress > pt.InventoryHigh {
		skew = 1.0 + (state.InventoryStress-pt.InventoryHigh)*0.67
		fired = append(fired, "skew_inv_stress")
	} else if state.InventoryStress < pt.InventoryLow {
		skew = 1.0
	}
	// Cross-asset: stronger skew when both legs have same-direction exposure (v1 urgency-2).
	if inputs.PeerSameDirection && state.InventoryStress > pt.InventoryLow && inputs.PeerInventoryUtil > pt.PeerInventoryMin {
		skew *= 1.5
		fired = append(fired, "skew_peer_same_dir")
	}
	// Skew relax on adverse markout. Turns skewMult into feedback: when recent
	// fills have been adverse and we have inventory, scale skew down linearly
	// toward a floor. Aggressive skew that gets picked off is worse than
	// neutral skew. Enabled only when both threshold (<0) and floor (>0) are
	// configured. Defers to the neutral L804 fallback when inventory is low.
	if pt.SkewRelaxAdverseMarkoutBps < 0 && pt.SkewRelaxFloor > 0 &&
		inputs.CompositeMarkoutBps < pt.SkewRelaxAdverseMarkoutBps &&
		math.Abs(inputs.InventoryUtil) > pt.SkewRelaxInvUtilMin {
		div := pt.SkewRelaxDivisor
		if div == 0 {
			div = 2.0
		}
		adversity := (pt.SkewRelaxAdverseMarkoutBps - inputs.CompositeMarkoutBps) / div
		relaxFactor := 1.0 - clamp(adversity, 0, 1.0-pt.SkewRelaxFloor)
		skew *= relaxFactor
		fired = append(fired, "skew_relax_on_adverse")
	}
	targets["skewMult"] = skew

	// --- Alpha target ---
	var alphaDelta float64
	if inputs.Autocorr > pt.AutocorrThreshold && state.Toxicity > pt.ToxicityMedium {
		// Trending + toxic: reduce VWAP weight.
		alphaDelta = -0.05
		fired = append(fired, "alpha_reduce")
	} else if inputs.Autocorr < -pt.AutocorrThreshold && state.Toxicity < pt.ToxicityLow {
		// Mean-reversion + benign: increase VWAP weight.
		alphaDelta = 0.05
		fired = append(fired, "alpha_increase")
	}
	targets["alphaDelta"] = alphaDelta

	// --- FillDecay target ---
	fillDecay := 1.0
	if state.Toxicity > pt.ToxicityLow {
		fillDecay = 1.0 + state.Toxicity*0.3
		fired = append(fired, "fdecay_tox")
	}
	targets["fillDecayMult"] = fillDecay

	// --- Refresh target ---
	refresh := 1.0
	if state.Opportunity > pt.OpportunityHigh && state.BudgetPressure < pt.BudgetMedium {
		// High opportunity + low budget pressure: faster refresh.
		refresh = 1.0 - math.Min(state.Opportunity, 1.0-state.BudgetPressure)*0.3
		fired = append(fired, "refresh_opp_fast")
	}
	if state.BudgetPressure > pt.BudgetHigh {
		// High budget pressure: slower refresh.
		refresh = 1.0 + state.BudgetPressure*0.5
		fired = append(fired, "refresh_budget_slow")
	}
	if state.Toxicity > pt.ToxicityMedium {
		// High toxicity: also slower.
		refresh *= 1.0 + state.Toxicity*0.3
		fired = append(fired, "refresh_tox_slow")
	}
	targets["refreshMult"] = refresh

	// --- ImbalanceShift target ---
	imbShift := 1.0
	if inputs.ImbalanceSaturationPct > pt.ImbalanceSatHigh {
		// Signal is noise at saturation — scale down.
		imbShift = 1.0 - 0.3*inputs.ImbalanceSaturationPct
		fired = append(fired, "imb_sat_reduce")
	} else if inputs.ImbalanceSaturationPct < pt.ImbalanceSatLow && state.Toxicity < pt.ToxicityLow {
		// Signal has edge and flow is benign — scale up.
		imbShift = 1.2
		fired = append(fired, "imb_edge_boost")
	}
	targets["imbalanceShiftMult"] = imbShift

	firedStr := "none"
	if len(fired) > 0 {
		firedStr = strings.Join(fired, ",")
	}

	slog.Info("[OptimizerV2] policy",
		"coin", o.coin,
		"eval", o.evalCount,
		"fired", firedStr,
		"tgt_spread_buy", fmt.Sprintf("%.4f", targets["spreadDeltaBuy"]),
		"tgt_spread_sell", fmt.Sprintf("%.4f", targets["spreadDeltaSell"]),
		"tgt_notional", fmt.Sprintf("%.4f", targets["notionalMult"]),
		"tgt_skew", fmt.Sprintf("%.4f", targets["skewMult"]),
		"tgt_alpha", fmt.Sprintf("%.4f", targets["alphaDelta"]),
		"tgt_fill_decay", fmt.Sprintf("%.4f", targets["fillDecayMult"]),
		"tgt_refresh", fmt.Sprintf("%.4f", targets["refreshMult"]),
		"tgt_imb_shift", fmt.Sprintf("%.4f", targets["imbalanceShiftMult"]),
	)

	return targets
}

// approach applies proportional control with momentum to move a knob toward its target.
// Returns true if the knob was actually moved. Must be called within locked context.
func (o *OptimizerV2) approach(knob string, target float64) bool {
	momentum := o.cfg.Momentum
	lr := o.cfg.LearningRate
	conf := o.confidence()

	direction := target - o.current[knob]
	if math.Abs(direction) < 1e-12 {
		return false
	}

	// Check directional cooldown: if this direction was recently rolled back, skip.
	now := time.Now()
	dirKey := knob + ":+"
	if direction < 0 {
		dirKey = knob + ":-"
	}
	if expiry, ok := o.rollbackDirCooldown[dirKey]; ok && now.Before(expiry) {
		return false
	}

	newVelocity := momentum*o.velocity[knob] + (1.0-momentum)*direction
	proposedDelta := lr * conf * newVelocity
	if shouldSkipKnobUpdate(knob, proposedDelta, o.cfg) {
		// Deadband: skip the write entirely (retain previous value, leave velocity
		// unchanged so noise does not accumulate momentum).
		o.pendingDeadbandSkips = append(o.pendingDeadbandSkips, knob)
		if abs := math.Abs(proposedDelta); abs > o.pendingDeadbandMaxAbs {
			o.pendingDeadbandMaxAbs = abs
		}
		return false
	}
	o.velocity[knob] = newVelocity
	o.current[knob] += proposedDelta
	o.current[knob] = clamp(o.current[knob], o.knobMin(knob), o.knobMax(knob))

	// Track approach direction for rollback cooldown.
	if direction > 0 {
		o.lastApproachDir[knob] = 1.0
	} else if direction < 0 {
		o.lastApproachDir[knob] = -1.0
	}

	return true
}

// maybeLogDeadbandSkip emits a rate-limited (30s/coin) INFO line summarizing
// any knobs whose proposed updates were below their deadband this eval.
// Must be called within locked context.
func (o *OptimizerV2) maybeLogDeadbandSkip() {
	if len(o.pendingDeadbandSkips) == 0 {
		return
	}
	now := time.Now()
	if !o.lastDeadbandLog.IsZero() && now.Sub(o.lastDeadbandLog) < 30*time.Second {
		return
	}
	o.lastDeadbandLog = now
	slog.Info("optimizer deadband skip",
		"coin", o.coin,
		"knobs_skipped", append([]string(nil), o.pendingDeadbandSkips...),
		"max_delta_abs", o.pendingDeadbandMaxAbs,
	)
}

// knobMin returns the lower bound for a knob from config.
func (o *OptimizerV2) knobMin(knob string) float64 {
	switch knob {
	case "spreadDeltaBuy", "spreadDeltaSell":
		if o.cfg.SpreadBps.Min != 0 {
			return o.cfg.SpreadBps.Min
		}
		return -o.cfg.SpreadBps.Max
	case "notionalMult":
		return o.cfg.NotionalMult.Min
	case "skewMult":
		return o.cfg.SkewMult.Min
	case "alphaDelta":
		return o.cfg.VWAPAlpha.Min - o.baseAlpha
	case "imbalanceShiftMult":
		return o.cfg.ImbalanceShiftMult.Min
	case "fillDecayMult":
		return o.cfg.FillDecayMult.Min
	case "refreshMult":
		return o.cfg.RefreshMult.Min
	default:
		return -math.MaxFloat64
	}
}

// knobMax returns the upper bound for a knob from config.
func (o *OptimizerV2) knobMax(knob string) float64 {
	switch knob {
	case "spreadDeltaBuy", "spreadDeltaSell":
		return o.cfg.SpreadBps.Max
	case "notionalMult":
		return o.cfg.NotionalMult.Max
	case "skewMult":
		return o.cfg.SkewMult.Max
	case "alphaDelta":
		return o.cfg.VWAPAlpha.Max - o.baseAlpha
	case "imbalanceShiftMult":
		return o.cfg.ImbalanceShiftMult.Max
	case "fillDecayMult":
		return o.cfg.FillDecayMult.Max
	case "refreshMult":
		return o.cfg.RefreshMult.Max
	default:
		return math.MaxFloat64
	}
}

// knobDistance represents a knob and its distance from target.
type knobDistance struct {
	name     string
	distance float64
}

// knobSignificantEpsilon returns the minimum change threshold for snapshotting a knob.
func knobSignificantEpsilon(knob string) float64 {
	switch knob {
	case "spreadDeltaBuy", "spreadDeltaSell", "alphaDelta":
		return 0.1
	default:
		return 0.01
	}
}

// Evaluate is the main entry point. Returns the number of knobs changed.
// Returns 0 if disabled, halted, or not enough data.
func (o *OptimizerV2) Evaluate(inputs OptimizerV2Inputs) int {
	o.mu.Lock()
	defer o.mu.Unlock()

	if !o.cfg.Enabled || inputs.Halted {
		return 0
	}

	o.evalCount++
	o.lastCompositeMarkout = inputs.CompositeMarkoutBps
	o.pendingDeadbandSkips = o.pendingDeadbandSkips[:0]
	o.pendingDeadbandMaxAbs = 0

	// Log key inputs that drive the optimizer.
	slog.Info("[OptimizerV2] inputs",
		"coin", o.coin,
		"eval", o.evalCount,
		"fills", o.totalFills,
		"confidence", o.confidence(),
		"worst_markout_bps", fmt.Sprintf("%.2f", inputs.WorstMarkoutBps),
		"buy_markout_bps", fmt.Sprintf("%.2f", inputs.BuyMarkoutBps),
		"sell_markout_bps", fmt.Sprintf("%.2f", inputs.SellMarkoutBps),
		"composite_markout_bps", fmt.Sprintf("%.2f", inputs.CompositeMarkoutBps),
		"baseline_bps", fmt.Sprintf("%.2f", inputs.BaselineBps),
		"fill_rate_ratio", fmt.Sprintf("%.3f", inputs.FillRateRatio),
		"buy_fill_rate", fmt.Sprintf("%.3f", inputs.BuyFillRateRatio),
		"sell_fill_rate", fmt.Sprintf("%.3f", inputs.SellFillRateRatio),
		"fill_samples", inputs.FillSamples,
		"buy_fills", inputs.BuyFillSamples,
		"sell_fills", inputs.SellFillSamples,
		"inv_util", fmt.Sprintf("%.3f", inputs.InventoryUtil),
		"inv_side", inputs.InventorySide,
		"autocorr", fmt.Sprintf("%.3f", inputs.Autocorr),
		"vwap_dev", fmt.Sprintf("%.5f", inputs.VWAPDeviation),
		"spread_bbo_ratio", fmt.Sprintf("%.2f", inputs.SpreadBBORatio),
		"budget_pressure", fmt.Sprintf("%.3f", inputs.BudgetPressure),
		"imb_sat_pct", fmt.Sprintf("%.3f", inputs.ImbalanceSaturationPct),
	)

	state := o.computeState(inputs)
	targets := o.policyTargets(state, inputs)

	// Rank knobs by distance from target.
	distances := make([]knobDistance, 0, len(v2Knobs))
	for _, k := range v2Knobs {
		d := math.Abs(targets[k] - o.current[k])
		distances = append(distances, knobDistance{name: k, distance: d})
	}
	sort.Slice(distances, func(i, j int) bool {
		return distances[i].distance > distances[j].distance
	})

	// Determine how many knobs to move.
	maxChanges := o.cfg.MaxKnobChangesPerEval
	if maxChanges <= 0 {
		maxChanges = 3
	}

	// Emergency bypass: fire if combined toxicity exceeds threshold, OR if any
	// toxicity sub-dimension (markout-EWMA or emergency-count) is saturated.
	// The sub-dim OR was added after v18 eval 503 where tox_emergency=0.33 and
	// sell_markout=-10.41 bps but combined tox peaked at 0.60 < 0.9, so the
	// emergency path never engaged during the session's worst adverse event.
	//
	// ToxEmergency threshold raised from 0.2 to 0.5: with EmergencyCount/3.0
	// scaling, 0.2 fired on a single adverse fill (streak=1 → 0.333), which
	// triggered emergency ENTER at combined tox 0.127 in v21 logs. Require
	// streak >= 2 (tox_emergency >= 0.667) before sub-dim bypass, so a single
	// adverse fill no longer overrides the combined gate. Deep markout
	// saturation still fires via the ToxMarkout > 0.95 branch.
	emergency := o.cfg.EmergencyToxicityThreshold > 0 &&
		(state.Toxicity > o.cfg.EmergencyToxicityThreshold ||
			state.ToxEmergency > 0.5 ||
			state.ToxMarkout > 0.95)
	if emergency {
		maxChanges = len(v2Knobs)
	}

	// Log emergency transitions. Include sub-dim values so operators can see
	// whether combined tox or a saturated sub-dim triggered the gate.
	if emergency && !o.prevEmergency {
		slog.Info("[OptimizerV2] emergency ENTER",
			"coin", o.coin,
			"eval", o.evalCount,
			"toxicity", state.Toxicity,
			"tox_markout", state.ToxMarkout,
			"tox_emergency", state.ToxEmergency,
			"threshold", o.cfg.EmergencyToxicityThreshold,
		)
	} else if !emergency && o.prevEmergency {
		slog.Info("[OptimizerV2] emergency EXIT",
			"coin", o.coin,
			"eval", o.evalCount,
			"toxicity", state.Toxicity,
			"tox_markout", state.ToxMarkout,
			"tox_emergency", state.ToxEmergency,
			"threshold", o.cfg.EmergencyToxicityThreshold,
		)
	}
	o.prevEmergency = emergency

	// Snapshot pre-approach values for per-knob delta logging.
	prev := make(map[string]float64, len(v2Knobs))
	for _, k := range v2Knobs {
		prev[k] = o.current[k]
	}

	changed := 0
	var knobDetails []string
	for i, kd := range distances {
		if !emergency && i >= maxChanges {
			break
		}
		if kd.distance < 1e-9 {
			continue
		}
		prevVal := o.current[kd.name]
		if o.approach(kd.name, targets[kd.name]) {
			changed++
			delta := o.current[kd.name] - prevVal
			knobDetails = append(knobDetails, fmt.Sprintf("%s:%.4f->%.4f(tgt=%.4f,vel=%.4f)",
				kd.name, prevVal, o.current[kd.name], targets[kd.name], o.velocity[kd.name]))
			_ = delta // used in the formatted string above
		} else {
			// Knob was skipped (cooldown or at target).
			now := time.Now()
			dirKey := kd.name + ":+"
			if targets[kd.name]-o.current[kd.name] < 0 {
				dirKey = kd.name + ":-"
			}
			if expiry, ok := o.rollbackDirCooldown[dirKey]; ok && now.Before(expiry) {
				knobDetails = append(knobDetails, fmt.Sprintf("%s:COOLDOWN(%.0fs)",
					kd.name, time.Until(expiry).Seconds()))
			}
		}
	}

	// Per-knob selective rollback: snapshot markout when target shifts significantly.
	for _, k := range v2Knobs {
		prevTarget, ok := o.perKnobTargetSnapshot[k]
		eps := knobSignificantEpsilon(k)
		if !ok || math.Abs(targets[k]-prevTarget) > eps {
			o.perKnobTargetSnapshot[k] = targets[k]
			o.perKnobMarkout[k] = inputs.CompositeMarkoutBps
		}
	}

	// P4: rate-limited "deadband skip" log — meta observation, NOT a fired policy action.
	o.maybeLogDeadbandSkip()

	// Update overlay from current knob values.
	o.overlay = OptimizerOverlay{
		SpreadDelta:        math.Max(o.current["spreadDeltaBuy"], o.current["spreadDeltaSell"]),
		SpreadDeltaBuy:     o.current["spreadDeltaBuy"],
		SpreadDeltaSell:    o.current["spreadDeltaSell"],
		NotionalMult:       o.current["notionalMult"],
		SkewMult:           o.current["skewMult"],
		AlphaDelta:         o.current["alphaDelta"],
		ImbalanceShiftMult: o.current["imbalanceShiftMult"],
		FillDecayMult:      o.current["fillDecayMult"],
		RefreshMult:        o.current["refreshMult"],
	}

	// Self-evaluate after applying changes.
	o.selfEvaluate(inputs)

	// Log per-knob approach detail.
	if changed > 0 {
		detailStr := strings.Join(knobDetails, " | ")
		slog.Info("[OptimizerV2] evaluate",
			"coin", o.coin,
			"eval", o.evalCount,
			"fills", o.totalFills,
			"confidence", o.confidence(),
			"tox", state.Toxicity,
			"opp", state.Opportunity,
			"inv_stress", state.InventoryStress,
			"budget_p", state.BudgetPressure,
			"fourth_dim_label", fourthDimLabel(o.cfg),
			"spread_buy", o.current["spreadDeltaBuy"],
			"spread_sell", o.current["spreadDeltaSell"],
			"notional", o.current["notionalMult"],
			"skew", o.current["skewMult"],
			"alpha", o.current["alphaDelta"],
			"fill_decay", o.current["fillDecayMult"],
			"refresh", o.current["refreshMult"],
			"imb_shift", o.current["imbalanceShiftMult"],
			"changed", changed,
			"emergency", emergency,
			"knobs", detailStr,
		)
	} else {
		slog.Info("[OptimizerV2] evaluate (no-op)",
			"coin", o.coin,
			"eval", o.evalCount,
			"fills", o.totalFills,
			"confidence", o.confidence(),
		)
	}

	// Detect state dimension threshold crossings.
	if o.prevState != nil {
		o.logStateTransitions(state)
	}
	o.prevState = &state

	return changed
}

// logStateTransitions logs when state dimensions cross policy thresholds.
func (o *OptimizerV2) logStateTransitions(state stateVector) {
	pt := o.cfg.PolicyThresholds
	prev := o.prevState

	type crossing struct {
		dim       string
		threshold string
		prevVal   float64
		newVal    float64
		level     float64
	}

	var crossings []crossing

	// Toxicity thresholds.
	for _, tc := range []struct {
		name  string
		level float64
	}{
		{"low", pt.ToxicityLow},
		{"medium", pt.ToxicityMedium},
		{"high", pt.ToxicityHigh},
	} {
		if (prev.Toxicity <= tc.level && state.Toxicity > tc.level) ||
			(prev.Toxicity > tc.level && state.Toxicity <= tc.level) {
			crossings = append(crossings, crossing{"tox", tc.name, prev.Toxicity, state.Toxicity, tc.level})
		}
	}

	// Opportunity thresholds.
	for _, tc := range []struct {
		name  string
		level float64
	}{
		{"low", pt.OpportunityLow},
		{"high", pt.OpportunityHigh},
	} {
		if (prev.Opportunity <= tc.level && state.Opportunity > tc.level) ||
			(prev.Opportunity > tc.level && state.Opportunity <= tc.level) {
			crossings = append(crossings, crossing{"opp", tc.name, prev.Opportunity, state.Opportunity, tc.level})
		}
	}

	// Inventory thresholds.
	for _, tc := range []struct {
		name  string
		level float64
	}{
		{"low", pt.InventoryLow},
		{"high", pt.InventoryHigh},
	} {
		if (prev.InventoryStress <= tc.level && state.InventoryStress > tc.level) ||
			(prev.InventoryStress > tc.level && state.InventoryStress <= tc.level) {
			crossings = append(crossings, crossing{"inv", tc.name, prev.InventoryStress, state.InventoryStress, tc.level})
		}
	}

	// Budget thresholds.
	for _, tc := range []struct {
		name  string
		level float64
	}{
		{"medium", pt.BudgetMedium},
		{"high", pt.BudgetHigh},
	} {
		if (prev.BudgetPressure <= tc.level && state.BudgetPressure > tc.level) ||
			(prev.BudgetPressure > tc.level && state.BudgetPressure <= tc.level) {
			crossings = append(crossings, crossing{"budget", tc.name, prev.BudgetPressure, state.BudgetPressure, tc.level})
		}
	}

	for _, c := range crossings {
		dir := "above"
		if c.newVal <= c.level {
			dir = "below"
		}
		slog.Info("[OptimizerV2] state crossing",
			"coin", o.coin,
			"eval", o.evalCount,
			"dim", c.dim,
			"threshold", c.threshold,
			"direction", dir,
			"prev", fmt.Sprintf("%.4f", c.prevVal),
			"new", fmt.Sprintf("%.4f", c.newVal),
			"level", fmt.Sprintf("%.4f", c.level),
		)
	}
}

// selfEvaluate tracks utility and triggers graduated rollback if utility is
// negative for rollbackWindowEvals consecutive evals.
func (o *OptimizerV2) selfEvaluate(inputs OptimizerV2Inputs) {
	// Use average fill rate (not min as in computeState). Opportunity uses min() to
	// detect bottlenecks. Utility uses avg() because total P&L scales with aggregate
	// fill volume, not just the weaker side.
	avgFillRate := inputs.FillRateRatio
	if inputs.BuyFillRateRatio > 0 && inputs.SellFillRateRatio > 0 {
		avgFillRate = (inputs.BuyFillRateRatio + inputs.SellFillRateRatio) / 2.0
	}
	utility := inputs.CompositeMarkoutBps * math.Max(avgFillRate, 0.01)
	o.utilityHistory = append(o.utilityHistory, utility)

	window := o.cfg.RollbackWindowEvals
	if window <= 0 {
		window = 3
	}

	// Trim history to prevent unbounded growth (keep 2x window for headroom).
	maxHistory := window * 2
	if len(o.utilityHistory) > maxHistory {
		o.utilityHistory = o.utilityHistory[len(o.utilityHistory)-maxHistory:]
	}

	// Check if the last N utilities are all negative.
	if len(o.utilityHistory) < window {
		slog.Info("[OptimizerV2] utility",
			"coin", o.coin,
			"eval", o.evalCount,
			"utility", fmt.Sprintf("%.4f", utility),
			"avg_fill_rate", fmt.Sprintf("%.3f", avgFillRate),
			"composite_markout", fmt.Sprintf("%.2f", inputs.CompositeMarkoutBps),
			"history_len", len(o.utilityHistory),
			"window", window,
			"ready", false,
		)
		return
	}
	recent := o.utilityHistory[len(o.utilityHistory)-window:]
	allNegative := true
	negCount := 0
	for _, u := range recent {
		if u < 0 {
			negCount++
		}
		if u >= 0 {
			allNegative = false
		}
	}
	o.lastAllNegative = allNegative

	slog.Info("[OptimizerV2] utility",
		"coin", o.coin,
		"eval", o.evalCount,
		"utility", fmt.Sprintf("%.4f", utility),
		"avg_fill_rate", fmt.Sprintf("%.3f", avgFillRate),
		"composite_markout", fmt.Sprintf("%.2f", inputs.CompositeMarkoutBps),
		"history_len", len(o.utilityHistory),
		"window", window,
		"neg_count", negCount,
		"all_negative", allNegative,
		"ready", true,
	)

	if allNegative {
		// V30 part 5: absolute-floor bypass. When composite markout is below
		// RollbackAbsoluteFloorBps across the window, force rollback of every
		// knob regardless of per-knob Δ-from-snapshot. V29 session had 484
		// consecutive all_negative evals with rollback firing ≥1 knob only 7.6%
		// of the time because the Δ test ("cur < snap") rejected uniformly-bad
		// series. Absolute floor catches slow drifts the Δ rule can't see.
		floor := o.cfg.RollbackAbsoluteFloorBps
		floorBreached := false
		if floor < 0 {
			// All utilities in the window must also have composite below floor.
			// We only track composite at the latest eval (lastCompositeMarkout);
			// gate on that plus the all_negative=true precondition above.
			floorBreached = o.lastCompositeMarkout < floor
		}
		o.graduatedRollback(floorBreached)
	}
}

// graduatedRollback reverts knobs partially toward their shadow (session-start) values.
// Uses per-knob selective rollback: only rolls back knobs whose markout has worsened
// since the snapshot, plus directional cooldown to prevent apply->rollback->reapply cycles.
//
// forceAll bypasses the per-knob Δ check (V30 part 5: triggered by absolute-floor
// breach). Every knob with a non-shadow value rolls back at `rate`.
func (o *OptimizerV2) graduatedRollback(forceAll bool) {
	rate := o.cfg.RollbackRate
	if rate <= 0 {
		rate = 0.5
	}

	cooldownSec := o.cfg.RollbackCooldownSec
	if cooldownSec <= 0 {
		cooldownSec = 120
	}
	cooldownExpiry := time.Now().Add(time.Duration(cooldownSec) * time.Second)

	// Build per-knob rollback detail BEFORE modifying state.
	var rbDetails []string
	for _, k := range v2Knobs {
		if forceAll {
			rbDetails = append(rbDetails, fmt.Sprintf("%s:rollback(abs_floor,cur_m=%.2f,prev=%.4f)",
				k, o.lastCompositeMarkout, o.current[k]))
			continue
		}
		if snapshotMarkout, ok := o.perKnobMarkout[k]; ok {
			if o.lastCompositeMarkout >= snapshotMarkout {
				rbDetails = append(rbDetails, fmt.Sprintf("%s:kept(markout_ok:%.2f>=%.2f)",
					k, o.lastCompositeMarkout, snapshotMarkout))
				continue
			}
			rbDetails = append(rbDetails, fmt.Sprintf("%s:rollback(snap_m=%.2f,cur_m=%.2f,prev=%.4f)",
				k, snapshotMarkout, o.lastCompositeMarkout, o.current[k]))
		} else {
			rbDetails = append(rbDetails, fmt.Sprintf("%s:rollback(no_snapshot,prev=%.4f)", k, o.current[k]))
		}
	}

	rolledBack := 0
	for _, k := range v2Knobs {
		// Per-knob selective: only roll back knobs whose markout has worsened,
		// unless forceAll (V30 part 5 absolute-floor bypass) overrides the check.
		if !forceAll {
			if snapshotMarkout, ok := o.perKnobMarkout[k]; ok {
				if o.lastCompositeMarkout >= snapshotMarkout {
					continue // markout is same or better — skip this knob
				}
			}
		}
		// else: no snapshot data or forceAll → roll back conservatively

		o.current[k] -= rate * (o.current[k] - o.shadow[k])
		o.current[k] = clamp(o.current[k], o.knobMin(k), o.knobMax(k))
		o.velocity[k] = 0 // reset momentum
		rolledBack++

		// Set directional cooldown on the direction this knob was previously moving.
		if dir, ok := o.lastApproachDir[k]; ok {
			dirStr := ":+"
			if dir < 0 {
				dirStr = ":-"
			}
			o.rollbackDirCooldown[k+dirStr] = cooldownExpiry
		}
	}

	// Clear per-knob tracking only for rolled-back knobs. Preserve snapshots for
	// knobs that were not rolled back so selectivity persists across rollback rounds.
	// V30 part 5: forceAll rolls back everything, so clear every knob's tracking.
	for _, k := range v2Knobs {
		if !forceAll {
			if snapshotMarkout, ok := o.perKnobMarkout[k]; ok {
				if o.lastCompositeMarkout >= snapshotMarkout {
					continue // this knob was NOT rolled back — preserve its snapshot
				}
			}
		}
		delete(o.perKnobMarkout, k)
		delete(o.perKnobTargetSnapshot, k)
		delete(o.lastApproachDir, k)
	}

	// Update overlay immediately so the pipeline sees the rolled-back values
	// this cycle (not one eval later).
	o.overlay = OptimizerOverlay{
		SpreadDelta:        math.Max(o.current["spreadDeltaBuy"], o.current["spreadDeltaSell"]),
		SpreadDeltaBuy:     o.current["spreadDeltaBuy"],
		SpreadDeltaSell:    o.current["spreadDeltaSell"],
		NotionalMult:       o.current["notionalMult"],
		SkewMult:           o.current["skewMult"],
		AlphaDelta:         o.current["alphaDelta"],
		ImbalanceShiftMult: o.current["imbalanceShiftMult"],
		FillDecayMult:      o.current["fillDecayMult"],
		RefreshMult:        o.current["refreshMult"],
	}

	slog.Info("[OptimizerV2] graduated rollback",
		"coin", o.coin,
		"eval", o.evalCount,
		"rate", rate,
		"rolled_back", rolledBack,
		"abs_floor_breach", forceAll,
		"spread_buy", o.current["spreadDeltaBuy"],
		"spread_sell", o.current["spreadDeltaSell"],
		"notional", o.current["notionalMult"],
		"skew", o.current["skewMult"],
		"alpha", o.current["alphaDelta"],
		"fill_decay", o.current["fillDecayMult"],
		"refresh", o.current["refreshMult"],
		"imb_shift", o.current["imbalanceShiftMult"],
		"cooldown_sec", cooldownSec,
		"detail", strings.Join(rbDetails, " | "),
	)
}

// EmergencyWiden immediately widens the spread on a specific side.
func (o *OptimizerV2) EmergencyWiden(side string, deltaBps float64) {
	o.mu.Lock()
	defer o.mu.Unlock()

	switch side {
	case "buy", "B", "bid":
		o.current["spreadDeltaBuy"] += deltaBps
		o.current["spreadDeltaBuy"] = clamp(o.current["spreadDeltaBuy"], o.knobMin("spreadDeltaBuy"), o.knobMax("spreadDeltaBuy"))
	case "sell", "A", "ask":
		o.current["spreadDeltaSell"] += deltaBps
		o.current["spreadDeltaSell"] = clamp(o.current["spreadDeltaSell"], o.knobMin("spreadDeltaSell"), o.knobMax("spreadDeltaSell"))
	default:
		// Both sides.
		o.current["spreadDeltaBuy"] += deltaBps
		o.current["spreadDeltaBuy"] = clamp(o.current["spreadDeltaBuy"], o.knobMin("spreadDeltaBuy"), o.knobMax("spreadDeltaBuy"))
		o.current["spreadDeltaSell"] += deltaBps
		o.current["spreadDeltaSell"] = clamp(o.current["spreadDeltaSell"], o.knobMin("spreadDeltaSell"), o.knobMax("spreadDeltaSell"))
	}

	// Update overlay.
	o.overlay.SpreadDeltaBuy = o.current["spreadDeltaBuy"]
	o.overlay.SpreadDeltaSell = o.current["spreadDeltaSell"]
	o.overlay.SpreadDelta = math.Max(o.current["spreadDeltaBuy"], o.current["spreadDeltaSell"])

	slog.Info("[OptimizerV2] emergency widen",
		"coin", o.coin,
		"side", side,
		"delta_bps", deltaBps,
		"spread_buy", o.current["spreadDeltaBuy"],
		"spread_sell", o.current["spreadDeltaSell"],
	)
}
