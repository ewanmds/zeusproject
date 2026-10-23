package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"hyperliquid-market-maker/internal/types"

	"gopkg.in/yaml.v3"
)

// timeLoadLocation is an alias retained to keep validate logic readable.
var timeLoadLocation = time.LoadLocation

// StrategyConfig holds spread strategy parameters.
type StrategyConfig struct {
	Assets              []string `yaml:"assets"`
	SpreadType          string   `yaml:"spread_type"`
	LookbackWindow      int      `yaml:"lookback_window"`
	UseBetaHedge        bool     `yaml:"use_beta_hedge"`
	SpreadEngineEnabled *bool    `yaml:"spread_engine_enabled"` // nil = auto (enabled with 2+ assets), false = disabled
	ZMinStdDev          float64  `yaml:"z_min_stddev"`          // floor for spread stddev before z-score computation; 0 = no floor
	BetaMin             float64  `yaml:"beta_min"`              // lower OLS beta clamp; default 0.5
	BetaMax             float64  `yaml:"beta_max"`              // upper OLS beta clamp; default 1.8
}

// KnobBoundsConfig defines bounds for a single optimizer knob.
type KnobBoundsConfig struct {
	Min float64 `yaml:"min"`
	Max float64 `yaml:"max"`
}

// OptimizerV2DimensionWeights defines weights for toxicity dimension signals.
type OptimizerV2DimensionWeights struct {
	MarkoutEWMA    float64 `yaml:"markout_ewma"`
	FillBurst      float64 `yaml:"fill_burst"`
	EmergencyCount float64 `yaml:"emergency_count"`
}

// OptimizerV2OpportunityWeights defines weights for opportunity dimension signals.
type OptimizerV2OpportunityWeights struct {
	FillRateRatio  float64 `yaml:"fill_rate_ratio"`
	RegimeSignal   float64 `yaml:"regime_signal"`
	VWAPDeviation  float64 `yaml:"vwap_deviation"`
	SpreadBBORatio float64 `yaml:"spread_bbo_ratio"`
}

// OptimizerV2InventoryWeights defines weights for inventory stress dimension signals.
type OptimizerV2InventoryWeights struct {
	PositionUtil     float64 `yaml:"position_util"`
	ControllerAdj    float64 `yaml:"controller_adj"`
	SameSideDuration float64 `yaml:"same_side_duration"`
}

// OptimizerV2PolicyThresholds defines configurable thresholds for policy decisions.
type OptimizerV2PolicyThresholds struct {
	ToxicityHigh      float64 `yaml:"toxicity_high"`      // 0.6
	ToxicityMedium    float64 `yaml:"toxicity_medium"`    // 0.5 — notional reduction, refresh slowdown
	ToxicityLow       float64 `yaml:"toxicity_low"`       // 0.3
	OpportunityHigh   float64 `yaml:"opportunity_high"`   // 0.5
	OpportunityLow    float64 `yaml:"opportunity_low"`    // 0.3
	InventoryHigh     float64 `yaml:"inventory_high"`     // 0.4
	InventoryLow      float64 `yaml:"inventory_low"`      // 0.15
	BudgetHigh        float64 `yaml:"budget_high"`        // 0.7
	BudgetMedium      float64 `yaml:"budget_medium"`      // 0.5 — refresh opportunity gate
	AutocorrThreshold float64 `yaml:"autocorr_threshold"` // 0.1
	FillRateDelta     float64 `yaml:"fill_rate_delta"`    // 0.5
	ImbalanceSatHigh  float64 `yaml:"imbalance_sat_high"` // 0.6
	ImbalanceSatLow   float64 `yaml:"imbalance_sat_low"`  // 0.2
	PeerInventoryMin  float64 `yaml:"peer_inventory_min"` // 0.2 — min peer util for cross-asset signals

	// P8: opp_tighten gate. Both default zero (disabled). When enabled, blocks
	// the opp_tighten branch from firing while inventory is adverse with
	// negative unrealized PnL, or while session PnL is below a configured floor.
	OppTightenInvUtilGate        float64 `yaml:"opp_tighten_inv_util_gate"`         // |inv_util| above this with unrealized<0 blocks opp_tighten; 0 = disabled
	OppTightenSessionPnLFloorBps float64 `yaml:"opp_tighten_session_pnl_floor_bps"` // session PnL bps floor; below this blocks opp_tighten; 0 = disabled (negative = active)

	// Skew relax on adverse markout. Turns skewMult into a feedback signal:
	// when recent fills have been adverse (CompositeMarkoutBps below threshold)
	// and we're carrying inventory, scale skewMult down linearly toward a
	// floor. Motivation: aggressive inventory skew that gets picked off
	// repeatedly is worse than neutral skew — the original skewMult was
	// effectively a dead knob in single-asset configs because skew_inv_stress
	// only fires above InventoryHigh (0.4). Zero values disable the rule.
	SkewRelaxAdverseMarkoutBps float64 `yaml:"skew_relax_adverse_markout_bps"` // markout threshold; 0 = disabled; must be negative when enabled
	SkewRelaxInvUtilMin        float64 `yaml:"skew_relax_inv_util_min"`        // |inv_util| floor for rule to fire; typical 0.15
	SkewRelaxFloor             float64 `yaml:"skew_relax_floor"`               // minimum skew multiplier; 0 = disabled; typical 0.6
	SkewRelaxDivisor           float64 `yaml:"skew_relax_divisor"`             // bps-of-adversity-per-unit-relax divisor; 0 = default 2.0

	// V31: Regime-widen entry asymmetry — apply more spread widening to the
	// entry side than the exit side during regime_widen. Entry gets entryMult×,
	// exit gets (2-entryMult)× so the total widening is preserved. 1.0 = symmetric
	// (legacy). 0 = default 1.0.
	RegimeWidenEntryMult float64 `yaml:"regime_widen_entry_mult"`
}

// OptimizerV2Config configures the gradient-based optimizer v2.
type OptimizerV2Config struct {
	Enabled                       bool                          `yaml:"enabled"`
	ShadowMode                    bool                          `yaml:"shadow_mode"`
	EvalIntervalSec               int                           `yaml:"eval_interval_sec"`
	WarmupEvals                   int                           `yaml:"warmup_evals"`
	MinFillSamples                int                           `yaml:"min_fill_samples"`
	MinFillSampleWindowSec        int                           `yaml:"min_fill_sample_window_sec"` // rolling window for fill counting; 0 = per-eval (legacy)
	Momentum                      float64                       `yaml:"momentum"`
	LearningRate                  float64                       `yaml:"learning_rate"`
	MaxKnobChangesPerEval         int                           `yaml:"max_knob_changes_per_eval"`
	EmergencyToxicityThreshold    float64                       `yaml:"emergency_toxicity_threshold"`
	RollbackWindowEvals           int                           `yaml:"rollback_window_evals"`
	RollbackRate                  float64                       `yaml:"rollback_rate"`
	RollbackCooldownSec           int                           `yaml:"rollback_cooldown_sec"`                // directional cooldown after rollback; default 120
	RollbackAbsoluteFloorBps      float64                       `yaml:"rollback_absolute_floor_bps"`          // V30 part 5: when composite_markout < floor across RollbackWindowEvals samples, force rollback regardless of Δ-from-snapshot. 0 = disabled (Δ-only); must be <= 0.
	BurstEvalEnabled              bool                          `yaml:"burst_eval_enabled"`                   // V20 (second bundle): trigger out-of-cadence optimizer eval when volume burst activates
	BurstEvalMinIntervalSec       int                           `yaml:"burst_eval_min_interval_sec"`          // min seconds between burst-triggered evals (throttle)
	BurstEvalAllNegCooldownSec    int                           `yaml:"burst_eval_all_negative_cooldown_sec"` // V30 part 5: extended burst cooldown when utility has been all_negative. 0 = disabled (use BurstEvalMinIntervalSec always).
	ForceRefreshOnEmergency       bool                          `yaml:"force_refresh_on_emergency"`           // V21: force quote refresh after material emergency overlay changes
	ForceRefreshMinIntervalMs     int                           `yaml:"force_refresh_min_interval_ms"`
	ForceRefreshMinSpreadDeltaBps float64                       `yaml:"force_refresh_min_spread_delta_bps"`
	ToxicityWeights               OptimizerV2DimensionWeights   `yaml:"toxicity_weights"`
	OpportunityWeights            OptimizerV2OpportunityWeights `yaml:"opportunity_weights"`
	InventoryStressWeights        OptimizerV2InventoryWeights   `yaml:"inventory_stress_weights"`
	PolicyThresholds              OptimizerV2PolicyThresholds   `yaml:"policy_thresholds"`
	SpreadBps                     KnobBoundsConfig              `yaml:"spread_bps"`
	NotionalMult                  KnobBoundsConfig              `yaml:"notional_mult"`
	SkewMult                      KnobBoundsConfig              `yaml:"skew_mult"`
	VWAPAlpha                     KnobBoundsConfig              `yaml:"vwap_alpha"`
	ImbalanceShiftMult            KnobBoundsConfig              `yaml:"imbalance_shift_mult"`
	FillDecayMult                 KnobBoundsConfig              `yaml:"fill_decay_mult"`
	RefreshMult                   KnobBoundsConfig              `yaml:"refresh_mult"`

	// P4: per-knob deadbands; updates with |delta| below threshold are skipped.
	SpreadDeltaDeadbandBps float64 `yaml:"spread_delta_deadband_bps"`
	SkewDeltaDeadband      float64 `yaml:"skew_delta_deadband"`
	NotionalDeltaDeadband  float64 `yaml:"notional_delta_deadband"`

	// P14: empirical-quantile sub-dimension normalization. Replaces the
	// hardcoded linear clips in computeState with rolling p_low/p_high
	// percentile normalization. Default "" / "linear" preserves v22.1
	// behavior bit-exactly.
	SubdimNormalization       string `yaml:"subdim_normalization"`         // "" | "linear" | "empirical_quantile"
	SubdimQuantileWindowEvals int    `yaml:"subdim_quantile_window_evals"` // rolling window size; default 500
	SubdimQuantileLowPct      int    `yaml:"subdim_quantile_low_pct"`      // low percentile (1..25); default 5
	SubdimQuantileHighPct     int    `yaml:"subdim_quantile_high_pct"`     // high percentile (75..99); default 95

	// P5: 4th-dimension semantics. When "session_pnl", the 4D state's 4th
	// axis is driven by SessionPnLBps bucketized via SessionPnLBucketEdgesBps
	// instead of BudgetPressure. Defensive bucket (>= FourthDimDefensiveFloor)
	// triggers a post-policy clamp that prevents net spread tightening below
	// baseline. Default "" / "budget_pressure" preserves prior behavior.
	FourthDimension          string    `yaml:"fourth_dimension"`             // "" | "budget_pressure" | "session_pnl"
	SessionPnLBucketEdgesBps []float64 `yaml:"session_pnl_bucket_edges_bps"` // [b1, b2, b3, b4]; default [-20,-10,10,20]
	FourthDimDefensiveFloor  float64   `yaml:"fourth_dim_defensive_floor"`   // defensive-bucket threshold in (0, 1]; default 0.75
}

// TradingScheduleWindowConfig describes a single time-of-day window in which
// per-asset notional is scaled by a multiplier in [0, 1]. All fields are
// required. Windows that cross midnight must be split into two entries.
type TradingScheduleWindowConfig struct {
	Start              string  `yaml:"start"`               // "HH:MM" local time in Tz
	End                string  `yaml:"end"`                 // "HH:MM" local time in Tz, exclusive
	Tz                 string  `yaml:"tz"`                  // IANA zone, e.g. "America/New_York"
	NotionalMultiplier float64 `yaml:"notional_multiplier"` // [0, 1] multiplier applied to notional while active
	Label              string  `yaml:"label"`               // short identifier for logs (e.g. "cme_open")
}

// MarketMakingConfig holds quoting parameters.
type MarketMakingConfig struct {
	QuoteSpreadBps          float64 `yaml:"quote_spread_bps"`
	QuoteSpreadBpsBuy       float64 `yaml:"quote_spread_bps_buy"`  // per-side buy base spread; 0 = use quote_spread_bps
	QuoteSpreadBpsSell      float64 `yaml:"quote_spread_bps_sell"` // per-side sell base spread; 0 = use quote_spread_bps
	QuoteNotional           float64 `yaml:"quote_notional"`
	RefreshIntervalMs       int     `yaml:"refresh_interval_ms"`
	RefreshPostFillMs       int     `yaml:"refresh_post_fill_ms"`       // faster refresh after fill; 0 = use base
	RefreshAdverseMs        int     `yaml:"refresh_adverse_ms"`         // slower refresh when adverse; 0 = use base
	RefreshQuietMs          int     `yaml:"refresh_quiet_ms"`           // slower refresh during quiet periods; 0 = use base
	RefreshQuietThresholdMs int     `yaml:"refresh_quiet_threshold_ms"` // book silence threshold for quiet mode; 0 = use base
	InventorySkewFactor     float64 `yaml:"inventory_skew_factor"`
	InventorySkewGamma      float64 `yaml:"inventory_skew_gamma"`   // convex skew exponent; 1.0 = linear
	LadderLevels            int     `yaml:"ladder_levels"`          // orders per side (1 = current behavior)
	LadderSpacingBps        int     `yaml:"ladder_spacing_bps"`     // bps between rungs
	DynamicWidth            bool    `yaml:"dynamic_width"`          // enable microstructure-aware width
	WidthVolWindow          int     `yaml:"width_vol_window"`       // samples for realized vol; default 60
	WidthMaxMultiplier      float64 `yaml:"width_max_multiplier"`   // cap on width scaling; default 3.0
	WidthImbalanceLevels    int     `yaml:"width_imbalance_levels"` // depth levels for imbalance; default 5
	RequoteThresholdBps     int     `yaml:"requote_threshold_bps"`  // minimum price delta to refresh existing quotes
	SizeRequotePct          float64 `yaml:"size_requote_pct"`       // minimum size delta fraction to refresh
	MaxQuoteAgeMs           int     `yaml:"max_quote_age_ms"`       // force refresh after this age
	MaxCrossLegSkewMs       int     `yaml:"max_cross_leg_skew_ms"`  // max arrival-time skew between spread legs
	MaxBookStalenessMs      int     `yaml:"max_book_staleness_ms"`  // cancel orders if book data is older than this; 0 = disabled

	// VWAP
	VWAPShortWindowMinutes int     `yaml:"vwap_short_window_minutes"` // short VWAP for fair value; default 5
	VWAPLongWindowMinutes  int     `yaml:"vwap_long_window_minutes"`  // long VWAP for wick sizing; default 30
	VWAPAlpha              float64 `yaml:"vwap_alpha"`                // blend: 0 = raw mid, 1 = pure short VWAP; default 0.40

	// P2: Drift-aware adaptive vwap_alpha. Fades alpha toward a floor when
	// |short_vwap - long_vwap| / sigma exceeds K * sigma so FV anchors more to
	// microprice during sustained trends. Default OFF preserves prior behavior.
	VWAPAlphaDriftScalingEnabled bool    `yaml:"vwap_alpha_drift_scaling_enabled"`
	VWAPAlphaDriftK              float64 `yaml:"vwap_alpha_drift_k"` // sigma multiplier; higher = less aggressive fade
	VWAPAlphaFloor               float64 `yaml:"vwap_alpha_floor"`   // never fade alpha below this

	// V31: Trend-aware alpha boost — boost alpha toward ceiling during confirmed
	// trends so FV anchors closer to (lagging) VWAP, naturally widening entry-side
	// quotes. Operates independently of P2 drift fade.
	VWAPAlphaTrendBoostEnabled bool    `yaml:"vwap_alpha_trend_boost_enabled"`
	VWAPAlphaTrendBoostCeiling float64 `yaml:"vwap_alpha_trend_boost_ceiling"` // boost alpha up to this value during trends; must be >= vwap_alpha

	// V31: Trend-momentum FV bias — shift fair value in trend direction based on
	// autocorrelation. Positive autocorr (uptrend) shifts FV up, widening asks
	// and tightening bids; vice versa for downtrends.
	TrendFVDriftBps         float64 `yaml:"trend_fv_drift_bps"`          // max FV shift per unit of autocorrelation; 0 = disabled
	TrendFVDriftAutocorrMin float64 `yaml:"trend_fv_drift_autocorr_min"` // min |autocorr| to trigger FV shift; default 0.1

	// V31: Entry reduction trend acceleration — scales entry reduction rate by
	// (1 + |adverse_autocorr| × k) so entry suppression is more aggressive during
	// confirmed trends. 0 = disabled.
	EntryReductionTrendK float64 `yaml:"entry_reduction_trend_k"`

	// Spread bias
	SpreadBiasScale       float64 `yaml:"spread_bias_scale"`        // tanh denominator; default 2.0
	SpreadBiasMaxBps      float64 `yaml:"spread_bias_max_bps"`      // max bias in bps; default 8
	SpreadBiasMaxFraction float64 `yaml:"spread_bias_max_fraction"` // max bias as fraction of half-spread; 0 = use legacy max_bps mode
	SpreadBiasAsymmetry   float64 `yaml:"spread_bias_asymmetry"`    // 0.5=symmetric, 1.0=all tighten convergence; default 0.70

	// Z-score widening (replaces circuit breaker)
	ZWideningThreshold  float64 `yaml:"z_widening_threshold"`   // start widening at this |z|; default 4.0
	ZWideningRate       float64 `yaml:"z_widening_rate"`        // width multiplier per z above threshold; default 0.15
	ZMaxWidthMultiplier float64 `yaml:"z_max_width_multiplier"` // cap on widening; default 3.0

	// Wick mean-reversion sizing
	WickSizeMultiplier         float64 `yaml:"wick_size_multiplier"`          // max sizing increase on fresh wicks; default 2.0
	WickDevThresholdPct        float64 `yaml:"wick_dev_threshold_pct"`        // min short VWAP deviation to trigger wick logic; default 0.10
	WickLongDevThresholdPct    float64 `yaml:"wick_long_dev_threshold_pct"`   // max long VWAP deviation for wick to fire; default 0.50 (10x short threshold)
	WickMaxInventoryUtil       float64 `yaml:"wick_max_inventory_util"`       // disable wick amplification above this inventory utilization; 0 = disabled (always allow wicks)
	WickTrendSuppressEnabled   bool    `yaml:"wick_trend_suppress_enabled"`   // V21: suppress adverse-side wick amplification during trend/shock guards
	WickTrendSuppressThreshold float64 `yaml:"wick_trend_suppress_threshold"` // guard suppress threshold; default 0.5

	// Ladder sizing
	LadderSizeGraduation  float64 `yaml:"ladder_size_graduation"`  // geometric ratio between levels; 1.0 = uniform, 1.5 = each level 1.5x previous
	LadderSpacingDynamic  bool    `yaml:"ladder_spacing_dynamic"`  // vol-scale inter-level spacing
	LadderSpacingExponent float64 `yaml:"ladder_spacing_exponent"` // power-law: 1.0 = linear, 1.3 = dense inner / sparse outer

	// Wall-fronting
	WallFrontEnabled                bool    `yaml:"wall_front_enabled"`       // snap nearest level in front of large resting orders
	WallMinSizeMultiplier           float64 `yaml:"wall_min_size_multiplier"` // wall must be Nx avg book level size; default 3.0
	WallQualityEnabled              bool    `yaml:"wall_quality_enabled"`     // V21: disable wall-fronting when snapped-fill quality degrades
	WallQualityHalfLifeSec          int     `yaml:"wall_quality_half_life_sec"`
	WallQualityMinScore             float64 `yaml:"wall_quality_min_score"`
	WallQualityDisableSec           int     `yaml:"wall_quality_disable_sec"`
	WallQualityHysteresisSec        int     `yaml:"wall_quality_hysteresis_sec"`         // P7: min-hold duration before flipping state; 0 = disabled (pre-P7 immediate flip)
	WallQualityReenableThresholdBps float64 `yaml:"wall_quality_reenable_threshold_bps"` // P7: asymmetric re-enable score bar (must be >= wall_quality_min_score); 0 = disabled

	// Fee awareness
	MakerFeeBps     float64 `yaml:"maker_fee_bps"`     // exchange maker fee per fill in bps
	BuilderFeeBps   float64 `yaml:"builder_fee_bps"`   // builder fee per fill in bps
	FeeFloorEnabled bool    `yaml:"fee_floor_enabled"` // enforce minimum spread >= round-trip fee cost

	// Edge gate: widen the exposure-increasing side until the quoted half-spread
	// covers measured fees + measured adverse selection + a target net edge.
	// The exit side is left to inventory skew / position-age / idle-breaker so a
	// position can always be reduced. Defaults only apply when enabled.
	EdgeGateEnabled     bool    `yaml:"edge_gate_enabled"`
	EdgeTargetBps       float64 `yaml:"edge_target_bps"`        // target net edge per round trip; default 1.0
	EdgeAdverseFloorBps float64 `yaml:"edge_adverse_floor_bps"` // adverse-selection buffer used before markout warmup; default 0.75
	EdgeMaxWidthBps     float64 `yaml:"edge_max_width_bps"`     // max half-spread the gate may impose before skipping the side; default 10
	EdgeFeeFloorBps     float64 `yaml:"edge_fee_floor_bps"`     // minimum measured fee used by the gate; default 0.5

	// Trade-flow adverse selection
	TradeImbalanceWindowSec    int     `yaml:"trade_imbalance_window_sec"`    // rolling window for trade imbalance; default 3
	TradeImbalanceMaxShiftBps  float64 `yaml:"trade_imbalance_max_shift_bps"` // max FV shift from imbalance; 0 = disabled
	TradeImbalanceSizeExponent float64 `yaml:"trade_imbalance_size_exponent"` // power-law size weight; 1.0 = raw (legacy), <1.0 dampens whales, >1.0 super-linear
	TradeImbalanceMode         string  `yaml:"trade_imbalance_mode"`          // "time" (default) or "volume" (VPIN-lite volume-clock)
	TradeImbalanceBucketSize   float64 `yaml:"trade_imbalance_bucket_size"`   // $ per volume bucket; default 5000 (volume mode only)
	TradeImbalanceBucketCount  int     `yaml:"trade_imbalance_bucket_count"`  // completed buckets to keep; default 20 (volume mode only)
	FillAdverseShiftBps        float64 `yaml:"fill_adverse_shift_bps"`        // post-fill FV shift; 0 = disabled
	FillDecayHalfLifeMs        int     `yaml:"fill_decay_half_life_ms"`       // half-life for fill decay; default 2000
	MicropriceLevels           int     `yaml:"microprice_levels"`             // book levels for microprice; default 1 (BBO-only)

	// Asymmetric adjustments
	ZWideningAsymmetry     float64 `yaml:"z_widening_asymmetry"`     // 0.5 = symmetric, 0.7 = 70% adverse side; default 0.5
	InventorySkewAsymmetry float64 `yaml:"inventory_skew_asymmetry"` // 0.5 = symmetric, 0.7 = 70% aggressive side; default 0.5

	// Cross-leg inventory awareness
	CrossLegSkewEnabled      bool    `yaml:"cross_leg_skew_enabled"`       // cross-leg inventory awareness; default false
	CrossLegSkewFactor       float64 `yaml:"cross_leg_skew_factor"`        // max cross-leg skew as fraction of spread; default 0.3
	PairReduceEntryFraction  float64 `yaml:"pair_reduce_entry_fraction"`   // combined same-side exposure fraction to enter pair-reduce mode; default 0.40. NOTE: read from global config only (cross-asset concept); per-asset overrides are ignored for entry/exit fractions
	PairReduceExitFraction   float64 `yaml:"pair_reduce_exit_fraction"`    // combined exposure fraction to exit pair-reduce mode (hysteresis); default 0.25. NOTE: read from global config only; per-asset overrides are ignored
	PairReduceTightenFactor  float64 `yaml:"pair_reduce_tighten_factor"`   // exit-side price tightening in pair-reduce mode (0.75 = 25% tighter); default 0.75. Read per-asset.
	MinCrossingSpreadBps     float64 `yaml:"min_crossing_spread_bps"`      // minimum spread maintained by crossing protection; default 2.0 bps
	PairReduceMaxDurationSec int     `yaml:"pair_reduce_max_duration_sec"` // max seconds in pair-reduce before force-deactivation; 0 = no timeout (default)

	// Fair value sanity
	MaxFVDeviationPct float64 `yaml:"max_fv_deviation_pct"` // max FV deviation from book mid before clamping to mid; default 10.0; 0 or negative = disabled

	// Feed death detection
	MaxNoBookUpdateMs int `yaml:"max_no_book_update_ms"` // cancel+close if no book update for this long; 0 = same as max_book_staleness_ms

	// Adaptive notional (Feature 2): adjust quote notional based on EWMA fill rate
	AdaptiveNotionalEnabled      bool    `yaml:"adaptive_notional_enabled"`        // off by default
	AdaptiveNotionalTargetPerMin float64 `yaml:"adaptive_notional_target_per_min"` // target filled $/min
	AdaptiveNotionalMin          float64 `yaml:"adaptive_notional_min"`            // floor $
	AdaptiveNotionalMax          float64 `yaml:"adaptive_notional_max"`            // ceiling $
	AdaptiveNotionalHalfLifeSec  int     `yaml:"adaptive_notional_half_life_sec"`  // EWMA smoothing; default 60

	// Regime detection (feeds optimizer autocorrelation input)
	RegimeWindow int `yaml:"regime_window"` // autocorrelation window; default 120

	// Adaptive z-score (Feature 5): scale z thresholds by rolling z-score stddev
	AdaptiveZEnabled   bool `yaml:"adaptive_z_enabled"`   // off by default; NOT per-asset
	ZCalibrationWindow int  `yaml:"z_calibration_window"` // number of z-score samples; default 2000

	// Trade freshness thresholds (seconds): override hardcoded 10s/30s defaults for thin markets
	TradeStaleDegradedSec int `yaml:"trade_stale_degraded_sec"` // widen spreads after this many seconds without a trade; 0 = default (10s)
	TradeStaleHaltSec     int `yaml:"trade_stale_halt_sec"`     // halt quoting after this many seconds without a trade; 0 = default (30s)

	// Post-fill markout tracking
	MarkoutEnabled         bool `yaml:"markout_enabled"`            // measure adverse selection by tracking mid price after fills
	MarkoutEWMAHalfLifeSec int  `yaml:"markout_ewma_half_life_sec"` // EWMA smoothing half-life in seconds; default 300 (5 min)

	// Warmup protection: reduce exposure while spread engine is still calibrating
	WarmupNotionalPct      float64 `yaml:"warmup_notional_pct"`      // fraction of quote_notional during warmup; default 1.0
	WarmupSpreadMultiplier float64 `yaml:"warmup_spread_multiplier"` // spread multiplier during warmup; default 1.0
	WarmupDurationSec      int     `yaml:"warmup_duration_sec"`      // time-based warmup fallback (seconds) when spread engine is disabled; 0 = disabled

	// Fill-triggered requote cooldown
	FillRefreshCooldownMs int `yaml:"fill_refresh_cooldown_ms"` // min ms between fill-triggered refreshes; default 2000

	// Fill burst circuit breaker
	FillBurstWindowSec  float64 `yaml:"fill_burst_window_sec"`  // rolling window for burst detection; 0 = disabled
	FillBurstThreshold  int     `yaml:"fill_burst_threshold"`   // same-side fills to trigger burst; default 3
	FillBurstSpreadMult float64 `yaml:"fill_burst_spread_mult"` // spread multiplier during burst; default 2.0
	FillBurstSizeFactor float64 `yaml:"fill_burst_size_factor"` // size factor during burst (0.3 = 30%); default 0.3
	FillBurstDecaySec   float64 `yaml:"fill_burst_decay_sec"`   // half-life for burst decay; default 15.0

	// Volume burst ladders: dynamically increase ladder levels when market trade velocity spikes
	VolumeBurstEnabled     bool    `yaml:"volume_burst_enabled"`     // master toggle; default false
	BurstVelocityThreshold float64 `yaml:"burst_velocity_threshold"` // fast/slow EWMA ratio to trigger; default 2.0
	BurstVelocityFastSec   float64 `yaml:"burst_velocity_fast_sec"`  // fast EWMA half-life in seconds; default 5
	BurstVelocitySlowSec   float64 `yaml:"burst_velocity_slow_sec"`  // slow EWMA half-life in seconds; default 60
	BurstVelocityMinFloor  float64 `yaml:"burst_velocity_min_floor"` // min slow rate (trades/sec) floor; default 0.5
	BurstLevelMultiplier   int     `yaml:"burst_level_multiplier"`   // multiply ladder_levels by this during burst; default 2
	BurstDecaySec          float64 `yaml:"burst_decay_sec"`          // seconds to decay back to base levels; default 30

	// Toxicity tracker parameters (shared by B2 per-side toxicity)
	ToxicityHalfLifeSec int     `yaml:"toxicity_half_life_sec"` // EWMA half-life; default 120
	ToxicityMaxWiden    float64 `yaml:"toxicity_max_widen"`     // max spread multiplier; default 2.0
	ToxicityBaselineBps float64 `yaml:"toxicity_baseline_bps"`  // expected capture bps; 0 = auto (half of quote_spread_bps)

	// A1: Exit-preserving inventory skew
	InventorySkewExitPreserve bool `yaml:"inventory_skew_exit_preserve"` // default false

	// A6: Entry-side size reduction — linearly reduce entry-side notional as inventory grows
	EntryReductionRate  float64 `yaml:"entry_size_reduction_rate"`  // reduction per unit of inventory utilization; 0 = disabled
	EntryReductionFloor float64 `yaml:"entry_size_reduction_floor"` // minimum size factor; default 0.2 when rate > 0

	// A6b: Entry acceleration penalty — power-law exponent above threshold for steeper cliff
	EntryAccelThreshold float64 `yaml:"entry_accel_threshold"` // utilization above which acceleration kicks in; 0 = disabled
	EntryAccelExponent  float64 `yaml:"entry_accel_exponent"`  // power-law exponent; 0 = disabled

	// Trend Guard: VWAP momentum entry gate
	TrendGuardEnabled              bool    `yaml:"trend_guard_enabled"`
	TrendGuardThresholdPct         float64 `yaml:"trend_guard_threshold_pct"`          // long VWAP deviation % to start suppression
	TrendGuardSaturationPct        float64 `yaml:"trend_guard_saturation_pct"`         // deviation % for max suppression
	TrendGuardMaxSuppress          float64 `yaml:"trend_guard_max_suppress"`           // max entry notional reduction (0.90 = reduce to 10%)
	TrendGuardSpreadWidenBps       float64 `yaml:"trend_guard_spread_widen_bps"`       // additional entry-side spread widening when suppress >= 0.5; 0 = disabled
	TrendGuardSpreadWidenAsymmetry float64 `yaml:"trend_guard_spread_widen_asymmetry"` // multiplier on entry-side widen vs exit-side; 1.0 = entry only (legacy), 2.0 = 2x entry widen; 0 = default 1.0
	TrendGuardFreezeThreshold      float64 `yaml:"trend_guard_freeze_threshold"`       // suppress factor at/above which entry-side notional is zeroed (full freeze); 0 = disabled

	// P1: Regime-gated quoting — suppress entry side during sustained trends
	// detected via autocorrelation + VWAP divergence.
	RegimeGateEnabled         bool    `yaml:"regime_gate_enabled"`
	RegimeGateMode            string  `yaml:"regime_gate_mode"`             // "exit_only" only; "directional" reserved
	RegimeAutocorrThreshold   float64 `yaml:"regime_autocorr_threshold"`    // |autocorr| required to qualify; in (0, 1)
	RegimeVWAPDivergenceSigma float64 `yaml:"regime_vwap_divergence_sigma"` // |shortV-longV|/sigma required to qualify
	RegimeGateMinDurationSec  int     `yaml:"regime_gate_min_duration_sec"` // min seconds qualified before activation
	RegimeGateMaxSuppression  float64 `yaml:"regime_gate_max_suppression"`  // fraction of entry-side notional to remove; in [0, 1]

	// Price Shock Guard: fast mid-price move protection independent of trade-count burst
	PriceShockEnabled                   bool    `yaml:"price_shock_enabled"`
	PriceShockWindowMs                  int     `yaml:"price_shock_window_ms"`
	PriceShockThresholdBps              float64 `yaml:"price_shock_threshold_bps"`
	PriceShockCooldownMs                int     `yaml:"price_shock_cooldown_ms"`
	PriceShockFreezeEntry               bool    `yaml:"price_shock_freeze_entry"`
	PriceShockExitSizeFactor            float64 `yaml:"price_shock_exit_size_factor"`              // V24: exit-side size factor during cooldown; 1.0 = disabled, 0.5 = half size
	PriceShockFavorableExitFactor       float64 `yaml:"price_shock_favorable_exit_factor"`         // V30: exit-side size multiplier when shock direction FAVORS current inventory (profit-taking mode); 1.0 = disabled, >1 = boost profit-taking side
	PriceShockFavorableExitTightenBps   float64 `yaml:"price_shock_favorable_exit_tighten_bps"`    // V30: absolute bps subtracted from favorable exit side's spread during shock cooldown — pulls profit-taking quotes closer to fair value; 0 = disabled
	PriceShockFavorableIocFrac          float64 `yaml:"price_shock_favorable_ioc_frac"`            // V30 part 4: fraction of position to close via limit-IOC on favorable shock activation; 0 = disabled
	PriceShockFavorableIocMinInvUtil    float64 `yaml:"price_shock_favorable_ioc_min_inv_util"`    // V30 part 4: minimum inventory utilization required to fire IOC; gates out near-flat noise
	PriceShockFavorableIocMinCaptureBps float64 `yaml:"price_shock_favorable_ioc_min_capture_bps"` // V30 part 4: minimum |move_bps| on the shock for the IOC to fire (should exceed 2× taker fee)
	PriceShockFavorableIocSlippageBps   float64 `yaml:"price_shock_favorable_ioc_slippage_bps"`    // V30 part 4: slippage cushion past best-opposite when pricing the IOC

	// Shock-Chain Detector (V30 part 3): count price_shock activations in a rolling window.
	// When ≥ threshold fire within window, treat as cascade and escalate beyond per-shock
	// cooldown: cancel all + close position + pause + enter extended recovery.
	ShockChainEnabled     bool `yaml:"shock_chain_enabled"`
	ShockChainWindowSec   int  `yaml:"shock_chain_window_sec"`
	ShockChainThreshold   int  `yaml:"shock_chain_threshold"`
	ShockChainPauseSec    int  `yaml:"shock_chain_pause_sec"`
	ShockChainRecoverySec int  `yaml:"shock_chain_recovery_sec"`

	// Directional Flow Gate: suppress entry-side notional on adverse trade flow
	FlowGateEnabled     bool    `yaml:"flow_gate_enabled"`
	FlowGateThreshold   float64 `yaml:"flow_gate_threshold"`    // imbalance magnitude to start gating
	FlowGateMaxSuppress float64 `yaml:"flow_gate_max_suppress"` // max entry notional reduction from flow
	FlowGateMinUtil     float64 `yaml:"flow_gate_min_util"`     // inv_util below which suppression is scaled down; 0 = disabled

	// A8: Position-age exit tightening — tighten exit side as position ages
	PositionAgeTightenMinSec int     `yaml:"position_age_tighten_min_sec"` // start tightening after this age; 0 = disabled
	PositionAgeTightenBps    float64 `yaml:"position_age_tighten_bps"`     // tighten exit side by this many bps per additional minute; default 0.5

	// P9: Idle-fill circuit breaker — one-shot exit-side tighten when adverse inventory sits idle
	IdleFillDetectorEnabled  bool    `yaml:"idle_fill_detector_enabled"`   // master toggle; default false
	IdleFillThresholdSec     int     `yaml:"idle_fill_threshold_sec"`      // seconds since last fill before breaker can fire
	IdleFillMinInvUtil       float64 `yaml:"idle_fill_min_inv_util"`       // |inv_util| floor for breaker activation
	IdleExitTightenBps       float64 `yaml:"idle_exit_tighten_bps"`        // exit-side tighten amount in bps
	IdleExitSizeMultiplier   float64 `yaml:"idle_exit_size_multiplier"`    // exit-side size multiplier
	IdleFillRetryIntervalSec int     `yaml:"idle_fill_retry_interval_sec"` // cooldown between consecutive triggers per asset

	// A9: Time-of-day spread multiplier — widen during configured hours
	TodSpreadStartHourUTC int     `yaml:"tod_spread_start_hour_utc"` // start hour (UTC, 0-23); 0 = disabled when end is also 0
	TodSpreadEndHourUTC   int     `yaml:"tod_spread_end_hour_utc"`   // end hour (UTC, 0-23)
	TodSpreadExtraBps     float64 `yaml:"tod_spread_extra_bps"`      // extra spread bps during time window; default 0

	// B2: Per-side toxicity
	ToxicityPerSide             bool    `yaml:"toxicity_per_side"`
	ToxicitySizeReduceThreshold float64 `yaml:"toxicity_size_reduce_threshold"` // bps, default -2.0
	ToxicitySizeReduceMax       float64 `yaml:"toxicity_size_reduce_max"`       // default 0.5
	ToxicityIdleDecayHalfLives  float64 `yaml:"toxicity_idle_decay_half_lives"` // grace period before idle decay (in half-lives); 0=default(4.0), <0=disabled
	ToxicityWarmupFills         int     `yaml:"toxicity_warmup_fills"`          // min fills before per-side toxicity output is trusted; 0=default(5), <0=disabled
	ToxicitySizeExponent        float64 `yaml:"toxicity_size_exponent"`         // power-law exponent for SizeFactor; 1.0=linear (default), >1.0=convex (gentle at mild, aggressive at extreme)
	ToxicityT5sWeight           float64 `yaml:"toxicity_t5s_weight"`            // blend weight for T5s markout in toxicity input; 0=pure T1s (default), 0.4=recommended

	// Smart Layer 1: Markout-to-FV feedback
	MarkoutFVScale       float64 `yaml:"markout_fv_scale"`         // scale factor for markout → FV shift; default 0.5
	MarkoutFVMaxShiftBps float64 `yaml:"markout_fv_max_shift_bps"` // max FV shift from markout feedback in bps; default 3.0

	// Smart Layer 2: Spread stack cap
	MaxTotalSpreadMultiplier float64 `yaml:"max_total_spread_multiplier"` // cap on total spread widening from all layers; default 3.0

	// Smart Layer 5: Rate limit allocator
	RateLimitAllocatorEnabled     bool    `yaml:"rate_limit_allocator_enabled"`      // master toggle; default false
	RateLimitROIEWMAHalfLifeSec   int     `yaml:"rate_limit_roi_ewma_half_life_sec"` // EWMA half-life for per-asset ROI tracking; default 300
	RateLimitMinRefreshMultiplier float64 `yaml:"rate_limit_min_refresh_multiplier"` // min refresh multiplier for low-ROI assets; default 0.7
	RateLimitMaxRefreshMultiplier float64 `yaml:"rate_limit_max_refresh_multiplier"` // max refresh multiplier for high-ROI assets; default 3.0

	// Funding rate session-end urgency
	FundingUrgencyEnabled        bool    `yaml:"funding_urgency_enabled"`         // master toggle; default false
	FundingUrgencyScale          float64 `yaml:"funding_urgency_scale"`           // urgency scale factor; default 1.5
	FundingUrgencyHorizonMinutes float64 `yaml:"funding_urgency_horizon_minutes"` // minutes before reset where urgency ramps; default 30.0
	FundingPollIntervalSec       int     `yaml:"funding_poll_interval_sec"`       // seconds between funding rate polls; default 60
	FundingIntervalHours         float64 `yaml:"funding_interval_hours"`          // default funding interval per asset; 0 = auto (1h for HIP-3, configurable per-asset)

	// Hardcoded value cleanup — configurable constants
	MarkoutCompositeWeights        [4]float64 `yaml:"markout_composite_weights"`     // weights for 500ms/1s/2s/5s markout horizons; default [0.20, 0.40, 0.25, 0.15]
	MicropriceDecay                float64    `yaml:"microprice_decay"`              // decay factor for multi-level microprice; default 0.7
	EmergencyWidenThresholdBps     float64    `yaml:"emergency_widen_threshold_bps"` // composite markout threshold for emergency widen; default -3.0
	EmergencyWidenAmountBps        float64    `yaml:"emergency_widen_amount_bps"`    // extra spread bps during emergency; default 2.0
	QuietLadderReduceSec           int        `yaml:"quiet_ladder_reduce_sec"`       // seconds of quiet before reducing ladder levels; default 30
	LazyLadderEnabled              bool       `yaml:"lazy_ladder_enabled"`           // start at 1 level/side, expand on fill; default false
	LazyLadderDecaySec             int        `yaml:"lazy_ladder_decay_sec"`         // seconds after fill before decaying back to 1 level; default 30
	ToxicitySizeWeightCap          float64    `yaml:"toxicity_size_weight_cap"`      // cap on toxicity size weight multiplier; default 3.0
	VelocityInitialSeed            float64    `yaml:"velocity_initial_seed"`         // initial seed for velocity EWMA; default 1.0
	QuoteAttributionEnabled        bool       `yaml:"quote_attribution_enabled"`     // V21: sampled per-layer quote attribution logs
	QuoteAttributionMinIntervalSec int        `yaml:"quote_attribution_min_interval_sec"`

	// B3: Inventory half-life controller (replaces Feature 3 when enabled)
	InventoryHalfLifeMinutes   float64 `yaml:"inventory_half_life_minutes"`   // 0=disabled
	InventoryControllerSkewMin float64 `yaml:"inventory_controller_skew_min"` // default 1.0
	InventoryControllerSkewMax float64 `yaml:"inventory_controller_skew_max"` // default 5.0
	InventoryControllerGainP   float64 `yaml:"inventory_controller_gain_p"`   // proportional gain; default 0.10
	InventoryControllerGainI   float64 `yaml:"inventory_controller_gain_i"`   // integral gain; default 0.02
	InventoryControllerGainD   float64 `yaml:"inventory_controller_gain_d"`   // derivative gain; default 0.05

	// P6: Per-side win rate / profit factor optimizer input.
	PerSideWinrateEnabled        bool    `yaml:"per_side_winrate_enabled"`         // master toggle; default false
	PerSideWinrateWindowFills    int     `yaml:"per_side_winrate_window_fills"`    // ring buffer size per side; default 50
	PerSideWinrateThreshold      float64 `yaml:"per_side_winrate_threshold"`       // win rate floor; default 0.35
	PerSideProfitFactorThreshold float64 `yaml:"per_side_profit_factor_threshold"` // profit factor floor; default 0.9
	PerSideWinrateWidenBps       float64 `yaml:"per_side_winrate_widen_bps"`       // additional spread bps when gate fires; default 0.5

	// Optimizer V2
	OptimizerV2 OptimizerV2Config `yaml:"optimizer_v2"`

	// P12: Time-of-day notional schedule. Global (not per-asset). When enabled,
	// notional is scaled by the most conservative (minimum) multiplier across
	// all active windows. Default OFF.
	TradingScheduleEnabled bool                          `yaml:"trading_schedule_enabled"`
	TradingSchedule        []TradingScheduleWindowConfig `yaml:"trading_schedule"`

	// Per-asset overrides
	PerAsset map[string]MarketMakingOverride `yaml:"per_asset"`
}

// MarketMakingOverride holds per-asset overrides for MarketMaking fields.
// All fields are pointers so nil = inherit from global config.
type MarketMakingOverride struct {
	QuoteSpreadBps          *float64 `yaml:"quote_spread_bps"`
	QuoteSpreadBpsBuy       *float64 `yaml:"quote_spread_bps_buy"`
	QuoteSpreadBpsSell      *float64 `yaml:"quote_spread_bps_sell"`
	QuoteNotional           *float64 `yaml:"quote_notional"`
	RefreshIntervalMs       *int     `yaml:"refresh_interval_ms"`
	RefreshPostFillMs       *int     `yaml:"refresh_post_fill_ms"`
	RefreshAdverseMs        *int     `yaml:"refresh_adverse_ms"`
	RefreshQuietMs          *int     `yaml:"refresh_quiet_ms"`
	RefreshQuietThresholdMs *int     `yaml:"refresh_quiet_threshold_ms"`
	InventorySkewFactor     *float64 `yaml:"inventory_skew_factor"`
	InventorySkewGamma      *float64 `yaml:"inventory_skew_gamma"`
	LadderLevels            *int     `yaml:"ladder_levels"`
	LadderSpacingBps        *int     `yaml:"ladder_spacing_bps"`
	DynamicWidth            *bool    `yaml:"dynamic_width"`
	WidthVolWindow          *int     `yaml:"width_vol_window"`
	WidthMaxMultiplier      *float64 `yaml:"width_max_multiplier"`
	WidthImbalanceLevels    *int     `yaml:"width_imbalance_levels"`
	RequoteThresholdBps     *int     `yaml:"requote_threshold_bps"`
	SizeRequotePct          *float64 `yaml:"size_requote_pct"`
	MaxQuoteAgeMs           *int     `yaml:"max_quote_age_ms"`
	MaxBookStalenessMs      *int     `yaml:"max_book_staleness_ms"`

	VWAPShortWindowMinutes *int     `yaml:"vwap_short_window_minutes"`
	VWAPLongWindowMinutes  *int     `yaml:"vwap_long_window_minutes"`
	VWAPAlpha              *float64 `yaml:"vwap_alpha"`

	VWAPAlphaDriftScalingEnabled *bool    `yaml:"vwap_alpha_drift_scaling_enabled"`
	VWAPAlphaDriftK              *float64 `yaml:"vwap_alpha_drift_k"`
	VWAPAlphaFloor               *float64 `yaml:"vwap_alpha_floor"`

	VWAPAlphaTrendBoostEnabled *bool    `yaml:"vwap_alpha_trend_boost_enabled"`
	VWAPAlphaTrendBoostCeiling *float64 `yaml:"vwap_alpha_trend_boost_ceiling"`
	TrendFVDriftBps            *float64 `yaml:"trend_fv_drift_bps"`
	TrendFVDriftAutocorrMin    *float64 `yaml:"trend_fv_drift_autocorr_min"`

	WickSizeMultiplier         *float64 `yaml:"wick_size_multiplier"`
	WickDevThresholdPct        *float64 `yaml:"wick_dev_threshold_pct"`
	WickLongDevThresholdPct    *float64 `yaml:"wick_long_dev_threshold_pct"`
	WickMaxInventoryUtil       *float64 `yaml:"wick_max_inventory_util"`
	WickTrendSuppressEnabled   *bool    `yaml:"wick_trend_suppress_enabled"`
	WickTrendSuppressThreshold *float64 `yaml:"wick_trend_suppress_threshold"`
	LadderSizeGraduation       *float64 `yaml:"ladder_size_graduation"`
	LadderSpacingDynamic       *bool    `yaml:"ladder_spacing_dynamic"`
	LadderSpacingExponent      *float64 `yaml:"ladder_spacing_exponent"`

	WallFrontEnabled                *bool    `yaml:"wall_front_enabled"`
	WallMinSizeMultiplier           *float64 `yaml:"wall_min_size_multiplier"`
	WallQualityEnabled              *bool    `yaml:"wall_quality_enabled"`
	WallQualityHalfLifeSec          *int     `yaml:"wall_quality_half_life_sec"`
	WallQualityMinScore             *float64 `yaml:"wall_quality_min_score"`
	WallQualityDisableSec           *int     `yaml:"wall_quality_disable_sec"`
	WallQualityHysteresisSec        *int     `yaml:"wall_quality_hysteresis_sec"`
	WallQualityReenableThresholdBps *float64 `yaml:"wall_quality_reenable_threshold_bps"`

	MakerFeeBps     *float64 `yaml:"maker_fee_bps"`
	BuilderFeeBps   *float64 `yaml:"builder_fee_bps"`
	FeeFloorEnabled *bool    `yaml:"fee_floor_enabled"`

	EdgeGateEnabled     *bool    `yaml:"edge_gate_enabled"`
	EdgeTargetBps       *float64 `yaml:"edge_target_bps"`
	EdgeAdverseFloorBps *float64 `yaml:"edge_adverse_floor_bps"`
	EdgeMaxWidthBps     *float64 `yaml:"edge_max_width_bps"`
	EdgeFeeFloorBps     *float64 `yaml:"edge_fee_floor_bps"`

	TradeImbalanceWindowSec    *int     `yaml:"trade_imbalance_window_sec"`
	TradeImbalanceMaxShiftBps  *float64 `yaml:"trade_imbalance_max_shift_bps"`
	TradeImbalanceSizeExponent *float64 `yaml:"trade_imbalance_size_exponent"`
	TradeImbalanceMode         *string  `yaml:"trade_imbalance_mode"`
	TradeImbalanceBucketSize   *float64 `yaml:"trade_imbalance_bucket_size"`
	TradeImbalanceBucketCount  *int     `yaml:"trade_imbalance_bucket_count"`
	FillAdverseShiftBps        *float64 `yaml:"fill_adverse_shift_bps"`
	FillDecayHalfLifeMs        *int     `yaml:"fill_decay_half_life_ms"`
	MicropriceLevels           *int     `yaml:"microprice_levels"`
	ZWideningAsymmetry         *float64 `yaml:"z_widening_asymmetry"`
	InventorySkewAsymmetry     *float64 `yaml:"inventory_skew_asymmetry"`
	CrossLegSkewEnabled        *bool    `yaml:"cross_leg_skew_enabled"`
	CrossLegSkewFactor         *float64 `yaml:"cross_leg_skew_factor"`
	PairReduceEntryFraction    *float64 `yaml:"pair_reduce_entry_fraction"`
	PairReduceExitFraction     *float64 `yaml:"pair_reduce_exit_fraction"`
	PairReduceTightenFactor    *float64 `yaml:"pair_reduce_tighten_factor"`
	MinCrossingSpreadBps       *float64 `yaml:"min_crossing_spread_bps"`
	PairReduceMaxDurationSec   *int     `yaml:"pair_reduce_max_duration_sec"`
	SpreadBiasMaxFraction      *float64 `yaml:"spread_bias_max_fraction"`
	SpreadBiasAsymmetry        *float64 `yaml:"spread_bias_asymmetry"`
	MaxNoBookUpdateMs          *int     `yaml:"max_no_book_update_ms"`
	MaxFVDeviationPct          *float64 `yaml:"max_fv_deviation_pct"`

	// Adaptive config overrides (per-asset)
	AdaptiveNotionalEnabled      *bool    `yaml:"adaptive_notional_enabled"`
	AdaptiveNotionalTargetPerMin *float64 `yaml:"adaptive_notional_target_per_min"`
	AdaptiveNotionalMin          *float64 `yaml:"adaptive_notional_min"`
	AdaptiveNotionalMax          *float64 `yaml:"adaptive_notional_max"`
	AdaptiveNotionalHalfLifeSec  *int     `yaml:"adaptive_notional_half_life_sec"`
	RegimeWindow                 *int     `yaml:"regime_window"`
	MarkoutEnabled               *bool    `yaml:"markout_enabled"`
	MarkoutEWMAHalfLifeSec       *int     `yaml:"markout_ewma_half_life_sec"`
	WarmupNotionalPct            *float64 `yaml:"warmup_notional_pct"`
	WarmupSpreadMultiplier       *float64 `yaml:"warmup_spread_multiplier"`
	WarmupDurationSec            *int     `yaml:"warmup_duration_sec"`
	TradeStaleDegradedSec        *int     `yaml:"trade_stale_degraded_sec"`
	TradeStaleHaltSec            *int     `yaml:"trade_stale_halt_sec"`
	FillRefreshCooldownMs        *int     `yaml:"fill_refresh_cooldown_ms"`
	FillBurstWindowSec           *float64 `yaml:"fill_burst_window_sec"`
	FillBurstThreshold           *int     `yaml:"fill_burst_threshold"`
	FillBurstSpreadMult          *float64 `yaml:"fill_burst_spread_mult"`
	FillBurstSizeFactor          *float64 `yaml:"fill_burst_size_factor"`
	FillBurstDecaySec            *float64 `yaml:"fill_burst_decay_sec"`
	VolumeBurstEnabled           *bool    `yaml:"volume_burst_enabled"`
	BurstVelocityThreshold       *float64 `yaml:"burst_velocity_threshold"`
	BurstVelocityFastSec         *float64 `yaml:"burst_velocity_fast_sec"`
	BurstVelocitySlowSec         *float64 `yaml:"burst_velocity_slow_sec"`
	BurstVelocityMinFloor        *float64 `yaml:"burst_velocity_min_floor"`
	BurstLevelMultiplier         *int     `yaml:"burst_level_multiplier"`
	BurstDecaySec                *float64 `yaml:"burst_decay_sec"`
	ToxicityHalfLifeSec          *int     `yaml:"toxicity_half_life_sec"`
	ToxicityMaxWiden             *float64 `yaml:"toxicity_max_widen"`
	ToxicityBaselineBps          *float64 `yaml:"toxicity_baseline_bps"`

	// Profitability improvement overrides (per-asset)
	InventorySkewExitPreserve           *bool    `yaml:"inventory_skew_exit_preserve"`
	EntryReductionRate                  *float64 `yaml:"entry_size_reduction_rate"`
	EntryReductionFloor                 *float64 `yaml:"entry_size_reduction_floor"`
	EntryAccelThreshold                 *float64 `yaml:"entry_accel_threshold"`
	EntryAccelExponent                  *float64 `yaml:"entry_accel_exponent"`
	EntryReductionTrendK                *float64 `yaml:"entry_reduction_trend_k"`
	TrendGuardEnabled                   *bool    `yaml:"trend_guard_enabled"`
	TrendGuardThresholdPct              *float64 `yaml:"trend_guard_threshold_pct"`
	TrendGuardSaturationPct             *float64 `yaml:"trend_guard_saturation_pct"`
	TrendGuardMaxSuppress               *float64 `yaml:"trend_guard_max_suppress"`
	TrendGuardSpreadWidenBps            *float64 `yaml:"trend_guard_spread_widen_bps"`
	TrendGuardSpreadWidenAsymmetry      *float64 `yaml:"trend_guard_spread_widen_asymmetry"`
	TrendGuardFreezeThreshold           *float64 `yaml:"trend_guard_freeze_threshold"`
	RegimeGateEnabled                   *bool    `yaml:"regime_gate_enabled"`
	RegimeGateMode                      *string  `yaml:"regime_gate_mode"`
	RegimeAutocorrThreshold             *float64 `yaml:"regime_autocorr_threshold"`
	RegimeVWAPDivergenceSigma           *float64 `yaml:"regime_vwap_divergence_sigma"`
	RegimeGateMinDurationSec            *int     `yaml:"regime_gate_min_duration_sec"`
	RegimeGateMaxSuppression            *float64 `yaml:"regime_gate_max_suppression"`
	PriceShockEnabled                   *bool    `yaml:"price_shock_enabled"`
	PriceShockWindowMs                  *int     `yaml:"price_shock_window_ms"`
	PriceShockThresholdBps              *float64 `yaml:"price_shock_threshold_bps"`
	PriceShockCooldownMs                *int     `yaml:"price_shock_cooldown_ms"`
	PriceShockFreezeEntry               *bool    `yaml:"price_shock_freeze_entry"`
	PriceShockExitSizeFactor            *float64 `yaml:"price_shock_exit_size_factor"`              // V24: per-asset override
	PriceShockFavorableExitFactor       *float64 `yaml:"price_shock_favorable_exit_factor"`         // V30: per-asset override
	PriceShockFavorableExitTightenBps   *float64 `yaml:"price_shock_favorable_exit_tighten_bps"`    // V30: per-asset override
	PriceShockFavorableIocFrac          *float64 `yaml:"price_shock_favorable_ioc_frac"`            // V30 part 4: per-asset override
	PriceShockFavorableIocMinInvUtil    *float64 `yaml:"price_shock_favorable_ioc_min_inv_util"`    // V30 part 4: per-asset override
	PriceShockFavorableIocMinCaptureBps *float64 `yaml:"price_shock_favorable_ioc_min_capture_bps"` // V30 part 4: per-asset override
	PriceShockFavorableIocSlippageBps   *float64 `yaml:"price_shock_favorable_ioc_slippage_bps"`    // V30 part 4: per-asset override
	ShockChainEnabled                   *bool    `yaml:"shock_chain_enabled"`                       // V30 part 3: per-asset override
	ShockChainWindowSec                 *int     `yaml:"shock_chain_window_sec"`                    // V30 part 3: per-asset override
	ShockChainThreshold                 *int     `yaml:"shock_chain_threshold"`                     // V30 part 3: per-asset override
	ShockChainPauseSec                  *int     `yaml:"shock_chain_pause_sec"`                     // V30 part 3: per-asset override
	ShockChainRecoverySec               *int     `yaml:"shock_chain_recovery_sec"`                  // V30 part 3: per-asset override
	FlowGateEnabled                     *bool    `yaml:"flow_gate_enabled"`
	FlowGateThreshold                   *float64 `yaml:"flow_gate_threshold"`
	FlowGateMaxSuppress                 *float64 `yaml:"flow_gate_max_suppress"`
	FlowGateMinUtil                     *float64 `yaml:"flow_gate_min_util"`
	PositionAgeTightenMinSec            *int     `yaml:"position_age_tighten_min_sec"`
	PositionAgeTightenBps               *float64 `yaml:"position_age_tighten_bps"`
	IdleFillDetectorEnabled             *bool    `yaml:"idle_fill_detector_enabled"`
	IdleFillThresholdSec                *int     `yaml:"idle_fill_threshold_sec"`
	IdleFillMinInvUtil                  *float64 `yaml:"idle_fill_min_inv_util"`
	IdleExitTightenBps                  *float64 `yaml:"idle_exit_tighten_bps"`
	IdleExitSizeMultiplier              *float64 `yaml:"idle_exit_size_multiplier"`
	IdleFillRetryIntervalSec            *int     `yaml:"idle_fill_retry_interval_sec"`
	TodSpreadStartHourUTC               *int     `yaml:"tod_spread_start_hour_utc"`
	TodSpreadEndHourUTC                 *int     `yaml:"tod_spread_end_hour_utc"`
	TodSpreadExtraBps                   *float64 `yaml:"tod_spread_extra_bps"`
	ToxicityPerSide                     *bool    `yaml:"toxicity_per_side"`
	ToxicitySizeReduceThreshold         *float64 `yaml:"toxicity_size_reduce_threshold"`
	ToxicitySizeReduceMax               *float64 `yaml:"toxicity_size_reduce_max"`
	ToxicityIdleDecayHalfLives          *float64 `yaml:"toxicity_idle_decay_half_lives"`
	ToxicityWarmupFills                 *int     `yaml:"toxicity_warmup_fills"`
	ToxicitySizeExponent                *float64 `yaml:"toxicity_size_exponent"`
	ToxicityT5sWeight                   *float64 `yaml:"toxicity_t5s_weight"`

	// Smart layer overrides (per-asset)
	MarkoutFVScale                 *float64    `yaml:"markout_fv_scale"`
	MarkoutFVMaxShiftBps           *float64    `yaml:"markout_fv_max_shift_bps"`
	MaxTotalSpreadMultiplier       *float64    `yaml:"max_total_spread_multiplier"`
	RateLimitAllocatorEnabled      *bool       `yaml:"rate_limit_allocator_enabled"`
	RateLimitROIEWMAHalfLifeSec    *int        `yaml:"rate_limit_roi_ewma_half_life_sec"`
	RateLimitMinRefreshMultiplier  *float64    `yaml:"rate_limit_min_refresh_multiplier"`
	RateLimitMaxRefreshMultiplier  *float64    `yaml:"rate_limit_max_refresh_multiplier"`
	MarkoutCompositeWeights        *[4]float64 `yaml:"markout_composite_weights"`
	MicropriceDecay                *float64    `yaml:"microprice_decay"`
	EmergencyWidenThresholdBps     *float64    `yaml:"emergency_widen_threshold_bps"`
	EmergencyWidenAmountBps        *float64    `yaml:"emergency_widen_amount_bps"`
	QuietLadderReduceSec           *int        `yaml:"quiet_ladder_reduce_sec"`
	LazyLadderEnabled              *bool       `yaml:"lazy_ladder_enabled"`
	LazyLadderDecaySec             *int        `yaml:"lazy_ladder_decay_sec"`
	ToxicitySizeWeightCap          *float64    `yaml:"toxicity_size_weight_cap"`
	VelocityInitialSeed            *float64    `yaml:"velocity_initial_seed"`
	QuoteAttributionEnabled        *bool       `yaml:"quote_attribution_enabled"`
	QuoteAttributionMinIntervalSec *int        `yaml:"quote_attribution_min_interval_sec"`

	// Funding rate session-end urgency overrides (per-asset)
	FundingUrgencyEnabled        *bool    `yaml:"funding_urgency_enabled"`
	FundingUrgencyScale          *float64 `yaml:"funding_urgency_scale"`
	FundingUrgencyHorizonMinutes *float64 `yaml:"funding_urgency_horizon_minutes"`
	FundingPollIntervalSec       *int     `yaml:"funding_poll_interval_sec"`
	FundingIntervalHours         *float64 `yaml:"funding_interval_hours"`

	// B3 inventory controller overrides (per-asset)
	InventoryHalfLifeMinutes   *float64 `yaml:"inventory_half_life_minutes"`
	InventoryControllerSkewMin *float64 `yaml:"inventory_controller_skew_min"`
	InventoryControllerSkewMax *float64 `yaml:"inventory_controller_skew_max"`
	InventoryControllerGainP   *float64 `yaml:"inventory_controller_gain_p"`
	InventoryControllerGainI   *float64 `yaml:"inventory_controller_gain_i"`
	InventoryControllerGainD   *float64 `yaml:"inventory_controller_gain_d"`

	// P6: Per-side win rate / profit factor overrides.
	PerSideWinrateEnabled        *bool    `yaml:"per_side_winrate_enabled"`
	PerSideWinrateWindowFills    *int     `yaml:"per_side_winrate_window_fills"`
	PerSideWinrateThreshold      *float64 `yaml:"per_side_winrate_threshold"`
	PerSideProfitFactorThreshold *float64 `yaml:"per_side_profit_factor_threshold"`
	PerSideWinrateWidenBps       *float64 `yaml:"per_side_winrate_widen_bps"`

	// Optimizer V2 override (per-asset)
	OptimizerV2 *OptimizerV2Config `yaml:"optimizer_v2"`
}

// RiskConfig holds risk limit parameters.
type RiskConfig struct {
	MaxPositionNotional float64 `yaml:"max_position_notional"`
	// MaxPositionHaltTolerancePct lets the position reach the configured cap
	// without forcing a flatten. The quote manager already stops the entry
	// side at the cap; this tolerance prevents a full inventory from being
	// liquidated at market. 0 = halt exactly at the cap (upstream behavior).
	MaxPositionHaltTolerancePct float64 `yaml:"max_position_halt_tolerance_pct"`
	MaxSpreadExposure           float64 `yaml:"max_spread_exposure"`
	MaxAggregateExposure        float64 `yaml:"max_aggregate_exposure"` // total portfolio exposure cap across all assets; 0 = use max_spread_exposure as fallback
	MaxDrawdownPct              float64 `yaml:"max_drawdown_pct"`
	MaxOpenOrders               int     `yaml:"max_open_orders"`
	DrawdownCooldownSec         int     `yaml:"drawdown_cooldown_sec"`      // seconds before auto-resume after drawdown halt; 0 = stay halted until restart
	MaxConsecutiveHalts         int     `yaml:"max_consecutive_halts"`      // 0 = disabled (infinite retries)
	MinResumeEquityPct          float64 `yaml:"min_resume_equity_pct"`      // 0 = disabled; % of high-water
	MaxCloseSlippageBps         int     `yaml:"max_close_slippage_bps"`     // slippage limit for position close IOC; 0 = use market close
	FailClosedCooldownSec       int     `yaml:"fail_closed_cooldown_sec"`   // seconds before auto-recovery from fail-closed halt; 0 = disabled (permanent halt)
	MaxFailClosedRetries        int     `yaml:"max_fail_closed_retries"`    // max recovery attempts before permanent halt; 0 = unlimited
	AbsoluteEquityFloor         float64 `yaml:"absolute_equity_floor"`      // permanent halt if equity drops below this; 0 = disabled
	DrawdownSmoothingSamples    int     `yaml:"drawdown_smoothing_samples"` // EMA samples for drawdown equity; 0/1 = no smoothing
	RecoveryDurationSec         int     `yaml:"recovery_duration_sec"`      // seconds of reduced quoting after drawdown resume; 0 = disabled
	RecoveryNotionalPct         float64 `yaml:"recovery_notional_pct"`      // notional multiplier during recovery (e.g. 0.50 = 50%)
	RecoverySpreadMultiplier    float64 `yaml:"recovery_spread_multiplier"` // spread multiplier during recovery (e.g. 1.50 = 150%)

	SessionPnLWindowMinutes int `yaml:"session_pnl_window_minutes"` // rolling window for WindowPnLBps; 0 = default 60

	// P3: Session stop-loss. Global (not per-asset). 0 = disabled.
	SessionStopLossBps                 float64 `yaml:"session_stop_loss_bps"`                   // trigger when session PnL bps <= this value (must be < 0); 0 = disabled
	SessionStopLossPauseSec            int     `yaml:"session_stop_loss_pause_sec"`             // seconds to pause after trigger before entering recovery
	SessionStopLossRecoveryNotionalPct float64 `yaml:"session_stop_loss_recovery_notional_pct"` // notional multiplier during session-stop recovery (e.g. 0.5)
	SessionStopLossRecoveryDurationSec int     `yaml:"session_stop_loss_recovery_duration_sec"` // duration of recovery window after session-stop pause

	// V24: widen-only early warning before the hard stop. When session PnL crosses warn
	// (less negative than stop_loss_bps), quoting manager widens spreads by WarnSpreadMult.
	// No flatten / no pause / no recovery — purely defensive. 0 = disabled.
	SessionStopLossWarnBps         float64 `yaml:"session_stop_loss_warn_bps"`          // warn level (must satisfy 0 > warn > stop_loss_bps); 0 = disabled
	SessionStopLossWarnSpreadMult  float64 `yaml:"session_stop_loss_warn_spread_mult"`  // spread multiplier while in warn zone; must be >= 1.0
	SessionStopLossWarnNotionalPct float64 `yaml:"session_stop_loss_warn_notional_pct"` // V30 part 5: notional multiplier while in warn zone (0, 1]; 0 or 1 = disabled (size unchanged). V29 showed two warn entries where 1.60× widen alone didn't prevent stop-loss; cutting size alongside widening is the remaining lever without further tightening warn_bps.

	// V30 part 6: session-end flatten. On SIGINT/SIGTERM, after cancelling all
	// orders, market-close any residual position so the bot doesn't leave
	// inventory parked across shutdowns. V29 ended long 2.218 CL (33% of
	// max_position_notional) carrying the 08:07 −$0.88 realized loss into an
	// undefined overnight hold. This is a clean on-shutdown action only —
	// the user decides when to stop, so there is no surprise timing risk.
	// 0 (default) = disabled; cancel-only shutdown (pre-V30 behavior).
	SessionFlattenOnShutdown bool `yaml:"session_flatten_on_shutdown"`
	SessionFlattenTimeoutSec int  `yaml:"session_flatten_timeout_sec"` // per-asset close timeout in seconds; 0 = default 15

	// Per-asset overrides
	PerAsset map[string]RiskOverride `yaml:"per_asset"`
}

// RiskOverride holds per-asset overrides for Risk fields.
type RiskOverride struct {
	MaxPositionNotional         *float64 `yaml:"max_position_notional"`
	MaxPositionHaltTolerancePct *float64 `yaml:"max_position_halt_tolerance_pct"`
	MaxSpreadExposure           *float64 `yaml:"max_spread_exposure"`
	MaxOpenOrders               *int     `yaml:"max_open_orders"`
}

// LoggingConfig holds logging settings.
type LoggingConfig struct {
	Verbose           bool `yaml:"verbose"`
	StatusIntervalSec int  `yaml:"status_interval_sec"` // periodic status log interval; 0 = default (15s)
}

// ExecutionConfig holds execution settings.
type ExecutionConfig struct {
	LiveMode                  bool    `yaml:"live_mode"`
	PaperStartingEquity       float64 `yaml:"paper_starting_equity"`
	Leverage                  int     `yaml:"leverage"`
	ReconGracePeriodMs        int     `yaml:"recon_grace_period_ms"`         // post-fill grace period for reconciliation (ms); default 3000
	ReconEmptyExchangeGraceMs int     `yaml:"recon_empty_exchange_grace_ms"` // shorter grace for tracked>0 && exchange=0 desync; default 5000; 0 = fall back to ReconGracePeriodMs
	PollEquitySec             int     `yaml:"poll_equity_sec"`               // equity polling interval (seconds); default 15
	PollOpenOrdersSec         int     `yaml:"poll_open_orders_sec"`          // open orders polling interval (seconds); default 15
	PollFillsSec              int     `yaml:"poll_fills_sec"`                // REST fill poller interval (seconds); default 10
	PollRateLimitSec          int     `yaml:"poll_rate_limit_sec"`           // rate limit polling interval (seconds); default 30

	ReconMismatchDiagnosticEnabled    bool `yaml:"recon_mismatch_diagnostic_enabled"`
	ReconMismatchDiagnosticMaxPerHour int  `yaml:"recon_mismatch_diagnostic_max_per_hour"`

	// Per-asset overrides
	PerAsset map[string]ExecutionOverride `yaml:"per_asset"`
}

// ExecutionOverride holds per-asset overrides for Execution fields.
type ExecutionOverride struct {
	Leverage *int `yaml:"leverage"`
}

// EnvConfig holds credentials that can be set in YAML or overridden by env vars.
type EnvConfig struct {
	QuickNodeSubdomain string `yaml:"quicknode_subdomain"`
	QuickNodeAuthToken string `yaml:"quicknode_auth_token"`
	PrivateKey         string `yaml:"private_key"`
}

// RateLimitConfig controls the rate-limit circuit breaker.
type RateLimitConfig struct {
	Disabled           bool `yaml:"disabled"`             // observe quota without local blocking or budget throttling
	AbsoluteFloor      int  `yaml:"absolute_floor"`       // remaining below this = immediate Critical
	TTECautionMinutes  int  `yaml:"tte_caution_minutes"`  // TTE below this = suppress burst, 1.5x throttle
	TTEDegradedMinutes int  `yaml:"tte_degraded_minutes"` // TTE below this = 1 ladder, 2x throttle, 0.5x notional
	TTECriticalMinutes int  `yaml:"tte_critical_minutes"` // TTE below this = cancel all + close all + dormant
	TTERecoveryMinutes int  `yaml:"tte_recovery_minutes"` // TTE above this + remaining > 2×floor = exit dormant

	// Long-window EWMA depletion tracker. Catches slow secular drain that the
	// short 90-second sliding window misses because noise makes per-window
	// depletion oscillate around zero.
	EWMAHalfLifeMinutes int `yaml:"ewma_half_life_minutes"` // default 10; EWMA half-life for depletion rate

	// Percentage-based remaining floors. Safety net independent of depletion
	// rate accuracy — triggers on absolute remaining/capacity ratio.
	RemainingPctWarn     float64 `yaml:"remaining_pct_warn"`     // default 10; WARN when remaining < X% of cap
	RemainingPctCritical float64 `yaml:"remaining_pct_critical"` // default 3; Critical when remaining < X% of cap

	// Ramp start for absolute-remaining budget pressure fed to the optimizer.
	// When remaining/cap % drops below this value, depletion-based pressure
	// begins ramping linearly from 0 to 1.0 at RemainingPctCritical. The
	// optimizer consumes max(headroom_pressure, depletion_pressure) so it
	// proactively throttles before the Critical tier (cancel-only) trips.
	// Covers the blind spot where headroom=1.0 (fills regen budget 1:1) while
	// absolute remaining is low — optimizer otherwise reads budget_pressure=0.
	BudgetPressurePctStart float64 `yaml:"budget_pressure_pct_start"` // default 20; 0 disables blend

	// Startup grace period: skip pct-based critical check for this many seconds
	// after Run() starts. Allows the bot to establish a depletion rate before
	// the percentage floor can trigger. Absolute floor is always enforced.
	PctCriticalGraceSec int `yaml:"pct_critical_grace_sec"` // default 120
}

// AssetConfig holds the fully resolved config for a single asset.
type AssetConfig struct {
	MarketMaking MarketMakingConfig
	Execution    ExecutionConfig
	Risk         RiskConfig
}

// PostMortemConfig controls the post-mortem dumper that snapshots state
// at halt/session-stop/clean-shutdown into a JSON file per event.
type PostMortemConfig struct {
	Enabled               bool   `yaml:"enabled"`
	OutputDir             string `yaml:"output_dir"`
	FilenameFormat        string `yaml:"filename_format"`
	IncludeLastNEvals     int    `yaml:"include_last_n_evals"`
	IncludeLastNRefreshes int    `yaml:"include_last_n_refreshes"`
	IncludeAllFills       bool   `yaml:"include_all_fills"`
	RotateKeepCount       int    `yaml:"rotate_keep_count"`
}

// Config is the top-level configuration struct.
type Config struct {
	SignalPausesEnabled *bool                  `yaml:"signal_pauses_enabled"`
	ZeusMode            bool                   `yaml:"-"`
	WalletAddress       string                 `yaml:"wallet_address"`
	DataSource          string                 `yaml:"data_source"`
	Strategy            StrategyConfig         `yaml:"strategy"`
	MarketMaking        MarketMakingConfig     `yaml:"market_making"`
	Risk                RiskConfig             `yaml:"risk"`
	Logging             LoggingConfig          `yaml:"logging"`
	Execution           ExecutionConfig        `yaml:"execution"`
	RateLimit           RateLimitConfig        `yaml:"rate_limit"`
	PostMortem          PostMortemConfig       `yaml:"postmortem"`
	Env                 EnvConfig              `yaml:"env"`
	PrivateKey          string                 `yaml:"-"`
	QuickNodeEndpoint   string                 `yaml:"-"`
	Resolved            map[string]AssetConfig `yaml:"-"` // per-asset resolved configs
}

// SignalPauses defaults to the original standalone behavior.
func (c *Config) SignalPauses() bool { return c.SignalPausesEnabled == nil || *c.SignalPausesEnabled }

// ForAsset returns the resolved config for a specific asset.
// Falls back to global config if the asset is not in the resolved map.
func (c *Config) ForAsset(asset string) AssetConfig {
	if ac, ok := c.Resolved[asset]; ok {
		return ac
	}
	return AssetConfig{
		MarketMaking: c.MarketMaking,
		Execution:    c.Execution,
		Risk:         c.Risk,
	}
}

// LoadOption customizes Load behavior.
type LoadOption func(*loadOpts)

type loadOpts struct {
	skipCredentials bool
}

// SkipCredentialCheck disables the QuickNode credential requirement during
// validate(). Use for offline config validation (`bot -validate`) where the
// goal is to check schema/topology without requiring real endpoints.
func SkipCredentialCheck() LoadOption {
	return func(o *loadOpts) { o.skipCredentials = true }
}

// Load reads the YAML file at path, applies environment variable overrides,
// and validates the resulting configuration.
func Load(path string, opts ...LoadOption) (*Config, error) {
	var lo loadOpts
	for _, o := range opts {
		o(&lo)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("config: read file: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("config: parse yaml: %w", err)
	}

	// Resolve credentials: env vars override YAML values.
	subdomain := firstNonEmpty(os.Getenv("QUICKNODE_SUBDOMAIN"), cfg.Env.QuickNodeSubdomain)
	token := firstNonEmpty(os.Getenv("QUICKNODE_AUTH_TOKEN"), cfg.Env.QuickNodeAuthToken)
	cfg.PrivateKey = firstNonEmpty(os.Getenv("PRIVATE_KEY"), cfg.Env.PrivateKey)
	cfg.WalletAddress = strings.TrimSpace(firstNonEmpty(os.Getenv("WALLET_ADDRESS"), cfg.WalletAddress))

	if subdomain != "" && !strings.Contains(subdomain, ".") {
		subdomain = subdomain + ".hype-mainnet.quiknode.pro"
	}

	if subdomain != "" && token != "" {
		cfg.QuickNodeEndpoint = fmt.Sprintf("https://%s/%s", subdomain, token)
	}

	// Default ladder_levels to 1 for backwards compatibility.
	if cfg.MarketMaking.LadderLevels == 0 {
		cfg.MarketMaking.LadderLevels = 1
	}

	if cfg.MarketMaking.WidthVolWindow == 0 {
		cfg.MarketMaking.WidthVolWindow = 60
	}
	if cfg.MarketMaking.WidthMaxMultiplier == 0 {
		cfg.MarketMaking.WidthMaxMultiplier = 3.0
	}
	if cfg.MarketMaking.WidthImbalanceLevels == 0 {
		cfg.MarketMaking.WidthImbalanceLevels = 5
	}
	if cfg.MarketMaking.RequoteThresholdBps == 0 {
		cfg.MarketMaking.RequoteThresholdBps = 2
	}
	if cfg.MarketMaking.SizeRequotePct == 0 {
		cfg.MarketMaking.SizeRequotePct = 0.10
	}
	if cfg.MarketMaking.MaxQuoteAgeMs == 0 {
		cfg.MarketMaking.MaxQuoteAgeMs = 3000
	}
	if cfg.MarketMaking.MaxCrossLegSkewMs == 0 {
		cfg.MarketMaking.MaxCrossLegSkewMs = 150
	}
	// MaxBookStalenessMs: 0 → default (5000); -1 → disabled (0 internally).
	if cfg.MarketMaking.MaxBookStalenessMs == 0 {
		cfg.MarketMaking.MaxBookStalenessMs = 5000
	} else if cfg.MarketMaking.MaxBookStalenessMs < 0 {
		cfg.MarketMaking.MaxBookStalenessMs = 0
	}
	// MaxCloseSlippageBps: 0 → default (200); -1 → always use market close (0 internally).
	if cfg.Risk.MaxCloseSlippageBps == 0 {
		cfg.Risk.MaxCloseSlippageBps = 200
	} else if cfg.Risk.MaxCloseSlippageBps < 0 {
		cfg.Risk.MaxCloseSlippageBps = 0
	}

	// VWAP defaults.
	if cfg.MarketMaking.VWAPShortWindowMinutes == 0 {
		cfg.MarketMaking.VWAPShortWindowMinutes = 5
	}
	if cfg.MarketMaking.VWAPLongWindowMinutes == 0 {
		cfg.MarketMaking.VWAPLongWindowMinutes = 30
	}
	if cfg.MarketMaking.VWAPAlpha == 0 {
		cfg.MarketMaking.VWAPAlpha = 0.40
	}
	// Spread bias defaults.
	if cfg.MarketMaking.SpreadBiasScale == 0 {
		cfg.MarketMaking.SpreadBiasScale = 2.0
	}
	if cfg.MarketMaking.SpreadBiasMaxBps == 0 {
		cfg.MarketMaking.SpreadBiasMaxBps = 8.0
	}
	if cfg.MarketMaking.SpreadBiasAsymmetry == 0 {
		cfg.MarketMaking.SpreadBiasAsymmetry = 0.70
	}
	// Z-score widening defaults.
	if cfg.MarketMaking.ZWideningThreshold == 0 {
		cfg.MarketMaking.ZWideningThreshold = 4.0
	}
	if cfg.MarketMaking.ZWideningRate == 0 {
		cfg.MarketMaking.ZWideningRate = 0.15
	}
	if cfg.MarketMaking.ZMaxWidthMultiplier == 0 {
		cfg.MarketMaking.ZMaxWidthMultiplier = 3.0
	}
	// Ladder sizing default.
	if cfg.MarketMaking.LadderSizeGraduation == 0 {
		cfg.MarketMaking.LadderSizeGraduation = 1.0
	}
	if cfg.MarketMaking.LadderSpacingExponent == 0 {
		cfg.MarketMaking.LadderSpacingExponent = 1.0
	}
	// Wall-fronting default.
	if cfg.MarketMaking.WallMinSizeMultiplier == 0 {
		cfg.MarketMaking.WallMinSizeMultiplier = 3.0
	}
	if cfg.MarketMaking.WallQualityHalfLifeSec == 0 {
		cfg.MarketMaking.WallQualityHalfLifeSec = 300
	}
	if cfg.MarketMaking.WallQualityMinScore == 0 {
		cfg.MarketMaking.WallQualityMinScore = 0.35
	}
	if cfg.MarketMaking.WallQualityDisableSec == 0 {
		cfg.MarketMaking.WallQualityDisableSec = 120
	}
	// Wick sizing defaults.
	if cfg.MarketMaking.WickSizeMultiplier == 0 {
		cfg.MarketMaking.WickSizeMultiplier = 2.0
	}
	if cfg.MarketMaking.WickDevThresholdPct == 0 {
		cfg.MarketMaking.WickDevThresholdPct = 0.10
	}
	if cfg.MarketMaking.WickLongDevThresholdPct == 0 {
		cfg.MarketMaking.WickLongDevThresholdPct = 0.50 // 50 bps — 10x default short threshold
	}
	if cfg.MarketMaking.WickTrendSuppressThreshold == 0 {
		cfg.MarketMaking.WickTrendSuppressThreshold = 0.50
	}
	if cfg.MarketMaking.InventorySkewGamma == 0 {
		cfg.MarketMaking.InventorySkewGamma = 1.0
	}
	// Trade-flow defaults.
	if cfg.MarketMaking.TradeImbalanceWindowSec == 0 {
		cfg.MarketMaking.TradeImbalanceWindowSec = 3
	}
	if cfg.MarketMaking.TradeImbalanceSizeExponent <= 0 {
		cfg.MarketMaking.TradeImbalanceSizeExponent = 1.0
	}
	if cfg.MarketMaking.TradeImbalanceMode == "" {
		cfg.MarketMaking.TradeImbalanceMode = "time"
	}
	if cfg.MarketMaking.TradeImbalanceBucketSize <= 0 {
		cfg.MarketMaking.TradeImbalanceBucketSize = 5000.0
	}
	if cfg.MarketMaking.TradeImbalanceBucketCount <= 0 {
		cfg.MarketMaking.TradeImbalanceBucketCount = 20
	}
	if cfg.MarketMaking.FillDecayHalfLifeMs == 0 {
		cfg.MarketMaking.FillDecayHalfLifeMs = 2000
	}
	if cfg.MarketMaking.MicropriceLevels == 0 {
		cfg.MarketMaking.MicropriceLevels = 1
	}
	// Asymmetric defaults.
	if cfg.MarketMaking.ZWideningAsymmetry == 0 {
		cfg.MarketMaking.ZWideningAsymmetry = 0.5
	}
	if cfg.MarketMaking.InventorySkewAsymmetry == 0 {
		cfg.MarketMaking.InventorySkewAsymmetry = 0.5
	}
	if cfg.MarketMaking.CrossLegSkewFactor == 0 {
		cfg.MarketMaking.CrossLegSkewFactor = 0.3
	}
	if cfg.MarketMaking.PairReduceEntryFraction == 0 {
		cfg.MarketMaking.PairReduceEntryFraction = 0.40
	}
	if cfg.MarketMaking.PairReduceExitFraction == 0 {
		cfg.MarketMaking.PairReduceExitFraction = 0.25
	}
	if cfg.MarketMaking.PairReduceTightenFactor == 0 {
		cfg.MarketMaking.PairReduceTightenFactor = 0.75
	}
	if cfg.MarketMaking.MinCrossingSpreadBps == 0 {
		cfg.MarketMaking.MinCrossingSpreadBps = 2.0
	}
	// MaxFVDeviationPct: only apply default if field was not explicitly set in YAML.
	// Since YAML zero-value for float64 is 0, we use the convention that 0 = disabled
	// and the default (10.0) is only applied to new configs that omit the field.
	// Production configs already set this explicitly, so no default override needed.
	// Adaptive defaults.
	if cfg.MarketMaking.AdaptiveNotionalHalfLifeSec == 0 {
		cfg.MarketMaking.AdaptiveNotionalHalfLifeSec = 60
	}
	if cfg.MarketMaking.RegimeWindow == 0 {
		cfg.MarketMaking.RegimeWindow = 120
	}
	if cfg.MarketMaking.ZCalibrationWindow == 0 {
		cfg.MarketMaking.ZCalibrationWindow = 2000
	}
	if cfg.MarketMaking.WarmupNotionalPct == 0 {
		cfg.MarketMaking.WarmupNotionalPct = 1.0
	}
	if cfg.MarketMaking.MarkoutEWMAHalfLifeSec == 0 {
		cfg.MarketMaking.MarkoutEWMAHalfLifeSec = 300
	}
	if cfg.MarketMaking.WarmupSpreadMultiplier == 0 {
		cfg.MarketMaking.WarmupSpreadMultiplier = 1.0
	}
	if cfg.MarketMaking.FillRefreshCooldownMs == 0 {
		cfg.MarketMaking.FillRefreshCooldownMs = 2000
	}
	// Fill burst circuit breaker defaults.
	if cfg.MarketMaking.FillBurstThreshold == 0 {
		cfg.MarketMaking.FillBurstThreshold = 3
	}
	if cfg.MarketMaking.FillBurstSpreadMult == 0 {
		cfg.MarketMaking.FillBurstSpreadMult = 2.0
	}
	if cfg.MarketMaking.FillBurstSizeFactor == 0 {
		cfg.MarketMaking.FillBurstSizeFactor = 0.3
	}
	if cfg.MarketMaking.FillBurstDecaySec == 0 {
		cfg.MarketMaking.FillBurstDecaySec = 15.0
	}
	// Volume burst ladders defaults.
	if cfg.MarketMaking.BurstVelocityThreshold == 0 {
		cfg.MarketMaking.BurstVelocityThreshold = 2.0
	}
	if cfg.MarketMaking.BurstVelocityFastSec == 0 {
		cfg.MarketMaking.BurstVelocityFastSec = 5.0
	}
	if cfg.MarketMaking.BurstVelocitySlowSec == 0 {
		cfg.MarketMaking.BurstVelocitySlowSec = 60.0
	}
	if cfg.MarketMaking.BurstVelocityMinFloor == 0 {
		cfg.MarketMaking.BurstVelocityMinFloor = 0.5
	}
	if cfg.MarketMaking.BurstLevelMultiplier == 0 {
		cfg.MarketMaking.BurstLevelMultiplier = 2
	}
	if cfg.MarketMaking.BurstDecaySec == 0 {
		cfg.MarketMaking.BurstDecaySec = 30.0
	}
	if cfg.MarketMaking.ToxicityHalfLifeSec == 0 {
		cfg.MarketMaking.ToxicityHalfLifeSec = 120
	}
	if cfg.MarketMaking.ToxicityMaxWiden == 0 {
		cfg.MarketMaking.ToxicityMaxWiden = 2.0
	}
	// Execution defaults.
	if cfg.Execution.ReconGracePeriodMs == 0 {
		cfg.Execution.ReconGracePeriodMs = 3000
	}
	if cfg.Execution.ReconEmptyExchangeGraceMs == 0 {
		cfg.Execution.ReconEmptyExchangeGraceMs = 5000
	}
	if cfg.Execution.PollEquitySec == 0 {
		cfg.Execution.PollEquitySec = 15
	}
	if cfg.Execution.PollOpenOrdersSec == 0 {
		cfg.Execution.PollOpenOrdersSec = 15
	}
	if cfg.Execution.PollFillsSec == 0 {
		cfg.Execution.PollFillsSec = 10
	}
	if cfg.Execution.PollRateLimitSec == 0 {
		cfg.Execution.PollRateLimitSec = 30
	}
	// A6: Entry reduction defaults.
	if cfg.MarketMaking.EntryReductionRate > 0 && cfg.MarketMaking.EntryReductionFloor == 0 {
		cfg.MarketMaking.EntryReductionFloor = 0.2
	}
	// Trend Guard defaults.
	if cfg.MarketMaking.TrendGuardThresholdPct == 0 && cfg.MarketMaking.TrendGuardEnabled {
		cfg.MarketMaking.TrendGuardThresholdPct = 0.15
	}
	if cfg.MarketMaking.TrendGuardSaturationPct == 0 && cfg.MarketMaking.TrendGuardEnabled {
		cfg.MarketMaking.TrendGuardSaturationPct = 0.80
	}
	if cfg.MarketMaking.TrendGuardMaxSuppress == 0 && cfg.MarketMaking.TrendGuardEnabled {
		cfg.MarketMaking.TrendGuardMaxSuppress = 0.90
	}
	// P1: Regime gate defaults.
	if cfg.MarketMaking.RegimeGateMode == "" {
		cfg.MarketMaking.RegimeGateMode = "exit_only"
	}
	if cfg.MarketMaking.RegimeGateEnabled {
		if cfg.MarketMaking.RegimeAutocorrThreshold == 0 {
			cfg.MarketMaking.RegimeAutocorrThreshold = 0.35
		}
		if cfg.MarketMaking.RegimeVWAPDivergenceSigma == 0 {
			cfg.MarketMaking.RegimeVWAPDivergenceSigma = 2.0
		}
		if cfg.MarketMaking.RegimeGateMinDurationSec == 0 {
			cfg.MarketMaking.RegimeGateMinDurationSec = 30
		}
		if cfg.MarketMaking.RegimeGateMaxSuppression == 0 {
			cfg.MarketMaking.RegimeGateMaxSuppression = 0.80
		}
	}
	// Price Shock Guard defaults.
	if cfg.MarketMaking.PriceShockWindowMs == 0 {
		cfg.MarketMaking.PriceShockWindowMs = 5000
	}
	if cfg.MarketMaking.PriceShockThresholdBps == 0 {
		cfg.MarketMaking.PriceShockThresholdBps = 35.0
	}
	if cfg.MarketMaking.PriceShockCooldownMs == 0 {
		cfg.MarketMaking.PriceShockCooldownMs = 15000
	}
	// Flow Gate defaults.
	if cfg.MarketMaking.FlowGateThreshold == 0 && cfg.MarketMaking.FlowGateEnabled {
		cfg.MarketMaking.FlowGateThreshold = 0.50
	}
	if cfg.MarketMaking.FlowGateMaxSuppress == 0 && cfg.MarketMaking.FlowGateEnabled {
		cfg.MarketMaking.FlowGateMaxSuppress = 0.70
	}
	// B2: Toxicity defaults.
	if cfg.MarketMaking.ToxicitySizeReduceThreshold == 0 {
		cfg.MarketMaking.ToxicitySizeReduceThreshold = -2.0
	}
	if cfg.MarketMaking.ToxicitySizeReduceMax == 0 {
		cfg.MarketMaking.ToxicitySizeReduceMax = 0.5
	}
	if cfg.MarketMaking.ToxicityWarmupFills == 0 {
		cfg.MarketMaking.ToxicityWarmupFills = 5 // 0 = default to 5
	} else if cfg.MarketMaking.ToxicityWarmupFills < 0 {
		cfg.MarketMaking.ToxicityWarmupFills = 0 // negative = disabled
	}
	if cfg.MarketMaking.ToxicitySizeExponent <= 0 {
		cfg.MarketMaking.ToxicitySizeExponent = 1.0 // default: linear (legacy)
	}
	// B3: Inventory controller defaults.
	if cfg.MarketMaking.InventoryControllerSkewMin == 0 {
		cfg.MarketMaking.InventoryControllerSkewMin = 1.0
	}
	if cfg.MarketMaking.InventoryControllerSkewMax == 0 {
		cfg.MarketMaking.InventoryControllerSkewMax = 5.0
	}
	if cfg.MarketMaking.InventoryControllerGainP == 0 {
		cfg.MarketMaking.InventoryControllerGainP = 0.10
	}
	if cfg.MarketMaking.InventoryControllerGainI == 0 {
		cfg.MarketMaking.InventoryControllerGainI = 0.02
	}
	if cfg.MarketMaking.InventoryControllerGainD == 0 {
		cfg.MarketMaking.InventoryControllerGainD = 0.05
	}
	// Smart Layer 1: Markout-to-FV feedback defaults.
	if cfg.MarketMaking.MarkoutFVScale == 0 {
		cfg.MarketMaking.MarkoutFVScale = 0.5
	}
	if cfg.MarketMaking.MarkoutFVMaxShiftBps == 0 {
		cfg.MarketMaking.MarkoutFVMaxShiftBps = 3.0
	}
	// Edge gate defaults (only used when edge_gate_enabled).
	if cfg.MarketMaking.EdgeTargetBps <= 0 {
		cfg.MarketMaking.EdgeTargetBps = 1.0
	}
	if cfg.MarketMaking.EdgeAdverseFloorBps <= 0 {
		cfg.MarketMaking.EdgeAdverseFloorBps = 0.75
	}
	if cfg.MarketMaking.EdgeMaxWidthBps <= 0 {
		cfg.MarketMaking.EdgeMaxWidthBps = 10.0
	}
	if cfg.MarketMaking.EdgeFeeFloorBps <= 0 {
		cfg.MarketMaking.EdgeFeeFloorBps = 0.5
	}
	// Smart Layer 2: Spread stack cap default.
	if cfg.MarketMaking.MaxTotalSpreadMultiplier == 0 {
		cfg.MarketMaking.MaxTotalSpreadMultiplier = 3.0
	}
	// Smart Layer 5: Rate limit allocator defaults.
	if cfg.MarketMaking.RateLimitROIEWMAHalfLifeSec == 0 {
		cfg.MarketMaking.RateLimitROIEWMAHalfLifeSec = 300
	}
	if cfg.MarketMaking.RateLimitMinRefreshMultiplier == 0 {
		cfg.MarketMaking.RateLimitMinRefreshMultiplier = 0.7
	}
	if cfg.MarketMaking.RateLimitMaxRefreshMultiplier == 0 {
		cfg.MarketMaking.RateLimitMaxRefreshMultiplier = 3.0
	}
	// Funding rate session-end urgency defaults.
	if cfg.MarketMaking.FundingUrgencyScale <= 0 {
		cfg.MarketMaking.FundingUrgencyScale = 1.5
	}
	if cfg.MarketMaking.FundingUrgencyHorizonMinutes <= 0 {
		cfg.MarketMaking.FundingUrgencyHorizonMinutes = 30.0
	}
	if cfg.MarketMaking.FundingPollIntervalSec <= 0 {
		cfg.MarketMaking.FundingPollIntervalSec = 60
	}
	// Hardcoded value cleanup defaults.
	if cfg.MarketMaking.MarkoutCompositeWeights == [4]float64{} {
		cfg.MarketMaking.MarkoutCompositeWeights = [4]float64{0.20, 0.40, 0.25, 0.15}
	}
	if cfg.MarketMaking.MicropriceDecay == 0 {
		cfg.MarketMaking.MicropriceDecay = 0.7
	}
	if cfg.MarketMaking.EmergencyWidenThresholdBps == 0 {
		cfg.MarketMaking.EmergencyWidenThresholdBps = -3.0
	}
	if cfg.MarketMaking.EmergencyWidenAmountBps == 0 {
		cfg.MarketMaking.EmergencyWidenAmountBps = 2.0
	}
	if cfg.MarketMaking.QuietLadderReduceSec == 0 {
		cfg.MarketMaking.QuietLadderReduceSec = 30
	}
	if cfg.MarketMaking.LazyLadderDecaySec == 0 {
		cfg.MarketMaking.LazyLadderDecaySec = 30
	}
	if cfg.MarketMaking.ToxicitySizeWeightCap == 0 {
		cfg.MarketMaking.ToxicitySizeWeightCap = 3.0
	}
	if cfg.MarketMaking.VelocityInitialSeed == 0 {
		cfg.MarketMaking.VelocityInitialSeed = 1.0
	}
	if cfg.MarketMaking.QuoteAttributionMinIntervalSec == 0 {
		cfg.MarketMaking.QuoteAttributionMinIntervalSec = 30
	}
	// Optimizer V2 defaults.
	v2 := &cfg.MarketMaking.OptimizerV2
	if v2.EvalIntervalSec == 0 {
		v2.EvalIntervalSec = 30
	}
	if v2.WarmupEvals == 0 {
		v2.WarmupEvals = 5
	}
	if v2.MinFillSamples == 0 {
		v2.MinFillSamples = 5
	}
	if v2.RollbackCooldownSec == 0 {
		v2.RollbackCooldownSec = 120
	}
	if v2.Momentum == 0 {
		v2.Momentum = 0.8
	}
	if v2.LearningRate == 0 {
		v2.LearningRate = 0.1
	}
	if v2.MaxKnobChangesPerEval == 0 {
		v2.MaxKnobChangesPerEval = 3
	}
	if v2.EmergencyToxicityThreshold == 0 {
		v2.EmergencyToxicityThreshold = 0.9
	}
	if v2.RollbackWindowEvals == 0 {
		v2.RollbackWindowEvals = 3
	}
	if v2.RollbackRate == 0 {
		v2.RollbackRate = 0.5
	}
	if v2.ForceRefreshMinIntervalMs == 0 {
		v2.ForceRefreshMinIntervalMs = 3000
	}
	if v2.ForceRefreshMinSpreadDeltaBps == 0 {
		v2.ForceRefreshMinSpreadDeltaBps = 0.5
	}
	// V2 toxicity dimension weights.
	if v2.ToxicityWeights.MarkoutEWMA == 0 {
		v2.ToxicityWeights.MarkoutEWMA = 0.5
	}
	if v2.ToxicityWeights.FillBurst == 0 {
		v2.ToxicityWeights.FillBurst = 0.3
	}
	if v2.ToxicityWeights.EmergencyCount == 0 {
		v2.ToxicityWeights.EmergencyCount = 0.2
	}
	// V2 opportunity dimension weights.
	if v2.OpportunityWeights.FillRateRatio == 0 {
		v2.OpportunityWeights.FillRateRatio = 0.4
	}
	if v2.OpportunityWeights.RegimeSignal == 0 {
		v2.OpportunityWeights.RegimeSignal = 0.2
	}
	if v2.OpportunityWeights.VWAPDeviation == 0 {
		v2.OpportunityWeights.VWAPDeviation = 0.2
	}
	if v2.OpportunityWeights.SpreadBBORatio == 0 {
		v2.OpportunityWeights.SpreadBBORatio = 0.2
	}
	// V2 inventory stress dimension weights.
	if v2.InventoryStressWeights.PositionUtil == 0 {
		v2.InventoryStressWeights.PositionUtil = 0.5
	}
	if v2.InventoryStressWeights.ControllerAdj == 0 {
		v2.InventoryStressWeights.ControllerAdj = 0.3
	}
	if v2.InventoryStressWeights.SameSideDuration == 0 {
		v2.InventoryStressWeights.SameSideDuration = 0.2
	}
	// V2 policy threshold defaults.
	pt := &v2.PolicyThresholds
	if pt.ToxicityHigh == 0 {
		pt.ToxicityHigh = 0.6
	}
	if pt.ToxicityMedium == 0 {
		pt.ToxicityMedium = 0.5
	}
	if pt.ToxicityLow == 0 {
		pt.ToxicityLow = 0.3
	}
	if pt.OpportunityHigh == 0 {
		pt.OpportunityHigh = 0.5
	}
	if pt.OpportunityLow == 0 {
		pt.OpportunityLow = 0.3
	}
	if pt.InventoryHigh == 0 {
		pt.InventoryHigh = 0.4
	}
	if pt.InventoryLow == 0 {
		pt.InventoryLow = 0.15
	}
	if pt.BudgetHigh == 0 {
		pt.BudgetHigh = 0.7
	}
	if pt.BudgetMedium == 0 {
		pt.BudgetMedium = 0.5
	}
	if pt.AutocorrThreshold == 0 {
		pt.AutocorrThreshold = 0.1
	}
	if pt.FillRateDelta == 0 {
		pt.FillRateDelta = 0.5
	}
	if pt.ImbalanceSatHigh == 0 {
		pt.ImbalanceSatHigh = 0.6
	}
	if pt.ImbalanceSatLow == 0 {
		pt.ImbalanceSatLow = 0.2
	}
	if pt.PeerInventoryMin == 0 {
		pt.PeerInventoryMin = 0.2
	}
	if cfg.Strategy.BetaMin == 0 {
		cfg.Strategy.BetaMin = 0.5
	}
	if cfg.Strategy.BetaMax == 0 {
		cfg.Strategy.BetaMax = 1.8
	}
	if cfg.Risk.DrawdownSmoothingSamples == 0 {
		cfg.Risk.DrawdownSmoothingSamples = 1
	}

	// Rate limit circuit breaker defaults.
	if cfg.RateLimit.AbsoluteFloor == 0 {
		cfg.RateLimit.AbsoluteFloor = 300
	}
	if cfg.RateLimit.TTECautionMinutes == 0 {
		cfg.RateLimit.TTECautionMinutes = 60
	}
	if cfg.RateLimit.TTEDegradedMinutes == 0 {
		cfg.RateLimit.TTEDegradedMinutes = 30
	}
	if cfg.RateLimit.TTECriticalMinutes == 0 {
		cfg.RateLimit.TTECriticalMinutes = 15
	}
	if cfg.RateLimit.TTERecoveryMinutes == 0 {
		cfg.RateLimit.TTERecoveryMinutes = 45
	}
	if cfg.RateLimit.EWMAHalfLifeMinutes == 0 {
		cfg.RateLimit.EWMAHalfLifeMinutes = 10
	}
	if cfg.RateLimit.RemainingPctWarn == 0 {
		cfg.RateLimit.RemainingPctWarn = 10
	}
	if cfg.RateLimit.RemainingPctCritical == 0 {
		cfg.RateLimit.RemainingPctCritical = 3
	}
	if cfg.RateLimit.PctCriticalGraceSec == 0 {
		cfg.RateLimit.PctCriticalGraceSec = 120
	}
	if cfg.RateLimit.BudgetPressurePctStart == 0 {
		cfg.RateLimit.BudgetPressurePctStart = 20
	}

	// Resolve per-asset configs.
	cfg.resolvePerAsset()

	// Apply v2 defaults to per-asset resolved configs. Per-asset OptimizerV2
	// overrides replace the entire struct, zeroing fields not in the override.
	// Re-apply defaults so validation doesn't fail on zero-valued control params.
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.Resolved[asset]
		pv2 := &ac.MarketMaking.OptimizerV2
		if pv2.Enabled {
			if pv2.EvalIntervalSec == 0 {
				pv2.EvalIntervalSec = v2.EvalIntervalSec
			}
			if pv2.WarmupEvals == 0 {
				pv2.WarmupEvals = v2.WarmupEvals
			}
			if pv2.MinFillSamples == 0 {
				pv2.MinFillSamples = v2.MinFillSamples
			}
			if pv2.RollbackCooldownSec == 0 {
				pv2.RollbackCooldownSec = v2.RollbackCooldownSec
			}
			if pv2.Momentum == 0 {
				pv2.Momentum = v2.Momentum
			}
			if pv2.LearningRate == 0 {
				pv2.LearningRate = v2.LearningRate
			}
			if pv2.MaxKnobChangesPerEval == 0 {
				pv2.MaxKnobChangesPerEval = v2.MaxKnobChangesPerEval
			}
			if pv2.EmergencyToxicityThreshold == 0 {
				pv2.EmergencyToxicityThreshold = v2.EmergencyToxicityThreshold
			}
			if pv2.RollbackWindowEvals == 0 {
				pv2.RollbackWindowEvals = v2.RollbackWindowEvals
			}
			if pv2.RollbackRate == 0 {
				pv2.RollbackRate = v2.RollbackRate
			}
			if pv2.ForceRefreshMinIntervalMs == 0 {
				pv2.ForceRefreshMinIntervalMs = v2.ForceRefreshMinIntervalMs
			}
			if pv2.ForceRefreshMinSpreadDeltaBps == 0 {
				pv2.ForceRefreshMinSpreadDeltaBps = v2.ForceRefreshMinSpreadDeltaBps
			}
			if pv2.ToxicityWeights == (OptimizerV2DimensionWeights{}) {
				pv2.ToxicityWeights = v2.ToxicityWeights
			}
			if pv2.OpportunityWeights == (OptimizerV2OpportunityWeights{}) {
				pv2.OpportunityWeights = v2.OpportunityWeights
			}
			if pv2.InventoryStressWeights == (OptimizerV2InventoryWeights{}) {
				pv2.InventoryStressWeights = v2.InventoryStressWeights
			}
			if pv2.PolicyThresholds == (OptimizerV2PolicyThresholds{}) {
				pv2.PolicyThresholds = v2.PolicyThresholds
			}
			// Knob bounds: copy from global v2 if per-asset v2 bounds are zero.
			if pv2.SpreadBps.Min == 0 && pv2.SpreadBps.Max == 0 {
				pv2.SpreadBps = v2.SpreadBps
			}
			if pv2.NotionalMult.Min == 0 && pv2.NotionalMult.Max == 0 {
				pv2.NotionalMult = v2.NotionalMult
			}
			if pv2.SkewMult.Min == 0 && pv2.SkewMult.Max == 0 {
				pv2.SkewMult = v2.SkewMult
			}
			if pv2.VWAPAlpha.Min == 0 && pv2.VWAPAlpha.Max == 0 {
				pv2.VWAPAlpha = v2.VWAPAlpha
			}
			if pv2.ImbalanceShiftMult.Min == 0 && pv2.ImbalanceShiftMult.Max == 0 {
				pv2.ImbalanceShiftMult = v2.ImbalanceShiftMult
			}
			if pv2.FillDecayMult.Min == 0 && pv2.FillDecayMult.Max == 0 {
				pv2.FillDecayMult = v2.FillDecayMult
			}
			if pv2.RefreshMult.Min == 0 && pv2.RefreshMult.Max == 0 {
				pv2.RefreshMult = v2.RefreshMult
			}
		}
		cfg.Resolved[asset] = ac
	}

	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if w := ac.MarketMaking.ToxicityT5sWeight; w < 0 || w >= 1 {
			return nil, fmt.Errorf("config: toxicity_t5s_weight must be in [0, 1), got %.2f for %s", w, asset)
		}
	}

	if err := cfg.validate(lo.skipCredentials); err != nil {
		return nil, err
	}

	return &cfg, nil
}

// resolvePerAsset builds the Resolved map by copying global config per asset
// and applying any per-asset overrides.
func (c *Config) resolvePerAsset() {
	c.Resolved = make(map[string]AssetConfig, len(c.Strategy.Assets))
	for _, asset := range c.Strategy.Assets {
		ac := AssetConfig{
			MarketMaking: c.MarketMaking,
			Execution:    c.Execution,
			Risk:         c.Risk,
		}
		// Clear the PerAsset maps on the copies to avoid nesting.
		ac.MarketMaking.PerAsset = nil
		ac.Execution.PerAsset = nil
		ac.Risk.PerAsset = nil

		// Apply per-asset overrides via reflection-based merge.
		if ov, ok := c.MarketMaking.PerAsset[asset]; ok {
			applyOverride(&ac.MarketMaking, &ov)
		}
		if ov, ok := c.Execution.PerAsset[asset]; ok {
			applyOverride(&ac.Execution, &ov)
		}
		if ov, ok := c.Risk.PerAsset[asset]; ok {
			applyOverride(&ac.Risk, &ov)
		}
		c.Resolved[asset] = ac
	}
}

// validate checks all configuration constraints. When skipCredentials is true,
// the QuickNode/private-key checks are skipped so offline `bot -validate` runs
// can verify schema + topology without requiring real credentials.
func (c *Config) validate(skipCredentials bool) error {
	if !skipCredentials {
		// QuickNode endpoint is always required.
		if c.DataSource != "public" && c.QuickNodeEndpoint == "" {
			return fmt.Errorf("config: quicknode_subdomain and quicknode_auth_token must be set (in env section or QUICKNODE_SUBDOMAIN/QUICKNODE_AUTH_TOKEN env vars)")
		}

		// Private key required only in live mode.
		if c.Execution.LiveMode && c.PrivateKey == "" {
			return fmt.Errorf("config: private_key must be set when live_mode=true (in env section or PRIVATE_KEY env var)")
		}
		if c.Execution.LiveMode && c.WalletAddress == "" {
			return fmt.Errorf("config: WALLET_ADDRESS must be the main account address in live mode")
		}
	}
	if c.WalletAddress != "" {
		if len(c.WalletAddress) != 42 || !strings.HasPrefix(c.WalletAddress, "0x") || strings.Trim(c.WalletAddress[2:], "0123456789abcdefABCDEF") != "" {
			return fmt.Errorf("config: invalid wallet_address (expected 0x and 40 hex characters)")
		}
	}

	// Strategy validations.
	if c.DataSource != "" && c.DataSource != "quicknode" && c.DataSource != "public" {
		return fmt.Errorf("config: data_source must be public or quicknode")
	}
	if len(c.Strategy.Assets) < 1 {
		return fmt.Errorf("config: strategy.assets must contain at least 1 entry")
	}

	// Validate asset topology.
	if len(c.Strategy.Assets) > 6 {
		return fmt.Errorf("config: at most 6 assets are supported, got %d", len(c.Strategy.Assets))
	}
	seenBases := make(map[string]bool)
	var hipDex string // the single HIP-3 dex name allowed (e.g. "xyz"); "" means none seen yet
	for i, asset := range c.Strategy.Assets {
		dex, base := types.ParseAsset(asset)
		if base == "" {
			return fmt.Errorf("config: asset %d (%q): malformed asset string — empty base symbol", i, asset)
		}
		if strings.Contains(asset, ":") && dex == "" {
			return fmt.Errorf("config: asset %d (%q): malformed asset string — empty dex prefix", i, asset)
		}
		if seenBases[base] {
			return fmt.Errorf("config: asset %d (%q): duplicate base symbol %q", i, asset, base)
		}
		seenBases[base] = true
		// Allow mixing HIP-3 (dex != "") with standard perps (dex == ""),
		// but reject two different HIP-3 dex namespaces (executor supports at most one).
		if dex != "" {
			if hipDex == "" {
				hipDex = dex
			} else if dex != hipDex {
				return fmt.Errorf("config: multiple HIP-3 dex namespaces not supported: saw %q and %q", hipDex, dex)
			}
		}
	}

	// Only require spread_type when the spread engine will actually be used.
	spreadEngineActive := len(c.Strategy.Assets) >= 2
	if c.Strategy.SpreadEngineEnabled != nil {
		spreadEngineActive = *c.Strategy.SpreadEngineEnabled
	}
	if spreadEngineActive && c.Strategy.SpreadType != "difference" && c.Strategy.SpreadType != "ratio" {
		return fmt.Errorf("config: strategy.spread_type must be \"difference\" or \"ratio\", got %q", c.Strategy.SpreadType)
	}
	if c.Strategy.LookbackWindow <= 0 {
		return fmt.Errorf("config: strategy.lookback_window must be positive")
	}
	if c.Strategy.BetaMin <= 0 {
		return fmt.Errorf("config: strategy.beta_min must be positive, got %g", c.Strategy.BetaMin)
	}
	if c.Strategy.BetaMax <= c.Strategy.BetaMin {
		return fmt.Errorf("config: strategy.beta_max must be > beta_min, got max=%g min=%g", c.Strategy.BetaMax, c.Strategy.BetaMin)
	}
	if c.Strategy.BetaMax > 5.0 {
		return fmt.Errorf("config: strategy.beta_max must be <= 5.0, got %g", c.Strategy.BetaMax)
	}

	// Validate per_asset keys reference actual assets.
	assetSet := make(map[string]bool, len(c.Strategy.Assets))
	for _, a := range c.Strategy.Assets {
		assetSet[a] = true
	}
	for key := range c.MarketMaking.PerAsset {
		if !assetSet[key] {
			return fmt.Errorf("config: market_making.per_asset key %q is not in strategy.assets", key)
		}
	}
	for key := range c.Execution.PerAsset {
		if !assetSet[key] {
			return fmt.Errorf("config: execution.per_asset key %q is not in strategy.assets", key)
		}
	}
	for key := range c.Risk.PerAsset {
		if !assetSet[key] {
			return fmt.Errorf("config: risk.per_asset key %q is not in strategy.assets", key)
		}
	}

	// Validate global config.
	if err := validateMarketMaking(&c.MarketMaking, "global"); err != nil {
		return err
	}
	if err := validateRisk(&c.Risk, "global"); err != nil {
		return err
	}

	// Validate each resolved per-asset config.
	for _, asset := range c.Strategy.Assets {
		ac := c.Resolved[asset]
		if err := validateMarketMaking(&ac.MarketMaking, asset); err != nil {
			return err
		}
		if err := validateRisk(&ac.Risk, asset); err != nil {
			return err
		}
		// Per-asset execution validation.
		if ac.Execution.Leverage < 1 || ac.Execution.Leverage > 50 {
			return fmt.Errorf("config[%s]: execution.leverage must be in [1, 50], got %d", asset, ac.Execution.Leverage)
		}
		// P5: cross-section check — session_pnl fourth-dim requires a nonzero
		// session PnL window on the resolved risk config.
		if ac.MarketMaking.OptimizerV2.Enabled &&
			ac.MarketMaking.OptimizerV2.FourthDimension == "session_pnl" &&
			ac.Risk.SessionPnLWindowMinutes <= 0 {
			return fmt.Errorf("config[%s]: optimizer_v2.fourth_dimension=\"session_pnl\" requires risk.session_pnl_window_minutes > 0", asset)
		}
	}
	// P5: cross-section check on global config.
	if c.MarketMaking.OptimizerV2.Enabled &&
		c.MarketMaking.OptimizerV2.FourthDimension == "session_pnl" &&
		c.Risk.SessionPnLWindowMinutes <= 0 {
		return fmt.Errorf("config: optimizer_v2.fourth_dimension=\"session_pnl\" requires risk.session_pnl_window_minutes > 0")
	}

	// Execution validations (global).
	if c.Execution.Leverage < 1 || c.Execution.Leverage > 50 {
		return fmt.Errorf("config: execution.leverage must be in [1, 50], got %d", c.Execution.Leverage)
	}

	if c.Execution.ReconMismatchDiagnosticMaxPerHour < 0 {
		return fmt.Errorf("config: execution.recon_mismatch_diagnostic_max_per_hour must be >= 0, got %d", c.Execution.ReconMismatchDiagnosticMaxPerHour)
	}

	// Rate limit circuit breaker validation.
	rl := c.RateLimit
	if rl.AbsoluteFloor < 0 {
		return fmt.Errorf("config: rate_limit.absolute_floor must be >= 0, got %d", rl.AbsoluteFloor)
	}
	if rl.TTECriticalMinutes >= rl.TTEDegradedMinutes {
		return fmt.Errorf("config: rate_limit.tte_critical_minutes (%d) must be < tte_degraded_minutes (%d)",
			rl.TTECriticalMinutes, rl.TTEDegradedMinutes)
	}
	if rl.TTEDegradedMinutes >= rl.TTECautionMinutes {
		return fmt.Errorf("config: rate_limit.tte_degraded_minutes (%d) must be < tte_caution_minutes (%d)",
			rl.TTEDegradedMinutes, rl.TTECautionMinutes)
	}
	if rl.TTERecoveryMinutes <= rl.TTECriticalMinutes {
		return fmt.Errorf("config: rate_limit.tte_recovery_minutes (%d) must be > tte_critical_minutes (%d)",
			rl.TTERecoveryMinutes, rl.TTECriticalMinutes)
	}
	if rl.EWMAHalfLifeMinutes <= 0 {
		return fmt.Errorf("config: rate_limit.ewma_half_life_minutes must be > 0, got %d", rl.EWMAHalfLifeMinutes)
	}
	if rl.RemainingPctWarn <= 0 || rl.RemainingPctWarn > 100 {
		return fmt.Errorf("config: rate_limit.remaining_pct_warn must be in (0, 100], got %.1f", rl.RemainingPctWarn)
	}
	if rl.RemainingPctCritical <= 0 || rl.RemainingPctCritical >= rl.RemainingPctWarn {
		return fmt.Errorf("config: rate_limit.remaining_pct_critical must be in (0, remaining_pct_warn), got %.1f (warn=%.1f)",
			rl.RemainingPctCritical, rl.RemainingPctWarn)
	}
	if rl.BudgetPressurePctStart < 0 || rl.BudgetPressurePctStart > 100 {
		return fmt.Errorf("config: rate_limit.budget_pressure_pct_start must be in [0, 100], got %.1f", rl.BudgetPressurePctStart)
	}
	if rl.BudgetPressurePctStart > 0 && rl.BudgetPressurePctStart <= rl.RemainingPctCritical {
		return fmt.Errorf("config: rate_limit.budget_pressure_pct_start (%.1f) must be > remaining_pct_critical (%.1f)",
			rl.BudgetPressurePctStart, rl.RemainingPctCritical)
	}
	if rl.PctCriticalGraceSec < 0 {
		return fmt.Errorf("config: rate_limit.pct_critical_grace_sec must be >= 0, got %d", rl.PctCriticalGraceSec)
	}

	// P12: Trading schedule window validation (format, tz, multiplier bounds).
	if err := validateTradingSchedule(c.MarketMaking.TradingSchedule); err != nil {
		return err
	}

	// P13: post-mortem dumper validation.
	if c.PostMortem.Enabled {
		if c.PostMortem.OutputDir == "" {
			// Defaulted at construction time, but flag missing dir up front
			// when explicitly enabled with no other PostMortem fields set —
			// users who enable should at least see the resolved default in
			// the startup config dump.
		}
		if c.PostMortem.IncludeLastNEvals < 0 {
			return fmt.Errorf("config: postmortem.include_last_n_evals must be >= 0, got %d", c.PostMortem.IncludeLastNEvals)
		}
		if c.PostMortem.IncludeLastNRefreshes < 0 {
			return fmt.Errorf("config: postmortem.include_last_n_refreshes must be >= 0, got %d", c.PostMortem.IncludeLastNRefreshes)
		}
		if c.PostMortem.RotateKeepCount < 0 {
			return fmt.Errorf("config: postmortem.rotate_keep_count must be >= 0, got %d", c.PostMortem.RotateKeepCount)
		}
	}

	return nil
}

// validateTradingSchedule checks each window's HH:MM format, IANA tz, multiplier
// range, and start<end ordering. Returns nil for empty input.
func validateTradingSchedule(windows []TradingScheduleWindowConfig) error {
	for i, w := range windows {
		startMin, err := parseScheduleHHMM(w.Start)
		if err != nil {
			return fmt.Errorf("config: market_making.trading_schedule[%d].start %q: %w", i, w.Start, err)
		}
		endMin, err := parseScheduleHHMM(w.End)
		if err != nil {
			return fmt.Errorf("config: market_making.trading_schedule[%d].end %q: %w", i, w.End, err)
		}
		if startMin >= endMin {
			return fmt.Errorf("config: market_making.trading_schedule[%d]: start %q must be before end %q (same calendar day; split cross-midnight windows)", i, w.Start, w.End)
		}
		if w.NotionalMultiplier < 0 || w.NotionalMultiplier > 1 {
			return fmt.Errorf("config: market_making.trading_schedule[%d].notional_multiplier must be in [0, 1], got %g", i, w.NotionalMultiplier)
		}
		if w.Tz == "" {
			return fmt.Errorf("config: market_making.trading_schedule[%d].tz must be set (IANA zone, e.g. America/New_York)", i)
		}
		if _, err := timeLoadLocation(w.Tz); err != nil {
			return fmt.Errorf("config: market_making.trading_schedule[%d].tz %q: %w", i, w.Tz, err)
		}
	}
	return nil
}

func parseScheduleHHMM(s string) (int, error) {
	parts := strings.Split(s, ":")
	if len(parts) != 2 {
		return 0, fmt.Errorf("must be HH:MM")
	}
	h, herr := strconv.Atoi(parts[0])
	if herr != nil || h < 0 || h > 23 {
		return 0, fmt.Errorf("hour must be 00-23")
	}
	m, merr := strconv.Atoi(parts[1])
	if merr != nil || m < 0 || m > 59 {
		return 0, fmt.Errorf("minute must be 00-59")
	}
	return h*60 + m, nil
}

// validateMarketMaking validates MarketMaking fields. asset is used in error messages.
func validateMarketMaking(mm *MarketMakingConfig, asset string) error {
	prefix := "config"
	if asset != "global" {
		prefix = fmt.Sprintf("config[%s]", asset)
	}

	if mm.QuoteSpreadBps <= 0 {
		return fmt.Errorf("%s: market_making.quote_spread_bps must be positive", prefix)
	}
	if mm.QuoteNotional < 10 {
		return fmt.Errorf("%s: market_making.quote_notional must be >= 10 (Hyperliquid minimum order value)", prefix)
	}
	if mm.RefreshIntervalMs <= 0 {
		return fmt.Errorf("%s: market_making.refresh_interval_ms must be positive", prefix)
	}
	if mm.InventorySkewFactor < 0 || mm.InventorySkewFactor > 10 {
		return fmt.Errorf("%s: market_making.inventory_skew_factor must be in [0, 10]", prefix)
	}
	if mm.InventorySkewGamma < 0.1 || mm.InventorySkewGamma > 5.0 {
		return fmt.Errorf("%s: market_making.inventory_skew_gamma must be in [0.1, 5.0], got %g", prefix, mm.InventorySkewGamma)
	}
	if mm.LadderLevels < 1 {
		return fmt.Errorf("%s: market_making.ladder_levels must be >= 1", prefix)
	}
	if mm.LadderSpacingBps < 0 {
		return fmt.Errorf("%s: market_making.ladder_spacing_bps must be >= 0", prefix)
	}
	if mm.WidthVolWindow < 1 {
		return fmt.Errorf("%s: market_making.width_vol_window must be >= 1", prefix)
	}
	if mm.WidthMaxMultiplier < 1.0 {
		return fmt.Errorf("%s: market_making.width_max_multiplier must be >= 1.0", prefix)
	}
	if mm.WidthImbalanceLevels < 1 {
		return fmt.Errorf("%s: market_making.width_imbalance_levels must be >= 1", prefix)
	}
	if mm.RequoteThresholdBps < 0 {
		return fmt.Errorf("%s: market_making.requote_threshold_bps must be >= 0", prefix)
	}
	if mm.SizeRequotePct < 0 || mm.SizeRequotePct > 1 {
		return fmt.Errorf("%s: market_making.size_requote_pct must be in [0, 1]", prefix)
	}
	if mm.MaxQuoteAgeMs < mm.RefreshIntervalMs {
		return fmt.Errorf("%s: market_making.max_quote_age_ms must be >= refresh_interval_ms", prefix)
	}
	if mm.MaxCrossLegSkewMs <= 0 {
		return fmt.Errorf("%s: market_making.max_cross_leg_skew_ms must be > 0", prefix)
	}
	if mm.VWAPAlpha < 0 || mm.VWAPAlpha > 1 {
		return fmt.Errorf("%s: market_making.vwap_alpha must be in [0, 1]", prefix)
	}
	if mm.VWAPAlphaDriftScalingEnabled {
		if mm.VWAPAlphaDriftK <= 0 {
			return fmt.Errorf("%s: market_making.vwap_alpha_drift_k must be > 0 when vwap_alpha_drift_scaling_enabled", prefix)
		}
		if mm.VWAPAlphaFloor < 0 || mm.VWAPAlphaFloor > mm.VWAPAlpha {
			return fmt.Errorf("%s: market_making.vwap_alpha_floor must be in [0, vwap_alpha]", prefix)
		}
	}
	if mm.VWAPAlphaTrendBoostEnabled {
		if mm.VWAPAlphaTrendBoostCeiling < mm.VWAPAlpha {
			return fmt.Errorf("%s: market_making.vwap_alpha_trend_boost_ceiling must be >= vwap_alpha", prefix)
		}
		if mm.VWAPAlphaTrendBoostCeiling > 1.0 {
			return fmt.Errorf("%s: market_making.vwap_alpha_trend_boost_ceiling must be <= 1.0", prefix)
		}
	}
	if mm.TrendFVDriftBps < 0 || mm.TrendFVDriftBps > 20.0 {
		return fmt.Errorf("%s: market_making.trend_fv_drift_bps must be in [0, 20.0]", prefix)
	}
	if mm.TrendFVDriftAutocorrMin < 0 || mm.TrendFVDriftAutocorrMin > 1.0 {
		return fmt.Errorf("%s: market_making.trend_fv_drift_autocorr_min must be in [0, 1.0]", prefix)
	}
	if mm.EntryReductionTrendK < 0 || mm.EntryReductionTrendK > 10.0 {
		return fmt.Errorf("%s: market_making.entry_reduction_trend_k must be in [0, 10.0]", prefix)
	}
	if mm.VWAPShortWindowMinutes < 1 {
		return fmt.Errorf("%s: market_making.vwap_short_window_minutes must be >= 1", prefix)
	}
	if mm.VWAPLongWindowMinutes < mm.VWAPShortWindowMinutes {
		return fmt.Errorf("%s: market_making.vwap_long_window_minutes must be >= vwap_short_window_minutes", prefix)
	}
	if mm.SpreadBiasScale <= 0 {
		return fmt.Errorf("%s: market_making.spread_bias_scale must be positive", prefix)
	}
	if mm.SpreadBiasMaxBps < 0 {
		return fmt.Errorf("%s: market_making.spread_bias_max_bps must be >= 0", prefix)
	}
	if mm.SpreadBiasMaxFraction < 0 || mm.SpreadBiasMaxFraction > 1.0 {
		return fmt.Errorf("%s: market_making.spread_bias_max_fraction must be in [0, 1.0]", prefix)
	}
	if mm.SpreadBiasAsymmetry < 0 || mm.SpreadBiasAsymmetry > 1.0 {
		return fmt.Errorf("%s: market_making.spread_bias_asymmetry must be in [0, 1.0]", prefix)
	}
	if mm.ZWideningThreshold <= 0 {
		return fmt.Errorf("%s: market_making.z_widening_threshold must be positive", prefix)
	}
	if mm.ZWideningRate < 0 {
		return fmt.Errorf("%s: market_making.z_widening_rate must be >= 0", prefix)
	}
	if mm.ZMaxWidthMultiplier < 1.0 {
		return fmt.Errorf("%s: market_making.z_max_width_multiplier must be >= 1.0", prefix)
	}
	if mm.WickSizeMultiplier < 1.0 {
		return fmt.Errorf("%s: market_making.wick_size_multiplier must be >= 1.0", prefix)
	}
	if mm.WickDevThresholdPct < 0 {
		return fmt.Errorf("%s: market_making.wick_dev_threshold_pct must be >= 0", prefix)
	}
	if mm.LadderSizeGraduation < 1.0 || mm.LadderSizeGraduation > 10.0 {
		return fmt.Errorf("%s: market_making.ladder_size_graduation must be in [1.0, 10.0], got %g", prefix, mm.LadderSizeGraduation)
	}
	if mm.LadderSpacingExponent < 0.5 || mm.LadderSpacingExponent > 3.0 {
		return fmt.Errorf("%s: market_making.ladder_spacing_exponent must be in [0.5, 3.0], got %g", prefix, mm.LadderSpacingExponent)
	}
	if mm.WallFrontEnabled && mm.WallMinSizeMultiplier < 1.0 {
		return fmt.Errorf("%s: wall_min_size_multiplier must be >= 1.0, got %g", prefix, mm.WallMinSizeMultiplier)
	}
	if mm.MakerFeeBps < 0 {
		return fmt.Errorf("%s: market_making.maker_fee_bps must be >= 0", prefix)
	}
	if mm.BuilderFeeBps < 0 {
		return fmt.Errorf("%s: market_making.builder_fee_bps must be >= 0", prefix)
	}
	if mm.EdgeTargetBps < 0 || mm.EdgeTargetBps > 50 {
		return fmt.Errorf("%s: market_making.edge_target_bps must be in [0, 50], got %g", prefix, mm.EdgeTargetBps)
	}
	if mm.EdgeAdverseFloorBps < 0 || mm.EdgeAdverseFloorBps > 50 {
		return fmt.Errorf("%s: market_making.edge_adverse_floor_bps must be in [0, 50], got %g", prefix, mm.EdgeAdverseFloorBps)
	}
	if mm.EdgeMaxWidthBps < 0 || mm.EdgeMaxWidthBps > 200 {
		return fmt.Errorf("%s: market_making.edge_max_width_bps must be in [0, 200], got %g", prefix, mm.EdgeMaxWidthBps)
	}
	if mm.EdgeFeeFloorBps < 0 || mm.EdgeFeeFloorBps > 50 {
		return fmt.Errorf("%s: market_making.edge_fee_floor_bps must be in [0, 50], got %g", prefix, mm.EdgeFeeFloorBps)
	}
	if mm.MaxBookStalenessMs > 0 && mm.MaxBookStalenessMs <= mm.RefreshIntervalMs {
		return fmt.Errorf("%s: max_book_staleness_ms(%d) must be > refresh_interval_ms(%d) to avoid perpetual staleness cancellations",
			prefix, mm.MaxBookStalenessMs, mm.RefreshIntervalMs)
	}
	if mm.TradeImbalanceWindowSec < 1 {
		return fmt.Errorf("%s: market_making.trade_imbalance_window_sec must be >= 1", prefix)
	}
	if mm.TradeImbalanceMaxShiftBps < 0 {
		return fmt.Errorf("%s: market_making.trade_imbalance_max_shift_bps must be >= 0", prefix)
	}
	if mm.TradeImbalanceSizeExponent > 5.0 {
		return fmt.Errorf("%s: market_making.trade_imbalance_size_exponent must be in (0, 5.0], got %g", prefix, mm.TradeImbalanceSizeExponent)
	}
	if mm.ToxicitySizeExponent > 5.0 {
		return fmt.Errorf("%s: market_making.toxicity_size_exponent must be in (0, 5.0], got %g", prefix, mm.ToxicitySizeExponent)
	}
	// Funding urgency bounds.
	if mm.FundingUrgencyScale > 10.0 {
		return fmt.Errorf("%s: market_making.funding_urgency_scale must be in (0, 10.0], got %g", prefix, mm.FundingUrgencyScale)
	}
	if mm.FundingUrgencyHorizonMinutes > 120.0 {
		return fmt.Errorf("%s: market_making.funding_urgency_horizon_minutes must be in (0, 120], got %g", prefix, mm.FundingUrgencyHorizonMinutes)
	}
	if mm.FundingPollIntervalSec > 0 && mm.FundingPollIntervalSec < 10 {
		return fmt.Errorf("%s: market_making.funding_poll_interval_sec must be >= 10 to avoid API abuse, got %d", prefix, mm.FundingPollIntervalSec)
	}
	if mm.TradeImbalanceMode != "time" && mm.TradeImbalanceMode != "volume" {
		return fmt.Errorf("%s: market_making.trade_imbalance_mode must be \"time\" or \"volume\", got %q", prefix, mm.TradeImbalanceMode)
	}
	if mm.TradeImbalanceMode == "volume" {
		if mm.TradeImbalanceBucketSize <= 0 {
			return fmt.Errorf("%s: market_making.trade_imbalance_bucket_size must be > 0", prefix)
		}
		if mm.TradeImbalanceBucketCount < 1 {
			return fmt.Errorf("%s: market_making.trade_imbalance_bucket_count must be >= 1", prefix)
		}
	}
	if mm.FillAdverseShiftBps < 0 {
		return fmt.Errorf("%s: market_making.fill_adverse_shift_bps must be >= 0", prefix)
	}
	if mm.FillDecayHalfLifeMs < 1 {
		return fmt.Errorf("%s: market_making.fill_decay_half_life_ms must be >= 1", prefix)
	}
	if mm.MicropriceLevels < 1 {
		return fmt.Errorf("%s: market_making.microprice_levels must be >= 1", prefix)
	}
	if mm.ZWideningAsymmetry < 0 || mm.ZWideningAsymmetry > 1 {
		return fmt.Errorf("%s: market_making.z_widening_asymmetry must be in [0, 1]", prefix)
	}
	if mm.InventorySkewAsymmetry < 0 || mm.InventorySkewAsymmetry > 1 {
		return fmt.Errorf("%s: market_making.inventory_skew_asymmetry must be in [0, 1]", prefix)
	}
	if mm.CrossLegSkewFactor < 0 || mm.CrossLegSkewFactor > 5.0 {
		return fmt.Errorf("%s: market_making.cross_leg_skew_factor must be in [0, 5.0]", prefix)
	}
	if mm.PairReduceEntryFraction <= 0 || mm.PairReduceEntryFraction > 1.0 {
		return fmt.Errorf("%s: market_making.pair_reduce_entry_fraction must be in (0, 1.0], got %g", prefix, mm.PairReduceEntryFraction)
	}
	if mm.PairReduceExitFraction <= 0 || mm.PairReduceExitFraction >= mm.PairReduceEntryFraction {
		return fmt.Errorf("%s: market_making.pair_reduce_exit_fraction must be in (0, entry_fraction=%g), got %g", prefix, mm.PairReduceEntryFraction, mm.PairReduceExitFraction)
	}
	if mm.PairReduceTightenFactor <= 0 || mm.PairReduceTightenFactor > 1.0 {
		return fmt.Errorf("%s: market_making.pair_reduce_tighten_factor must be in (0, 1.0], got %g", prefix, mm.PairReduceTightenFactor)
	}
	if mm.MinCrossingSpreadBps < 0 || mm.MinCrossingSpreadBps > 20.0 {
		return fmt.Errorf("%s: market_making.min_crossing_spread_bps must be in [0, 20.0], got %g", prefix, mm.MinCrossingSpreadBps)
	}
	if mm.PairReduceMaxDurationSec < 0 {
		return fmt.Errorf("%s: market_making.pair_reduce_max_duration_sec must be >= 0, got %d", prefix, mm.PairReduceMaxDurationSec)
	}
	if mm.WickMaxInventoryUtil < 0 || mm.WickMaxInventoryUtil > 1.0 {
		return fmt.Errorf("%s: market_making.wick_max_inventory_util must be in [0, 1.0], got %g", prefix, mm.WickMaxInventoryUtil)
	}
	if mm.MaxFVDeviationPct < 0 {
		return fmt.Errorf("%s: market_making.max_fv_deviation_pct must be >= 0", prefix)
	}
	if mm.MaxNoBookUpdateMs < 0 {
		return fmt.Errorf("%s: market_making.max_no_book_update_ms must be >= 0", prefix)
	}
	if mm.MaxNoBookUpdateMs > 0 && mm.MaxBookStalenessMs > 0 && mm.MaxNoBookUpdateMs < mm.MaxBookStalenessMs {
		return fmt.Errorf("%s: max_no_book_update_ms(%d) must be >= max_book_staleness_ms(%d)",
			prefix, mm.MaxNoBookUpdateMs, mm.MaxBookStalenessMs)
	}
	// A7: Dynamic refresh interval validation.
	if mm.RefreshPostFillMs < 0 {
		return fmt.Errorf("%s: market_making.refresh_post_fill_ms must be >= 0", prefix)
	}
	if mm.RefreshAdverseMs < 0 {
		return fmt.Errorf("%s: market_making.refresh_adverse_ms must be >= 0", prefix)
	}
	if mm.RefreshQuietMs < 0 {
		return fmt.Errorf("%s: market_making.refresh_quiet_ms must be >= 0", prefix)
	}
	if mm.RefreshQuietThresholdMs < 0 {
		return fmt.Errorf("%s: market_making.refresh_quiet_threshold_ms must be >= 0", prefix)
	}
	// A6: Entry reduction validation.
	if mm.EntryReductionRate < 0 || mm.EntryReductionRate > 5.0 {
		return fmt.Errorf("%s: market_making.entry_size_reduction_rate must be in [0, 5.0]", prefix)
	}
	if mm.EntryReductionFloor < 0 || mm.EntryReductionFloor > 1.0 {
		return fmt.Errorf("%s: market_making.entry_size_reduction_floor must be in [0, 1.0]", prefix)
	}
	// A6b: Entry acceleration validation.
	if mm.EntryAccelThreshold < 0 || mm.EntryAccelThreshold > 1.0 {
		return fmt.Errorf("%s: market_making.entry_accel_threshold must be in [0, 1.0]", prefix)
	}
	if mm.EntryAccelExponent < 0 || mm.EntryAccelExponent > 10.0 {
		return fmt.Errorf("%s: market_making.entry_accel_exponent must be in [0, 10.0]", prefix)
	}
	// Trend Guard validation.
	if mm.TrendGuardEnabled {
		if mm.TrendGuardThresholdPct <= 0 || mm.TrendGuardThresholdPct > 5.0 {
			return fmt.Errorf("%s: market_making.trend_guard_threshold_pct must be in (0, 5.0]", prefix)
		}
		if mm.TrendGuardSaturationPct <= mm.TrendGuardThresholdPct {
			return fmt.Errorf("%s: market_making.trend_guard_saturation_pct must be > threshold_pct", prefix)
		}
		if mm.TrendGuardMaxSuppress <= 0 || mm.TrendGuardMaxSuppress > 1.0 {
			return fmt.Errorf("%s: market_making.trend_guard_max_suppress must be in (0, 1.0]", prefix)
		}
		if mm.TrendGuardSpreadWidenBps < 0 || mm.TrendGuardSpreadWidenBps > 20.0 {
			return fmt.Errorf("%s: market_making.trend_guard_spread_widen_bps must be in [0, 20.0]", prefix)
		}
		if mm.TrendGuardFreezeThreshold < 0 || mm.TrendGuardFreezeThreshold > 1.0 {
			return fmt.Errorf("%s: market_making.trend_guard_freeze_threshold must be in [0, 1.0]", prefix)
		}
		if mm.TrendGuardSpreadWidenAsymmetry < 0 || mm.TrendGuardSpreadWidenAsymmetry > 5.0 {
			return fmt.Errorf("%s: market_making.trend_guard_spread_widen_asymmetry must be in [0, 5.0]", prefix)
		}
	}
	// P1: Regime gate validation. Mode "exit_only" (or empty → defaulted) is
	// the only value accepted; "directional" is reserved for future work.
	if mm.RegimeGateMode != "" && mm.RegimeGateMode != "exit_only" {
		return fmt.Errorf("%s: market_making.regime_gate_mode must be \"exit_only\" (got %q)", prefix, mm.RegimeGateMode)
	}
	if mm.RegimeGateEnabled {
		if mm.RegimeAutocorrThreshold <= 0 || mm.RegimeAutocorrThreshold >= 1.0 {
			return fmt.Errorf("%s: market_making.regime_autocorr_threshold must be in (0, 1)", prefix)
		}
		if mm.RegimeVWAPDivergenceSigma <= 0 {
			return fmt.Errorf("%s: market_making.regime_vwap_divergence_sigma must be > 0", prefix)
		}
		if mm.RegimeGateMinDurationSec < 0 {
			return fmt.Errorf("%s: market_making.regime_gate_min_duration_sec must be >= 0", prefix)
		}
		if mm.RegimeGateMaxSuppression < 0 || mm.RegimeGateMaxSuppression > 1.0 {
			return fmt.Errorf("%s: market_making.regime_gate_max_suppression must be in [0, 1]", prefix)
		}
	}
	// Flow Gate validation.
	if mm.FlowGateEnabled {
		if mm.FlowGateThreshold <= 0 || mm.FlowGateThreshold >= 1.0 {
			return fmt.Errorf("%s: market_making.flow_gate_threshold must be in (0, 1.0)", prefix)
		}
		if mm.FlowGateMaxSuppress <= 0 || mm.FlowGateMaxSuppress > 1.0 {
			return fmt.Errorf("%s: market_making.flow_gate_max_suppress must be in (0, 1.0]", prefix)
		}
	}
	// P9: Idle-fill circuit breaker validation.
	if mm.IdleFillDetectorEnabled {
		if mm.IdleFillThresholdSec <= 0 {
			return fmt.Errorf("%s: market_making.idle_fill_threshold_sec must be > 0 when idle_fill_detector_enabled", prefix)
		}
		if mm.IdleFillMinInvUtil <= 0 || mm.IdleFillMinInvUtil > 1.0 {
			return fmt.Errorf("%s: market_making.idle_fill_min_inv_util must be in (0, 1.0]", prefix)
		}
		if mm.IdleExitTightenBps < 0 {
			return fmt.Errorf("%s: market_making.idle_exit_tighten_bps must be >= 0", prefix)
		}
		if mm.IdleExitSizeMultiplier < 1.0 {
			return fmt.Errorf("%s: market_making.idle_exit_size_multiplier must be >= 1.0", prefix)
		}
		if mm.IdleFillRetryIntervalSec <= 0 {
			return fmt.Errorf("%s: market_making.idle_fill_retry_interval_sec must be > 0 when idle_fill_detector_enabled", prefix)
		}
	}
	// Adaptive validation.
	if mm.AdaptiveNotionalHalfLifeSec < 1 {
		return fmt.Errorf("%s: market_making.adaptive_notional_half_life_sec must be >= 1", prefix)
	}
	if mm.AdaptiveNotionalEnabled {
		if mm.AdaptiveNotionalTargetPerMin <= 0 {
			return fmt.Errorf("%s: market_making.adaptive_notional_target_per_min must be positive when adaptive_notional_enabled", prefix)
		}
		if mm.AdaptiveNotionalMin <= 0 {
			return fmt.Errorf("%s: market_making.adaptive_notional_min must be positive when adaptive_notional_enabled", prefix)
		}
		if mm.AdaptiveNotionalMax < mm.AdaptiveNotionalMin {
			return fmt.Errorf("%s: market_making.adaptive_notional_max must be >= adaptive_notional_min", prefix)
		}
	}
	if mm.RegimeWindow < 10 {
		return fmt.Errorf("%s: market_making.regime_window must be >= 10", prefix)
	}
	if mm.ZCalibrationWindow < 10 {
		return fmt.Errorf("%s: market_making.z_calibration_window must be >= 10", prefix)
	}
	if mm.FillRefreshCooldownMs < 0 {
		return fmt.Errorf("%s: market_making.fill_refresh_cooldown_ms must be >= 0", prefix)
	}
	if mm.ToxicityHalfLifeSec < 1 {
		return fmt.Errorf("%s: market_making.toxicity_half_life_sec must be >= 1", prefix)
	}
	if mm.ToxicityMaxWiden < 1.0 {
		return fmt.Errorf("%s: market_making.toxicity_max_widen must be >= 1.0", prefix)
	}
	if mm.ToxicityBaselineBps < 0 {
		return fmt.Errorf("%s: market_making.toxicity_baseline_bps must be >= 0", prefix)
	}
	// Fill burst validation.
	if mm.FillBurstSpreadMult < 1.0 {
		return fmt.Errorf("%s: market_making.fill_burst_spread_mult must be >= 1.0 (widening, not narrowing), got %g", prefix, mm.FillBurstSpreadMult)
	}
	if mm.FillBurstSizeFactor <= 0 || mm.FillBurstSizeFactor > 1.0 {
		return fmt.Errorf("%s: market_making.fill_burst_size_factor must be in (0, 1.0] (reducing, not increasing), got %g", prefix, mm.FillBurstSizeFactor)
	}
	if mm.FillBurstWindowSec > 0 {
		if mm.FillBurstDecaySec <= 0 {
			return fmt.Errorf("%s: market_making.fill_burst_decay_sec must be > 0 when fill_burst_window_sec > 0, got %g", prefix, mm.FillBurstDecaySec)
		}
		if mm.FillBurstThreshold < 1 {
			return fmt.Errorf("%s: market_making.fill_burst_threshold must be >= 1 when fill_burst_window_sec > 0, got %d", prefix, mm.FillBurstThreshold)
		}
	}
	// Smart Layer validation.
	if mm.MarkoutFVScale < 0 {
		return fmt.Errorf("%s: market_making.markout_fv_scale must be >= 0", prefix)
	}
	if mm.MarkoutFVMaxShiftBps < 0 {
		return fmt.Errorf("%s: market_making.markout_fv_max_shift_bps must be >= 0", prefix)
	}
	if mm.MaxTotalSpreadMultiplier < 1.0 {
		return fmt.Errorf("%s: market_making.max_total_spread_multiplier must be >= 1.0 (caps spread at Nx base), got %g", prefix, mm.MaxTotalSpreadMultiplier)
	}
	if mm.MicropriceDecay <= 0 || mm.MicropriceDecay >= 1.0 {
		return fmt.Errorf("%s: market_making.microprice_decay must be in (0, 1.0), got %g", prefix, mm.MicropriceDecay)
	}
	if mm.EmergencyWidenAmountBps < 0 {
		return fmt.Errorf("%s: market_making.emergency_widen_amount_bps must be >= 0", prefix)
	}
	if mm.ToxicitySizeWeightCap < 1.0 {
		return fmt.Errorf("%s: market_making.toxicity_size_weight_cap must be >= 1.0, got %g", prefix, mm.ToxicitySizeWeightCap)
	}
	if mm.VelocityInitialSeed <= 0 {
		return fmt.Errorf("%s: market_making.velocity_initial_seed must be > 0", prefix)
	}
	if mm.WickTrendSuppressThreshold < 0 || mm.WickTrendSuppressThreshold > 1 {
		return fmt.Errorf("%s: market_making.wick_trend_suppress_threshold must be in [0, 1], got %g", prefix, mm.WickTrendSuppressThreshold)
	}
	if mm.PriceShockEnabled {
		if mm.PriceShockWindowMs <= 0 {
			return fmt.Errorf("%s: market_making.price_shock_window_ms must be > 0 when price_shock_enabled", prefix)
		}
		if mm.PriceShockThresholdBps <= 0 {
			return fmt.Errorf("%s: market_making.price_shock_threshold_bps must be > 0 when price_shock_enabled", prefix)
		}
		if mm.PriceShockCooldownMs <= 0 {
			return fmt.Errorf("%s: market_making.price_shock_cooldown_ms must be > 0 when price_shock_enabled", prefix)
		}
		if mm.PriceShockExitSizeFactor < 0 || mm.PriceShockExitSizeFactor > 1 {
			return fmt.Errorf("%s: market_making.price_shock_exit_size_factor must be in [0, 1] when price_shock_enabled, got %g (1.0 = disabled, <1 = reduce exit-side size)", prefix, mm.PriceShockExitSizeFactor)
		}
		if mm.PriceShockFavorableExitFactor < 0 || mm.PriceShockFavorableExitFactor > 5 {
			return fmt.Errorf("%s: market_making.price_shock_favorable_exit_factor must be in [0, 5] when price_shock_enabled, got %g (1.0 = disabled, >1 = boost profit-taking side on favorable shock)", prefix, mm.PriceShockFavorableExitFactor)
		}
		if mm.PriceShockFavorableExitTightenBps < 0 || mm.PriceShockFavorableExitTightenBps > 50 {
			return fmt.Errorf("%s: market_making.price_shock_favorable_exit_tighten_bps must be in [0, 50] when price_shock_enabled, got %g (0 = disabled, >0 = pull profit-taking side closer to fair value by that many bps)", prefix, mm.PriceShockFavorableExitTightenBps)
		}
		if mm.PriceShockFavorableIocFrac < 0 || mm.PriceShockFavorableIocFrac > 1 {
			return fmt.Errorf("%s: market_making.price_shock_favorable_ioc_frac must be in [0, 1] when price_shock_enabled, got %g (0 = disabled, 0.25 = close 25%% of position on favorable shock via IOC)", prefix, mm.PriceShockFavorableIocFrac)
		}
		if mm.PriceShockFavorableIocFrac > 0 {
			if mm.PriceShockFavorableIocMinInvUtil < 0 || mm.PriceShockFavorableIocMinInvUtil > 1 {
				return fmt.Errorf("%s: market_making.price_shock_favorable_ioc_min_inv_util must be in [0, 1] when favorable_ioc_frac > 0, got %g", prefix, mm.PriceShockFavorableIocMinInvUtil)
			}
			if mm.PriceShockFavorableIocMinCaptureBps < 0 {
				return fmt.Errorf("%s: market_making.price_shock_favorable_ioc_min_capture_bps must be >= 0 when favorable_ioc_frac > 0, got %g", prefix, mm.PriceShockFavorableIocMinCaptureBps)
			}
			if mm.PriceShockFavorableIocSlippageBps < 0 || mm.PriceShockFavorableIocSlippageBps > 100 {
				return fmt.Errorf("%s: market_making.price_shock_favorable_ioc_slippage_bps must be in [0, 100] when favorable_ioc_frac > 0, got %g", prefix, mm.PriceShockFavorableIocSlippageBps)
			}
		}
	}
	if mm.ShockChainEnabled {
		if mm.ShockChainWindowSec <= 0 {
			return fmt.Errorf("%s: market_making.shock_chain_window_sec must be > 0 when shock_chain_enabled", prefix)
		}
		if mm.ShockChainThreshold <= 1 {
			return fmt.Errorf("%s: market_making.shock_chain_threshold must be >= 2 when shock_chain_enabled (threshold of 1 would fire on every shock — set shock_chain_enabled=false to disable cascade detection)", prefix)
		}
		if mm.ShockChainPauseSec < 60 {
			return fmt.Errorf("%s: market_making.shock_chain_pause_sec must be >= 60 when shock_chain_enabled, got %d", prefix, mm.ShockChainPauseSec)
		}
		if mm.ShockChainRecoverySec < 0 {
			return fmt.Errorf("%s: market_making.shock_chain_recovery_sec must be >= 0 when shock_chain_enabled, got %d (0 = no recovery phase after pause, resume directly to normal)", prefix, mm.ShockChainRecoverySec)
		}
	}
	if mm.WallQualityEnabled {
		if mm.WallQualityHalfLifeSec <= 0 {
			return fmt.Errorf("%s: market_making.wall_quality_half_life_sec must be > 0 when wall_quality_enabled", prefix)
		}
		if mm.WallQualityMinScore < 0 || mm.WallQualityMinScore > 1 {
			return fmt.Errorf("%s: market_making.wall_quality_min_score must be in [0, 1], got %g", prefix, mm.WallQualityMinScore)
		}
		if mm.WallQualityDisableSec <= 0 {
			return fmt.Errorf("%s: market_making.wall_quality_disable_sec must be > 0 when wall_quality_enabled", prefix)
		}
		if mm.WallQualityHysteresisSec < 0 {
			return fmt.Errorf("%s: market_making.wall_quality_hysteresis_sec must be >= 0, got %d", prefix, mm.WallQualityHysteresisSec)
		}
		if mm.WallQualityReenableThresholdBps < 0 {
			return fmt.Errorf("%s: market_making.wall_quality_reenable_threshold_bps must be >= 0, got %g", prefix, mm.WallQualityReenableThresholdBps)
		}
		if mm.WallQualityReenableThresholdBps > 0 && mm.WallQualityMinScore > 0 {
			// threshold is bps; convert to score space (0.5 + bps/10) for the
			// ordering check against min_score (also score space).
			reenableScore := 0.5 + mm.WallQualityReenableThresholdBps/10.0
			if reenableScore < mm.WallQualityMinScore {
				return fmt.Errorf("%s: market_making.wall_quality_reenable_threshold_bps (%g -> score %.2f) must map to a score >= wall_quality_min_score (%g)", prefix, mm.WallQualityReenableThresholdBps, reenableScore, mm.WallQualityMinScore)
			}
		}
	}
	if mm.QuoteAttributionMinIntervalSec < 0 {
		return fmt.Errorf("%s: market_making.quote_attribution_min_interval_sec must be >= 0", prefix)
	}
	// Rate limit allocator validation.
	if mm.RateLimitAllocatorEnabled {
		if mm.RateLimitMinRefreshMultiplier <= 0 {
			return fmt.Errorf("%s: market_making.rate_limit_min_refresh_multiplier must be > 0 when allocator enabled", prefix)
		}
		if mm.RateLimitMaxRefreshMultiplier < mm.RateLimitMinRefreshMultiplier {
			return fmt.Errorf("%s: market_making.rate_limit_max_refresh_multiplier must be >= min (%g), got %g",
				prefix, mm.RateLimitMinRefreshMultiplier, mm.RateLimitMaxRefreshMultiplier)
		}
	}
	// Markout composite weights: must be non-negative.
	for i, w := range mm.MarkoutCompositeWeights {
		if w < 0 {
			return fmt.Errorf("%s: market_making.markout_composite_weights[%d] must be >= 0, got %g", prefix, i, w)
		}
	}
	// P6: per-side win rate / profit factor.
	if mm.PerSideWinrateEnabled {
		if mm.PerSideWinrateWindowFills < 10 || mm.PerSideWinrateWindowFills > 500 {
			return fmt.Errorf("%s: market_making.per_side_winrate_window_fills must be in [10, 500], got %d", prefix, mm.PerSideWinrateWindowFills)
		}
		if mm.PerSideWinrateThreshold <= 0 || mm.PerSideWinrateThreshold >= 1 {
			return fmt.Errorf("%s: market_making.per_side_winrate_threshold must be in (0, 1), got %g", prefix, mm.PerSideWinrateThreshold)
		}
		if mm.PerSideProfitFactorThreshold <= 0 {
			return fmt.Errorf("%s: market_making.per_side_profit_factor_threshold must be > 0, got %g", prefix, mm.PerSideProfitFactorThreshold)
		}
		if mm.PerSideWinrateWidenBps < 0 {
			return fmt.Errorf("%s: market_making.per_side_winrate_widen_bps must be >= 0, got %g", prefix, mm.PerSideWinrateWidenBps)
		}
	}
	// Optimizer V2 validation.
	if mm.OptimizerV2.Enabled {
		v2 := &mm.OptimizerV2
		if v2.LearningRate <= 0 || v2.LearningRate > 1.0 {
			return fmt.Errorf("%s: optimizer_v2.learning_rate must be in (0, 1.0], got %g", prefix, v2.LearningRate)
		}
		if v2.Momentum < 0 || v2.Momentum >= 1.0 {
			return fmt.Errorf("%s: optimizer_v2.momentum must be in [0, 1.0), got %g", prefix, v2.Momentum)
		}
		if v2.EmergencyToxicityThreshold <= 0 || v2.EmergencyToxicityThreshold > 1.0 {
			return fmt.Errorf("%s: optimizer_v2.emergency_toxicity_threshold must be in (0, 1.0], got %g", prefix, v2.EmergencyToxicityThreshold)
		}
		if v2.RollbackRate <= 0 || v2.RollbackRate > 1.0 {
			return fmt.Errorf("%s: optimizer_v2.rollback_rate must be in (0, 1.0], got %g", prefix, v2.RollbackRate)
		}
		if v2.RollbackAbsoluteFloorBps > 0 {
			return fmt.Errorf("%s: optimizer_v2.rollback_absolute_floor_bps must be <= 0 (0 = disabled, negative = active floor), got %g", prefix, v2.RollbackAbsoluteFloorBps)
		}
		if v2.BurstEvalAllNegCooldownSec < 0 {
			return fmt.Errorf("%s: optimizer_v2.burst_eval_all_negative_cooldown_sec must be >= 0, got %d", prefix, v2.BurstEvalAllNegCooldownSec)
		}
		if v2.ForceRefreshMinIntervalMs < 0 {
			return fmt.Errorf("%s: optimizer_v2.force_refresh_min_interval_ms must be >= 0", prefix)
		}
		if v2.ForceRefreshMinSpreadDeltaBps < 0 {
			return fmt.Errorf("%s: optimizer_v2.force_refresh_min_spread_delta_bps must be >= 0", prefix)
		}
		if v2.SpreadBps.Max <= 0 {
			return fmt.Errorf("%s: optimizer_v2.spread_bps.max must be > 0", prefix)
		}
		// Dimension weight sums should be approximately 1.0.
		toxSum := v2.ToxicityWeights.MarkoutEWMA + v2.ToxicityWeights.FillBurst + v2.ToxicityWeights.EmergencyCount
		if toxSum > 0 && (toxSum < 0.95 || toxSum > 1.05) {
			return fmt.Errorf("%s: optimizer_v2.toxicity_weights must sum to ~1.0, got %g", prefix, toxSum)
		}
		oppSum := v2.OpportunityWeights.FillRateRatio + v2.OpportunityWeights.RegimeSignal + v2.OpportunityWeights.VWAPDeviation + v2.OpportunityWeights.SpreadBBORatio
		if oppSum > 0 && (oppSum < 0.95 || oppSum > 1.05) {
			return fmt.Errorf("%s: optimizer_v2.opportunity_weights must sum to ~1.0, got %g", prefix, oppSum)
		}
		invSum := v2.InventoryStressWeights.PositionUtil + v2.InventoryStressWeights.ControllerAdj + v2.InventoryStressWeights.SameSideDuration
		if invSum > 0 && (invSum < 0.95 || invSum > 1.05) {
			return fmt.Errorf("%s: optimizer_v2.inventory_stress_weights must sum to ~1.0, got %g", prefix, invSum)
		}
		if v2.SpreadDeltaDeadbandBps < 0 || v2.SpreadDeltaDeadbandBps > 1.0 {
			return fmt.Errorf("%s: optimizer_v2.spread_delta_deadband_bps must be in [0, 1.0], got %g", prefix, v2.SpreadDeltaDeadbandBps)
		}
		if v2.SkewDeltaDeadband < 0 || v2.SkewDeltaDeadband > 1.0 {
			return fmt.Errorf("%s: optimizer_v2.skew_delta_deadband must be in [0, 1.0], got %g", prefix, v2.SkewDeltaDeadband)
		}
		if v2.NotionalDeltaDeadband < 0 || v2.NotionalDeltaDeadband > 0.5 {
			return fmt.Errorf("%s: optimizer_v2.notional_delta_deadband must be in [0, 0.5], got %g", prefix, v2.NotionalDeltaDeadband)
		}
		switch v2.SubdimNormalization {
		case "", "linear", "empirical_quantile":
		default:
			return fmt.Errorf("%s: optimizer_v2.subdim_normalization must be one of {\"\", \"linear\", \"empirical_quantile\"}, got %q", prefix, v2.SubdimNormalization)
		}
		if v2.SubdimNormalization == "empirical_quantile" {
			if v2.SubdimQuantileWindowEvals != 0 && v2.SubdimQuantileWindowEvals < 100 {
				return fmt.Errorf("%s: optimizer_v2.subdim_quantile_window_evals must be >= 100, got %d", prefix, v2.SubdimQuantileWindowEvals)
			}
			if v2.SubdimQuantileLowPct != 0 && (v2.SubdimQuantileLowPct < 1 || v2.SubdimQuantileLowPct > 25) {
				return fmt.Errorf("%s: optimizer_v2.subdim_quantile_low_pct must be in [1, 25], got %d", prefix, v2.SubdimQuantileLowPct)
			}
			if v2.SubdimQuantileHighPct != 0 && (v2.SubdimQuantileHighPct < 75 || v2.SubdimQuantileHighPct > 99) {
				return fmt.Errorf("%s: optimizer_v2.subdim_quantile_high_pct must be in [75, 99], got %d", prefix, v2.SubdimQuantileHighPct)
			}
			lo := v2.SubdimQuantileLowPct
			hi := v2.SubdimQuantileHighPct
			if lo == 0 {
				lo = 5
			}
			if hi == 0 {
				hi = 95
			}
			if hi <= lo {
				return fmt.Errorf("%s: optimizer_v2.subdim_quantile_high_pct (%d) must be > subdim_quantile_low_pct (%d)", prefix, hi, lo)
			}
		}
		if v2.PolicyThresholds.OppTightenInvUtilGate < 0 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.opp_tighten_inv_util_gate must be >= 0 (0 = disabled), got %g", prefix, v2.PolicyThresholds.OppTightenInvUtilGate)
		}
		if v2.PolicyThresholds.OppTightenSessionPnLFloorBps > 0 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.opp_tighten_session_pnl_floor_bps must be <= 0 (0 = disabled, negative = active floor), got %g", prefix, v2.PolicyThresholds.OppTightenSessionPnLFloorBps)
		}
		if v2.PolicyThresholds.SkewRelaxAdverseMarkoutBps > 0 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.skew_relax_adverse_markout_bps must be <= 0 (0 = disabled, negative = active threshold), got %g", prefix, v2.PolicyThresholds.SkewRelaxAdverseMarkoutBps)
		}
		if v2.PolicyThresholds.SkewRelaxInvUtilMin < 0 || v2.PolicyThresholds.SkewRelaxInvUtilMin > 1 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.skew_relax_inv_util_min must be in [0,1], got %g", prefix, v2.PolicyThresholds.SkewRelaxInvUtilMin)
		}
		if v2.PolicyThresholds.SkewRelaxFloor < 0 || v2.PolicyThresholds.SkewRelaxFloor > 1 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.skew_relax_floor must be in [0,1] (0 = disabled), got %g", prefix, v2.PolicyThresholds.SkewRelaxFloor)
		}
		if v2.PolicyThresholds.SkewRelaxDivisor < 0 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.skew_relax_divisor must be >= 0 (0 = default 2.0), got %g", prefix, v2.PolicyThresholds.SkewRelaxDivisor)
		}
		if v2.PolicyThresholds.RegimeWidenEntryMult < 0 || v2.PolicyThresholds.RegimeWidenEntryMult > 3.0 {
			return fmt.Errorf("%s: optimizer_v2.policy_thresholds.regime_widen_entry_mult must be in [0, 3.0] (0 = default 1.0), got %g", prefix, v2.PolicyThresholds.RegimeWidenEntryMult)
		}
		// P5: fourth-dimension validation.
		switch v2.FourthDimension {
		case "", "budget_pressure", "session_pnl":
		default:
			return fmt.Errorf("%s: optimizer_v2.fourth_dimension must be one of {\"\", \"budget_pressure\", \"session_pnl\"}, got %q", prefix, v2.FourthDimension)
		}
		if v2.FourthDimension == "session_pnl" {
			if len(v2.SessionPnLBucketEdgesBps) != 0 {
				if len(v2.SessionPnLBucketEdgesBps) != 4 {
					return fmt.Errorf("%s: optimizer_v2.session_pnl_bucket_edges_bps must have exactly 4 entries, got %d", prefix, len(v2.SessionPnLBucketEdgesBps))
				}
				for i := 1; i < 4; i++ {
					if v2.SessionPnLBucketEdgesBps[i] <= v2.SessionPnLBucketEdgesBps[i-1] {
						return fmt.Errorf("%s: optimizer_v2.session_pnl_bucket_edges_bps must be strictly ascending, got %v", prefix, v2.SessionPnLBucketEdgesBps)
					}
				}
			}
			if v2.FourthDimDefensiveFloor != 0 && (v2.FourthDimDefensiveFloor <= 0 || v2.FourthDimDefensiveFloor > 1) {
				return fmt.Errorf("%s: optimizer_v2.fourth_dim_defensive_floor must be in (0, 1], got %g", prefix, v2.FourthDimDefensiveFloor)
			}
		}
	}
	return nil
}

// validateRisk validates Risk fields. asset is used in error messages.
func validateRisk(r *RiskConfig, asset string) error {
	prefix := "config"
	if asset != "global" {
		prefix = fmt.Sprintf("config[%s]", asset)
	}

	if r.MaxPositionNotional <= 0 {
		return fmt.Errorf("%s: risk.max_position_notional must be positive", prefix)
	}
	if r.MaxPositionHaltTolerancePct < 0 || r.MaxPositionHaltTolerancePct > 50 {
		return fmt.Errorf("%s: risk.max_position_halt_tolerance_pct must be in [0, 50], got %g", prefix, r.MaxPositionHaltTolerancePct)
	}
	if r.MaxSpreadExposure <= 0 {
		return fmt.Errorf("%s: risk.max_spread_exposure must be positive", prefix)
	}
	if r.MaxDrawdownPct <= 0 || r.MaxDrawdownPct > 100 {
		return fmt.Errorf("%s: risk.max_drawdown_pct must be in (0, 100]", prefix)
	}
	if r.MaxOpenOrders <= 0 {
		return fmt.Errorf("%s: risk.max_open_orders must be positive", prefix)
	}
	if r.DrawdownCooldownSec < 0 {
		return fmt.Errorf("%s: risk.drawdown_cooldown_sec must be >= 0", prefix)
	}
	if r.MaxConsecutiveHalts < 0 {
		return fmt.Errorf("%s: risk.max_consecutive_halts must be >= 0", prefix)
	}
	if r.MinResumeEquityPct < 0 || r.MinResumeEquityPct > 100 {
		return fmt.Errorf("%s: risk.min_resume_equity_pct must be in [0, 100]", prefix)
	}
	if r.FailClosedCooldownSec < 0 {
		return fmt.Errorf("%s: risk.fail_closed_cooldown_sec must be >= 0", prefix)
	}
	if r.MaxFailClosedRetries < 0 {
		return fmt.Errorf("%s: risk.max_fail_closed_retries must be >= 0", prefix)
	}
	if r.AbsoluteEquityFloor < 0 {
		return fmt.Errorf("%s: risk.absolute_equity_floor must be >= 0", prefix)
	}

	if r.MaxAggregateExposure < 0 {
		return fmt.Errorf("%s: risk.max_aggregate_exposure must be >= 0", prefix)
	}

	if r.SessionPnLWindowMinutes < 0 {
		return fmt.Errorf("%s: risk.session_pnl_window_minutes must be >= 0", prefix)
	}

	// P3: Session stop-loss.
	if r.SessionStopLossBps > 0 {
		return fmt.Errorf("%s: risk.session_stop_loss_bps must be <= 0 (0 = disabled, negative = active floor), got %g", prefix, r.SessionStopLossBps)
	}
	if r.SessionStopLossBps < 0 {
		if r.SessionStopLossPauseSec < 60 {
			return fmt.Errorf("%s: risk.session_stop_loss_pause_sec must be >= 60 when session_stop_loss_bps < 0, got %d", prefix, r.SessionStopLossPauseSec)
		}
		if r.SessionStopLossRecoveryNotionalPct <= 0 || r.SessionStopLossRecoveryNotionalPct > 1 {
			return fmt.Errorf("%s: risk.session_stop_loss_recovery_notional_pct must be in (0, 1] when session_stop_loss_bps < 0, got %g", prefix, r.SessionStopLossRecoveryNotionalPct)
		}
		if r.SessionStopLossRecoveryDurationSec < 60 {
			return fmt.Errorf("%s: risk.session_stop_loss_recovery_duration_sec must be >= 60 when session_stop_loss_bps < 0, got %d", prefix, r.SessionStopLossRecoveryDurationSec)
		}
	}

	// V24: session stop-loss early-warn level.
	if r.SessionStopLossWarnBps > 0 {
		return fmt.Errorf("%s: risk.session_stop_loss_warn_bps must be <= 0 (0 = disabled, negative = active warn), got %g", prefix, r.SessionStopLossWarnBps)
	}
	if r.SessionStopLossWarnBps < 0 {
		// Warn must be less negative than stop floor (fires earlier).
		if r.SessionStopLossBps < 0 && r.SessionStopLossWarnBps <= r.SessionStopLossBps {
			return fmt.Errorf("%s: risk.session_stop_loss_warn_bps (%g) must be greater than session_stop_loss_bps (%g) (warn fires before hard stop)", prefix, r.SessionStopLossWarnBps, r.SessionStopLossBps)
		}
		if r.SessionStopLossWarnSpreadMult < 1.0 {
			return fmt.Errorf("%s: risk.session_stop_loss_warn_spread_mult must be >= 1.0 when warn_bps < 0, got %g", prefix, r.SessionStopLossWarnSpreadMult)
		}
		if r.SessionStopLossWarnNotionalPct < 0 || r.SessionStopLossWarnNotionalPct > 1 {
			return fmt.Errorf("%s: risk.session_stop_loss_warn_notional_pct must be in [0, 1] (0 or 1 = no size cut; fractional = active cut), got %g", prefix, r.SessionStopLossWarnNotionalPct)
		}
	}

	// V30 part 6: session flatten on shutdown.
	if r.SessionFlattenTimeoutSec < 0 {
		return fmt.Errorf("%s: risk.session_flatten_timeout_sec must be >= 0 (0 = default 15s), got %d", prefix, r.SessionFlattenTimeoutSec)
	}

	// Cross-field validations.
	if r.MaxSpreadExposure < r.MaxPositionNotional {
		return fmt.Errorf("%s: max_spread_exposure(%g) must be >= max_position_notional(%g)",
			prefix, r.MaxSpreadExposure, r.MaxPositionNotional)
	}

	return nil
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
