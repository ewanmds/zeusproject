package quoting

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sort"
	"strconv"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/execution"
	"hyperliquid-market-maker/internal/risk"
	"hyperliquid-market-maker/internal/tradeflow"
	"hyperliquid-market-maker/internal/types"
	"hyperliquid-market-maker/internal/vwap"
)

// applyFeeFloor widens bid/ask symmetrically around ref if the current
// half-spread is less than minHalfSpreadBps. This ensures quotes are always
// wide enough to cover per-fill fee costs. Centering on the reference (fair
// value) rather than the bid/ask midpoint keeps every fill at least the fee
// distance from FV even when the two sides use different width multipliers.
func applyFeeFloor(bid, ask, ref, minHalfSpreadBps float64) (float64, float64) {
	if ref <= 0 || minHalfSpreadBps <= 0 {
		return bid, ask
	}
	currentHalfSpread := (ask - bid) / 2.0
	minHalfSpread := ref * minHalfSpreadBps / 10000.0
	if currentHalfSpread >= minHalfSpread {
		return bid, ask
	}
	return ref - minHalfSpread, ref + minHalfSpread
}

// requiredHalfSpreadBps is the minimum half-spread needed on one side for a
// round trip to clear measured fees, adverse selection and a target net edge.
// A round trip pays one fee on each of its two fills and suffers one adverse
// move per fill, so fee + adverse + target/2 is the per-side requirement.
func requiredHalfSpreadBps(feeBps, adverseBps, targetBps float64) float64 {
	if feeBps < 0 {
		feeBps = 0
	}
	if adverseBps < 0 {
		adverseBps = 0
	}
	if targetBps < 0 {
		targetBps = 0
	}
	return feeBps + adverseBps + targetBps/2.0
}

// applyEdgeFloor widens one side to at least requiredBps away from ref.
// It returns the (possibly widened) price and whether the requirement exceeds
// maxBps, in which case the side should not be quoted at all.
func applyEdgeFloor(price, ref float64, isBid bool, requiredBps, maxBps float64) (float64, bool) {
	if ref <= 0 || requiredBps <= 0 {
		return price, false
	}
	if maxBps > 0 && requiredBps > maxBps {
		return price, true
	}
	half := ref * requiredBps / 10000.0
	if isBid {
		floor := ref - half
		if price > floor {
			return floor, false
		}
		return price, false
	}
	ceiling := ref + half
	if price < ceiling {
		return ceiling, false
	}
	return price, false
}

// measuredFeeBpsFor returns the fee used by the edge gate: the measured fee
// once at least one fill has been seen, otherwise the configured fee. The
// measured value is floored so a single cheap fill cannot collapse the gate.
func (m *Manager) measuredFeeBpsFor(coin string, mm config.MarketMakingConfig) float64 {
	configured := mm.MakerFeeBps + mm.BuilderFeeBps
	m.feeBpsMu.RLock()
	measured, seeded := m.feeBps[coin], m.feeBpsSeeded[coin]
	m.feeBpsMu.RUnlock()
	if !seeded {
		return configured
	}
	if measured < mm.EdgeFeeFloorBps {
		measured = mm.EdgeFeeFloorBps
	}
	return measured
}

// adverseBpsFor returns the adverse-selection buffer for one side. It uses the
// per-side markout EWMA once warmup is complete and falls back to the config
// floor otherwise, so the gate is never blind at session start.
func (m *Manager) adverseBpsFor(coin, side string, floorBps float64) float64 {
	if floorBps < 0 {
		floorBps = 0
	}
	if st := m.toxicityTrackers[coin]; st != nil {
		if ewma, ready := st.MarkoutEWMA(side); ready {
			if adverse := -ewma; adverse > floorBps {
				return adverse
			}
		}
	}
	return floorBps
}

// applyInventorySkew shifts bid/ask to encourage position reduction.
// Long position: shift down. Short position: shift up.
// shiftBps = pow(notional / maxNotional, gamma) * skewFactor * spreadBps
// shift = refPrice * shiftBps / 10000
// gamma controls convexity: 1.0 = linear, >1.0 = gentle at low inventory, aggressive at high.
// asymmetry controls shift distribution: 0.5 = symmetric,
// >0.5 = more shift on the aggressive side (long→ask shifts more, short→bid shifts more).
func applyInventorySkew(bid, ask, refPrice, notional float64, side string, maxNotional, skewFactor, spreadBps, gamma, asymmetry float64, exitPreserve bool) (float64, float64) {
	if side == "" || notional == 0 || maxNotional == 0 {
		return bid, ask
	}

	ratio := notional / maxNotional
	if ratio > 1.0 {
		ratio = 1.0
	}
	shiftBps := math.Pow(ratio, gamma) * skewFactor * spreadBps
	shift := refPrice * shiftBps / 10000.0

	if exitPreserve {
		// Only shift the exposure-increasing side. Exit side stays at its
		// bias-adjusted price. Double the shift to maintain total skew magnitude.
		fullShift := shift * 2.0
		switch side {
		case "long":
			bid -= fullShift // discourage more buys
			// ask unchanged — let exit trade at bias-adjusted price
		case "short":
			ask += fullShift // discourage more sells
			// bid unchanged — let exit trade at bias-adjusted price
		}
		return bid, ask
	}

	if asymmetry == 0.5 {
		// Symmetric: identical to original behavior
		switch side {
		case "long":
			bid -= shift
			ask -= shift
		case "short":
			bid += shift
			ask += shift
		}
		return bid, ask
	}

	aggressiveShift := shift * asymmetry * 2.0
	passiveShift := shift * (1.0 - asymmetry) * 2.0

	switch side {
	case "long":
		// Long: ask is aggressive (shift down more), bid is passive
		bid -= passiveShift
		ask -= aggressiveShift
	case "short":
		// Short: bid is aggressive (shift up more), ask is passive
		bid += aggressiveShift
		ask += passiveShift
	}

	return bid, ask
}

// applyCrossingProtection ensures bid < ask after all pipeline adjustments.
// When position-aware: keeps the exit side (position-reducing) at its computed
// price and pulls the entry side to maintain minimum spread (minSpreadBps).
// When flat: falls back to midpoint clamping (legacy behavior).
// bestBid/bestAsk are book-level clamps (0 = no clamp).
func applyCrossingProtection(bid, ask, fairValue float64, posSide string, posNotional, bestBid, bestAsk, minSpreadBps float64) (float64, float64) {
	// Book-level clamping
	if bestAsk > 0 && bid >= bestAsk {
		bid = bestBid
	}
	if bestBid > 0 && ask <= bestBid {
		ask = bestAsk
	}

	if bid < ask {
		return bid, ask // no crossing
	}

	minSpread := fairValue * minSpreadBps / 10000.0 // full minimum spread
	minHalf := minSpread / 2.0                      // half of minimum spread (for symmetric clamp)

	// Position-aware: keep exit side, pull entry side
	if posNotional > 0 {
		switch posSide {
		case "long":
			// Exit is sell (ask). Keep ask, pull bid below.
			bid = ask - minSpread
			return bid, ask
		case "short":
			// Exit is buy (bid). Keep bid, pull ask above.
			ask = bid + minSpread
			return bid, ask
		}
	}

	// Flat or unknown: midpoint clamp (legacy behavior)
	clampMid := (bid + ask) / 2.0
	bid = clampMid - minHalf
	ask = clampMid + minHalf
	return bid, ask
}

// applyCrossLegSkew shifts quotes to discourage same-side accumulation across
// correlated legs. When both legs are long (or both short), the skew pushes
// quotes toward reducing the combined exposure. No effect when either leg is
// flat or when the factor is 0.
func applyCrossLegSkew(bid, ask, refPrice float64, thisPos, otherPos types.PositionUpdate, beta float64, isFirstAsset bool, maxNotional, crossSkewFactor, spreadBps float64) (float64, float64, float64) {
	if crossSkewFactor == 0 || maxNotional == 0 {
		return bid, ask, 0
	}
	if thisPos.Side == "" || otherPos.Side == "" {
		return bid, ask, 0
	}

	// Beta-normalized signed notionals.
	signedThis := thisPos.Notional
	if thisPos.Side == "short" {
		signedThis = -signedThis
	}
	signedOther := otherPos.Notional
	if otherPos.Side == "short" {
		signedOther = -signedOther
	}
	// Normalize second asset's notional by beta for comparable units.
	if !isFirstAsset {
		signedThis *= beta
	} else {
		signedOther *= beta
	}

	sameSideMetric := signedThis + signedOther
	ratio := math.Abs(sameSideMetric) / (2.0 * maxNotional)
	if ratio > 1.0 {
		ratio = 1.0
	}
	shiftBps := ratio * crossSkewFactor * spreadBps
	shift := refPrice * shiftBps / 10000.0

	// Net long → shift down (sell), net short → shift up (buy)
	if sameSideMetric > 0 {
		bid -= shift
		ask -= shift
	} else {
		bid += shift
		ask += shift
	}
	// Return signed bps for diagnostics
	diagBps := shiftBps
	if sameSideMetric > 0 {
		diagBps = -diagBps
	}
	return bid, ask, diagBps
}

// applySpreadBias adjusts quotes using a tanh-based spread bias.
// Positive z-score means first asset is expensive relative to second.
// For the first asset: shift quotes down (encourage selling).
// For the second asset: shift quotes up (encourage buying).
//
// Two modes:
//   - Legacy (maxBiasFraction == 0): symmetric parallel shift using maxBiasBps.
//   - Asymmetric (maxBiasFraction > 0): spread-relative bias where convergence
//     side tightens more than divergence side widens.
func applySpreadBias(bid, ask, mid, zScore, biasScale, maxBiasBps float64, coin, firstAsset string, maxBiasFraction, asymmetry float64) (float64, float64) {
	if zScore == 0 || biasScale == 0 {
		return bid, ask
	}

	// Legacy mode: symmetric parallel shift.
	if maxBiasFraction == 0 {
		biasBps := math.Tanh(zScore/biasScale) * maxBiasBps
		shift := mid * biasBps / 10000.0
		if coin == firstAsset {
			bid -= shift
			ask -= shift
		} else {
			bid += shift
			ask += shift
		}
		return bid, ask
	}

	// Asymmetric spread-relative mode.
	rawBias := math.Tanh(zScore / biasScale)
	absBias := math.Abs(rawBias)
	halfSpread := (ask - bid) / 2.0
	biasMagnitude := absBias * maxBiasFraction * halfSpread

	convergeTighten := biasMagnitude * asymmetry * 2.0
	divergeWiden := biasMagnitude * (1.0 - asymmetry) * 2.0

	// Determine convergence side:
	// z > 0 + first asset → convergence = ask (sell CL when CL expensive)
	// z > 0 + second asset → convergence = bid (buy BRENT when BRENT cheap)
	// z < 0 reverses
	convergenceIsAsk := (zScore > 0 && coin == firstAsset) || (zScore < 0 && coin != firstAsset)

	if convergenceIsAsk {
		ask -= convergeTighten
		bid -= divergeWiden
	} else {
		bid += convergeTighten
		ask += divergeWiden
	}

	return bid, ask
}

// computeWickSizing returns per-side size multipliers and adjusted spacing
// based on how far mid is from the short and long VWAPs.
// A wick is detected when short VWAP deviates (price spiked recently) but long
// VWAP has NOT deviated beyond longDevThreshold (the spike is not a sustained trend).
// The side closer to short VWAP gets a larger size multiplier (lean into mean reversion).
// Multiplier scales linearly from 1.0 at threshold to maxMult at 3x threshold.
// Spacing is gradually reduced as the wick intensifies.
func computeWickSizing(mid, shortVWAP, longVWAP, devThreshold, longDevThreshold, maxMult, normalSpacingBps float64) (bidMult, askMult, spacingBps float64) {
	if shortVWAP == 0 || longVWAP == 0 {
		return 1.0, 1.0, normalSpacingBps
	}
	shortDev := math.Abs(mid-shortVWAP) / shortVWAP
	longDev := math.Abs(mid-longVWAP) / longVWAP
	shortFar := shortDev > devThreshold

	// Wick = short-term spike (shortFar) without long-term trend confirmation.
	// Uses a separate (wider) threshold for long VWAP since it naturally drifts more.
	if shortFar && longDev < longDevThreshold {
		// Scale linearly from 1.0 at threshold to maxMult at 3x threshold.
		scale := (shortDev - devThreshold) / (2.0 * devThreshold)
		if scale > 1.0 {
			scale = 1.0
		}
		if scale < 0 {
			scale = 0
		}
		mult := 1.0 + scale*(maxMult-1.0)

		// Gradual spacing reduction: full spacing at scale=0, half at scale=1.
		adjSpacing := normalSpacingBps * (1.0 - 0.5*scale)

		// Direction: reference short VWAP for wick direction.
		if mid < shortVWAP {
			return mult, 1.0, adjSpacing
		}
		return 1.0, mult, adjSpacing
	}
	return 1.0, 1.0, normalSpacingBps
}

// applyZWidening widens quotes when |z-score| exceeds a threshold.
// The widening is proportional to the excess z-score above the threshold.
// asymmetry controls how widening is distributed: 0.5 = symmetric,
// >0.5 = more widening on the adverse side (z>0+firstAsset → bid adverse).
func applyZWidening(bid, ask, mid, absZ, threshold, rate, maxMult, asymmetry float64, zPositive bool, isFirstAsset bool) (float64, float64) {
	if absZ <= threshold {
		return bid, ask
	}
	mult := 1.0 + (absZ-threshold)*rate
	if mult > maxMult {
		mult = maxMult
	}
	extra := mult - 1.0
	bidDist := mid - bid
	askDist := ask - mid

	// Determine which side is adverse:
	// z>0 + first asset → bid is adverse (first asset expensive, should sell)
	// z>0 + second asset → ask is adverse
	// z<0 reverses
	bidAdverse := (zPositive && isFirstAsset) || (!zPositive && !isFirstAsset)

	if asymmetry == 0.5 {
		// Symmetric: same as before
		return mid - bidDist*(1.0+extra), mid + askDist*(1.0+extra)
	}

	adverseExtra := extra * asymmetry * 2.0
	passiveExtra := extra * (1.0 - asymmetry) * 2.0

	if bidAdverse {
		return mid - bidDist*(1.0+adverseExtra), mid + askDist*(1.0+passiveExtra)
	}
	return mid - bidDist*(1.0+passiveExtra), mid + askDist*(1.0+adverseExtra)
}

// computeMultiLevelMicroprice computes a volume-weighted microprice using multiple
// book levels. Each level's size is weighted by geometric decay (decayFactor^i) to give
// deeper levels progressively less influence. Incorporates actual level prices
// (not just BBO), capturing directional pressure from deeper book structure.
// Degrades to BBO microprice when only 1 level is available.
// decayFactor controls the geometric weight per level (default 0.7 via config).
func computeMultiLevelMicroprice(bids, asks []types.BookLevel, mid float64, nLevels int, decayFactor float64) float64 {
	if len(bids) == 0 || len(asks) == 0 || mid == 0 {
		return mid
	}
	if nLevels < 1 {
		nLevels = 1
	}
	if decayFactor <= 0 {
		decayFactor = 0.7
	}

	bidLevels := nLevels
	if bidLevels > len(bids) {
		bidLevels = len(bids)
	}
	askLevels := nLevels
	if askLevels > len(asks) {
		askLevels = len(asks)
	}

	// Volume-weighted average mid: weight each level's price by its size × geometric decay.
	// Geometric decay prevents noisy outer levels from dominating on thin books.
	var bidPressure, bidWeight, askPressure, askWeight float64
	for i := 0; i < bidLevels; i++ {
		w := bids[i].Size * math.Pow(decayFactor, float64(i))
		bidPressure += w * bids[i].Price
		bidWeight += w
	}
	for i := 0; i < askLevels; i++ {
		w := asks[i].Size * math.Pow(decayFactor, float64(i))
		askPressure += w * asks[i].Price
		askWeight += w
	}

	totalWeight := bidWeight + askWeight
	if totalWeight <= 0 {
		return mid
	}

	// The microprice blends bid and ask weighted prices.
	// Heavy bids → FV shifts up (toward asks). Heavy asks → FV shifts down (toward bids).
	bidVWAP := bidPressure / bidWeight
	askVWAP := askPressure / askWeight
	return (bidVWAP*askWeight + askVWAP*bidWeight) / totalWeight
}

// Cancel timeout constants -- sized to accommodate concurrent dex cancels
// which require one SDK call per OID (each = 2 HTTP round-trips).
const (
	cancelTimeoutFailClosed   = 15 * time.Second // fail-closed halt: must succeed
	cancelTimeoutRiskAction   = 10 * time.Second // risk halt
	defaultDegradedTradeStale = 10 * time.Second
	defaultHaltTradeStale     = 30 * time.Second
)

// minOrderNotional is the Hyperliquid exchange minimum order value.
const minOrderNotional = 10.0

const (
	degradedSpreadMultiplier   = 1.5
	degradedNotionalMultiplier = 0.5
)

// wallFrontLogEntry rate-limits wall-front snap Info logs.
// Logs at Info when the wall price changes or every 30s; Debug otherwise.
type wallFrontLogEntry struct {
	price  float64
	lastAt time.Time
}

const wallFrontLogInterval = 30 * time.Second

// vwapAlphaFadeLogEntry rate-limits "vwap_alpha drift fade" Info logs.
// Emits when the effective alpha bucket (0.1 resolution) changes OR once per
// 60s per coin, whichever happens first.
type vwapAlphaFadeLogEntry struct {
	bucket int
	lastAt time.Time
}

const vwapAlphaFadeLogInterval = 60 * time.Second

type rateLimitSample struct {
	remaining int
	at        time.Time
}

// rateLimitTier represents the current rate limit circuit breaker state.
type rateLimitTier int

const (
	rlTierNormal   rateLimitTier = iota // TTE > caution threshold
	rlTierCaution                       // TTE between degraded and caution thresholds
	rlTierDegraded                      // TTE between critical and degraded thresholds
	rlTierCritical                      // TTE < critical threshold or remaining < floor
)

func (t rateLimitTier) String() string {
	return t.toRLTier().String()
}

// toRLTier converts the local rateLimitTier to the supervisor's exported RLTier.
// Both types use iota in the same order (Normal=0, Caution=1, Degraded=2, Critical=3).
func (t rateLimitTier) toRLTier() RLTier {
	return RLTier(t)
}

// computeSize returns the order size, rounded down to szDecimals.
// Ensures the order meets the exchange minimum notional ($10).
// Uses a 1% safety margin so orders placed below mid (bids) still clear the minimum.
// pow10Mult is the precomputed 10^szDecimals value.
func computeSize(notional, mid, pow10Mult float64) float64 {
	if mid == 0 {
		return 0
	}
	const safeMinNotional = minOrderNotional * 1.01
	effectiveNotional := math.Max(notional, safeMinNotional)
	size := effectiveNotional / mid
	mult := pow10Mult
	floored := math.Floor(size*mult) / mult
	// If flooring pushed notional below the safe minimum, round up instead.
	if floored*mid < safeMinNotional {
		floored = math.Ceil(size*mult) / mult
	}
	return floored
}

// refreshScratch holds pre-allocated maps reused across maybeRefreshQuotes calls
// to avoid per-tick allocations. Safe because the refreshingAll guard prevents
// concurrent access.
type refreshScratch struct {
	books         map[string]types.BookUpdate
	quoteStates   map[string]quoteState
	positions     map[string]types.PositionUpdate
	currentOrders map[string][]types.OrderRequest
	exposures     map[string]sideExposure
	desiredByCoin map[string][]types.OrderRequest
	refreshCoins  []string
	refreshReason map[string]string
	rlTier        RLTier               // snapshot of supervisor.RLTier() taken under lock
	lastFillTimes map[string]time.Time // snapshot of lastFillTime taken under lock
}

// Manager computes quotes and sends them to the executor.
type Manager struct {
	cfg        *config.Config
	bookCh     <-chan types.BookUpdate
	signalCh   <-chan types.SpreadSignal
	riskCh     <-chan types.RiskAction
	executor   execution.Executor
	szDecimals map[string]int
	shortVWAPs map[string]*vwap.Calculator
	longVWAPs  map[string]*vwap.Calculator

	imbalanceCalcs map[string]tradeflow.ImbalanceCalculator
	fillDecays     map[string]*tradeflow.FillDecay
	fillRates      map[string]*FillRateTracker
	regimes        map[string]*RegimeDetector
	markouts       map[string]*MarkoutTracker
	perSideStats   map[string]*PerSideStats // P6: per-side win rate / profit factor

	sessionPnL *SessionPnLTracker // shared session PnL tracker (P0); required (non-nil)

	// P3: session stop-loss. When true, the active OpRecovery window came from a
	// session-stop halt (not a drawdown halt). Used to pick the correct recovery
	// notional/spread multipliers. Cleared when recovery expires or a new halt arrives.
	sessionStopRecovery bool

	// Edge-triggered state for session stop-loss warn-zone transitions. True while
	// session PnL is inside the warn band (warn_bps >= pnl > stop_loss_bps). Used to
	// emit a single log on entry/exit rather than every refresh. Mutated only inside
	// the serial per-coin loop in maybeRefreshQuotes.
	sessionWarnActive bool

	fillTrigger       chan string          // buffered(1), signaled from RecordFill with filled coin
	lastFillTime      map[string]time.Time // per-coin last fill time for dynamic refresh
	lastFillRefresh   time.Time            // rate limit for fill-triggered refreshes
	fillPending       bool                 // sticky: fill arrived while refreshingAll
	fillPendingCoin   string               // which coin the pending fill was for
	fillTriggered     bool                 // bypass normal rate limiter on next maybeRefreshQuotes
	fillTriggeredCoin string               // scopes the current fill-triggered refresh to this coin

	// V20 (second bundle): burst-triggered optimizer eval. Fires an out-of-cadence
	// optimizer evaluation when a volume burst activates, throttled by min interval.
	burstEvalCh         chan struct{} // buffered(1), signaled from RecordMarketTrade on new burst
	lastOptEvalMu       sync.Mutex    // guards lastOptEval
	lastOptEval         time.Time     // set at start of each evaluateOptimizers() call
	forceRefreshReasons map[string]string
	lastForceRefresh    map[string]time.Time

	// Rate-limit circuit breaker.
	rateLimitRemaining   int
	rateLimitCap         int               // last-known capacity from API
	rateLimitHistory     []rateLimitSample // circular buffer of recent samples
	rateLimitHistoryIdx  int
	rateLimitHistoryFull bool
	rlTTE                float64 // seconds to exhaustion; +Inf when safe
	rlDepletion          float64 // requests/sec depletion rate; positive = draining

	// Long-window EWMA depletion tracker for slow secular drain detection.
	rlEWMADepletion      float64   // EWMA of depletion rate (requests/sec); positive = draining
	rlEWMALastAt         time.Time // timestamp of last EWMA update
	rlEWMASeeded         bool      // true after first update (need 2 points)
	rlEWMALastRem        int       // remaining at last EWMA update
	rlPctWarned          bool      // true once pct-warn has been logged (avoid log spam)
	rlBootRemaining      int       // remaining at first UpdateRateLimit call (for session accounting)
	rlBootCap            int       // cap at first UpdateRateLimit call (for volume-regen tracking)
	rlBootSeeded         bool      // true after first UpdateRateLimit (boot remaining captured)
	rlLastCapSeen        int       // cap at previous UpdateRateLimit; for diffing observed regen credits (V30 part 5)
	rlSessionExchangeReq int64     // cumulative exchange requests this session (from RPSTracker snapshots)
	toxicityTrackers     map[string]*SideToxicityTracker
	fillBurstTrackers    map[string]*FillBurstTracker
	velocityTrackers     map[string]*TradeVelocityTracker
	priceShockTrackers   map[string]*PriceShockTracker
	shockChainTrackers   map[string]*ShockChainTracker
	wallQualityTrackers  map[string]*WallQualityTracker
	inventoryControllers map[string]*InventoryController
	optimizersV2         map[string]*OptimizerV2
	virtCycle            *VirtCycleEngine
	rateLimitAllocator   *RateLimitAllocator
	fundingTracker       *FundingTracker
	trendGuardPrev       map[string]bool              // per-coin: was trend guard active last cycle (for hysteresis)
	regimeGatePrev       map[string]regimeGateState   // per-coin: last regime gate edge state (P1)
	adverseStreaks       map[string]int               // per "coin:side" consecutive adverse markout count for emergency widen
	inventoryHighSince   map[string]time.Time         // when inventory exceeded high_inventory_util
	optFillTimes         map[string][]time.Time       // fill timestamps for optimizer rolling window
	optBuyFillTimes      map[string][]time.Time       // buy-side fill timestamps for optimizer per-side gating
	optSellFillTimes     map[string][]time.Time       // sell-side fill timestamps for optimizer per-side gating
	idleBreakers         map[string]*idleBreakerState // P9: per-asset idle-fill circuit breaker state

	latestBook        map[string]types.BookUpdate
	lastBookUpdate    map[string]time.Time
	latestSignal      types.SpreadSignal
	micros            map[string]*Microstructure // per-asset; nil entries when dynamic_width=false
	supervisor        *Supervisor                // unified operational state machine
	failClosedAt      time.Time                  // timestamp of current fail-closed halt
	failClosedRetries int                        // consecutive recovery attempts
	refreshingAll     bool                       // inflight guard for quote replace cycle
	lastRefreshAll    time.Time                  // rate limit for quote refresh
	lastRefreshMid    map[string]float64         // mid prices at last refresh, for emergency bypass
	pendingOrders     map[string][]types.OrderRequest
	quoteStates       map[string]quoteState

	reconnectCh          <-chan struct{} // signals WS reconnect; suppresses quoting until fresh book data
	reconnectCooldownEnd time.Time       // quoting suppressed until this time after WS reconnect

	scratch      refreshScratch  // reusable scratch space for maybeRefreshQuotes
	pow10Cache   map[int]float64 // szDecimals → 10^szDecimals
	lastReasonZ  float64         // cached z-score for reason string
	cachedReason string          // cached "mm z=..." reason string

	warmupFn       func() (int, int) // returns (count, window) from spread engine; nil if no engine
	warmupDuration time.Duration     // time-based warmup fallback when spread engine is disabled
	startedAt      time.Time         // when Run() started (for time-based warmup)

	// P12: Time-of-day notional schedule. nil = disabled. Nil-safe — all helpers
	// treat nil as "no windows, multiplier always 1.0".
	schedule              *risk.TradingSchedule
	scheduleMu            sync.Mutex
	scheduleLastLabel     string // last observed label for transition logging ("" = inactive)
	scheduleLastMult      float64
	pairReduceActive      bool
	pairReduceSide        string
	pairReduceBelow       int
	pairReduceActivatedAt time.Time // when pair_reduce first activated (for timeout)

	wallFrontLog           map[string]*wallFrontLogEntry     // rate-limits wall-front snap Info logs; key: "coin:side"
	vwapAlphaFadeLog       map[string]*vwapAlphaFadeLogEntry // rate-limits vwap_alpha drift fade logs; key: coin
	vwapAlphaFadeLogMu     sync.Mutex
	lastQuoteAttributionAt map[string]time.Time
	lastGuardActive        map[string]bool                 // per-coin guard-state edge detector for quote attribution
	sidePresence           map[string]*SidePresenceTracker // per-coin counter of quote cycles where one side was absent (allowed &lt; min)

	diagSnaps map[string]DiagnosticSnapshot
	fillStats map[string]*FillWindowStats
	diagMu    sync.RWMutex

	// Measured fee per fill as bps of notional. Drives the edge gate so the
	// quoted spread covers the fee actually charged by the venue, not a static
	// guess. Seeded on the first fill, then EWMA-smoothed.
	feeBpsMu     sync.RWMutex
	feeBps       map[string]float64
	feeBpsSeeded map[string]bool

	// V32: per-coin counters for trend-bias mechanism firings. Incremented on each
	// quote build where the mechanism applied a non-zero adjustment. Snapshotted and
	// reset by the status-tick markout log to expose which V31 mechanisms are dormant.
	trendMechCounts   map[string]*TrendMechCounts
	trendMechCountsMu sync.Mutex

	// P13: post-mortem dumper (nil-safe; guarded by Enabled() at all call sites).
	postMortem     *PostMortemDumper
	prevSupState   OpState // last observed supervisor state for OpFlatten edge detection
	prevSupStateMu sync.Mutex
	lastDumpAt     time.Time // throttle to avoid duplicate dumps within a short window
	lastDumpReason string

	mu sync.Mutex
}

type quoteState struct {
	SentAt time.Time
}

// regimeGateState tracks per-asset P1 regime-gate edges and heartbeat cadence.
type regimeGateState struct {
	Active        bool
	Dir           int
	ActivatedAt   time.Time
	LastHeartbeat time.Time
}

// idleBreakerState tracks the per-asset idle-fill circuit breaker (P9).
// activeFlag is set on fire and cleared on the next fill or after the retry
// interval elapses. activeSide is the exit side ("buy" or "sell").
type idleBreakerState struct {
	lastFiredAt time.Time
	activeFlag  bool
	activeSide  string
}

// TrendMechCounts tallies how many times each V31 trend-bias mechanism
// applied a non-zero adjustment since the last snapshot. Used by the
// periodic markout log to show whether the mechanisms are firing or dormant.
type TrendMechCounts struct {
	TrendFVShift uint64 // trend_fv_drift_bps produced a non-zero FV shift
	AlphaBoost   uint64 // vwap_alpha_trend_boost applied a non-zero boost
	EntryAccel   uint64 // entry_reduction_trend_k accelerated entry reduction
}

// bumpTrendMech increments the named counter for a coin. Safe under mutex;
// caller need not hold any lock.
func (m *Manager) bumpTrendMech(coin string, which string) {
	m.trendMechCountsMu.Lock()
	defer m.trendMechCountsMu.Unlock()
	if m.trendMechCounts == nil {
		m.trendMechCounts = make(map[string]*TrendMechCounts)
	}
	c, ok := m.trendMechCounts[coin]
	if !ok {
		c = &TrendMechCounts{}
		m.trendMechCounts[coin] = c
	}
	switch which {
	case "fv_shift":
		c.TrendFVShift++
	case "alpha_boost":
		c.AlphaBoost++
	case "entry_accel":
		c.EntryAccel++
	}
}

// TrendMechCountsSnapshot returns and resets the per-coin counters.
// Call from the status tick to expose mechanism firing rates.
func (m *Manager) TrendMechCountsSnapshot(coin string) TrendMechCounts {
	m.trendMechCountsMu.Lock()
	defer m.trendMechCountsMu.Unlock()
	if m.trendMechCounts == nil {
		return TrendMechCounts{}
	}
	c, ok := m.trendMechCounts[coin]
	if !ok {
		return TrendMechCounts{}
	}
	snap := *c
	*c = TrendMechCounts{}
	return snap
}

// DiagnosticSnapshot holds pipeline intermediates for the STATUS dump.
type DiagnosticSnapshot struct {
	FairValue          float64
	EffectiveSpreadBps float64
	SkewShiftBps       float64 // signed: negative when long, positive when short
	CrossLegSkewBps    float64 // signed: cross-leg inventory skew shift
	BiasShiftBps       float64 // signed: negative = shift down (first asset), positive = shift up (second)
	NotionalMult       float64 // adaptive notional multiplier (1.0 = base)
	SkewFactor         float64 // effective skew factor after adaptive
	VWAPAlpha          float64 // effective VWAP alpha after adaptive
	Autocorrelation    float64 // regime autocorrelation, NaN if not ready
	WickBidMult        float64 // wick sizing bid multiplier (1.0 = no wick)
	WickAskMult        float64 // wick sizing ask multiplier (1.0 = no wick)
	ZWideningMult      float64 // z-score widening multiplier (1.0 = no widening)
	WarmupActive       bool    // true while spread engine is still warming up
	RecoveryActive     bool    // true while in post-drawdown recovery mode
	HealthDegraded     bool
	HealthHalted       bool
	HealthReason       string
	PairReduceOnly     bool
	Timestamp          time.Time

	// B2: Per-side toxicity
	ToxBuyMult  float64
	ToxSellMult float64
	ToxBuyEWMA  float64
	ToxSellEWMA float64

	// C: Runtime optimizer
	OptSpreadDelta        float64
	OptSpreadDeltaBuy     float64
	OptSpreadDeltaSell    float64
	OptNotionalMult       float64
	OptSkewMult           float64
	OptAlphaDelta         float64
	OptImbalanceShiftMult float64
	OptFillDecayMult      float64
	OptRefreshMult        float64
	OptEvalSeq            int

	// D: Fill burst
	BurstBuySpreadMult  float64
	BurstSellSpreadMult float64
	BurstBuySizeFactor  float64
	BurstSellSizeFactor float64

	// E: Volume burst ladders
	VolBurstRatio  float64 // fast/slow trade velocity ratio
	VolBurstLevels int     // effective ladder levels (after burst adjustment)
	VolBurstActive bool    // whether volume burst is active

	// Pipeline internals for post-session analysis
	BookMid             float64 // raw book mid price (before FV computation)
	BBOSpreadBps        float64 // raw book BBO spread in bps
	TradeImbalanceShift float64 // trade-flow imbalance FV shift in bps (signed)
	FillDecayShift      float64 // post-fill adverse decay FV shift in bps (signed)
	InventoryUtil       float64 // position notional / max_position_notional
	EntrySizeFactor     float64 // entry-side size reduction factor (1.0 = no reduction)
	AllowedBuyNotional  float64 // final buy-side notional after all multipliers + caps
	AllowedSellNotional float64 // final sell-side notional after all multipliers + caps
	TrendGuardActive    bool    // whether trend guard is currently suppressing entry
	TrendGuardSuppress  float64 // trend guard suppression factor applied (0 = none)
	TrendGuardDevPct    float64 // mid deviation from long VWAP in %
	FlowGateActive      bool    // whether flow gate is currently suppressing entry
	FlowGateFactor      float64 // flow gate suppression factor applied (1.0 = none)
	PriceShockActive    bool
	PriceShockSide      string
	PriceShockMoveBps   float64
	WickBidSuppressed   bool
	WickAskSuppressed   bool
	ScheduleMult        float64 // time-of-day notional schedule multiplier (1.0 = inactive)
	BidAbsentPct        float64 // % of quote cycles since last STATUS where buy side produced 0 orders
	AskAbsentPct        float64 // % of quote cycles since last STATUS where sell side produced 0 orders
	BothAbsentPct       float64 // % of quote cycles since last STATUS where both sides were absent
	PresenceCycles      int     // number of quote cycles observed in the window (0 = no data)

	// Edge gate (measured cost vs quoted half-spread, bps)
	EdgeRequiredBidBps float64
	EdgeRequiredAskBps float64
	QuoteHalfBidBps    float64
	QuoteHalfAskBps    float64
	EdgeSkipBid        bool
	EdgeSkipAsk        bool
	MeasuredFeeBps     float64
	MarkoutBuyBps      float64
	MarkoutSellBps     float64
}

// FillWindowStats accumulates fill statistics between STATUS dumps.
type FillWindowStats struct {
	BuyCount            int
	SellCount           int
	BuyNotional         float64
	SellNotional        float64
	TotalFees           float64
	SpreadCaptureBpsSum float64 // sum of |fill_price - fair_value| / fair_value * 10000
	SpreadCaptureCount  int     // fills with valid spread capture
}

type sideExposure struct {
	Long  float64
	Short float64
}

type coinQuotingHealth struct {
	Degraded bool
	Halted   bool
	Reason   string
}

type pairReduceState struct {
	Active   bool
	ExitSide string
}

// New creates a new quote Manager.
func New(
	cfg *config.Config,
	bookCh <-chan types.BookUpdate,
	signalCh <-chan types.SpreadSignal,
	riskCh <-chan types.RiskAction,
	executor execution.Executor,
	szDecimals map[string]int,
	shortVWAPs map[string]*vwap.Calculator,
	longVWAPs map[string]*vwap.Calculator,
	imbalanceCalcs map[string]tradeflow.ImbalanceCalculator,
	fillDecays map[string]*tradeflow.FillDecay,
	sessionPnL *SessionPnLTracker,
) *Manager {
	if sessionPnL == nil {
		panic("quoting.New: sessionPnL must be non-nil")
	}
	numAssets := len(cfg.Strategy.Assets)
	m := &Manager{
		cfg:            cfg,
		bookCh:         bookCh,
		signalCh:       signalCh,
		riskCh:         riskCh,
		executor:       executor,
		szDecimals:     szDecimals,
		shortVWAPs:     shortVWAPs,
		longVWAPs:      longVWAPs,
		imbalanceCalcs: imbalanceCalcs,
		fillDecays:     fillDecays,
		sessionPnL:     sessionPnL,
		latestBook:     make(map[string]types.BookUpdate),
		lastBookUpdate: make(map[string]time.Time),
		lastRefreshMid: make(map[string]float64, numAssets),
		pendingOrders:  make(map[string][]types.OrderRequest),
		quoteStates:    make(map[string]quoteState),
		scratch: refreshScratch{
			books:         make(map[string]types.BookUpdate, numAssets),
			quoteStates:   make(map[string]quoteState, numAssets),
			positions:     make(map[string]types.PositionUpdate, numAssets),
			currentOrders: make(map[string][]types.OrderRequest, numAssets),
			exposures:     make(map[string]sideExposure, numAssets),
			desiredByCoin: make(map[string][]types.OrderRequest, numAssets),
			refreshCoins:  make([]string, 0, numAssets),
			refreshReason: make(map[string]string, numAssets),
			lastFillTimes: make(map[string]time.Time, numAssets),
		},
	}
	m.wallFrontLog = make(map[string]*wallFrontLogEntry)
	m.vwapAlphaFadeLog = make(map[string]*vwapAlphaFadeLogEntry, numAssets)
	m.lastQuoteAttributionAt = make(map[string]time.Time, numAssets)
	m.lastGuardActive = make(map[string]bool, numAssets)
	m.sidePresence = make(map[string]*SidePresenceTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		m.sidePresence[asset] = &SidePresenceTracker{}
	}
	m.supervisor = NewSupervisor()
	m.diagSnaps = make(map[string]DiagnosticSnapshot, numAssets)
	m.fillStats = make(map[string]*FillWindowStats, numAssets)
	m.feeBps = make(map[string]float64, numAssets)
	m.feeBpsSeeded = make(map[string]bool, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		m.fillStats[asset] = &FillWindowStats{}
	}
	m.pow10Cache = make(map[int]float64, len(szDecimals))
	for _, dec := range szDecimals {
		m.pow10Cache[dec] = math.Pow(10, float64(dec))
	}
	m.micros = make(map[string]*Microstructure, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.DynamicWidth || ac.MarketMaking.LadderSpacingDynamic {
			m.micros[asset] = NewMicrostructure(
				ac.MarketMaking.WidthVolWindow,
				ac.MarketMaking.WidthMaxMultiplier,
				ac.MarketMaking.WidthImbalanceLevels,
				ac.MarketMaking.QuoteSpreadBps,
			)
		}
	}
	// Fill-rate trackers (Feature 2).
	// Always created for all assets so the optimizer has fill rate data for FillRateRatio,
	// even when adaptive notional sizing is disabled. The tracker computes $/min EWMA
	// regardless of whether the notional multiplier is applied to quote sizing.
	m.fillRates = make(map[string]*FillRateTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		m.fillRates[asset] = NewFillRateTracker(
			time.Duration(ac.MarketMaking.AdaptiveNotionalHalfLifeSec)*time.Second,
			ac.MarketMaking.AdaptiveNotionalTargetPerMin,
			ac.MarketMaking.QuoteNotional,
			ac.MarketMaking.AdaptiveNotionalMin,
			ac.MarketMaking.AdaptiveNotionalMax,
		)
	}
	m.lastFillTime = make(map[string]time.Time, numAssets)
	m.idleBreakers = make(map[string]*idleBreakerState, numAssets)
	// Regime detectors (also feeds optimizer autocorrelation input).
	// Always created for all assets so the optimizer and diagnostics have valid
	// autocorrelation data even when adaptive skew/alpha features are disabled.
	m.regimes = make(map[string]*RegimeDetector, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.RegimeWindow > 0 {
			m.regimes[asset] = NewRegimeDetector(ac.MarketMaking.RegimeWindow)
		}
	}
	m.fillTrigger = make(chan string, 1)
	m.burstEvalCh = make(chan struct{}, 1)
	m.forceRefreshReasons = make(map[string]string, numAssets)
	m.lastForceRefresh = make(map[string]time.Time, numAssets)
	// Per-side toxicity trackers (B2).
	m.toxicityTrackers = make(map[string]*SideToxicityTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.ToxicityPerSide {
			baselineBps := ac.MarketMaking.ToxicityBaselineBps
			if baselineBps == 0 {
				baselineBps = ac.MarketMaking.QuoteSpreadBps / 2.0
			}
			m.toxicityTrackers[asset] = NewSideToxicityTracker(
				time.Duration(ac.MarketMaking.ToxicityHalfLifeSec)*time.Second,
				ac.MarketMaking.ToxicityMaxWiden,
				baselineBps,
				ac.MarketMaking.ToxicityIdleDecayHalfLives,
				ac.MarketMaking.ToxicityWarmupFills,
				ac.MarketMaking.ToxicitySizeExponent,
			)
		}
	}
	// Fill burst circuit breaker (D).
	m.fillBurstTrackers = make(map[string]*FillBurstTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.FillBurstWindowSec > 0 {
			m.fillBurstTrackers[asset] = NewFillBurstTracker(
				ac.MarketMaking.FillBurstWindowSec,
				ac.MarketMaking.FillBurstThreshold,
				ac.MarketMaking.FillBurstSpreadMult,
				ac.MarketMaking.FillBurstSizeFactor,
				ac.MarketMaking.FillBurstDecaySec,
			)
		}
	}
	// Inventory half-life controller (B3).
	m.inventoryControllers = make(map[string]*InventoryController, numAssets)
	// Volume burst ladders: trade velocity trackers (per-asset).
	m.velocityTrackers = make(map[string]*TradeVelocityTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.VolumeBurstEnabled {
			m.velocityTrackers[asset] = NewTradeVelocityTracker(
				ac.MarketMaking.BurstVelocityFastSec,
				ac.MarketMaking.BurstVelocitySlowSec,
				ac.MarketMaking.BurstVelocityThreshold,
				ac.MarketMaking.BurstVelocityMinFloor,
				ac.MarketMaking.BurstLevelMultiplier,
				ac.MarketMaking.BurstDecaySec,
			)
		}
	}
	m.priceShockTrackers = make(map[string]*PriceShockTracker, numAssets)
	m.shockChainTrackers = make(map[string]*ShockChainTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.PriceShockEnabled {
			m.priceShockTrackers[asset] = NewPriceShockTracker(
				time.Duration(ac.MarketMaking.PriceShockWindowMs)*time.Millisecond,
				time.Duration(ac.MarketMaking.PriceShockCooldownMs)*time.Millisecond,
				ac.MarketMaking.PriceShockThresholdBps,
			)
		}
		if ac.MarketMaking.ShockChainEnabled {
			m.shockChainTrackers[asset] = NewShockChainTracker(
				time.Duration(ac.MarketMaking.ShockChainWindowSec)*time.Second,
				ac.MarketMaking.ShockChainThreshold,
			)
		}
	}
	m.wallQualityTrackers = make(map[string]*WallQualityTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.WallQualityEnabled {
			m.wallQualityTrackers[asset] = NewWallQualityTracker(
				time.Duration(ac.MarketMaking.WallQualityHalfLifeSec)*time.Second,
				ac.MarketMaking.WallQualityMinScore,
				time.Duration(ac.MarketMaking.WallQualityDisableSec)*time.Second,
				time.Duration(ac.MarketMaking.WallQualityHysteresisSec)*time.Second,
				ac.MarketMaking.WallQualityReenableThresholdBps,
			)
		}
	}
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.InventoryHalfLifeMinutes > 0 {
			m.inventoryControllers[asset] = NewInventoryController(
				ac.MarketMaking.InventoryHalfLifeMinutes,
				ac.MarketMaking.InventorySkewFactor,
				ac.MarketMaking.InventoryControllerSkewMin,
				ac.MarketMaking.InventoryControllerSkewMax,
				ac.MarketMaking.InventoryControllerGainP,
				ac.MarketMaking.InventoryControllerGainI,
				ac.MarketMaking.InventoryControllerGainD,
			)
		}
	}
	// Runtime optimizer state.
	m.inventoryHighSince = make(map[string]time.Time, numAssets)
	m.trendGuardPrev = make(map[string]bool, numAssets)
	m.regimeGatePrev = make(map[string]regimeGateState, numAssets)
	m.adverseStreaks = make(map[string]int, numAssets*2)
	m.optFillTimes = make(map[string][]time.Time, numAssets)
	m.optBuyFillTimes = make(map[string][]time.Time, numAssets)
	m.optSellFillTimes = make(map[string][]time.Time, numAssets)
	// Optimizer V2 — unified utility-driven controller.
	m.optimizersV2 = make(map[string]*OptimizerV2, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.OptimizerV2.Enabled {
			optBase := math.Max(ac.MarketMaking.QuoteSpreadBps,
				math.Max(ac.MarketMaking.QuoteSpreadBpsBuy, ac.MarketMaking.QuoteSpreadBpsSell))
			m.optimizersV2[asset] = NewOptimizerV2(
				ac.MarketMaking.OptimizerV2, asset, optBase, ac.MarketMaking.VWAPAlpha)
		}
	}
	// Virtuous cycle engine — tracks volume regen vs API burn.
	m.virtCycle = NewVirtCycleEngine(5 * time.Minute)
	// Rate limit allocator — per-asset ROI-based refresh allocation.
	if !cfg.RateLimit.Disabled && cfg.MarketMaking.RateLimitAllocatorEnabled && len(cfg.Strategy.Assets) > 1 {
		m.rateLimitAllocator = NewRateLimitAllocator(
			cfg.Strategy.Assets,
			time.Duration(cfg.MarketMaking.RateLimitROIEWMAHalfLifeSec)*time.Second,
			cfg.MarketMaking.RateLimitMinRefreshMultiplier,
			cfg.MarketMaking.RateLimitMaxRefreshMultiplier)
	}
	// Funding rate session-end urgency tracker.
	if cfg.MarketMaking.FundingUrgencyEnabled {
		intervalOverrides := make(map[string]float64)
		for _, asset := range cfg.Strategy.Assets {
			ac := cfg.ForAsset(asset)
			if ac.MarketMaking.FundingIntervalHours > 0 {
				intervalOverrides[asset] = ac.MarketMaking.FundingIntervalHours
			}
		}
		m.fundingTracker = NewFundingTracker(
			nil, // info set later via SetFundingInfo
			cfg.Strategy.Assets,
			time.Duration(cfg.MarketMaking.FundingPollIntervalSec)*time.Second,
			cfg.MarketMaking.FundingUrgencyScale,
			cfg.MarketMaking.FundingUrgencyHorizonMinutes,
			intervalOverrides,
		)
	}
	// Markout trackers.
	m.markouts = make(map[string]*MarkoutTracker, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.MarkoutEnabled {
			m.markouts[asset] = NewMarkoutTracker(
				time.Duration(ac.MarketMaking.MarkoutEWMAHalfLifeSec) * time.Second,
			)
		}
	}
	// P6: per-side win rate / profit factor stats trackers.
	m.perSideStats = make(map[string]*PerSideStats, numAssets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.PerSideWinrateEnabled {
			m.perSideStats[asset] = NewPerSideStats(ac.MarketMaking.PerSideWinrateWindowFills)
		}
	}
	return m
}

// Diagnostics returns a snapshot of pipeline intermediates for each asset.
func (m *Manager) Diagnostics() map[string]DiagnosticSnapshot {
	m.diagMu.RLock()
	out := make(map[string]DiagnosticSnapshot, len(m.diagSnaps))
	for k, v := range m.diagSnaps {
		out[k] = v
	}
	m.diagMu.RUnlock()
	// Side-presence counters are read-and-reset so each STATUS dump
	// reflects the most recent window only.
	for coin, snap := range out {
		if tracker := m.sidePresence[coin]; tracker != nil {
			bid, ask, both, cycles := tracker.SnapshotAndReset()
			snap.BidAbsentPct = bid
			snap.AskAbsentPct = ask
			snap.BothAbsentPct = both
			snap.PresenceCycles = cycles
			out[coin] = snap
		}
	}
	return out
}

// SetWarmupFn sets the function used to query spread engine warmup progress.
func (m *Manager) SetWarmupFn(fn func() (int, int)) {
	m.warmupFn = fn
}

// RecordMarketTrade records a public market trade for velocity tracking.
// Called from the trade fan-out goroutine in main.go.
func (m *Manager) RecordMarketTrade(trade types.TradeUpdate) {
	if vt, ok := m.velocityTrackers[trade.Coin]; ok && vt != nil {
		vt.RecordTradeUpdate(trade)
		// V20 (second bundle): trigger an out-of-cadence optimizer evaluation
		// when a volume burst is active AND sufficient time has elapsed since the
		// last eval. Allows the optimizer to react on a sub-60s timescale during
		// vol spikes rather than waiting for the next eval-ticker tick.
		ac := m.cfg.ForAsset(trade.Coin)
		ov := ac.MarketMaking.OptimizerV2
		if ov.Enabled && ov.BurstEvalEnabled && ov.BurstEvalMinIntervalSec > 0 && vt.IsBurst() {
			// Atomic check-and-advance. The v21 c19eada fix left a TOCTOU race:
			// two goroutines (trade fan-out + fill callback) could both pass the
			// `elapsed >= interval` check in the same ~500µs window, drain the
			// buffered channel, and trigger paired duplicate evaluations. Writing
			// lastOptEval under the lock at send time slams that window shut —
			// only one caller can pass per BurstEvalMinIntervalSec.
			interval := m.burstEvalInterval(trade.Coin, ov)
			m.lastOptEvalMu.Lock()
			if time.Since(m.lastOptEval) >= interval {
				m.lastOptEval = time.Now()
				m.lastOptEvalMu.Unlock()
				select {
				case m.burstEvalCh <- struct{}{}:
				default: // already pending
				}
			} else {
				m.lastOptEvalMu.Unlock()
			}
		}
	}
}

// burstEvalInterval returns the effective minimum interval between burst-triggered
// optimizer evaluations. V30 part 5: extended to BurstEvalAllNegCooldownSec (when
// > 0) while the asset's OptimizerV2 is in an all_negative utility streak. This
// throttles churn during regimes where repeated burst evals shuffle knobs without
// improving markout (V29 observed 1034 burst-triggered evals / 1624 total).
func (m *Manager) burstEvalInterval(coin string, ov config.OptimizerV2Config) time.Duration {
	base := time.Duration(ov.BurstEvalMinIntervalSec) * time.Second
	if ov.BurstEvalAllNegCooldownSec <= 0 {
		return base
	}
	opt, ok := m.optimizersV2[coin]
	if !ok || opt == nil || !opt.IsAllNegative() {
		return base
	}
	extended := time.Duration(ov.BurstEvalAllNegCooldownSec) * time.Second
	if extended < base {
		return base
	}
	return extended
}

type forceRefreshRequest struct {
	coin   string
	reason string
}

func (m *Manager) markForceRefresh(coin, reason string) {
	m.mu.Lock()
	if m.forceRefreshReasons == nil {
		m.forceRefreshReasons = make(map[string]string)
	}
	m.forceRefreshReasons[coin] = reason
	m.mu.Unlock()
}

func (m *Manager) markForceRefreshes(reqs []forceRefreshRequest) {
	if len(reqs) == 0 {
		return
	}
	m.mu.Lock()
	if m.forceRefreshReasons == nil {
		m.forceRefreshReasons = make(map[string]string, len(reqs))
	}
	for _, req := range reqs {
		m.forceRefreshReasons[req.coin] = req.reason
	}
	m.mu.Unlock()
}

// checkIdleBreaker evaluates the P9 idle-fill circuit breaker for coin and,
// when conditions are met, marks the per-asset state as fired for one refresh.
// Returns (fired, exitSide). exitSide is "buy" when invUtil < 0 (short exits via
// buys) and "sell" when invUtil > 0 (long exits via sells).
//
// Caller must hold m.mu (writes m.idleBreakers entry). Reads supervisor state
// without the manager lock — Supervisor is independently mutex-protected.
func (m *Manager) checkIdleBreaker(coin string, invUtil float64, now time.Time) (bool, string) {
	mm := m.cfg.ForAsset(coin).MarketMaking
	if !mm.IdleFillDetectorEnabled {
		return false, ""
	}
	if mm.IdleFillThresholdSec <= 0 || mm.IdleFillRetryIntervalSec <= 0 {
		return false, ""
	}
	last, ok := m.lastFillTime[coin]
	if !ok || last.IsZero() {
		return false, ""
	}
	if now.Sub(last) < time.Duration(mm.IdleFillThresholdSec)*time.Second {
		return false, ""
	}
	if math.Abs(invUtil) < mm.IdleFillMinInvUtil {
		return false, ""
	}
	if !m.supervisor.CanQuote() || m.supervisor.IsCoinHalted(coin) {
		return false, ""
	}
	state := m.idleBreakers[coin]
	if state == nil {
		state = &idleBreakerState{}
		m.idleBreakers[coin] = state
	}
	retryDur := time.Duration(mm.IdleFillRetryIntervalSec) * time.Second
	if !state.lastFiredAt.IsZero() && now.Sub(state.lastFiredAt) < retryDur {
		return false, ""
	}
	if state.activeFlag && now.Sub(state.lastFiredAt) >= retryDur {
		state.activeFlag = false
	}
	exitSide := "sell"
	if invUtil < 0 {
		exitSide = "buy"
	}
	state.lastFiredAt = now
	state.activeFlag = true
	state.activeSide = exitSide
	return true, exitSide
}

func markOrdersForced(orders []types.OrderRequest, reason string) {
	for i := range orders {
		orders[i].Force = true
		orders[i].Reason = appendQuoteReason(orders[i].Reason, reason)
	}
}

func appendQuoteReason(base, suffix string) string {
	if suffix == "" {
		return base
	}
	if base == "" {
		return suffix
	}
	return base + " " + suffix
}

func overlayMateriallyChanged(before, after OptimizerOverlay, minSpreadDeltaBps float64) bool {
	if minSpreadDeltaBps <= 0 {
		minSpreadDeltaBps = 0.5
	}
	if math.Abs(after.SpreadDeltaBuy-before.SpreadDeltaBuy) >= minSpreadDeltaBps {
		return true
	}
	if math.Abs(after.SpreadDeltaSell-before.SpreadDeltaSell) >= minSpreadDeltaBps {
		return true
	}
	if math.Abs(after.SpreadDelta-before.SpreadDelta) >= minSpreadDeltaBps {
		return true
	}
	if math.Abs(after.NotionalMult-before.NotionalMult) >= 0.02 {
		return true
	}
	if math.Abs(after.SkewMult-before.SkewMult) >= 0.02 {
		return true
	}
	if math.Abs(after.RefreshMult-before.RefreshMult) >= 0.05 {
		return true
	}
	return false
}

func (m *Manager) recordPriceShock(ctx context.Context, update types.BookUpdate) {
	tracker := m.priceShockTrackers[update.Coin]
	if tracker == nil {
		return
	}
	snap, activated := tracker.Record(update.Mid, time.Now())
	if !activated {
		return
	}

	pos := m.executor.Position(update.Coin)
	ac := m.cfg.ForAsset(update.Coin)
	mm := ac.MarketMaking
	slog.Warn("price shock guard activated",
		"coin", update.Coin,
		"direction", snap.Direction,
		"entry_side", snap.EntrySide,
		"move_bps", fmt.Sprintf("%.1f", snap.MoveBps),
		"cooldown_ms", time.Until(snap.ActiveUntil).Milliseconds(),
		"position_side", pos.Side,
	)
	if m.cfg.SignalPauses() && sideIncreasesPosition(snap.EntrySide, pos.Side) {
		cancelCtx, cancelCancel := context.WithTimeout(ctx, 3*time.Second)
		if err := m.executor.CancelSide(cancelCtx, update.Coin, snap.EntrySide, "price_shock_"+snap.Direction); err != nil {
			slog.Error("price shock side cancel failed",
				"coin", update.Coin,
				"side", snap.EntrySide,
				"error", err,
			)
		}
		cancelCancel()
	}

	// V30 part 4: fractional IOC close on favorable shock.
	// Fires exactly once per shock activation (this function only runs when
	// the tracker reports a NEW activation). Gates: non-flat, shock direction
	// favors inventory, inv_util above threshold, move exceeds min capture.
	// The IOC runs on a goroutine so the book-update goroutine that called
	// this function is not blocked for ~3s during the IOC round-trip.
	favorable := pos.Side != "" &&
		!sideIncreasesPosition(snap.EntrySide, pos.Side)
	if favorable && mm.PriceShockFavorableIocFrac > 0 && mm.PriceShockFavorableIocFrac <= 1 {
		maxPos := ac.Risk.MaxPositionNotional
		invUtil := 0.0
		if maxPos > 0 {
			invUtil = pos.Notional / maxPos
		}
		moveBpsAbs := math.Abs(snap.MoveBps)
		gateUtil := invUtil >= mm.PriceShockFavorableIocMinInvUtil
		gateMove := moveBpsAbs >= mm.PriceShockFavorableIocMinCaptureBps
		if !gateUtil || !gateMove {
			slog.Info("favorable shock IOC skipped (gate)",
				"coin", update.Coin,
				"direction", snap.Direction,
				"inv_util", invUtil,
				"min_inv_util", mm.PriceShockFavorableIocMinInvUtil,
				"move_bps_abs", moveBpsAbs,
				"min_capture_bps", mm.PriceShockFavorableIocMinCaptureBps,
				"gate_util", gateUtil,
				"gate_move", gateMove,
			)
		} else {
			iocCoin := update.Coin
			iocFrac := mm.PriceShockFavorableIocFrac
			iocSlippage := mm.PriceShockFavorableIocSlippageBps
			iocDirection := snap.Direction
			iocMoveBps := snap.MoveBps
			iocInvUtil := invUtil
			go func() {
				// V30 follow-up: re-check supervisor state before firing. If a
				// fill (or another signal) triggered a risk-halt or shock-chain
				// cascade between the gate check above and the goroutine
				// scheduling, we should not push an aggressive IOC while the
				// supervisor is trying to flatten / hold quoting.
				if !m.supervisor.CanQuote() {
					slog.Info("favorable shock IOC skipped (supervisor not quoting)",
						"coin", iocCoin,
						"state", m.supervisor.State().String(),
					)
					return
				}
				iocCtx, iocCancel := context.WithTimeout(ctx, 3*time.Second)
				defer iocCancel()
				filled, err := m.executor.ReducePositionIOC(
					iocCtx,
					iocCoin,
					iocFrac,
					iocSlippage,
					"price_shock_favorable_"+iocDirection,
				)
				if err != nil {
					slog.Warn("favorable shock IOC failed",
						"coin", iocCoin,
						"direction", iocDirection,
						"filled_size", filled,
						"error", err,
					)
					return
				}
				slog.Info("favorable shock IOC executed",
					"coin", iocCoin,
					"direction", iocDirection,
					"move_bps", fmt.Sprintf("%.1f", iocMoveBps),
					"inv_util", iocInvUtil,
					"frac", iocFrac,
					"filled_size", filled,
				)
			}()
		}
	}

	// V30 part 3: shock-chain cascade detection. Record the activation; if
	// the count in the rolling window crosses the threshold, escalate to a
	// hard flatten + pause. Reuses the supervisor halt/recovery pattern
	// (SignalRiskHalt → OpFlatten → pause → SignalRecovery → OpRecovery).
	if chain := m.shockChainTrackers[update.Coin]; chain != nil {
		count, crossed := chain.Record(time.Now())
		if count >= 2 {
			slog.Warn("shock chain count",
				"coin", update.Coin,
				"count", count,
				"threshold", mm.ShockChainThreshold,
				"window_sec", mm.ShockChainWindowSec,
			)
		}
		if crossed {
			m.triggerShockChainEscalation(ctx, update.Coin, count, snap)
		}
	}

	slog.Info("optimizer price-shock-triggered eval", "coin", update.Coin)
	if reqs := m.evaluateOptimizers(); len(reqs) > 0 {
		m.markForceRefreshes(reqs)
	}
	m.markForceRefresh(update.Coin, "price_shock_"+snap.Direction)
}

// triggerShockChainEscalation handles the V30 part 3 cascade response.
// Asserts an external halt + recovery deadline on the supervisor immediately
// (non-blocking, no goroutine needed — state resolution reads the timestamps
// directly), then spawns a goroutine to perform cancel-all + close-position
// across all managed assets. The halt outranks RiskHalt/RiskResume in the
// supervisor's state priority so a risk-manager resume (e.g. drawdown
// cleared because the flatten just happened) cannot prematurely end the
// pause. The chain tracker counter is reset immediately so a new cascade
// requires a fresh threshold crossing rather than replaying prior
// activations after the pause ends.
func (m *Manager) triggerShockChainEscalation(ctx context.Context, coin string, count int, snap PriceShockSnapshot) {
	if !m.cfg.SignalPauses() {
		return
	}
	ac := m.cfg.ForAsset(coin)
	mm := ac.MarketMaking
	pauseDur := time.Duration(mm.ShockChainPauseSec) * time.Second
	recoveryDur := time.Duration(mm.ShockChainRecoverySec) * time.Second
	slog.Error("shock chain threshold crossed — flatten + pause",
		"coin", coin,
		"shock_count", count,
		"threshold", mm.ShockChainThreshold,
		"window_sec", mm.ShockChainWindowSec,
		"trigger_direction", snap.Direction,
		"trigger_move_bps", fmt.Sprintf("%.1f", snap.MoveBps),
		"pause_sec", mm.ShockChainPauseSec,
		"recovery_sec", mm.ShockChainRecoverySec,
	)

	// Step 1: assert the halt deadline FIRST so the next quoting cycle sees
	// OpFlatten before any flatten API call completes. Timestamp-based: no
	// goroutine or timer required to transition out of halt → recovery →
	// normal; state resolution in supervisor.stateLocked() reads the
	// deadlines on each call.
	m.supervisor.SignalExternalHalt(pauseDur, recoveryDur)

	// Step 2: reset ALL chain trackers. The flatten + pause is global across
	// every managed asset, so stale shock counts on other assets would let
	// an unrelated asset re-cascade the moment the pause+recovery ends on
	// a single residual activation from before the cascade. Clearing all
	// trackers forces each asset to observe a fresh threshold crossing
	// post-recovery.
	for _, chain := range m.shockChainTrackers {
		if chain != nil {
			chain.Reset()
		}
	}

	// Step 3: flatten on a dedicated goroutine so the book-update goroutine
	// that called this function returns immediately. Flatten is best-effort;
	// if it times out or errors, the halt deadline still gates quoting. Uses
	// the parent ctx so app shutdown aborts the flatten via ctx cancellation.
	assets := append([]string(nil), m.cfg.Strategy.Assets...) // snapshot
	go func() {
		flattenCtx, flattenCancel := context.WithTimeout(ctx, cancelTimeoutRiskAction)
		defer flattenCancel()
		for _, asset := range assets {
			if err := m.executor.CancelAll(flattenCtx, asset); err != nil {
				slog.Error("shock chain: cancel failed", "asset", asset, "error", err)
			}
			if err := m.executor.ClosePosition(flattenCtx, asset, "shock_chain_cascade"); err != nil {
				slog.Error("shock chain: close failed", "asset", asset, "error", err)
			}
		}
	}()
}

// SetWarmupDuration sets the time-based warmup fallback duration.
// Used when the spread engine is disabled.
func (m *Manager) SetWarmupDuration(d time.Duration) {
	m.warmupDuration = d
}

// SetSchedule installs a P12 time-of-day notional schedule. Passing nil
// disables the schedule (default). The schedule is global (not per-asset).
func (m *Manager) SetSchedule(s *risk.TradingSchedule) {
	m.schedule = s
	m.scheduleLastMult = 1.0
}

// scheduleMultiplier returns the current [0,1] notional multiplier from the
// configured schedule (nil-safe: returns 1.0 when no schedule). Emits INFO
// transition logs ("schedule window entered" / "schedule window exited") exactly
// once per label change, guarded by scheduleMu so multi-asset callers don't
// double-log.
func (m *Manager) scheduleMultiplier(now time.Time) float64 {
	mult, label := m.schedule.NotionalMultiplier(now)
	m.scheduleMu.Lock()
	prev := m.scheduleLastLabel
	prevMult := m.scheduleLastMult
	if prev == "" && prevMult == 0 {
		// Initial state before SetSchedule set it to 1.0 — treat as inactive.
		prevMult = 1.0
	}
	changed := label != prev
	if changed {
		m.scheduleLastLabel = label
		m.scheduleLastMult = mult
	}
	m.scheduleMu.Unlock()

	if changed {
		if prev != "" {
			slog.Info("schedule window exited",
				"label", prev,
				"multiplier", prevMult,
			)
		}
		if label != "" {
			slog.Info("schedule window entered",
				"label", label,
				"multiplier", mult,
			)
		}
	}
	return mult
}

// RateLimitStatus returns the current circuit breaker state for STATUS logging.
// sessionExchangeReqs is the cumulative exchange request count this session
// (caller accumulates from RPSTracker snapshots).
func (m *Manager) RateLimitStatus(sessionExchangeReqs int64) (tier string, tteMinutes float64, depletion float64, halted bool, remaining int, ewmaDepletion float64, cap int, sessionUsed int64, sessionGenerated int64, sessionNet int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	tteMin := m.rlTTE / 60.0
	// Session accounting: how much the bucket changed since boot.
	sessionNet = m.rlBootRemaining - m.rateLimitRemaining // positive = net drain
	// session_generated = cap growth from volume-based regeneration.
	// The exchange increases nRequestsCap by 1 per 1 USDC traded (lifetime cumulative).
	// Previous formula (sessionExchangeReqs - sessionNet) was always negative because
	// sessionNet includes info+exchange calls but sessionExchangeReqs only counted exchange.
	sessionGenerated = int64(m.rateLimitCap - m.rlBootCap)
	if sessionGenerated < 0 {
		sessionGenerated = 0
	}
	return m.supervisor.RLTier().String(), tteMin, m.rlDepletion, m.supervisor.IsRLCritical(), m.rateLimitRemaining, m.rlEWMADepletion, m.rateLimitCap, sessionExchangeReqs, sessionGenerated, sessionNet
}

// resolveBaselineBps returns the toxicity baseline bps, auto-computing from
// spread if not explicitly configured (same logic as toxicity tracker init).
func (m *Manager) resolveBaselineBps(ac config.AssetConfig) float64 {
	if ac.MarketMaking.ToxicityBaselineBps > 0 {
		return ac.MarketMaking.ToxicityBaselineBps
	}
	return ac.MarketMaking.QuoteSpreadBps / 2.0
}

// VirtCycle returns the virtuous cycle engine, for external STATUS logging.
func (m *Manager) VirtCycle() *VirtCycleEngine {
	return m.virtCycle
}

// MarkoutSnapshot holds a per-coin post-fill markout view: per-horizon
// composite EWMA from MarkoutTracker (T500ms / T1s / T2s / T5s in bps) plus
// per-side composite EWMA from SideToxicityTracker. Surfaces what previously
// required grepping hundreds of raw per-fill `markout` log lines. Added in
// V30 part 5 after V29 post-mortem §3 had to mark per-side per-horizon
// EWMA as "n/a — not logged".
type MarkoutSnapshot struct {
	Composite     MarkoutEWMA // per-horizon composite across both sides
	BuyComposite  float64     // side-toxicity buy EWMA (bps)
	SellComposite float64     // side-toxicity sell EWMA (bps)
	BuyFills      int         // per-side fill counts feeding the EWMAs
	SellFills     int
}

// MarkoutSnapshot returns the current markout snapshot for a coin, for
// external periodic logging. Returns the zero value if the coin is not
// configured.
func (m *Manager) MarkoutSnapshot(coin string) MarkoutSnapshot {
	var snap MarkoutSnapshot
	if mt, ok := m.markouts[coin]; ok && mt != nil {
		snap.Composite = mt.EWMA()
	}
	if st, ok := m.toxicityTrackers[coin]; ok && st != nil {
		buy, sell := st.SideEWMAs()
		snap.BuyComposite = buy
		snap.SellComposite = sell
		snap.BuyFills, snap.SellFills = st.SideFillCounts()
	}
	return snap
}

// RateLimitAllocatorInstance returns the rate limit allocator, for external STATUS logging.
func (m *Manager) RateLimitAllocatorInstance() *RateLimitAllocator {
	return m.rateLimitAllocator
}

// maybeLogVWAPAlphaFade emits an INFO log for P2 drift-aware alpha fade,
// rate-limited per coin: log on bucket change (0.1 resolution) OR once per
// 60 seconds, whichever comes first.
func (m *Manager) maybeLogVWAPAlphaFade(coin string, baseAlpha, effectiveAlpha, divergence, sigma float64) {
	bucket := int(effectiveAlpha * 10)
	now := time.Now()
	m.vwapAlphaFadeLogMu.Lock()
	entry, ok := m.vwapAlphaFadeLog[coin]
	if ok && bucket == entry.bucket && now.Sub(entry.lastAt) < vwapAlphaFadeLogInterval {
		m.vwapAlphaFadeLogMu.Unlock()
		return
	}
	if !ok {
		entry = &vwapAlphaFadeLogEntry{}
		m.vwapAlphaFadeLog[coin] = entry
	}
	entry.bucket = bucket
	entry.lastAt = now
	m.vwapAlphaFadeLogMu.Unlock()

	slog.Info("vwap_alpha drift fade",
		"coin", coin,
		"base_alpha", baseAlpha,
		"effective_alpha", effectiveAlpha,
		"divergence", divergence,
		"sigma", sigma,
	)
}

// SetFundingInfo sets the FundingInfo implementation for the funding tracker.
// Called after SDK initialization in main.go. Starts the background polling
// goroutine if the funding tracker was created (funding_urgency_enabled=true).
func (m *Manager) SetFundingInfo(info FundingInfo) {
	if m.fundingTracker != nil {
		m.fundingTracker.info = info
	}
}

// SetReconnectCh sets the channel that signals WS reconnects.
// On signal, the manager suppresses quoting for 1s to allow fresh book data
// to arrive before re-quoting (prevents stale-mid dislocation fills).
func (m *Manager) SetReconnectCh(ch <-chan struct{}) {
	m.reconnectCh = ch
}

// SetPostMortemDumper attaches the post-mortem dumper. Nil-safe.
func (m *Manager) SetPostMortemDumper(d *PostMortemDumper) {
	m.postMortem = d
}

// PostMortemDumper returns the attached dumper (may be nil).
func (m *Manager) PostMortemDumper() *PostMortemDumper {
	return m.postMortem
}

// DumpPostMortem builds finalState for each configured asset and triggers a
// dump per asset with the supplied reason. Safe to call when the dumper is
// disabled or nil (no-op).
func (m *Manager) DumpPostMortem(reason string) {
	if m == nil || m.postMortem == nil || !m.postMortem.Enabled() {
		return
	}
	for _, asset := range m.cfg.Strategy.Assets {
		state := m.buildFinalState(asset)
		if _, err := m.postMortem.Dump(asset, reason, state); err != nil {
			slog.Error("post-mortem dump failed", "coin", asset, "reason", reason, "error", err)
		}
	}
}

// buildFinalState assembles a per-asset finalState payload for the post-mortem.
func (m *Manager) buildFinalState(asset string) map[string]any {
	state := map[string]any{
		"supervisor": m.supervisor.State().String(),
	}
	if m.sessionPnL != nil {
		state["start_equity"] = m.sessionPnL.StartEquity()
		state["end_equity"] = m.sessionPnL.CurrentEquity()
		state["duration_sec"] = m.sessionPnL.ElapsedSec()
	}
	if optV2, ok := m.optimizersV2[asset]; ok && optV2 != nil {
		state["optimizer"] = optV2.Snapshot()
	}
	if pos := m.executor.Position(asset); pos.Coin != "" || pos.Size != 0 {
		state["position"] = map[string]any{
			"coin":      pos.Coin,
			"side":      pos.Side,
			"size":      pos.Size,
			"notional":  pos.Notional,
			"avg_entry": pos.AvgEntry,
		}
	}
	m.diagMu.RLock()
	if snap, ok := m.diagSnaps[asset]; ok {
		state["diagnostics"] = map[string]any{
			"fair_value":           snap.FairValue,
			"book_mid":             snap.BookMid,
			"effective_spread_bps": snap.EffectiveSpreadBps,
			"inventory_util":       snap.InventoryUtil,
			"halted":               snap.HealthHalted,
			"degraded":             snap.HealthDegraded,
			"reason":               snap.HealthReason,
		}
	}
	m.diagMu.RUnlock()
	return state
}

// maybeDumpOnFlatten observes supervisor state transitions into OpFlatten
// and triggers a per-asset post-mortem dump for each configured coin.
// Throttled to avoid back-to-back dumps for the same condition.
func (m *Manager) maybeDumpOnFlatten(reason string) {
	if m.postMortem == nil || !m.postMortem.Enabled() {
		return
	}
	curr := m.supervisor.State()
	m.prevSupStateMu.Lock()
	prev := m.prevSupState
	m.prevSupState = curr
	throttled := !m.lastDumpAt.IsZero() && time.Since(m.lastDumpAt) < 5*time.Second && m.lastDumpReason == reason
	if curr == OpFlatten && prev != OpFlatten && !throttled {
		m.lastDumpAt = time.Now()
		m.lastDumpReason = reason
	} else {
		m.prevSupStateMu.Unlock()
		return
	}
	m.prevSupStateMu.Unlock()
	m.DumpPostMortem(reason)
}

// UpdateRateLimit feeds the latest rate limit snapshot for the circuit breaker.
func (m *Manager) UpdateRateLimit(remaining, cap int) {
	m.mu.Lock()
	m.rateLimitRemaining = remaining
	m.rateLimitCap = cap

	// Capture boot-time remaining and cap for session accounting.
	if !m.rlBootSeeded {
		m.rlBootRemaining = remaining
		m.rlBootCap = cap
		m.rlLastCapSeen = cap
		m.rlBootSeeded = true
	} else if cap > m.rlLastCapSeen {
		// V30 part 5: feed observed cap growth into VirtCycle so Headroom
		// reflects actual exchange-side regen credit, not the 1-USDC=1-req
		// assumption on volumeRate (V29 observed ratio ~0.10).
		if m.virtCycle != nil {
			m.virtCycle.RecordRegen(cap - m.rlLastCapSeen)
		}
		m.rlLastCapSeen = cap
	} else if cap < m.rlLastCapSeen {
		// Cap moved down (should not happen under normal Hyperliquid
		// semantics — nRequestsCap is monotonic non-decreasing).
		// Re-anchor so we don't credit a phantom huge growth next poll.
		m.rlLastCapSeen = cap
	}

	// Update long-window EWMA depletion tracker.
	now := time.Now()
	if !m.rlEWMASeeded {
		// First sample — seed and wait for next.
		m.rlEWMALastAt = now
		m.rlEWMALastRem = remaining
		m.rlEWMASeeded = true
	} else {
		elapsed := now.Sub(m.rlEWMALastAt)
		if elapsed > 0 {
			// Instantaneous depletion: positive = remaining shrinking.
			instantDepl := float64(m.rlEWMALastRem-remaining) / elapsed.Seconds()
			halfLife := time.Duration(m.cfg.RateLimit.EWMAHalfLifeMinutes) * time.Minute
			alpha := ewmaAlpha(elapsed, halfLife)
			m.rlEWMADepletion = m.rlEWMADepletion*(1-alpha) + instantDepl*alpha
			m.rlEWMALastAt = now
			m.rlEWMALastRem = remaining
		}
	}

	// Track history for short-window depletion rate computation.
	if m.rateLimitHistory == nil {
		// Buffer must span > 120s (minimum elapsed for short-window TTE).
		// At 10s polling interval, 15 samples = 140s of history.
		m.rateLimitHistory = make([]rateLimitSample, 15)
	}
	m.rateLimitHistory[m.rateLimitHistoryIdx] = rateLimitSample{remaining: remaining, at: now}
	m.rateLimitHistoryIdx = (m.rateLimitHistoryIdx + 1) % len(m.rateLimitHistory)
	if m.rateLimitHistoryIdx == 0 {
		m.rateLimitHistoryFull = true
	}

	// Compute current tier.
	newTier, tte, depletion := m.computeRateLimitTierLocked()
	oldTier := m.supervisor.RLTier()
	m.rlTTE = tte
	m.rlDepletion = depletion

	// Percentage-based remaining warning (log once, reset if remaining recovers).
	if cap > 0 {
		pctRemaining := m.rateLimitRemainingPctLocked(remaining)
		rl := m.cfg.RateLimit
		if pctRemaining < rl.RemainingPctWarn && !m.rlPctWarned {
			m.rlPctWarned = true
			slog.Warn("rate limit: remaining below warning threshold",
				"remaining", remaining,
				"cap", cap,
				"pct_remaining", fmt.Sprintf("%.1f%%", pctRemaining),
				"warn_threshold", fmt.Sprintf("%.0f%%", rl.RemainingPctWarn),
				"ewma_depletion", fmt.Sprintf("%.4f", m.rlEWMADepletion),
			)
		} else if pctRemaining >= rl.RemainingPctWarn && m.rlPctWarned {
			m.rlPctWarned = false // reset so we warn again if it dips back
		}
	}

	// Log tier transitions.
	if newTier.toRLTier() != oldTier {
		tteMin := tte / 60.0
		if math.IsInf(tteMin, 1) {
			tteMin = -1 // sentinel for logging
		}
		slog.Info("rate limit tier changed",
			"old_tier", oldTier.String(),
			"new_tier", newTier.String(),
			"tte_minutes", fmt.Sprintf("%.1f", tteMin),
			"remaining", remaining,
			"depletion_rate", fmt.Sprintf("%.2f", depletion),
			"ewma_depletion", fmt.Sprintf("%.4f", m.rlEWMADepletion),
		)
		// Signal supervisor for non-critical tier transitions.
		// Critical/Recover are signaled below, outside the lock.
		switch {
		case newTier == rlTierDegraded:
			m.supervisor.Signal(SignalRLDegraded)
		case newTier == rlTierCaution:
			m.supervisor.Signal(SignalRLCaution)
		case newTier == rlTierNormal && oldTier != RLTierCritical:
			m.supervisor.Signal(SignalRLRecover)
		}
	}

	// Check for Critical entry. Signal supervisor under the lock to prevent a
	// concurrent call from triggering a second shutdown.
	needShutdown := newTier == rlTierCritical && !m.supervisor.IsRLCritical()
	if needShutdown {
		m.supervisor.Signal(SignalRLCritical)
	}
	// Check for recovery: use raw TTE (not tier) because hysteresis holds tier at Critical.
	// Recovery requires TTE > recovery threshold AND remaining above 2× floor.
	// Also require remaining/cap percentage above the critical threshold to prevent
	// oscillation: the pct-based critical floor would immediately re-trigger if we
	// recover while still below the percentage threshold.
	needRecovery := false
	if m.supervisor.IsRLCritical() && !needShutdown {
		rl := m.cfg.RateLimit
		tteMin := tte / 60.0
		recoverable := (math.IsInf(tte, 1) || tteMin > float64(rl.TTERecoveryMinutes)) &&
			remaining > rl.AbsoluteFloor*2
		// Block recovery if remaining/cap is still below the percentage critical threshold.
		if recoverable && cap > 0 {
			pctRemaining := m.rateLimitRemainingPctLocked(remaining)
			if pctRemaining < rl.RemainingPctCritical {
				recoverable = false
			}
		}
		if recoverable || m.cfg.RateLimit.Disabled {
			needRecovery = true
		}
	}
	m.mu.Unlock()

	if needShutdown {
		m.rateLimitShutdown(remaining, tte, depletion)
		m.maybeDumpOnFlatten("rate_limit_critical")
	}
	if needRecovery {
		m.supervisor.Signal(SignalRLRecover)
		m.rateLimitRecover(remaining, tte, depletion)
	}
}

// rateLimitShutdown cancels all orders and closes all positions.
// REQUIRES: caller has already signaled SignalRLCritical.
func (m *Manager) rateLimitShutdown(remaining int, tte, depletion float64) {
	trigger := "tte"
	if remaining < m.cfg.RateLimit.AbsoluteFloor {
		trigger = "floor"
	} else if m.rateLimitCap > 0 {
		pctRemaining := m.rateLimitRemainingPctLocked(remaining)
		if pctRemaining < m.cfg.RateLimit.RemainingPctCritical {
			trigger = "pct"
		}
	}
	tteMin := tte / 60.0
	slog.Warn("rate limit CRITICAL: cancelling all orders and closing positions",
		"remaining", remaining,
		"tte_minutes", fmt.Sprintf("%.1f", tteMin),
		"depletion_rate", fmt.Sprintf("%.2f", depletion),
		"trigger", trigger,
	)

	ctx, cancel := context.WithTimeout(context.Background(), cancelTimeoutRiskAction)
	defer cancel()
	for _, asset := range m.cfg.Strategy.Assets {
		if err := m.executor.CancelAll(ctx, asset); err != nil {
			slog.Error("rate limit shutdown: cancel failed", "coin", asset, "err", err)
		}
		pos := m.executor.Position(asset)
		if pos.Size != 0 {
			if err := m.executor.ClosePosition(ctx, asset, "rate_limit_critical"); err != nil {
				slog.Error("rate limit shutdown: close position failed", "coin", asset, "err", err)
			}
		}
	}
}

// rateLimitRecover clears the rate limit halt and re-enters warmup.
func (m *Manager) rateLimitRecover(remaining int, tte, depletion float64) {
	tteMin := tte / 60.0
	slog.Info("rate limit recovered from critical, entering warmup",
		"tte_minutes", fmt.Sprintf("%.1f", tteMin),
		"remaining", remaining,
		"depletion_rate", fmt.Sprintf("%.2f", depletion),
	)

	m.mu.Lock()
	// Reset warmup timer so the bot ramps up gradually after recovery.
	m.startedAt = time.Now()
	m.mu.Unlock()
}

// computeRateLimitTierLocked computes the depletion rate, TTE, and current tier.
// REQUIRES: caller holds m.mu.
func (m *Manager) computeRateLimitTierLocked() (tier rateLimitTier, tte float64, depletion float64) {
	rl := m.cfg.RateLimit
	if rl.Disabled {
		return rlTierNormal, math.Inf(1), 0
	}

	// Absolute floor check — always active, even without history.
	if m.rateLimitRemaining < rl.AbsoluteFloor {
		return rlTierCritical, 0, 0
	}

	// Percentage-based Critical floor — fires when remaining/cap ratio is
	// critically low, regardless of depletion rate calculations.
	// Skipped during startup grace period to let the bot establish a depletion
	// rate before the pct floor can fire (accounts with low remaining at boot).
	graceSec := rl.PctCriticalGraceSec
	inGrace := graceSec > 0 && !m.startedAt.IsZero() && time.Since(m.startedAt) < time.Duration(graceSec)*time.Second
	if m.rateLimitCap > 0 && !inGrace {
		pctRemaining := m.rateLimitRemainingPctLocked(m.rateLimitRemaining)
		if pctRemaining < rl.RemainingPctCritical {
			// Use EWMA depletion if available for TTE estimate, else 0.
			dep := m.rlEWMADepletion
			if dep <= 0 {
				dep = 0
			}
			var pctTTE float64
			if dep > 0 {
				pctTTE = float64(m.rateLimitRemaining) / dep
			}
			return rlTierCritical, pctTTE, dep
		}
	}

	// Need full history buffer for short-window rate-based computation.
	if !m.rateLimitHistoryFull {
		return rlTierNormal, math.Inf(1), 0
	}

	newest := (m.rateLimitHistoryIdx - 1 + len(m.rateLimitHistory)) % len(m.rateLimitHistory)
	oldest := m.rateLimitHistoryIdx

	oldSample := m.rateLimitHistory[oldest]
	newSample := m.rateLimitHistory[newest]

	elapsed := newSample.at.Sub(oldSample.at).Seconds()
	if elapsed < 120 { // need at least 2 min of data
		return rlTierNormal, math.Inf(1), 0
	}

	// Short-window depletion rate: positive means remaining is shrinking.
	depletion = float64(oldSample.remaining-newSample.remaining) / elapsed

	if depletion <= 0 {
		// Short window says remaining is stable or growing.
		// Check EWMA for slow secular drain that the short window misses.
		if m.rlEWMASeeded && m.rlEWMADepletion > 0 {
			// EWMA sees positive depletion — use it as fallback.
			depletion = m.rlEWMADepletion
		} else {
			// Both short window and EWMA agree: safe.
			return rlTierNormal, math.Inf(1), depletion
		}
	}

	// Time to exhaustion.
	tte = float64(m.rateLimitRemaining) / depletion
	tteMin := tte / 60.0

	// Compute raw tier from TTE.
	var rawTier rateLimitTier
	switch {
	case tteMin < float64(rl.TTECriticalMinutes):
		rawTier = rlTierCritical
	case tteMin < float64(rl.TTEDegradedMinutes):
		rawTier = rlTierDegraded
	case tteMin < float64(rl.TTECautionMinutes):
		rawTier = rlTierCaution
	default:
		rawTier = rlTierNormal
	}

	// Hysteresis: to de-escalate, TTE must exceed a higher threshold than the entry.
	// Critical → requires TTE > recovery threshold (stricter than caution).
	// Degraded → requires TTE > caution threshold.
	currentTier := m.supervisor.RLTier()
	if rawTier.toRLTier() < currentTier {
		switch currentTier {
		case RLTierDegraded:
			if tteMin < float64(rl.TTECautionMinutes) {
				rawTier = rlTierDegraded
			}
		case RLTierCritical:
			if tteMin < float64(rl.TTERecoveryMinutes) {
				rawTier = rlTierCritical // hold until recovery threshold
			}
		}
	}

	return rawTier, tte, depletion
}

// RecordFill records a fill for adaptive notional sizing and fill window stats.
func (m *Manager) RecordFill(fill types.FillEvent) {
	// Per-side fill rate recording (Smart Layer 3).
	// RecordFillSide updates BOTH the per-side tracker AND the global tracker,
	// so we do NOT call RecordFill separately (that would double-count globally).
	if ft := m.fillRates[fill.Coin]; ft != nil {
		ft.RecordFillSide(fill.Side, fill.Size*fill.Price)
	}
	// Virtuous cycle: track fill volume for budget regen estimation.
	if m.virtCycle != nil {
		m.virtCycle.RecordVolume(fill.Size * fill.Price)
	}
	// Optimizer v2: increment fill counter.
	if optV2 := m.optimizersV2[fill.Coin]; optV2 != nil {
		optV2.RecordFill()
	}
	// Rate limit allocator: record fill with capture bps.
	if m.rateLimitAllocator != nil {
		var captureBps float64
		m.diagMu.RLock()
		if snap, ok := m.diagSnaps[fill.Coin]; ok && snap.FairValue > 0 {
			captureBps = math.Abs(fill.Price-snap.FairValue) / snap.FairValue * 10000.0
		}
		m.diagMu.RUnlock()
		m.rateLimitAllocator.RecordFill(fill.Coin, captureBps, fill.Size*fill.Price)
	}

	notional := fill.Price * fill.Size
	// Measured fee (bps of notional) for the edge gate. Extreme or malformed
	// values are ignored so one bad print cannot pin the required spread.
	if notional > 0 {
		feeBps := (fill.Fee + fill.BuilderFee) / notional * 10000.0
		if !math.IsNaN(feeBps) && !math.IsInf(feeBps, 0) && feeBps >= 0 && feeBps <= 100 {
			m.feeBpsMu.Lock()
			if m.feeBpsSeeded[fill.Coin] {
				m.feeBps[fill.Coin] = 0.8*m.feeBps[fill.Coin] + 0.2*feeBps
			} else {
				m.feeBps[fill.Coin] = feeBps
				m.feeBpsSeeded[fill.Coin] = true
			}
			m.feeBpsMu.Unlock()
		}
	}
	var captureBps float64
	var fv float64
	var wickMult float64
	m.diagMu.Lock()
	if fs, ok := m.fillStats[fill.Coin]; ok {
		switch fill.Side {
		case "buy":
			fs.BuyCount++
			fs.BuyNotional += notional
		case "sell":
			fs.SellCount++
			fs.SellNotional += notional
		}
		fs.TotalFees += fill.Fee + fill.BuilderFee
		// Spread capture: how far the fill price was from fair value.
		if snap, ok := m.diagSnaps[fill.Coin]; ok && snap.FairValue > 0 {
			fv = snap.FairValue
			captureBps = math.Abs(fill.Price-snap.FairValue) / snap.FairValue * 10000.0
			fs.SpreadCaptureBpsSum += captureBps
			fs.SpreadCaptureCount++
			if fill.Side == "buy" {
				wickMult = snap.WickBidMult
			} else {
				wickMult = snap.WickAskMult
			}
		}
	}
	m.diagMu.Unlock()

	totalFee := fill.Fee + fill.BuilderFee
	netPnL := fill.RealizedPnL - totalFee

	// P13: record fill for post-mortem dump.
	if m.postMortem != nil && m.postMortem.Enabled() {
		m.postMortem.RecordFill(FillRecord{
			Timestamp:   time.Now(),
			Coin:        fill.Coin,
			Side:        fill.Side,
			Size:        fill.Size,
			Price:       fill.Price,
			MidAtFill:   m.midForCoin(fill.Coin),
			Notional:    notional,
			Fee:         totalFee,
			RealizedPnL: fill.RealizedPnL,
			NetPnL:      netPnL,
			WallFronted: fill.WallFront,
		})
	}

	slog.Info("fill detail",
		"coin", fill.Coin,
		"side", fill.Side,
		"size", fill.Size,
		"price", fill.Price,
		"fair_value", fmt.Sprintf("%.4f", fv),
		"capture_bps", fmt.Sprintf("%.1f", captureBps),
		"notional", fmt.Sprintf("%.2f", notional),
		"fee", fmt.Sprintf("%.4f", totalFee),
		"wick_mult", fmt.Sprintf("%.2f", wickMult),
		"realized_pnl", fmt.Sprintf("%.4f", fill.RealizedPnL),
		"net_pnl", fmt.Sprintf("%.4f", netPnL),
		"closed_size", fmt.Sprintf("%.6f", fill.ClosedSize),
	)

	// V24: price sanity check. V23 fill row logged price=$45.56 while mid was $92.41 —
	// almost certainly a format/parse artifact. 50 bps gap between fill price and mid
	// is catastrophic in normal operation; worth a WARN so future anomalies surface.
	if mid := m.midForCoin(fill.Coin); mid > 0 && fill.Price > 0 {
		deviationBps := math.Abs(fill.Price-mid) / mid * 10000.0
		if deviationBps > 50.0 {
			slog.Warn("fill price anomaly: large price vs mid deviation",
				"coin", fill.Coin,
				"side", fill.Side,
				"price", fill.Price,
				"mid", fmt.Sprintf("%.4f", mid),
				"deviation_bps", fmt.Sprintf("%.1f", deviationBps),
				"threshold_bps", 50.0,
			)
		}
	}

	// Record fill for markout tracking.
	if mt := m.markouts[fill.Coin]; mt != nil {
		mid := m.midForCoin(fill.Coin)
		if mid > 0 {
			mt.RecordFillWithMeta(fill.Price, mid, fill.Side, fill.Size, fill.WallFront)
		}
	}

	// Record fill timestamp for optimizer rolling window and dynamic refresh.
	m.mu.Lock()
	now := time.Now()
	m.lastFillTime[fill.Coin] = now
	m.optFillTimes[fill.Coin] = append(m.optFillTimes[fill.Coin], now)
	switch fill.Side {
	case "buy":
		m.optBuyFillTimes[fill.Coin] = append(m.optBuyFillTimes[fill.Coin], now)
	case "sell":
		m.optSellFillTimes[fill.Coin] = append(m.optSellFillTimes[fill.Coin], now)
	}
	// P9: clear idle-fill breaker active flag on any fill; the bleed scenario
	// it targets has resolved itself for this asset.
	if state := m.idleBreakers[fill.Coin]; state != nil {
		state.activeFlag = false
	}
	m.mu.Unlock()

	// Feed fill burst tracker.
	if bt := m.fillBurstTrackers[fill.Coin]; bt != nil {
		bt.RecordFill(fill.Side)
	}

	// Feature 6: Trigger out-of-cadence optimizer evaluation on significant fill
	// burst activation. Routed through the same throttled burstEvalCh as the
	// trade-velocity burst trigger in RecordMarketTrade, so the two burst
	// sources coalesce and respect optimizer_v2.burst_eval_min_interval_sec.
	// Prior implementation spawned a goroutine per qualifying fill with no
	// throttle, which doubled optimizer work when both sources fired on the
	// same burst event.
	if bt := m.fillBurstTrackers[fill.Coin]; bt != nil {
		ov := m.cfg.ForAsset(fill.Coin).MarketMaking.OptimizerV2
		if ov.Enabled && ov.BurstEvalEnabled && ov.BurstEvalMinIntervalSec > 0 {
			buyMult, sellMult := bt.SpreadMultiplier()
			if buyMult > 1.5 || sellMult > 1.5 {
				// Atomic check-and-advance under lastOptEvalMu. See sibling
				// comment in RecordMarketTrade — this closes the same TOCTOU
				// race between the fill-burst and velocity-burst paths that
				// produced paired double-fires in v21 logs.
				interval := m.burstEvalInterval(fill.Coin, ov)
				m.lastOptEvalMu.Lock()
				if time.Since(m.lastOptEval) >= interval {
					m.lastOptEval = time.Now()
					m.lastOptEvalMu.Unlock()
					select {
					case m.burstEvalCh <- struct{}{}:
					default: // already pending
					}
				} else {
					m.lastOptEvalMu.Unlock()
				}
			}
		}
	}

	// Signal fill-triggered requote.
	select {
	case m.fillTrigger <- fill.Coin:
	default:
	}
}

// midForCoin returns the latest book mid price for a coin, or 0 if unavailable.
func (m *Manager) midForCoin(coin string) float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	if book, ok := m.latestBook[coin]; ok {
		return book.Mid
	}
	return 0
}

// computeRefreshIntervalMs returns the dynamic refresh interval in milliseconds.
// Picks the minimum across all assets after evaluating per-asset conditions.
// Must be called with m.mu held.
func (m *Manager) computeRefreshIntervalMs() int {
	now := time.Now()
	minMs := m.cfg.MarketMaking.RefreshIntervalMs

	for _, asset := range m.cfg.Strategy.Assets {
		mm := m.cfg.ForAsset(asset).MarketMaking
		baseMs := mm.RefreshIntervalMs

		// 1. Post-fill: if recent fill within 30s → use faster refresh.
		if mm.RefreshPostFillMs > 0 {
			if ft, ok := m.lastFillTime[asset]; ok && now.Sub(ft) < 30*time.Second {
				if mm.RefreshPostFillMs < baseMs {
					baseMs = mm.RefreshPostFillMs
				}
			}
		}

		// 2. Adverse: if per-side toxicity worst markout is strongly negative → slower refresh.
		if mm.RefreshAdverseMs > 0 {
			if st := m.toxicityTrackers[asset]; st != nil {
				if st.WorstMarkoutEWMA() < -1.5 {
					if mm.RefreshAdverseMs > baseMs {
						baseMs = mm.RefreshAdverseMs
					}
				}
			}
		}

		// 3. Quiet: if book data is idle past threshold → use slower refresh.
		if mm.RefreshQuietMs > 0 && mm.RefreshQuietThresholdMs > 0 {
			if lastBook, ok := m.lastBookUpdate[asset]; ok {
				if now.Sub(lastBook) > time.Duration(mm.RefreshQuietThresholdMs)*time.Millisecond {
					if mm.RefreshQuietMs > baseMs {
						baseMs = mm.RefreshQuietMs
					}
				}
			}
		}

		// 4. Optimizer refresh mult: scale interval up (slower) or down (faster).
		var refreshMult float64 = 1.0
		if optV2 := m.optimizersV2[asset]; optV2 != nil {
			refreshMult = optV2.Overlay().RefreshMult
		}
		baseMs = int(math.Round(float64(baseMs) * refreshMult))

		// 5. Rate limit allocator: per-asset ROI-based refresh scaling.
		if m.rateLimitAllocator != nil {
			baseMs = int(math.Round(float64(baseMs) * m.rateLimitAllocator.RefreshMultiplier(asset)))
		}
		if baseMs < 500 {
			baseMs = 500 // floor: never go below 500ms
		}

		if baseMs < minMs {
			minMs = baseMs
		}
	}
	return minMs
}

// FillStatsSnapshot returns fill window stats per asset and resets counters.
func (m *Manager) FillStatsSnapshot() map[string]FillWindowStats {
	m.diagMu.Lock()
	defer m.diagMu.Unlock()
	out := make(map[string]FillWindowStats, len(m.fillStats))
	for k, v := range m.fillStats {
		out[k] = *v
		*v = FillWindowStats{} // reset
	}
	return out
}

// MarkoutEWMA returns the rolling EWMA markout values for a coin, or a zero value if markout is disabled.
func (m *Manager) MarkoutEWMA(coin string) MarkoutEWMA {
	if mt := m.markouts[coin]; mt != nil {
		return mt.EWMA()
	}
	return MarkoutEWMA{}
}

func (m *Manager) computeCoinHealth(now time.Time) map[string]coinQuotingHealth {
	out := make(map[string]coinQuotingHealth, len(m.cfg.Strategy.Assets))

	var execHealth execution.HealthSnapshot
	if reporter, ok := m.executor.(execution.HealthReporter); ok {
		execHealth = reporter.HealthSnapshot()
	}

	for _, coin := range m.cfg.Strategy.Assets {
		health := coinQuotingHealth{}
		if coinHealth, ok := execHealth.Coins[coin]; ok && coinHealth.QuoteHalt {
			health.Halted = true
			health.Reason = coinHealth.Reason
			out[coin] = health
			continue
		}

		if halted, degraded := m.tradeFreshnessState(coin, now); halted {
			// On thin HIP-3 markets, trades can be legitimately sporadic.
			// If book data is still fresh, degrade (wider spreads + microprice
			// fallback) instead of halting — prevents stranding positions with
			// zero orders.
			bookFresh := false
			if t, ok := m.lastBookUpdate[coin]; ok {
				staleMs := m.cfg.ForAsset(coin).MarketMaking.MaxBookStalenessMs
				if staleMs > 0 {
					bookFresh = now.Sub(t) <= time.Duration(staleMs)*time.Millisecond
				}
			}
			if bookFresh {
				health.Degraded = true
				health.Reason = "trade_stale"
			} else {
				health.Halted = true
				health.Reason = "trade_stale"
			}
		} else if degraded {
			health.Degraded = true
			health.Reason = "trade_stale"
		}

		if !health.Halted {
			if halted, degraded := m.fillPollFreshnessState(execHealth, now); halted {
				health.Halted = true
				health.Reason = "fill_poll_stale"
			} else if degraded {
				health.Degraded = true
				if health.Reason == "" {
					health.Reason = "fill_poll_stale"
				}
			}
		}

		out[coin] = health
	}

	return out
}

func (m *Manager) tradeFreshnessState(coin string, now time.Time) (halted bool, degraded bool) {
	if m.shortVWAPs == nil {
		return false, false
	}
	calc, ok := m.shortVWAPs[coin]
	if !ok || calc == nil {
		return false, false
	}

	// Use per-asset config thresholds if set, otherwise defaults.
	haltThreshold := defaultHaltTradeStale
	degradedThreshold := defaultDegradedTradeStale
	ac := m.cfg.ForAsset(coin)
	if ac.MarketMaking.TradeStaleHaltSec > 0 {
		haltThreshold = time.Duration(ac.MarketMaking.TradeStaleHaltSec) * time.Second
	}
	if ac.MarketMaking.TradeStaleDegradedSec > 0 {
		degradedThreshold = time.Duration(ac.MarketMaking.TradeStaleDegradedSec) * time.Second
	}

	lastTrade, ok := calc.LastTradeTime()
	if !ok {
		if m.startedAt.IsZero() {
			return false, false
		}
		age := now.Sub(m.startedAt)
		return age > haltThreshold, age > degradedThreshold
	}

	age := now.Sub(lastTrade)
	return age > haltThreshold, age > degradedThreshold
}

func (m *Manager) fillPollFreshnessState(execHealth execution.HealthSnapshot, now time.Time) (halted bool, degraded bool) {
	if execHealth.LastFillPollAt.IsZero() && execHealth.LastFillPollSuccessAt.IsZero() {
		return false, false
	}

	// Derive thresholds from the configured poll interval so they can never
	// be tighter than the poll cadence.  Degraded = 2.5× poll interval
	// (tolerates one missed cycle + HTTP latency), halt = 6× poll interval.
	pollSec := m.cfg.Execution.PollFillsSec
	if pollSec <= 0 {
		pollSec = 10
	}
	degradedThreshold := time.Duration(float64(pollSec)*2.5) * time.Second
	haltThreshold := time.Duration(pollSec*6) * time.Second

	ref := execHealth.LastFillPollSuccessAt
	if ref.IsZero() {
		ref = m.startedAt
	}
	age := now.Sub(ref)
	if age > haltThreshold || execHealth.FillPollConsecutiveErrors >= 5 {
		return true, true
	}
	if age > degradedThreshold || execHealth.FillPollConsecutiveErrors >= 2 {
		return false, true
	}
	return false, false
}

// updatePairReduceMode checks whether both assets are on the same side and combined
// exposure exceeds the entry threshold. Entry/exit fractions are read from the global
// config (not per-asset) because pair-reduce is a cross-asset concept. The tighten factor
// is read per-asset in computeOrdersForCoin. Per-asset overrides of entry/exit fractions
// are silently ignored here.
func (m *Manager) updatePairReduceMode(positions map[string]types.PositionUpdate) pairReduceState {
	if !m.cfg.Strategy.UseBetaHedge || len(m.cfg.Strategy.Assets) != 2 {
		m.pairReduceActive = false
		m.pairReduceBelow = 0
		m.pairReduceSide = ""
		m.pairReduceActivatedAt = time.Time{}
		return pairReduceState{}
	}

	first := positions[m.cfg.Strategy.Assets[0]]
	second := positions[m.cfg.Strategy.Assets[1]]
	sameSide := first.Side != "" && first.Side == second.Side
	limit := math.Min(
		m.cfg.ForAsset(m.cfg.Strategy.Assets[0]).Risk.MaxSpreadExposure,
		m.cfg.ForAsset(m.cfg.Strategy.Assets[1]).Risk.MaxSpreadExposure,
	)
	if limit <= 0 {
		limit = math.MaxFloat64
	}
	combined := first.Notional + second.Notional
	entryThreshold := m.cfg.MarketMaking.PairReduceEntryFraction * limit
	exitThreshold := m.cfg.MarketMaking.PairReduceExitFraction * limit

	if sameSide && combined >= entryThreshold {
		if !m.pairReduceActive {
			m.pairReduceActivatedAt = time.Now()
		}
		m.pairReduceActive = true
		m.pairReduceBelow = 0
		m.pairReduceSide = first.Side
	} else if m.pairReduceActive {
		if !sameSide || combined < exitThreshold {
			m.pairReduceBelow++
			if m.pairReduceBelow >= 2 {
				m.pairReduceActive = false
				m.pairReduceBelow = 0
				m.pairReduceSide = ""
				m.pairReduceActivatedAt = time.Time{}
			}
		} else {
			m.pairReduceBelow = 0
		}
	}

	// Timeout: force-deactivate pair-reduce after max duration.
	// The bot's existing aggressive skew (hl_skew can be 7+, skew_bps ~25)
	// will continue to discourage re-entry fills after timeout.
	// Pair-reduce timeout uses global config (cross-asset concept; per-asset overrides not applicable).
	maxDur := m.cfg.MarketMaking.PairReduceMaxDurationSec
	if m.pairReduceActive && maxDur > 0 && !m.pairReduceActivatedAt.IsZero() {
		if time.Since(m.pairReduceActivatedAt) > time.Duration(maxDur)*time.Second {
			slog.Warn("pair_reduce timeout: force-deactivating after max duration",
				"duration_sec", maxDur,
				"side", m.pairReduceSide,
				"combined_notional", fmt.Sprintf("%.0f", combined),
			)
			m.pairReduceActive = false
			m.pairReduceBelow = 0
			m.pairReduceSide = ""
			m.pairReduceActivatedAt = time.Time{}
		}
	}

	if !m.pairReduceActive {
		return pairReduceState{}
	}

	exitSide := "sell"
	if m.pairReduceSide == "short" {
		exitSide = "buy"
	}
	return pairReduceState{
		Active:   true,
		ExitSide: exitSide,
	}
}

// Run processes book updates, spread signals, and risk actions. Blocks until ctx is cancelled.
func (m *Manager) Run(ctx context.Context) {
	m.mu.Lock()
	m.startedAt = time.Now()
	m.mu.Unlock()

	for _, asset := range m.cfg.Strategy.Assets {
		ac := m.cfg.ForAsset(asset)
		slog.Info("quote manager started",
			"coin", asset,
			"spread_bps", ac.MarketMaking.QuoteSpreadBps,
			"notional", ac.MarketMaking.QuoteNotional,
			"refresh_ms", ac.MarketMaking.RefreshIntervalMs,
			"leverage", ac.Execution.Leverage,
			"max_position_notional", ac.Risk.MaxPositionNotional,
		)
	}

	// Start funding tracker background polling if configured.
	if m.fundingTracker != nil && m.fundingTracker.info != nil {
		go m.fundingTracker.Start(ctx)
	}

	// Optimizer evaluation ticker — derive interval from v2 configs.
	// Use the minimum configured interval across all v2-enabled assets.
	optInterval := time.Duration(0)
	anyV2Enabled := false
	for _, asset := range m.cfg.Strategy.Assets {
		ac := m.cfg.ForAsset(asset)
		if ac.MarketMaking.OptimizerV2.Enabled {
			anyV2Enabled = true
			v2Interval := time.Duration(ac.MarketMaking.OptimizerV2.EvalIntervalSec) * time.Second
			if v2Interval > 0 && (optInterval == 0 || v2Interval < optInterval) {
				optInterval = v2Interval
			}
		}
	}
	if optInterval == 0 {
		optInterval = 60 * time.Second // fallback default
	}
	optTicker := time.NewTicker(optInterval)
	defer optTicker.Stop()
	if !anyV2Enabled {
		optTicker.Stop()
	}

	// reconnectCh may be nil; a nil channel never fires in select.
	reconnectCh := m.reconnectCh

	// V30 follow-up: poll supervisor for shock-chain external halt/recovery
	// transitions so the pause→recovery→normal edges get logged. The
	// supervisor resolves these via timestamp deadlines with no timer
	// goroutine, so without this poller the "shock chain pause expired"
	// events the monitor parses would never fire.
	extTransTicker := time.NewTicker(500 * time.Millisecond)
	defer extTransTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("quote manager stopped")
			for coin, mt := range m.markouts {
				if mt != nil && mt.PendingCount() > 0 {
					slog.Info("markout: abandoning pending entries on shutdown",
						"coin", coin, "pending", mt.PendingCount())
				}
			}
			return

		case <-extTransTicker.C:
			m.supervisor.CheckExternalTransitions()

		case update := <-m.bookCh:
			m.mu.Lock()
			m.latestBook[update.Coin] = update
			m.lastBookUpdate[update.Coin] = time.Now()
			if micro := m.micros[update.Coin]; micro != nil {
				micro.Update(update.Coin, update)
			}
			if regime := m.regimes[update.Coin]; regime != nil {
				regime.Update(update.Mid)
			}
			m.mu.Unlock()
			m.recordPriceShock(ctx, update)
			if mt := m.markouts[update.Coin]; mt != nil {
				results := mt.OnBookUpdate(update.Mid, time.Now())
				for _, r := range results {
					slog.Info("markout",
						"coin", update.Coin,
						"side", r.Side,
						"fill_px", fmt.Sprintf("%.4f", r.FillPrice),
						"mid_at_fill", fmt.Sprintf("%.4f", r.MidAtFill),
						"size", fmt.Sprintf("%.4f", r.Size),
						"T500ms_bps", fmt.Sprintf("%.2f", r.Markouts[0]),
						"T1s_bps", fmt.Sprintf("%.2f", r.Markouts[1]),
						"T2s_bps", fmt.Sprintf("%.2f", r.Markouts[2]),
						"T5s_bps", fmt.Sprintf("%.2f", r.Markouts[3]),
						"wall_front", r.WallFront,
					)
					// Compute blended markout for toxicity input.
					// Default: pure T1s (weight=0). With toxicity_t5s_weight>0,
					// blends in T5s to catch slow-developing adverse selection
					// that only manifests between 1s and 5s post-fill.
					ac := m.cfg.ForAsset(update.Coin)
					t5w := ac.MarketMaking.ToxicityT5sWeight
					blendedMarkout := (1-t5w)*r.Markouts[1] + t5w*r.Markouts[3]

					// Feed blended markout into per-side toxicity tracker (B2).
					// Use volume-weighted recording: larger fills have more impact on EWMA.
					if st := m.toxicityTrackers[update.Coin]; st != nil {
						refNotional := ac.MarketMaking.QuoteNotional
						fillNotional := r.Size * r.FillPrice
						st.RecordMarkoutWeighted(r.Side, blendedMarkout, fillNotional, refNotional)
					}
					// P6: per-side win rate / profit factor distribution-shape signal.
					// Uses the 5s markout horizon (index 3) — the most actionable for
					// market-making. Sign is already side-adjusted by MarkoutTracker
					// (positive = good for the maker regardless of side).
					if ps := m.perSideStats[update.Coin]; ps != nil {
						ps.Record(r.Side, r.Markouts[3], r.Size*r.FillPrice)
					}
					if r.WallFront {
						if wq := m.wallQualityTrackers[update.Coin]; wq != nil {
							score, disabledUntil, disabled, transition := wq.RecordMarkout(r.Side, r.Markouts[3], time.Now())
							slog.Info("wall-front quality update",
								"coin", update.Coin,
								"side", r.Side,
								"score", fmt.Sprintf("%.2f", score),
								"T5s_bps", fmt.Sprintf("%.2f", r.Markouts[3]),
								"disabled", disabled,
								"disabled_until", disabledUntil,
							)
							if transition != nil {
								slog.Info("wall_quality state",
									"coin", update.Coin,
									"side", transition.Side,
									"from", transition.From,
									"to", transition.To,
									"score", fmt.Sprintf("%.2f", transition.Score),
									"held_sec", fmt.Sprintf("%.1f", transition.HeldSec),
								)
							}
						}
					}

					// Emergency widen bypass: if 3 consecutive fills on the same side
					// have markout below threshold, immediately widen that side.
					streakKey := update.Coin + ":" + r.Side
					if m.cfg.SignalPauses() && r.Markouts[1] < ac.MarketMaking.EmergencyWidenThresholdBps {
						m.adverseStreaks[streakKey]++
						if m.adverseStreaks[streakKey] >= 3 {
							if optV2 := m.optimizersV2[update.Coin]; optV2 != nil {
								optV2.EmergencyWiden(r.Side, ac.MarketMaking.EmergencyWidenAmountBps)
								cancelCtx, cancelCancel := context.WithTimeout(ctx, 3*time.Second)
								if err := m.executor.CancelSide(cancelCtx, update.Coin, r.Side, "markout_emergency"); err != nil {
									slog.Error("markout emergency side cancel failed",
										"coin", update.Coin,
										"side", r.Side,
										"error", err,
									)
								}
								cancelCancel()
								if ac.MarketMaking.OptimizerV2.ForceRefreshOnEmergency {
									m.markForceRefresh(update.Coin, "markout_emergency")
								}
							}
							m.adverseStreaks[streakKey] = 0 // reset after firing
						}
					} else {
						m.adverseStreaks[streakKey] = 0
					}
				}
			}
			m.maybeRefreshQuotes(ctx)

		case sig := <-m.signalCh:
			m.mu.Lock()
			m.latestSignal = sig
			m.mu.Unlock()
			m.maybeRefreshQuotes(ctx)

		case action := <-m.riskCh:
			m.handleRiskAction(ctx, action)

		case filledCoin := <-m.fillTrigger:
			m.maybeRefreshFromFill(ctx, filledCoin)

		case <-optTicker.C:
			if reqs := m.evaluateOptimizers(); len(reqs) > 0 {
				m.markForceRefreshes(reqs)
				m.maybeRefreshQuotes(ctx)
			}

		case <-m.burstEvalCh:
			// V20 (second bundle): out-of-cadence optimizer eval fired by a new
			// volume-burst activation. Throttled at the send site by
			// optimizer_v2.burst_eval_min_interval_sec.
			slog.Info("optimizer burst-triggered eval")
			if reqs := m.evaluateOptimizers(); len(reqs) > 0 {
				m.markForceRefreshes(reqs)
				m.maybeRefreshQuotes(ctx)
			}

		case <-reconnectCh:
			m.mu.Lock()
			m.reconnectCooldownEnd = time.Now().Add(1 * time.Second)
			m.mu.Unlock()
			slog.Info("quote manager: WS reconnect detected, suppressing quoting for 1s until fresh book data arrives")
		}
	}
}

// evaluateOptimizers runs a single evaluation cycle for all per-asset optimizers.
func (m *Manager) evaluateOptimizers() []forceRefreshRequest {
	m.lastOptEvalMu.Lock()
	m.lastOptEval = time.Now()
	m.lastOptEvalMu.Unlock()
	m.mu.Lock()
	defer m.mu.Unlock()

	var forceReqs []forceRefreshRequest
	now := time.Now()
	for _, asset := range m.cfg.Strategy.Assets {
		optV2 := m.optimizersV2[asset]
		if optV2 == nil {
			continue
		}

		ac := m.cfg.ForAsset(asset)
		supState := m.supervisor.State()
		halted := supState >= OpCancelOnly

		// Markout signals from per-side toxicity tracker.
		// Optimizer inputs use the RAW (undecayed) EWMAs — idle decay toward
		// baseline would starve the optimizer of toxicity signal exactly
		// when quotes stop getting filled due to adverse selection (v22.1
		// session 21:07–21:22 saw both sides asymptote to baseline=8.0 during
		// a 15-min drawdown; tox_markout consequently lost all resolution).
		// The decayed MarkoutEWMA path is still used for FV feedback and
		// refresh-timing callers where fading to baseline is desirable.
		baseline := m.resolveBaselineBps(ac)
		var worstMarkout1s float64
		buyMarkout := baseline
		sellMarkout := baseline
		if tox := m.toxicityTrackers[asset]; tox != nil {
			worstMarkout1s = tox.WorstMarkoutEWMARaw()
			if bm, ready := tox.RawMarkoutEWMA("buy"); ready {
				buyMarkout = bm
			}
			if sm, ready := tox.RawMarkoutEWMA("sell"); ready {
				sellMarkout = sm
			}
			// Override worst-markout when per-side values have moved off
			// baseline (i.e. at least one side produced a real observation).
			if buyMarkout != baseline || sellMarkout != baseline {
				worstMarkout1s = math.Min(buyMarkout, sellMarkout)
			}
		}

		// Multi-horizon composite markout from MarkoutTracker.
		var compositeMarkout float64
		if mt := m.markouts[asset]; mt != nil {
			mewma := mt.EWMA()
			if mewma.Count > 0 {
				w := ac.MarketMaking.MarkoutCompositeWeights
				compositeMarkout = w[0]*mewma.Values[0] + w[1]*mewma.Values[1] + w[2]*mewma.Values[2] + w[3]*mewma.Values[3]
			}
		}
		// Fallback: if markout tracker has no composite, use the per-side worst.
		if compositeMarkout == 0 && buyMarkout == 0 && sellMarkout == 0 {
			compositeMarkout = worstMarkout1s
		}

		// Fill rate ratio.
		var fillRateRatio, buyFillRateRatio, sellFillRateRatio float64
		if ft := m.fillRates[asset]; ft != nil {
			if nm := ft.NotionalMultiplier(); nm > 0 {
				fillRateRatio = 1.0 / nm
			}
			if buyMult := ft.NotionalMultiplierSide("buy"); buyMult > 0 {
				buyFillRateRatio = 1.0 / buyMult
			}
			if sellMult := ft.NotionalMultiplierSide("sell"); sellMult > 0 {
				sellFillRateRatio = 1.0 / sellMult
			}
		}

		// Inventory utilization.
		pos := m.executor.Position(asset)
		maxPos := ac.Risk.MaxPositionNotional
		var inventoryUtil float64
		if maxPos > 0 {
			inventoryUtil = pos.Notional / maxPos
		}

		// Inventory persistence — threshold sourced from v2 policy thresholds.
		invHighThreshold := ac.MarketMaking.OptimizerV2.PolicyThresholds.InventoryHigh
		if invHighThreshold == 0 {
			invHighThreshold = 0.4
		}
		var inventoryPersistSec float64
		if inventoryUtil > invHighThreshold {
			if since, ok := m.inventoryHighSince[asset]; ok && !since.IsZero() {
				inventoryPersistSec = time.Since(since).Seconds()
			} else {
				m.inventoryHighSince[asset] = time.Now()
			}
		} else {
			m.inventoryHighSince[asset] = time.Time{}
		}

		// Autocorrelation.
		var autocorr float64
		if rd := m.regimes[asset]; rd != nil {
			if a, ok := rd.Autocorrelation(); ok {
				autocorr = a
			}
		}

		// Vol ratio.
		var volRatio float64
		if micro := m.micros[asset]; micro != nil {
			volRatio = micro.VolMultiplier(asset)
		}

		// Fill sample count (total + per-side).
		var fillSamples, buyFillSamples, sellFillSamples int
		windowSec := ac.MarketMaking.OptimizerV2.MinFillSampleWindowSec
		if windowSec > 0 {
			cutoff := time.Now().Add(-time.Duration(windowSec) * time.Second)
			times := m.optFillTimes[asset]
			i := 0
			for i < len(times) && times[i].Before(cutoff) {
				i++
			}
			m.optFillTimes[asset] = times[i:]
			fillSamples = len(m.optFillTimes[asset])

			buyTimes := m.optBuyFillTimes[asset]
			bi := 0
			for bi < len(buyTimes) && buyTimes[bi].Before(cutoff) {
				bi++
			}
			m.optBuyFillTimes[asset] = buyTimes[bi:]
			buyFillSamples = len(m.optBuyFillTimes[asset])

			sellTimes := m.optSellFillTimes[asset]
			si := 0
			for si < len(sellTimes) && sellTimes[si].Before(cutoff) {
				si++
			}
			m.optSellFillTimes[asset] = sellTimes[si:]
			sellFillSamples = len(m.optSellFillTimes[asset])
		} else {
			fillSamples = len(m.optFillTimes[asset])
			buyFillSamples = len(m.optBuyFillTimes[asset])
			sellFillSamples = len(m.optSellFillTimes[asset])
			m.optFillTimes[asset] = m.optFillTimes[asset][:0]
			m.optBuyFillTimes[asset] = m.optBuyFillTimes[asset][:0]
			m.optSellFillTimes[asset] = m.optSellFillTimes[asset][:0]
		}

		// Fill burst state.
		var burstActive bool
		if bt := m.fillBurstTrackers[asset]; bt != nil {
			buyMult, sellMult := bt.SpreadMultiplier()
			burstActive = buyMult > 1.01 || sellMult > 1.01
		}

		// Emergency widen streak count.
		emergencyCount := 0
		if s := m.adverseStreaks[asset+":buy"]; s > 0 {
			emergencyCount += s
		}
		if s := m.adverseStreaks[asset+":sell"]; s > 0 {
			emergencyCount += s
		}

		v2in := OptimizerV2Inputs{
			WorstMarkoutBps:     worstMarkout1s,
			BuyMarkoutBps:       buyMarkout,
			SellMarkoutBps:      sellMarkout,
			CompositeMarkoutBps: compositeMarkout,
			BaselineBps:         m.resolveBaselineBps(ac),
			FillRateRatio:       fillRateRatio,
			BuyFillRateRatio:    buyFillRateRatio,
			SellFillRateRatio:   sellFillRateRatio,
			FillSamples:         fillSamples,
			BuyFillSamples:      buyFillSamples,
			SellFillSamples:     sellFillSamples,
			FillBurstActive:     burstActive,
			EmergencyCount:      emergencyCount,
			InventoryUtil:       inventoryUtil,
			SameSideDurationS:   inventoryPersistSec,
			Autocorr:            autocorr,
			VolRatio:            volRatio,
			Halted:              halted,
			RLDegraded:          supState == OpDegraded,
			RLCritical:          supState >= OpCancelOnly,
		}
		_, v2in.SessionPnLBps = m.sessionPnL.PnL()

		// P8: position-level unrealized PnL in bps of equity. Direction-aware:
		// positive when the position is in profit, negative in loss. Used by the
		// opp_tighten gate to block tightening while the current inventory is
		// underwater.
		if pos.Size != 0 && pos.AvgEntry > 0 {
			equity := m.executor.Equity()
			if equity > 0 {
				if b, ok := m.latestBook[asset]; ok && b.Mid > 0 {
					var unrealized float64
					switch pos.Side {
					case "long":
						unrealized = pos.Size * (b.Mid - pos.AvgEntry)
					case "short":
						unrealized = pos.Size * (pos.AvgEntry - b.Mid)
					}
					v2in.PositionUnrealizedBps = unrealized / equity * 10000.0
				}
			}
		}

		// Budget pressure blended from two sources:
		//   (a) headroom-based: virt cycle pressure = (1 - regen/burn ratio)/2
		//   (b) depletion-based: linear ramp on absolute remaining/cap %,
		//       from 0 at BudgetPressurePctStart down to 1.0 at RemainingPctCritical
		// Take max() so either signal can force the optimizer to throttle.
		// Motivation: when fills regen budget at the same rate it burns,
		// headroom reads 1.0 and virt-cycle pressure reads 0 regardless of
		// how close the absolute remaining pool is to the Critical floor.
		// The depletion ramp closes that blind spot between the warn
		// threshold and the cancel-only Critical trip.
		var headroomBp, depletionBp float64
		if m.virtCycle != nil {
			headroomBp = m.virtCycle.BudgetPressure()
		}
		rl := m.cfg.RateLimit
		if rl.BudgetPressurePctStart > 0 && m.rateLimitCap > 0 {
			pctRemaining := m.rateLimitRemainingPctLocked(m.rateLimitRemaining)
			span := rl.BudgetPressurePctStart - rl.RemainingPctCritical
			if span > 0 && pctRemaining < rl.BudgetPressurePctStart {
				depletionBp = (rl.BudgetPressurePctStart - pctRemaining) / span
				if depletionBp < 0 {
					depletionBp = 0
				} else if depletionBp > 1 {
					depletionBp = 1
				}
			}
		}
		if rl.Disabled {
			v2in.BudgetPressure = 0
		} else if depletionBp > headroomBp {
			v2in.BudgetPressure = depletionBp
		} else {
			v2in.BudgetPressure = headroomBp
		}
		// Inventory controller adjustment. ControllerAdj is the raw skew factor
		// clamped to [skewMin, skewMax]; the optimizer's inv_ctrl sub-dim is
		// normalized over this configured range. Passing the bounds explicitly
		// prevents the saturation seen in v18 (inv_ctrl pegged at 1.0 on 93 %
		// of evals with the legacy [0.3, 3.0] mapping).
		if ic := m.inventoryControllers[asset]; ic != nil {
			v2in.ControllerAdj = ic.EffectiveSkewFactor(1.0)
			v2in.ControllerSkewMin = ac.MarketMaking.InventoryControllerSkewMin
			v2in.ControllerSkewMax = ac.MarketMaking.InventoryControllerSkewMax
		}
		// Position side for inventory stress.
		if pos.Side == "long" {
			v2in.InventorySide = "buy"
		} else if pos.Side == "short" {
			v2in.InventorySide = "sell"
		}
		// VWAP deviation for opportunity.
		if m.shortVWAPs != nil {
			if calc, ok := m.shortVWAPs[asset]; ok && calc != nil {
				if vwap, ready := calc.VWAP(); ready {
					bookMid := 0.0
					if b, ok := m.latestBook[asset]; ok {
						bookMid = b.Mid
					}
					if bookMid > 0 {
						v2in.VWAPDeviation = math.Abs(bookMid-vwap) / bookMid
					}
				}
			}
		}
		v2in.WickThreshold = ac.MarketMaking.WickDevThresholdPct / 100.0
		// Spread-to-BBO ratio.
		if b, ok := m.latestBook[asset]; ok && b.BestAsk > b.BestBid && b.Mid > 0 {
			bboSpread := (b.BestAsk - b.BestBid) / b.Mid * 10000.0
			ourSpread := math.Max(ac.MarketMaking.QuoteSpreadBps,
				math.Max(ac.MarketMaking.QuoteSpreadBpsBuy, ac.MarketMaking.QuoteSpreadBpsSell))
			if bboSpread > 0 {
				v2in.SpreadBBORatio = ourSpread / bboSpread
			}
		}
		// Cross-asset signals.
		if len(m.cfg.Strategy.Assets) >= 2 {
			for _, peerAsset := range m.cfg.Strategy.Assets {
				if peerAsset == asset {
					continue
				}
				peerPos := m.executor.Position(peerAsset)
				peerMaxPos := m.cfg.ForAsset(peerAsset).Risk.MaxPositionNotional
				if peerMaxPos > 0 {
					v2in.PeerInventoryUtil = peerPos.Notional / peerMaxPos
				}
				v2in.PeerSameDirection = (pos.Side == peerPos.Side && pos.Side != "")
				break
			}
			v2in.PairReduceActive = m.pairReduceActive
		}
		// Trade flow inputs.
		if calc, ok := m.imbalanceCalcs[asset]; ok && calc != nil {
			v2in.ImbalanceSaturationPct = calc.SaturationRatio()
		}

		// P6: per-side win rate / profit factor inputs.
		if ac.MarketMaking.PerSideWinrateEnabled {
			v2in.PerSideWinrateEnabled = true
			v2in.PerSideWinrateWindow = ac.MarketMaking.PerSideWinrateWindowFills
			v2in.PerSideWinrateThreshold = ac.MarketMaking.PerSideWinrateThreshold
			v2in.PerSideProfitFactorThreshold = ac.MarketMaking.PerSideProfitFactorThreshold
			v2in.PerSideWinrateWidenBps = ac.MarketMaking.PerSideWinrateWidenBps
			if ps := m.perSideStats[asset]; ps != nil {
				v2in.BuyWinRate = ps.WinRate("buy")
				v2in.SellWinRate = ps.WinRate("sell")
				v2in.BuyProfitFactor = ps.ProfitFactor("buy")
				v2in.SellProfitFactor = ps.ProfitFactor("sell")
				v2in.BuyPnLSamples = ps.Samples("buy")
				v2in.SellPnLSamples = ps.Samples("sell")
			}
		}

		before := optV2.Overlay()
		changed := optV2.Evaluate(v2in)
		after := optV2.Overlay()

		// P13: capture eval snapshot for post-mortem.
		if m.postMortem != nil && m.postMortem.Enabled() {
			snap := optV2.Snapshot()
			esnap := EvalSnapshot{
				Timestamp: time.Now(),
				Coin:      asset,
				Emergency: optV2.EmergencyActive(),
				EvalCount: optV2.EvalCount(),
			}
			if knobs, ok := snap["knobs"].(map[string]float64); ok {
				esnap.Knobs = knobs
			}
			if st, ok := snap["state"].(map[string]float64); ok {
				esnap.State = st
			}
			if changed > 0 {
				esnap.Fired = []string{fmt.Sprintf("changed=%d", changed)}
			}
			m.postMortem.RecordEval(esnap)
		}

		ov := ac.MarketMaking.OptimizerV2
		minInterval := time.Duration(ov.ForceRefreshMinIntervalMs) * time.Millisecond
		if minInterval <= 0 {
			minInterval = 3 * time.Second
		}
		if ov.ForceRefreshOnEmergency && changed > 0 && optV2.EmergencyActive() &&
			overlayMateriallyChanged(before, after, ov.ForceRefreshMinSpreadDeltaBps) &&
			now.Sub(m.lastForceRefresh[asset]) >= minInterval {
			m.lastForceRefresh[asset] = now
			forceReqs = append(forceReqs, forceRefreshRequest{
				coin:   asset,
				reason: "optimizer_emergency",
			})
		}
	}

	// Rate limit allocator evaluation (cross-asset).
	if m.rateLimitAllocator != nil {
		m.rateLimitAllocator.Evaluate()
	}
	return forceReqs
}

// maybeRefreshFromFill handles fill-triggered requotes with cooldown rate limiting.
func (m *Manager) maybeRefreshFromFill(ctx context.Context, filledCoin string) {
	m.mu.Lock()
	if m.refreshingAll {
		m.fillPending = true
		m.fillPendingCoin = filledCoin
		m.mu.Unlock()
		return
	}
	if m.supervisor.IsRiskHalted() || m.supervisor.IsRefreshFailed() || m.supervisor.IsRLCritical() {
		m.mu.Unlock()
		return
	}

	// Apply fill-specific cooldown.
	cooldownMs := m.cfg.MarketMaking.FillRefreshCooldownMs
	for _, asset := range m.cfg.Strategy.Assets {
		if ms := m.cfg.ForAsset(asset).MarketMaking.FillRefreshCooldownMs; ms < cooldownMs {
			cooldownMs = ms
		}
	}
	cooldown := time.Duration(cooldownMs) * time.Millisecond
	if !m.lastFillRefresh.IsZero() && time.Since(m.lastFillRefresh) < cooldown {
		m.mu.Unlock()
		return
	}
	m.lastFillRefresh = time.Now()
	m.fillTriggered = true
	m.fillTriggeredCoin = filledCoin
	m.mu.Unlock()

	slog.Info("fill-triggered requote", "coin", filledCoin)
	m.maybeRefreshQuotes(ctx)
}

func (m *Manager) handleRiskAction(ctx context.Context, action types.RiskAction) {
	slog.Info("quote manager received risk action", "action", action)

	switch action {
	case types.RiskHalt:
		m.supervisor.Signal(SignalRiskHalt)
		m.mu.Lock()
		m.sessionStopRecovery = false
		m.mu.Unlock()
		// P13: post-mortem dump on entering Flatten via risk halt.
		m.maybeDumpOnFlatten("risk_halt")
		// Cancel all orders and close positions with timeout.
		cancelCtx, cancelCancel := context.WithTimeout(ctx, cancelTimeoutRiskAction)
		for _, asset := range m.cfg.Strategy.Assets {
			if err := m.executor.CancelAll(cancelCtx, asset); err != nil {
				if cancelCtx.Err() != nil {
					slog.Warn("cancel timed out on halt, will retry next risk cycle", "asset", asset)
					break
				}
				slog.Error("failed to cancel orders on halt", "asset", asset, "error", err)
			}
			if err := m.executor.ClosePosition(cancelCtx, asset, "risk_halt"); err != nil {
				if cancelCtx.Err() != nil {
					slog.Warn("close timed out on halt, will retry next risk cycle", "asset", asset)
					break
				}
				slog.Error("failed to close position on halt", "asset", asset, "error", err)
			}
		}
		cancelCancel()

	case types.RiskSessionStopLoss:
		m.supervisor.Signal(SignalRiskHalt)
		m.mu.Lock()
		m.sessionStopRecovery = false
		m.mu.Unlock()
		m.maybeDumpOnFlatten("session_stop_loss")
		cancelCtx, cancelCancel := context.WithTimeout(ctx, cancelTimeoutRiskAction)
		for _, asset := range m.cfg.Strategy.Assets {
			if err := m.executor.CancelAll(cancelCtx, asset); err != nil {
				if cancelCtx.Err() != nil {
					slog.Warn("cancel timed out on session-stop, will retry next risk cycle", "asset", asset)
					break
				}
				slog.Error("failed to cancel orders on session-stop", "asset", asset, "error", err)
			}
			if err := m.executor.ClosePosition(cancelCtx, asset, "session_stop_loss"); err != nil {
				if cancelCtx.Err() != nil {
					slog.Warn("close timed out on session-stop, will retry next risk cycle", "asset", asset)
					break
				}
				slog.Error("failed to close position on session-stop", "asset", asset, "error", err)
			}
		}
		cancelCancel()

	case types.RiskResumeFromSessionStop:
		if m.supervisor.IsRefreshFailed() {
			slog.Warn("ignoring risk resume while operationally halted")
			return
		}
		dur := m.cfg.Risk.SessionStopLossRecoveryDurationSec
		notionalPct := m.cfg.Risk.SessionStopLossRecoveryNotionalPct
		if dur > 0 {
			slog.Info("entering recovery mode after session stop-loss resume",
				"duration_sec", dur,
				"notional_pct", notionalPct,
			)
			m.mu.Lock()
			m.sessionStopRecovery = true
			m.mu.Unlock()
			m.supervisor.SignalRecovery(time.Duration(dur) * time.Second)
		} else {
			m.supervisor.Signal(SignalRiskResume)
		}
		// Reset session PnL tracker so a fresh slate is measured against the
		// post-halt equity — a failed recovery can re-trigger the stop.
		if m.sessionPnL != nil {
			m.sessionPnL.Reset(m.executor.Equity(), time.Now())
			slog.Info("session stop-loss reset")
		}
		slog.Info("quoting resumed (session stop-loss recovery)")

	case types.RiskResume:
		if m.supervisor.IsRefreshFailed() {
			slog.Warn("ignoring risk resume while operationally halted")
			return
		}
		// Defensive: clear session-stop recovery flag on any risk resume. The
		// flag is also cleared on RiskHalt, RiskSessionStopLoss, and
		// RiskResumeFromDrawdown; add RiskResume for completeness so a stale
		// "true" from a prior session-stop never lingers across a plain resume.
		m.mu.Lock()
		m.sessionStopRecovery = false
		m.mu.Unlock()
		m.supervisor.Signal(SignalRiskResume)
		slog.Info("quoting resumed (risk, non-drawdown)")

	case types.RiskResumeFromDrawdown:
		if m.supervisor.IsRefreshFailed() {
			slog.Warn("ignoring risk resume while operationally halted")
			return
		}
		m.mu.Lock()
		m.sessionStopRecovery = false
		m.mu.Unlock()
		// Enter recovery mode: quote at reduced size and wider spreads
		// to limit re-entry shock after a drawdown halt.
		if dur := m.cfg.Risk.RecoveryDurationSec; dur > 0 {
			slog.Info("entering recovery mode after drawdown resume",
				"duration_sec", dur,
				"notional_pct", m.cfg.Risk.RecoveryNotionalPct,
				"spread_mult", m.cfg.Risk.RecoverySpreadMultiplier,
			)
			m.supervisor.SignalRecovery(time.Duration(dur) * time.Second)
		} else {
			m.supervisor.Signal(SignalRiskResume)
		}
		slog.Info("quoting resumed (risk, drawdown recovery)")
	}
}

// maybeRefreshQuotes computes desired orders, compares them to the existing book,
// and refreshes only the assets whose quotes are stale or materially changed.
func (m *Manager) maybeRefreshQuotes(ctx context.Context) {
	m.mu.Lock()
	if m.refreshingAll {
		m.mu.Unlock()
		return
	}
	// Rate limit circuit breaker: block ALL refreshes when Critical.
	if m.supervisor.IsRLCritical() {
		m.mu.Unlock()
		return
	}
	// WS reconnect cooldown: suppress quoting until fresh book data arrives.
	if !m.reconnectCooldownEnd.IsZero() && time.Now().Before(m.reconnectCooldownEnd) {
		m.mu.Unlock()
		return
	}
	if m.supervisor.IsRefreshFailed() {
		if !m.shouldAttemptAutoRecovery() {
			m.mu.Unlock()
			return
		}
		retries := m.failClosedRetries
		m.mu.Unlock()
		m.supervisor.Signal(SignalRefreshRecovered)
		slog.Info("attempting auto-recovery from fail-closed halt",
			"retry_count", retries,
		)
		m.mu.Lock()
	}
	if m.supervisor.IsRiskHalted() || m.supervisor.IsRefreshFailed() {
		m.mu.Unlock()
		return
	}
	forceReasons := make(map[string]string, len(m.forceRefreshReasons))
	for coin, reason := range m.forceRefreshReasons {
		forceReasons[coin] = reason
	}
	forceActive := len(forceReasons) > 0

	// Check book staleness: if any asset's book data is older than its threshold,
	// cancel all orders once and skip quoting until fresh data arrives.
	{
		for _, asset := range m.cfg.Strategy.Assets {
			staleMs := m.cfg.ForAsset(asset).MarketMaking.MaxBookStalenessMs
			if staleMs <= 0 {
				continue
			}
			staleDuration := time.Duration(staleMs) * time.Millisecond
			t, ok := m.lastBookUpdate[asset]
			if !ok || time.Since(t) > staleDuration {
				alreadySuppressed := m.supervisor.IsBookStale()
				ageMs := int64(-1)
				if ok {
					ageMs = time.Since(t).Milliseconds()
				}

				// Feed death escalation: if already stale-suppressed and an explicit
				// max_no_book_update_ms is configured, check if the outage has
				// escalated to feed death (close positions too).
				if alreadySuppressed {
					deadMs := m.cfg.ForAsset(asset).MarketMaking.MaxNoBookUpdateMs
					if deadMs <= 0 {
						// No explicit feed death threshold — stale suppression is sufficient.
						m.mu.Unlock()
						return
					}
					if ok && time.Since(t) > time.Duration(deadMs)*time.Millisecond {
						alreadyDead := m.supervisor.IsFeedDead()
						m.mu.Unlock()
						if !alreadyDead {
							m.supervisor.Signal(SignalFeedDead)
							// P13: post-mortem dump on feed death (Flatten via SignalFeedDead).
							m.maybeDumpOnFlatten("feed_death")
							slog.Error("feed death detected, cancelling orders and closing positions",
								"coin", asset,
								"age_ms", ageMs,
								"max_no_book_update_ms", deadMs,
							)
							deadCtx, deadCancel := context.WithTimeout(ctx, cancelTimeoutRiskAction)
							for _, a := range m.cfg.Strategy.Assets {
								if err := m.executor.CancelAll(deadCtx, a); err != nil {
									slog.Error("feed death: failed to cancel orders", "asset", a, "error", err)
								}
								if err := m.executor.ClosePosition(deadCtx, a, "feed_death"); err != nil {
									slog.Error("feed death: failed to close position", "asset", a, "error", err)
								}
							}
							deadCancel()
						}
						return
					}
					m.mu.Unlock()
					return
				}
				m.mu.Unlock()
				m.supervisor.Signal(SignalBookStale)
				slog.Warn("book data stale, cancelling orders and suppressing quotes",
					"coin", asset,
					"age_ms", ageMs,
					"max_staleness_ms", staleMs,
				)
				for _, a := range m.cfg.Strategy.Assets {
					if err := m.executor.CancelAll(ctx, a); err != nil {
						slog.Error("failed to cancel orders on stale book", "asset", a, "error", err)
					}
				}
				return
			}
		}

		if m.supervisor.IsBookStale() || m.supervisor.IsFeedDead() {
			slog.Info("book data fresh again, resuming quoting")
			if m.supervisor.IsFeedDead() {
				slog.Info("feed recovered from death")
			}
			m.supervisor.Signal(SignalFeedRecover)
		}
	}

	// Use dynamic refresh interval (A.7) — minimum across all assets.
	// Fill-triggered refreshes bypass the normal rate limiter.
	if !m.fillTriggered && !forceActive {
		minRefreshMs := m.computeRefreshIntervalMs()
		// Rate-limit circuit breaker throttle.
		throttleMult := 1.0
		switch m.supervisor.RLTier() {
		case RLTierCritical:
			// Critical: no refreshes at all.
			m.mu.Unlock()
			return
		case RLTierDegraded:
			throttleMult = 2.0
		case RLTierCaution:
			throttleMult = 1.5
		}
		minInterval := time.Duration(float64(minRefreshMs)*throttleMult) * time.Millisecond
		if !m.lastRefreshAll.IsZero() && time.Since(m.lastRefreshAll) < minInterval {
			// Emergency bypass: if any asset's mid has moved significantly
			// from where we last quoted, bypass the rate limiter to prevent
			// stale orders during fast moves (e.g. crashes).
			bypass := false
			for coin, book := range m.latestBook {
				lastMid, ok := m.lastRefreshMid[coin]
				if !ok || lastMid == 0 || book.Mid == 0 {
					continue
				}
				thresholdBps := float64(m.cfg.ForAsset(coin).MarketMaking.RequoteThresholdBps) * 5
				if thresholdBps == 0 {
					thresholdBps = 10
				}
				midDevBps := math.Abs(book.Mid-lastMid) / lastMid * 10000
				if midDevBps > thresholdBps {
					slog.Info("emergency requote: mid deviation exceeds threshold",
						"coin", coin,
						"last_mid", fmt.Sprintf("%.4f", lastMid),
						"current_mid", fmt.Sprintf("%.4f", book.Mid),
						"deviation_bps", fmt.Sprintf("%.1f", midDevBps),
						"threshold_bps", fmt.Sprintf("%.0f", thresholdBps),
					)
					bypass = true
					break
				}
			}
			// Suppress emergency requotes at Degraded tier to conserve rate limit budget.
			if bypass && m.supervisor.RLTier() >= RLTierDegraded {
				bypass = false
			}
			if !bypass {
				m.mu.Unlock()
				return
			}
		}
	}
	m.fillTriggered = false
	fillScopedCoin := m.fillTriggeredCoin
	m.fillTriggeredCoin = ""

	clear(m.scratch.books)
	for coin, book := range m.latestBook {
		m.scratch.books[coin] = book
	}
	sig := m.latestSignal
	pendingOrders := copyOrdersByCoin(m.pendingOrders)
	clear(m.scratch.quoteStates)
	for coin, state := range m.quoteStates {
		m.scratch.quoteStates[coin] = state
	}
	m.scratch.rlTier = m.supervisor.RLTier() // snapshot under lock for computeOrdersForCoin
	for coin, ft := range m.lastFillTime {
		m.scratch.lastFillTimes[coin] = ft
	}
	m.mu.Unlock()

	equity := m.executor.Equity()
	if equity <= 0 {
		slog.Warn("skipping quotes: equity is zero")
		return
	}

	clear(m.scratch.positions)
	clear(m.scratch.currentOrders)
	clear(m.scratch.exposures)
	for _, asset := range m.cfg.Strategy.Assets {
		pos := m.executor.Position(asset)
		m.scratch.positions[asset] = pos
		accumulatePositionExposure(m.scratch.exposures, pos)
		orders := normalizeOrders(m.executor.OpenOrders(asset))
		m.scratch.currentOrders[asset] = orders
		accumulateOrderExposure(m.scratch.exposures, orders)
	}
	for _, orders := range pendingOrders {
		accumulateOrderExposure(m.scratch.exposures, orders)
	}

	now := time.Now()
	healthByCoin := m.computeCoinHealth(now)
	pairState := m.updatePairReduceMode(m.scratch.positions)

	clear(m.scratch.desiredByCoin)
	for _, coin := range m.cfg.Strategy.Assets {
		ac := m.cfg.ForAsset(coin)
		leverage := float64(ac.Execution.Leverage)
		if leverage < 1 {
			leverage = 1
		}
		accountCap := equity * leverage * 0.8
		effectiveMaxPosition := math.Min(ac.Risk.MaxPositionNotional, accountCap)
		effectiveMaxSpread := math.Min(ac.Risk.MaxSpreadExposure, accountCap)
		m.scratch.desiredByCoin[coin] = m.computeOrdersForCoin(
			coin,
			m.scratch.books[coin],
			sig,
			m.scratch.positions[coin],
			m.scratch.exposures,
			effectiveMaxPosition,
			effectiveMaxSpread,
			healthByCoin[coin],
			pairState,
		)
	}

	diagByCoin := make(map[string]DiagnosticSnapshot, len(m.cfg.Strategy.Assets))
	m.diagMu.RLock()
	for _, coin := range m.cfg.Strategy.Assets {
		diagByCoin[coin] = m.diagSnaps[coin]
	}
	m.diagMu.RUnlock()

	for _, coin := range m.cfg.Strategy.Assets {
		pos := m.scratch.positions[coin]
		entrySide := ""
		switch pos.Side {
		case "long":
			entrySide = "buy"
		case "short":
			entrySide = "sell"
		}
		if entrySide == "" {
			continue
		}
		current := m.scratch.currentOrders[coin]
		desired := m.scratch.desiredByCoin[coin]
		if !hasOrderSide(current, entrySide) || hasOrderSide(desired, entrySide) {
			continue
		}
		if !m.cfg.SignalPauses() {
			continue
		}
		diag := diagByCoin[coin]
		mm := m.cfg.ForAsset(coin).MarketMaking
		reason := ""
		switch {
		case diag.PriceShockActive && diag.PriceShockSide == entrySide:
			reason = "price_shock"
		case mm.TrendGuardFreezeThreshold > 0 && diag.TrendGuardActive && diag.TrendGuardSuppress >= mm.TrendGuardFreezeThreshold:
			reason = "trend_guard_freeze"
		}
		if reason == "" {
			continue
		}
		cancelCtx, cancelCancel := context.WithTimeout(ctx, 3*time.Second)
		if err := m.executor.CancelSide(cancelCtx, coin, entrySide, reason); err != nil {
			slog.Error("protective side cancel failed",
				"coin", coin,
				"side", entrySide,
				"reason", reason,
				"error", err,
			)
		} else {
			m.scratch.currentOrders[coin] = removeOrdersBySide(current, entrySide)
		}
		cancelCancel()
	}

	m.scratch.refreshCoins = m.scratch.refreshCoins[:0]
	clear(m.scratch.refreshReason)
	for _, coin := range m.cfg.Strategy.Assets {
		forceReason, forced := forceReasons[coin]
		// Fill-triggered refresh: only evaluate the filled coin.
		if fillScopedCoin != "" && coin != fillScopedCoin && !forced {
			continue
		}
		state := m.scratch.quoteStates[coin]
		desired := m.scratch.desiredByCoin[coin]
		current := m.scratch.currentOrders[coin]
		refresh, reason := m.shouldRefreshCoin(coin, current, desired, state, now)
		if forced {
			if len(current) > 0 || len(desired) > 0 {
				refresh = true
				reason = forceReason
				markOrdersForced(desired, forceReason)
				m.scratch.desiredByCoin[coin] = desired
			}
		}
		if refresh {
			m.scratch.refreshCoins = append(m.scratch.refreshCoins, coin)
			m.scratch.refreshReason[coin] = reason
		}
	}

	if len(m.scratch.refreshCoins) == 0 {
		if forceActive {
			m.mu.Lock()
			for coin := range forceReasons {
				delete(m.forceRefreshReasons, coin)
			}
			m.mu.Unlock()
		}
		return
	}

	m.mu.Lock()
	if (m.supervisor.IsRiskHalted() || m.supervisor.IsRefreshFailed()) || m.refreshingAll {
		m.mu.Unlock()
		return
	}
	m.refreshingAll = true
	m.lastRefreshAll = now
	for coin, book := range m.scratch.books {
		m.lastRefreshMid[coin] = book.Mid
	}
	m.pendingOrders = make(map[string][]types.OrderRequest, len(m.scratch.refreshCoins))
	for _, coin := range m.scratch.refreshCoins {
		delete(m.forceRefreshReasons, coin)
		m.pendingOrders[coin] = copyOrders(m.scratch.desiredByCoin[coin])
	}
	m.mu.Unlock()

	go func(sentAt time.Time, refreshCoins []string) {
		totalStart := time.Now()

		// Build coalesced order map.
		ordersByCoin := make(map[string][]types.OrderRequest, len(refreshCoins))
		for _, coin := range refreshCoins {
			reason := m.scratch.refreshReason[coin]
			orders := len(m.scratch.desiredByCoin[coin])
			slog.Info("refreshing quotes",
				"coin", coin,
				"reason", reason,
				"orders", orders,
			)
			// P13: record refresh attribution.
			if m.postMortem != nil && m.postMortem.Enabled() {
				m.postMortem.RecordRefresh(RefreshAttribution{
					Timestamp: time.Now(),
					Coin:      coin,
					Trigger:   reason,
					Action:    "refresh",
					Orders:    orders,
				})
			}
			ordersByCoin[coin] = m.scratch.desiredByCoin[coin]
		}

		refreshErr := m.executor.RefreshMulti(ctx, ordersByCoin)

		// Log per-coin completion for compatibility with log parsers.
		elapsed := time.Since(totalStart).Milliseconds()
		for _, coin := range refreshCoins {
			slog.Info("coin refresh complete", "coin", coin, "duration_ms", elapsed)
		}
		slog.Debug("all coins refreshed", "duration_ms", elapsed)

		m.mu.Lock()
		m.refreshingAll = false
		m.pendingOrders = make(map[string][]types.OrderRequest)
		halted := m.supervisor.IsRiskHalted() || m.supervisor.IsRefreshFailed()
		hadFillPending := m.fillPending
		fillPendCoin := m.fillPendingCoin
		m.fillPending = false
		m.fillPendingCoin = ""
		m.mu.Unlock()

		if refreshErr != nil {
			// A fill or reconciliation can change the order state while a
			// refresh is in flight. The executor aborts safely and clears its
			// tracking; that is a normal race, not an API failure. Retry on the
			// next tick instead of entering a permanent fail-closed halt.
			if errors.Is(refreshErr, execution.ErrRefreshSuperseded) {
				slog.Warn("quote refresh superseded by order-state change; retrying",
					"error", refreshErr,
				)
				return
			}
			slog.Error("RefreshMulti failed", "error", refreshErr)
			m.failClosed(ctx, "quote_refresh_failure", refreshErr)
			return
		}

		// Record per-coin API calls for rate limit allocator.
		if m.rateLimitAllocator != nil {
			for _, coin := range refreshCoins {
				m.rateLimitAllocator.RecordAPICall(coin)
			}
		}

		// Successful refresh — update quote states and reset fail-closed counter.
		m.mu.Lock()
		for _, coin := range refreshCoins {
			delete(m.pendingOrders, coin)
			m.quoteStates[coin] = quoteState{SentAt: sentAt}
		}
		if m.failClosedRetries > 0 {
			slog.Info("auto-recovery successful, resetting fail-closed counter",
				"previous_retries", m.failClosedRetries,
			)
			m.failClosedRetries = 0
		}
		m.mu.Unlock()

		if halted {
			slog.Warn("refresh completed while halted, cancelling stale orders")
			for _, asset := range m.cfg.Strategy.Assets {
				if err := m.executor.CancelAll(ctx, asset); err != nil {
					slog.Error("failed to cancel stale orders after halt", "asset", asset, "error", err)
				}
			}
		}

		// Re-signal fill trigger if a fill arrived while we were refreshing.
		if hadFillPending && !halted {
			select {
			case m.fillTrigger <- fillPendCoin:
			default:
			}
		}
	}(now, append([]string(nil), m.scratch.refreshCoins...))
}

func (m *Manager) failClosed(ctx context.Context, reason string, cause error) {
	m.mu.Lock()
	m.failClosedAt = time.Now()
	m.failClosedRetries++
	retries := m.failClosedRetries
	m.pendingOrders = make(map[string][]types.OrderRequest)
	m.mu.Unlock()
	m.supervisor.Signal(SignalRefreshFailed)

	// The underlying API error is logged here so the Zeus banner shows why the
	// halt happened, not just that it happened (the bridge forwards this line).
	slog.Error("quote manager entering fail-closed halt",
		"reason", reason,
		"error", cause,
		"retry_count", retries,
	)
	cancelCtx, cancelCancel := context.WithTimeout(ctx, cancelTimeoutFailClosed)
	var wg sync.WaitGroup
	for _, asset := range m.cfg.Strategy.Assets {
		wg.Add(1)
		go func(asset string) {
			defer wg.Done()
			if err := m.executor.CancelAll(cancelCtx, asset); err != nil {
				slog.Error("failed to cancel orders during fail-closed halt", "asset", asset, "error", err)
			}
		}(asset)
	}
	wg.Wait()
	cancelCancel()
}

// shouldAttemptAutoRecovery returns true if the manager should attempt to
// recover from a fail-closed halt. Must be called with m.mu held.
func (m *Manager) shouldAttemptAutoRecovery() bool {
	cooldown := m.cfg.Risk.FailClosedCooldownSec
	if cooldown <= 0 {
		return false
	}
	maxRetries := m.cfg.Risk.MaxFailClosedRetries
	if maxRetries > 0 && m.failClosedRetries >= maxRetries {
		return false
	}
	return time.Since(m.failClosedAt) >= time.Duration(cooldown)*time.Second
}

func (m *Manager) shouldRefreshCoin(
	coin string,
	current []types.OrderRequest,
	desired []types.OrderRequest,
	state quoteState,
	now time.Time,
) (bool, string) {
	mm := m.cfg.ForAsset(coin).MarketMaking
	maxAgeMs := mm.MaxQuoteAgeMs
	if maxAgeMs == 0 {
		maxAgeMs = 3000
	}
	priceThresholdBps := float64(mm.RequoteThresholdBps)
	if mm.RequoteThresholdBps == 0 {
		priceThresholdBps = 2
	}
	sizeThresholdPct := mm.SizeRequotePct
	if sizeThresholdPct == 0 {
		sizeThresholdPct = 0.10
	}

	switch {
	case len(current) == 0 && len(desired) == 0:
		return false, "no_orders"
	case len(current) == 0 && len(desired) > 0:
		return true, "bootstrap"
	case len(current) > 0 && len(desired) == 0:
		return true, "cancel"
	}

	if state.SentAt.IsZero() {
		return true, "unknown_state"
	}
	if now.Sub(state.SentAt) >= time.Duration(maxAgeMs)*time.Millisecond {
		return true, "max_age"
	}
	if quotesMateriallyDifferent(
		current,
		desired,
		priceThresholdBps,
		sizeThresholdPct,
	) {
		return true, "price_or_size"
	}

	return false, "unchanged"
}

// computeOrdersForCoin computes the desired order set for a single coin using a 7-step pipeline.
func (m *Manager) computeOrdersForCoin(
	coin string,
	book types.BookUpdate,
	sig types.SpreadSignal,
	pos types.PositionUpdate,
	exposures map[string]sideExposure,
	maxPositionNotional float64,
	maxSpreadExposure float64,
	health coinQuotingHealth,
	pairState pairReduceState,
) []types.OrderRequest {
	if book.Mid == 0 {
		slog.Info("skipping quotes: no book mid", "coin", coin)
		return nil
	}
	mid := book.Mid
	firstAsset := m.cfg.Strategy.Assets[0]
	ac := m.cfg.ForAsset(coin)
	mm := ac.MarketMaking
	now := time.Now()
	priceShock := PriceShockSnapshot{}
	if ps := m.priceShockTrackers[coin]; ps != nil {
		priceShock = ps.Snapshot(now)
	}

	// Apply optimizer overlay to base config.
	var optOverlay OptimizerOverlay
	if optV2 := m.optimizersV2[coin]; optV2 != nil {
		optOverlay = optV2.Overlay()
	} else {
		optOverlay = neutralOverlay()
	}

	// B3: Record inventory sample for half-life controller
	if ic := m.inventoryControllers[coin]; ic != nil {
		ratio := 0.0
		if maxPositionNotional > 0 {
			ratio = pos.Notional / maxPositionNotional
		}
		ic.RecordSample(ratio, now)
	}

	// Warmup protection: reduce exposure while spread engine calibrates or time-based warmup.
	warmupActive := false
	if m.warmupFn != nil {
		count, window := m.warmupFn()
		warmupActive = count < window
	}
	// Time-based warmup fallback when spread engine is disabled.
	if !warmupActive && m.warmupDuration > 0 && !m.startedAt.IsZero() {
		if time.Since(m.startedAt) < m.warmupDuration {
			warmupActive = true
		}
	}

	// Recovery mode: reduced quoting after drawdown resume.
	recoveryActive := m.supervisor.IsRecovery()

	if health.Halted {
		m.diagMu.Lock()
		prev := m.diagSnaps[coin]
		wasHalted := prev.HealthHalted
		m.diagSnaps[coin] = DiagnosticSnapshot{
			HealthHalted: health.Halted,
			HealthReason: health.Reason,
			Timestamp:    time.Now(),
		}
		m.diagMu.Unlock()
		if !wasHalted {
			slog.Warn("health halt: suppressing quotes", "coin", coin, "reason", health.Reason)
		}
		return nil
	}

	// Log health transitions.
	m.diagMu.Lock()
	prev := m.diagSnaps[coin]
	wasHaltedBefore := prev.HealthHalted
	wasDegradedBefore := prev.HealthDegraded
	if wasHaltedBefore {
		slog.Info("health recovered: resuming quotes", "coin", coin)
		snap := m.diagSnaps[coin]
		snap.HealthHalted = false
		snap.HealthReason = ""
		m.diagSnaps[coin] = snap
	}
	if health.Degraded && !wasDegradedBefore {
		slog.Warn("health degraded: widening spreads", "coin", coin, "reason", health.Reason)
	}
	if !health.Degraded && wasDegradedBefore {
		slog.Info("health no longer degraded", "coin", coin)
	}
	m.diagMu.Unlock()

	// Step 1: Fair Value (microprice-anchored)
	microFV := book.Microprice
	if microFV == 0 {
		microFV = mid
	}

	// Step 1 (continued): Multi-level microprice
	if mm.MicropriceLevels > 1 {
		microFV = computeMultiLevelMicroprice(book.Bids, book.Asks, mid, mm.MicropriceLevels, mm.MicropriceDecay)
	}

	// Read autocorrelation once (used by adaptive alpha + adaptive skew).
	autocorr := math.NaN()
	if regime := m.regimes[coin]; regime != nil {
		if ac, ready := regime.Autocorrelation(); ready {
			autocorr = ac
		}
	}

	// Resolve short/long VWAPs + sigma once so the V31 trend mechanisms below
	// can derive a direction from sign(shortV - longV) rather than from
	// autocorr's sign (which is direction-invariant — an up-trend and its
	// mirror-image down-trend produce identical autocorr).
	var shortV, longV, vwapSigma float64
	var shortVOK, longVOK, sigmaOK bool
	if m.shortVWAPs != nil {
		if sc, ok := m.shortVWAPs[coin]; ok && sc != nil {
			shortV, shortVOK = sc.VWAP()
			vwapSigma, sigmaOK = sc.PriceStddev()
		}
	}
	if m.longVWAPs != nil {
		if lc, ok := m.longVWAPs[coin]; ok && lc != nil {
			longV, longVOK = lc.VWAP()
		}
	}
	// trendDir: +1 if short VWAP above long (up-trend), -1 if below (down-trend),
	// 0 if either VWAP isn't ready. Used by trend FV drift + entry reduction
	// accel to pick a sign; autocorr magnitude supplies strength.
	var trendDir int
	if shortVOK && longVOK && shortV > 0 && longV > 0 {
		if shortV > longV {
			trendDir = 1
		} else if shortV < longV {
			trendDir = -1
		}
	}

	vwapAlpha := mm.VWAPAlpha + optOverlay.AlphaDelta
	if vwapAlpha < 0 {
		vwapAlpha = 0
	}
	if vwapAlpha > 1 {
		vwapAlpha = 1
	}

	// P2: Drift-aware adaptive vwap_alpha. Fade alpha toward the floor when
	// |short_vwap - long_vwap| / sigma exceeds K * sigma so FV anchors more to
	// microprice during sustained trends. Applied BEFORE step 1.8 markout FV
	// feedback (which acts on fairValue after the blend).
	effectiveAlpha := vwapAlpha
	if mm.VWAPAlphaDriftScalingEnabled && shortVOK && longVOK && sigmaOK &&
		vwapSigma > 0 && shortV > 0 && longV > 0 && mm.VWAPAlphaDriftK > 0 {
		div := math.Abs(shortV-longV) / (mm.VWAPAlphaDriftK * vwapSigma)
		fade := 1.0 - div
		if fade < 0 {
			fade = 0
		}
		faded := vwapAlpha * fade
		if faded < mm.VWAPAlphaFloor {
			faded = mm.VWAPAlphaFloor
		}
		effectiveAlpha = faded
		m.maybeLogVWAPAlphaFade(coin, vwapAlpha, effectiveAlpha, div, vwapSigma)
	}

	// V31: Trend-aware alpha boost — boost alpha toward ceiling during confirmed
	// trends so FV anchors closer to the lagging VWAP, naturally widening entry-side
	// quotes. Gated on autocorr > threshold (positive only = genuine trending
	// regime); negative autocorr means mean-reversion and must not trigger a
	// "trend" boost despite its magnitude.
	if mm.VWAPAlphaTrendBoostEnabled && !math.IsNaN(autocorr) {
		minAutocorr := mm.TrendFVDriftAutocorrMin
		if minAutocorr == 0 {
			minAutocorr = 0.1
		}
		if autocorr > minAutocorr {
			boostFrac := autocorr
			boosted := effectiveAlpha + boostFrac*(mm.VWAPAlphaTrendBoostCeiling-effectiveAlpha)
			if boosted > mm.VWAPAlphaTrendBoostCeiling {
				boosted = mm.VWAPAlphaTrendBoostCeiling
			}
			if boosted > effectiveAlpha {
				m.bumpTrendMech(coin, "alpha_boost")
			}
			effectiveAlpha = boosted
		}
	}

	fairValue := microFV
	if shortVOK && shortV > 0 {
		fairValue = effectiveAlpha*shortV + (1-effectiveAlpha)*microFV
	}

	// V31: Trend-momentum FV bias — shift fair value in trend direction.
	// Direction comes from sign(shortV - longV) since autocorr itself is
	// direction-invariant; magnitude = |autocorr| × drift_bps so a stronger
	// trending regime produces a stronger shift. Gated on autocorr > threshold
	// (positive regime = trending) so we don't shift FV during mean-reversion.
	var trendFVShiftBps float64
	if mm.TrendFVDriftBps > 0 && !math.IsNaN(autocorr) && trendDir != 0 {
		minAutocorr := mm.TrendFVDriftAutocorrMin
		if minAutocorr == 0 {
			minAutocorr = 0.1
		}
		if autocorr > minAutocorr {
			trendFVShiftBps = float64(trendDir) * autocorr * mm.TrendFVDriftBps
			fairValue *= 1.0 + trendFVShiftBps/10000.0
			if trendFVShiftBps != 0 {
				m.bumpTrendMech(coin, "fv_shift")
			}
		}
	}

	// FV sanity clamp: if VWAP blend produces a fair value that diverges wildly
	// from book mid, clamp to mid. Prevents corrupted/stale VWAP from producing
	// nonsensical order prices (e.g., negative FV during exchange downtime).
	if maxDev := mm.MaxFVDeviationPct; maxDev > 0 && mid > 0 {
		devPct := math.Abs(fairValue-mid) / mid * 100
		if devPct > maxDev {
			slog.Warn("fair value diverged from book mid, clamping to mid",
				"coin", coin,
				"fair_value", fmt.Sprintf("%.4f", fairValue),
				"mid", fmt.Sprintf("%.4f", mid),
				"dev_pct", fmt.Sprintf("%.1f", devPct),
				"max_dev_pct", maxDev,
			)
			fairValue = mid
		}
	}

	// Step 1.6: Trade-flow imbalance shift
	var tradeImbalanceShiftBps float64
	if mm.TradeImbalanceMaxShiftBps > 0 && m.imbalanceCalcs != nil {
		if calc, ok := m.imbalanceCalcs[coin]; ok && calc != nil {
			if imb, ready := calc.ImbalanceAndRecord(); ready {
				effectiveMaxShift := mm.TradeImbalanceMaxShiftBps * optOverlay.ImbalanceShiftMult
				tradeImbalanceShiftBps = imb * effectiveMaxShift
				// Clamp to configured bounds (defense-in-depth against float drift).
				if tradeImbalanceShiftBps > effectiveMaxShift {
					tradeImbalanceShiftBps = effectiveMaxShift
				} else if tradeImbalanceShiftBps < -effectiveMaxShift {
					tradeImbalanceShiftBps = -effectiveMaxShift
				}
				fairValue *= 1.0 + tradeImbalanceShiftBps/10000.0
			}
		}
	}

	// Step 1.7: Post-fill adverse decay
	var fillDecayShiftBps float64
	if mm.FillAdverseShiftBps > 0 && m.fillDecays != nil {
		if fd, ok := m.fillDecays[coin]; ok && fd != nil {
			if shift := fd.CurrentShiftBps(); shift != 0 {
				fillDecayShiftBps = shift * optOverlay.FillDecayMult
				fairValue *= 1.0 + fillDecayShiftBps/10000.0
			}
		}
	}

	// Step 1.8: Markout-to-FV feedback
	// Uses per-side markout EWMAs from the toxicity tracker to shift FV.
	// When sells are more toxic than buys, FV shifts up (and vice versa).
	if mm.MarkoutFVScale > 0 {
		if tox := m.toxicityTrackers[coin]; tox != nil {
			buyEWMA, buyReady := tox.MarkoutEWMA("buy")
			sellEWMA, sellReady := tox.MarkoutEWMA("sell")
			if buyReady && sellReady {
				// When buys are more toxic (buyEWMA more negative), delta < 0,
				// shifting FV DOWN — widens bids (protective) and tightens asks
				// (capturing more fills on the benign sell side).
				delta := (buyEWMA - sellEWMA) / 2.0
				markoutFVShift := delta * mm.MarkoutFVScale
				if markoutFVShift > mm.MarkoutFVMaxShiftBps {
					markoutFVShift = mm.MarkoutFVMaxShiftBps
				}
				if markoutFVShift < -mm.MarkoutFVMaxShiftBps {
					markoutFVShift = -mm.MarkoutFVMaxShiftBps
				}
				// Attenuate when toxicity spread multiplier is high
				// (avoid double-counting with per-side toxicity widening).
				toxMult := math.Max(tox.SpreadMultiplier("buy"), tox.SpreadMultiplier("sell"))
				if toxMult > 1.5 && mm.ToxicityMaxWiden > 1.5 {
					att := math.Max(0.2, 1.0-(toxMult-1.5)/(mm.ToxicityMaxWiden-1.5))
					markoutFVShift *= att
				}
				fairValue *= 1.0 + markoutFVShift/10000.0
			}
		}
	}

	// Step 2: Base Quotes + microstructure
	// Per-side spread: use per-side base spreads when configured, fallback to symmetric.
	baseBidSpreadBps := mm.QuoteSpreadBps
	baseAskSpreadBps := mm.QuoteSpreadBps
	if mm.QuoteSpreadBpsBuy > 0 {
		baseBidSpreadBps = mm.QuoteSpreadBpsBuy
	}
	if mm.QuoteSpreadBpsSell > 0 {
		baseAskSpreadBps = mm.QuoteSpreadBpsSell
	}
	bidSpreadBps := baseBidSpreadBps + optOverlay.SpreadDeltaBuy
	askSpreadBps := baseAskSpreadBps + optOverlay.SpreadDeltaSell
	// Apply fill burst spread widening (per-side).
	burstBuySpreadMult, burstSellSpreadMult := 1.0, 1.0
	burstBuySizeFactor, burstSellSizeFactor := 1.0, 1.0
	if bt := m.fillBurstTrackers[coin]; bt != nil {
		burstBuySpreadMult, burstSellSpreadMult = bt.SpreadMultiplier()
		burstBuySizeFactor, burstSellSizeFactor = bt.SizeFactor()
	}
	bidSpreadBps *= burstBuySpreadMult
	askSpreadBps *= burstSellSpreadMult
	// Trade arrival rate spread adjustment: widen when trade arrival drops (thin market),
	// tighten when arrival is high (amortizes adverse selection across more fills).
	// Uses the velocity tracker's slow EWMA as baseline. Multiplier in [0.85, 1.25].
	if vt := m.velocityTrackers[coin]; vt != nil && !warmupActive {
		snap := vt.Snapshot(mm.LadderLevels)
		if snap.RateRatio > 0 {
			// RateRatio = fast/slow. When ratio < 1 (thin), widen. When > 1 (active), tighten.
			arrivalMult := 1.0 / math.Max(0.8, math.Min(1.2, snap.RateRatio))
			bidSpreadBps *= arrivalMult
			askSpreadBps *= arrivalMult
		}
	}
	// A9: Time-of-day spread adjustment — widen during configured hours.
	if mm.TodSpreadExtraBps > 0 && (mm.TodSpreadStartHourUTC != 0 || mm.TodSpreadEndHourUTC != 0) {
		hour := time.Now().UTC().Hour()
		inWindow := false
		if mm.TodSpreadStartHourUTC <= mm.TodSpreadEndHourUTC {
			inWindow = hour >= mm.TodSpreadStartHourUTC && hour < mm.TodSpreadEndHourUTC
		} else { // wraps midnight
			inWindow = hour >= mm.TodSpreadStartHourUTC || hour < mm.TodSpreadEndHourUTC
		}
		if inWindow {
			bidSpreadBps += mm.TodSpreadExtraBps
			askSpreadBps += mm.TodSpreadExtraBps
		}
	}
	// Use max of per-side spreads for pipeline stages that need a single value.
	effectiveSpreadBps := math.Max(bidSpreadBps, askSpreadBps)
	if warmupActive {
		effectiveSpreadBps *= mm.WarmupSpreadMultiplier
		bidSpreadBps *= mm.WarmupSpreadMultiplier
		askSpreadBps *= mm.WarmupSpreadMultiplier
	}
	if recoveryActive && m.cfg.Risk.RecoverySpreadMultiplier > 0 {
		effectiveSpreadBps *= m.cfg.Risk.RecoverySpreadMultiplier
		bidSpreadBps *= m.cfg.Risk.RecoverySpreadMultiplier
		askSpreadBps *= m.cfg.Risk.RecoverySpreadMultiplier
	}
	// V24: session stop-loss early-warn spread widen. When session PnL is in the
	// warn zone (between warn_bps and stop_loss_bps, both negative), widen both
	// sides by WarnSpreadMult to reduce further losses before the hard stop.
	// Widen-only: no flatten, no pause — purely defensive quote widening.
	if m.cfg.Risk.SessionStopLossWarnBps < 0 && m.cfg.Risk.SessionStopLossWarnSpreadMult > 1.0 && m.sessionPnL != nil {
		_, sessionBps := m.sessionPnL.PnL()
		inHardStop := m.cfg.Risk.SessionStopLossBps < 0 && sessionBps <= m.cfg.Risk.SessionStopLossBps
		inWarn := sessionBps <= m.cfg.Risk.SessionStopLossWarnBps && !inHardStop
		if inWarn {
			mult := m.cfg.Risk.SessionStopLossWarnSpreadMult
			if !m.sessionWarnActive {
				slog.Warn("session_pnl warn zone entered: widening spreads",
					"coin", coin,
					"session_pnl_bps", sessionBps,
					"warn_bps", m.cfg.Risk.SessionStopLossWarnBps,
					"stop_loss_bps", m.cfg.Risk.SessionStopLossBps,
					"warn_spread_mult", mult,
				)
				m.sessionWarnActive = true
				// V32: force an immediate requote on warn-zone entry so the
				// defensive widen applies within the next refresh tick instead
				// of waiting up to 25s for the regular cadence.
				m.markForceRefresh(coin, "session_pnl_warn_zone_entry")
			}
			effectiveSpreadBps *= mult
			bidSpreadBps *= mult
			askSpreadBps *= mult
		} else if m.sessionWarnActive {
			slog.Info("session_pnl warn zone exited: restoring base spreads",
				"coin", coin,
				"session_pnl_bps", sessionBps,
				"warn_bps", m.cfg.Risk.SessionStopLossWarnBps,
				"in_hard_stop", inHardStop,
			)
			m.sessionWarnActive = false
		}
	}
	if health.Degraded {
		effectiveSpreadBps *= degradedSpreadMultiplier
		bidSpreadBps *= degradedSpreadMultiplier
		askSpreadBps *= degradedSpreadMultiplier
	}
	// V30: favorable shock quote tightening. When a price shock direction
	// favors current inventory, subtract an absolute bps amount from that
	// side's spread so the profit-taking quote sits closer to fair value and
	// is more likely to fill during the cooldown. Applied AFTER all spread
	// multipliers (burst, arrival, warmup, recovery, warn-zone, degraded) so
	// it composes additively — a 2 bps tighten on a warn-zone-widened spread
	// still leaves the warn widening intact, it just carves a fixed amount
	// off the one side that's in the money. Floor at 0; crossing protection
	// (min_crossing_spread_bps) handles the safety backstop downstream.
	if priceShock.Active && pos.Side != "" &&
		!sideIncreasesPosition(priceShock.EntrySide, pos.Side) &&
		mm.PriceShockFavorableExitTightenBps > 0 {
		tighten := mm.PriceShockFavorableExitTightenBps
		switch priceShock.EntrySide {
		case "buy":
			bidSpreadBps = math.Max(0, bidSpreadBps-tighten)
		case "sell":
			askSpreadBps = math.Max(0, askSpreadBps-tighten)
		}
	}
	// Construct base quotes using per-side spread.
	bidHalfSpread := bidSpreadBps / 20000.0
	askHalfSpread := askSpreadBps / 20000.0
	bid := fairValue * (1.0 - bidHalfSpread)
	ask := fairValue * (1.0 + askHalfSpread)
	if micro := m.micros[coin]; micro != nil {
		w := micro.Width(coin)
		bid = fairValue * (1.0 - bidHalfSpread*w.BidMultiplier)
		ask = fairValue * (1.0 + askHalfSpread*w.AskMultiplier)
	}

	// Step 2.5: Fee Floor
	// Center on fair value (the reference the quotes were built from), not the
	// raw book mid: centering on mid while quoting around FV pushed both prices
	// to one side of the book and could break the minimum spread guarantee.
	if mm.FeeFloorEnabled {
		minHalfSpreadBps := mm.MakerFeeBps + mm.BuilderFeeBps
		bid, ask = applyFeeFloor(bid, ask, fairValue, minHalfSpreadBps)
	}

	// Step 3: Spread Bias (+ Feature 5: z-score calibration)
	biasScale := mm.SpreadBiasScale
	if sig.ZStdDev != 0 && sig.ZStdDev != 1.0 {
		biasScale *= sig.ZStdDev
	}
	bid, ask = applySpreadBias(bid, ask, mid, sig.ZScore,
		biasScale, mm.SpreadBiasMaxBps, coin, firstAsset, mm.SpreadBiasMaxFraction, mm.SpreadBiasAsymmetry)

	// Compute bias shift for diagnostics.
	var biasBps float64
	if sig.ZScore != 0 && biasScale != 0 {
		if mm.SpreadBiasMaxFraction == 0 {
			biasBps = math.Tanh(sig.ZScore/biasScale) * mm.SpreadBiasMaxBps
			if coin == firstAsset {
				biasBps = -biasBps
			}
		} else {
			rawBias := math.Tanh(sig.ZScore / biasScale)
			halfSpread := fairValue * effectiveSpreadBps / 20000.0
			biasBps = math.Abs(rawBias) * mm.SpreadBiasMaxFraction * halfSpread / (fairValue / 10000.0)
			if (sig.ZScore > 0 && coin == firstAsset) || (sig.ZScore < 0 && coin != firstAsset) {
				biasBps = -biasBps // convergence is ask side, net shift is downward
			}
		}
	}

	// Step 4: Inventory Skew (convex, asymmetric)
	skewFactor := mm.InventorySkewFactor
	if ic := m.inventoryControllers[coin]; ic != nil {
		// B3: Half-life controller overrides static skew
		skewFactor = ic.EffectiveSkewFactor(mm.InventorySkewFactor)
	}
	skewFactor *= optOverlay.SkewMult
	if f := m.fundingTracker; f != nil {
		skewFactor *= f.UrgencyMultiplier(coin, pos.Side)
	}
	bid, ask = applyInventorySkew(bid, ask, fairValue, pos.Notional, pos.Side,
		maxPositionNotional, skewFactor, effectiveSpreadBps, mm.InventorySkewGamma, mm.InventorySkewAsymmetry, mm.InventorySkewExitPreserve)

	// Compute skew shift for diagnostics.
	var skewShiftBps float64
	if pos.Side != "" && pos.Notional > 0 && maxPositionNotional > 0 {
		ratio := pos.Notional / maxPositionNotional
		if ratio > 1.0 {
			ratio = 1.0
		}
		skewShiftBps = math.Pow(ratio, mm.InventorySkewGamma) * skewFactor * effectiveSpreadBps
		if pos.Side == "long" {
			skewShiftBps = -skewShiftBps
		}
	}

	// Step 4.5: Cross-Leg Inventory Skew
	var crossLegBps float64
	if mm.CrossLegSkewEnabled && len(m.cfg.Strategy.Assets) >= 2 {
		var otherCoin string
		if coin == m.cfg.Strategy.Assets[0] {
			otherCoin = m.cfg.Strategy.Assets[1]
		} else {
			otherCoin = m.cfg.Strategy.Assets[0]
		}
		if otherPos, ok := m.scratch.positions[otherCoin]; ok {
			bid, ask, crossLegBps = applyCrossLegSkew(bid, ask, fairValue, pos, otherPos,
				sig.Beta, coin == firstAsset, maxPositionNotional, mm.CrossLegSkewFactor, effectiveSpreadBps)
		}
	}

	// Step 5: VWAP Mean-Reversion Sizing
	bidMult, askMult := 1.0, 1.0
	spacingBps := float64(mm.LadderSpacingBps)
	// Vol-scale ladder spacing: widen inter-level spacing when realized vol exceeds baseline.
	if mm.LadderSpacingDynamic {
		if micro := m.micros[coin]; micro != nil {
			spacingBps *= micro.VolMultiplier(coin)
		}
	}
	// Check inventory utilization to suppress wick amplification on the inventory-increasing
	// side when position is large. Exit-side wick amplification is preserved to help unwind.
	wickSuppressBid := false // suppress buy-side wick (would increase long)
	wickSuppressAsk := false // suppress sell-side wick (would increase short)
	if mm.WickMaxInventoryUtil > 0 && maxPositionNotional > 0 {
		if pos.Notional/maxPositionNotional > mm.WickMaxInventoryUtil {
			switch pos.Side {
			case "long":
				wickSuppressBid = true
			case "short":
				wickSuppressAsk = true
			}
		}
	}
	if m.shortVWAPs != nil && m.longVWAPs != nil {
		if sc, ok1 := m.shortVWAPs[coin]; ok1 && sc != nil {
			if lc, ok2 := m.longVWAPs[coin]; ok2 && lc != nil {
				if sv, sr := sc.VWAP(); sr {
					if lv, lr := lc.VWAP(); lr {
						bidMult, askMult, spacingBps = computeWickSizing(mid, sv, lv,
							mm.WickDevThresholdPct/100.0, mm.WickLongDevThresholdPct/100.0,
							mm.WickSizeMultiplier, float64(mm.LadderSpacingBps))
					}
				}
			}
		}
	}
	// Per-side wick suppression: clamp the inventory-increasing side's mult to 1.0,
	// preserving exit-side wick amplification to help unwind positions.
	if wickSuppressBid {
		bidMult = 1.0
	}
	if wickSuppressAsk {
		askMult = 1.0
	}

	// Step 6: Z-Score Widening (asymmetric, + Feature 5: z-score calibration)
	zWideningThreshold := mm.ZWideningThreshold
	if sig.ZStdDev != 0 && sig.ZStdDev != 1.0 {
		zWideningThreshold *= sig.ZStdDev
	}
	bid, ask = applyZWidening(bid, ask, mid, math.Abs(sig.ZScore),
		zWideningThreshold, mm.ZWideningRate, mm.ZMaxWidthMultiplier,
		mm.ZWideningAsymmetry, sig.ZScore > 0, coin == firstAsset)

	// Compute z-widening multiplier for diagnostics.
	zWideningMult := 1.0
	if absZ := math.Abs(sig.ZScore); absZ > zWideningThreshold {
		zWideningMult = 1.0 + (absZ-zWideningThreshold)*mm.ZWideningRate
		if zWideningMult > mm.ZMaxWidthMultiplier {
			zWideningMult = mm.ZMaxWidthMultiplier
		}
	}

	// Step 6.5: Per-side toxicity widening + size scaling
	toxBuyMult, toxSellMult := 1.0, 1.0
	toxBuySizeFactor, toxSellSizeFactor := 1.0, 1.0
	if st := m.toxicityTrackers[coin]; st != nil {
		toxBuyMult = st.SpreadMultiplier("buy")
		toxSellMult = st.SpreadMultiplier("sell")
		toxBuySizeFactor = st.SizeFactor("buy", mm.ToxicitySizeReduceThreshold, mm.ToxicitySizeReduceMax)
		toxSellSizeFactor = st.SizeFactor("sell", mm.ToxicitySizeReduceThreshold, mm.ToxicitySizeReduceMax)

		// Attenuate toxicity multiplier when the optimizer has already widened the
		// same side. Both systems respond to per-side markout EWMA — without this
		// cap the multiplicative cross-term double-counts the adverse signal.
		// Formula: effective = 1 + (mult-1) * max(0, 1 - delta/baseSpread)
		if baseBidSpreadBps > 0 {
			if d := optOverlay.SpreadDeltaBuy; d > 0 && toxBuyMult > 1.0 {
				atten := math.Max(0, 1.0-d/baseBidSpreadBps)
				toxBuyMult = 1.0 + (toxBuyMult-1.0)*atten
			}
		}
		if baseAskSpreadBps > 0 {
			if d := optOverlay.SpreadDeltaSell; d > 0 && toxSellMult > 1.0 {
				atten := math.Max(0, 1.0-d/baseAskSpreadBps)
				toxSellMult = 1.0 + (toxSellMult-1.0)*atten
			}
		}
		// Also attenuate size reduction when optimizer has widened.
		if baseBidSpreadBps > 0 {
			if d := optOverlay.SpreadDeltaBuy; d > 0 && toxBuySizeFactor < 1.0 {
				atten := math.Max(0, 1.0-d/baseBidSpreadBps)
				toxBuySizeFactor = 1.0 - (1.0-toxBuySizeFactor)*atten
			}
		}
		if baseAskSpreadBps > 0 {
			if d := optOverlay.SpreadDeltaSell; d > 0 && toxSellSizeFactor < 1.0 {
				atten := math.Max(0, 1.0-d/baseAskSpreadBps)
				toxSellSizeFactor = 1.0 - (1.0-toxSellSizeFactor)*atten
			}
		}

		// Apply per-side spread widening
		halfSpread := (ask - bid) / 2.0
		midPoint := (bid + ask) / 2.0
		bid = midPoint - halfSpread*toxBuyMult
		ask = midPoint + halfSpread*toxSellMult
	}

	// Step 6.9: Spread stack cap — prevent runaway widening from stacked layers.
	if mm.MaxTotalSpreadMultiplier > 0 {
		baseBidHalf := mm.QuoteSpreadBpsBuy
		if baseBidHalf == 0 {
			baseBidHalf = mm.QuoteSpreadBps
		}
		baseAskHalf := mm.QuoteSpreadBpsSell
		if baseAskHalf == 0 {
			baseAskHalf = mm.QuoteSpreadBps
		}
		maxBidSpread := baseBidHalf * mm.MaxTotalSpreadMultiplier / 10000.0 * fairValue
		maxAskSpread := baseAskHalf * mm.MaxTotalSpreadMultiplier / 10000.0 * fairValue
		if fairValue-bid > maxBidSpread {
			bid = fairValue - maxBidSpread
		}
		if ask-fairValue > maxAskSpread {
			ask = fairValue + maxAskSpread
		}
	}

	// A8: Position-age exit tightening — tighten exit side as position ages.
	// When no fill for PositionAgeTightenMinSec and holding a position,
	// progressively tighten exit price by PositionAgeTightenBps per extra minute.
	// Capped so the exit side never crosses fairValue (crossing protection handles the rest).
	if mm.PositionAgeTightenMinSec > 0 && mm.PositionAgeTightenBps > 0 && pos.Side != "" {
		if ft, ok := m.scratch.lastFillTimes[coin]; ok && !ft.IsZero() {
			ageSec := time.Since(ft).Seconds()
			minSec := float64(mm.PositionAgeTightenMinSec)
			if ageSec > minSec {
				extraMin := (ageSec - minSec) / 60.0
				tightenFrac := extraMin * mm.PositionAgeTightenBps / 10000.0
				// Cap at half the base spread to prevent crossing
				maxFrac := mm.QuoteSpreadBps / 2.0 / 10000.0
				if tightenFrac > maxFrac {
					tightenFrac = maxFrac
				}
				switch pos.Side {
				case "long":
					ask -= ask * tightenFrac // pull ask closer to mid
				case "short":
					bid += bid * tightenFrac // pull bid closer to mid
				}
			}
		}
	}

	// P9: Idle-fill circuit breaker — one-shot exit-side tighten when adverse
	// inventory has sat without a fill for IdleFillThresholdSec. Evaluated and
	// applied here so the override lands BEFORE crossing protection clamps it.
	idleBreakerSizeMult := 1.0
	idleBreakerSide := ""
	if mm.IdleFillDetectorEnabled && pos.Side != "" && maxPositionNotional > 0 {
		signedInvUtil := pos.Notional / maxPositionNotional
		if pos.Side == "short" {
			signedInvUtil = -signedInvUtil
		}
		now := time.Now()
		m.mu.Lock()
		fired, exitSide := m.checkIdleBreaker(coin, signedInvUtil, now)
		var lastFill time.Time
		if ft, ok := m.lastFillTime[coin]; ok {
			lastFill = ft
		}
		m.mu.Unlock()
		if fired {
			tightenFrac := mm.IdleExitTightenBps / 10000.0
			switch exitSide {
			case "sell":
				ask -= ask * tightenFrac
			case "buy":
				bid += bid * tightenFrac
			}
			idleBreakerSizeMult = mm.IdleExitSizeMultiplier
			idleBreakerSide = exitSide
			idleSec := 0.0
			if !lastFill.IsZero() {
				idleSec = now.Sub(lastFill).Seconds()
			}
			slog.Info("idle-fill breaker fired",
				"coin", coin,
				"side", exitSide,
				"inv_util", signedInvUtil,
				"idle_sec", idleSec,
				"tighten_bps", mm.IdleExitTightenBps,
				"size_mult", mm.IdleExitSizeMultiplier,
			)
			m.markForceRefresh(coin, "idle_fill_breaker")
		}
	}

	// Step 7: Crossing Protection
	bid, ask = applyCrossingProtection(bid, ask, fairValue, pos.Side, pos.Notional, book.BestBid, book.BestAsk, float64(mm.MinCrossingSpreadBps))

	// Sizing
	notionalMult := 1.0
	if mm.AdaptiveNotionalEnabled {
		if ft := m.fillRates[coin]; ft != nil {
			notionalMult = ft.NotionalMultiplier()
		}
	}
	effectiveNotional := mm.QuoteNotional * notionalMult * optOverlay.NotionalMult
	// P12: Time-of-day notional schedule (global). Nil-safe when disabled.
	scheduleMult := m.scheduleMultiplier(time.Now())
	if scheduleMult < 1.0 {
		effectiveNotional *= scheduleMult
	}
	if warmupActive {
		effectiveNotional *= mm.WarmupNotionalPct
	}
	if recoveryActive {
		m.mu.Lock()
		ssr := m.sessionStopRecovery
		m.mu.Unlock()
		if ssr && m.cfg.Risk.SessionStopLossRecoveryNotionalPct > 0 {
			effectiveNotional *= m.cfg.Risk.SessionStopLossRecoveryNotionalPct
		} else if m.cfg.Risk.RecoveryNotionalPct > 0 {
			effectiveNotional *= m.cfg.Risk.RecoveryNotionalPct
		}
	}
	// V30 part 5: session stop-loss warn zone notional cut. Complements the
	// existing spread widen (applied earlier) so warn zone both widens quotes
	// AND reduces exposure. Gated on the same `sessionWarnActive` flag set by
	// the warn-zone widen block. Pct is validated (0, 1]; 0 or 1 = disabled.
	if m.sessionWarnActive && m.cfg.Risk.SessionStopLossWarnNotionalPct > 0 && m.cfg.Risk.SessionStopLossWarnNotionalPct < 1 {
		effectiveNotional *= m.cfg.Risk.SessionStopLossWarnNotionalPct
	}
	if health.Degraded {
		effectiveNotional *= degradedNotionalMultiplier
	}
	// Floor: ensure enough notional for all configured ladder levels so warmup/adaptive
	// stacking doesn't crush levels to 1 (e.g. $350 × 0.29 × 0.10 = $10.15 → 1 level).
	minLadderNotional := minOrderNotional * float64(mm.LadderLevels)
	if effectiveNotional < minLadderNotional {
		effectiveNotional = minLadderNotional
	}
	// Per-side fill rate multipliers (Smart Layer 3): scale notional per side
	// based on how actively each side is filling relative to its target.
	var buyFillMult, sellFillMult float64 = 1.0, 1.0
	// Fixed sizing must stay at 1x after fills, even when adaptive target/bounds
	// are unset (zero). Keep recording fill statistics for diagnostics.
	if ft := m.fillRates[coin]; mm.AdaptiveNotionalEnabled && ft != nil {
		buyFillMult = ft.NotionalMultiplierSide("buy")
		sellFillMult = ft.NotionalMultiplierSide("sell")
	}
	buyNotional := effectiveNotional * bidMult * toxBuySizeFactor * burstBuySizeFactor * buyFillMult
	sellNotional := effectiveNotional * askMult * toxSellSizeFactor * burstSellSizeFactor * sellFillMult
	if m.cfg.Strategy.UseBetaHedge && sig.Beta > 0 && coin != firstAsset {
		buyNotional *= sig.Beta
		sellNotional *= sig.Beta
	}
	// P9: idle-fill breaker exit-side size boost (applied before exposure caps).
	if idleBreakerSizeMult != 1.0 {
		switch idleBreakerSide {
		case "buy":
			buyNotional *= idleBreakerSizeMult
		case "sell":
			sellNotional *= idleBreakerSizeMult
		}
	}

	// A6: Entry-side size reduction — linearly reduce entry-side notional as inventory grows.
	// Regime-adaptive: use softer rate during mean-reverting periods (autocorr < -0.1)
	// because larger entry positions benefit from expected reversion.
	entrySizeFactor := 1.0
	if mm.EntryReductionRate > 0 && maxPositionNotional > 0 {
		util := pos.Notional / maxPositionNotional
		if util > 1.0 {
			util = 1.0
		}
		effectiveRate := mm.EntryReductionRate
		if !math.IsNaN(autocorr) && autocorr < -0.1 {
			effectiveRate *= 0.5 // halve the reduction rate during mean-reverting regimes
		}
		// V31: Trend acceleration — boost entry reduction when the VWAP-
		// inferred trend direction is adverse to the current position.
		// Adverse = downtrend while long or uptrend while short. Strength
		// scales with |autocorr| (how clustered/confirmed the regime is),
		// gated on positive autocorr (trending, not mean-reverting) so a
		// choppy market doesn't accelerate reductions.
		if mm.EntryReductionTrendK > 0 && !math.IsNaN(autocorr) && autocorr > 0 && trendDir != 0 && pos.Side != "" {
			adverse := (pos.Side == "long" && trendDir < 0) || (pos.Side == "short" && trendDir > 0)
			if adverse {
				effectiveRate *= 1.0 + autocorr*mm.EntryReductionTrendK
				m.bumpTrendMech(coin, "entry_accel")
			}
		}
		entrySizeFactor = math.Max(mm.EntryReductionFloor, 1.0-util*effectiveRate)
		// A6b: Entry acceleration penalty — power-law cliff above threshold
		if mm.EntryAccelExponent > 0 && mm.EntryAccelThreshold > 0 && util > mm.EntryAccelThreshold {
			exponent := 1.0 + (util-mm.EntryAccelThreshold)*mm.EntryAccelExponent
			entrySizeFactor = math.Pow(entrySizeFactor, exponent)
		}
		switch pos.Side {
		case "long":
			buyNotional *= entrySizeFactor
		case "short":
			sellNotional *= entrySizeFactor
		}
	}

	// Trend Guard: suppress entry-side notional when mid deviates from long VWAP (sustained trend).
	// Dual-VWAP confirmation: both short and long VWAPs must agree on direction to confirm a trend
	// (distinguishes genuine trends from wicks that have already reverted).
	// Hysteresis: deactivation threshold is 80% of activation threshold to prevent noisy on/off cycling.
	// Per-side gate factors (1.0 = no suppression). Populated by trend_guard,
	// flow_gate, and regime_gate below. The most-conservative (smallest) factor
	// per side is applied to the notional once, after all gates have evaluated.
	// This replaces multiplicative composition to avoid double-suppression,
	// ordering issues, and divide-by-zero reapply tricks.
	buyTrendGuardFactor := 1.0
	sellTrendGuardFactor := 1.0
	buyFlowGateFactor := 1.0
	sellFlowGateFactor := 1.0
	buyRegimeFactor := 1.0
	sellRegimeFactor := 1.0

	var trendGuardActive bool
	var trendGuardSuppress float64
	var trendGuardDevPct float64
	if mm.TrendGuardEnabled && m.longVWAPs != nil && pos.Side != "" {
		if lc, ok := m.longVWAPs[coin]; ok && lc != nil {
			if lv, lr := lc.VWAP(); lr && lv > 0 {
				devPct := (mid - lv) / lv * 100.0 // signed: positive = mid above long VWAP
				trendGuardDevPct = devPct

				// Dual-VWAP confirmation: require short VWAP to agree on direction.
				// If mid is above long VWAP but short VWAP has caught up (wick completed), skip.
				trendConfirmed := true // default to confirmed if short VWAP unavailable
				if m.shortVWAPs != nil {
					if sc, ok2 := m.shortVWAPs[coin]; ok2 && sc != nil {
						if sv, sr := sc.VWAP(); sr && sv > 0 {
							shortDevPct := (mid - sv) / sv * 100.0
							// Both must agree: mid above both VWAPs (rally) or below both (drop)
							trendConfirmed = (devPct > 0 && shortDevPct > 0) || (devPct < 0 && shortDevPct < 0)
						}
					}
				}

				if trendConfirmed {
					// Adverse deviation: when long, a DROP is adverse (position losing, don't add more);
					// when short, a RALLY is adverse (position losing, don't add more).
					var adverseDev float64
					switch pos.Side {
					case "long":
						adverseDev = -devPct // drop (negative devPct) → positive adverseDev
					case "short":
						adverseDev = devPct // rally (positive devPct) → positive adverseDev
					}
					// Hysteresis: use 80% of threshold for deactivation to prevent noisy on/off cycling.
					threshold := mm.TrendGuardThresholdPct
					if m.trendGuardPrev[coin] {
						threshold *= 0.80
					}
					if adverseDev > threshold {
						// Linear ramp from activation threshold to saturation.
						// Clamp t to [0, 1]: in the hysteresis band (adverseDev between deactivation
						// and activation thresholds), t can be negative — clamp to 0 so the guard
						// stays active (preventing flicker) but applies zero suppression.
						t := (adverseDev - mm.TrendGuardThresholdPct) / (mm.TrendGuardSaturationPct - mm.TrendGuardThresholdPct)
						if t < 0 {
							t = 0
						}
						if t > 1.0 {
							t = 1.0
						}
						suppress := t * mm.TrendGuardMaxSuppress
						trendGuardActive = true
						trendGuardSuppress = suppress
						factor := 1.0 - suppress
						// V20 (second bundle): full entry-side freeze at extreme suppress.
						// Zero the entry-side notional so the side is skipped downstream.
						if mm.TrendGuardFreezeThreshold > 0 && suppress >= mm.TrendGuardFreezeThreshold {
							factor = 0
						}
						switch pos.Side {
						case "long":
							buyTrendGuardFactor = factor
						case "short":
							sellTrendGuardFactor = factor
						}
						// V20 (second bundle): asymmetric entry-side spread widening.
						// When trend_guard is suppressing notional at ≥50%, also push the
						// entry-side price away from the adverse move. Notional suppression
						// alone did not prevent v19 09:49 flash-crash wick fills; widening
						// the quote lifts those levels out of the taker's path too.
						if mm.TrendGuardSpreadWidenBps > 0 && suppress >= 0.5 {
							asymmetry := mm.TrendGuardSpreadWidenAsymmetry
							if asymmetry == 0 {
								asymmetry = 1.0
							}
							entryShift := fairValue * mm.TrendGuardSpreadWidenBps * asymmetry / 10000.0
							switch pos.Side {
							case "long":
								bid -= entryShift
							case "short":
								ask += entryShift
							}
						}
					}
				}
			}
		}
	}
	m.trendGuardPrev[coin] = trendGuardActive

	// Compute inventory utilization once for flow gate and downstream sizing.
	var invUtil float64
	if maxPositionNotional > 0 {
		invUtil = pos.Notional / maxPositionNotional
	}

	// Directional Flow Gate: suppress entry-side notional on adverse trade flow.
	var flowGateActive bool
	var flowGateFactor float64 = 1.0
	if mm.FlowGateEnabled && m.imbalanceCalcs != nil && pos.Side != "" {
		if calc, ok := m.imbalanceCalcs[coin]; ok && calc != nil {
			if imb, ready := calc.Imbalance(); ready {
				// Adverse imbalance: when long, SELL flow is adverse (market moving against position);
				// when short, BUY flow is adverse (market moving against position).
				var adverseImb float64
				switch pos.Side {
				case "long":
					adverseImb = -imb // sell flow (negative imb) → positive adverseImb
				case "short":
					adverseImb = imb // buy flow (positive imb) → positive adverseImb
				}
				if adverseImb > mm.FlowGateThreshold {
					t := (adverseImb - mm.FlowGateThreshold) / (1.0 - mm.FlowGateThreshold)
					if t > 1.0 {
						t = 1.0
					}
					suppress := t * mm.FlowGateMaxSuppress
					// Scale suppression by inventory utilization so flow gate is a no-op when nearly flat.
					if mm.FlowGateMinUtil > 0 {
						utilScale := invUtil / mm.FlowGateMinUtil
						if utilScale > 1.0 {
							utilScale = 1.0
						}
						suppress *= utilScale
					}
					flowGateFactor = 1.0 - suppress
					flowGateActive = true
					switch pos.Side {
					case "long":
						buyFlowGateFactor = flowGateFactor
					case "short":
						sellFlowGateFactor = flowGateFactor
					}
				}
			}
		}
	}

	// P1: Regime-gated quoting. When a sustained trend is detected via
	// autocorrelation + VWAP divergence for at least min_duration_sec, suppress
	// the trend-aligned side (buy in uptrend, sell in downtrend — the side
	// that chases or adds adverse exposure).
	//
	// Composition: regime_gate stages its per-side factor like trend_guard and
	// flow_gate. All three are applied once below via min(factor) — the most
	// conservative (largest) suppression wins. This avoids multiplicative
	// double-suppression, ordering dependencies, and divide-by-zero reapply
	// tricks.
	if mm.RegimeGateEnabled && m.regimes != nil {
		regime := m.regimes[coin]
		if regime != nil && m.shortVWAPs != nil && m.longVWAPs != nil {
			sc := m.shortVWAPs[coin]
			lc := m.longVWAPs[coin]
			var shortV, longV, sigma float64
			var shortOK, longOK, sigOK bool
			if sc != nil {
				shortV, shortOK = sc.VWAP()
				sigma, sigOK = sc.PriceStddev()
			}
			if lc != nil {
				longV, longOK = lc.VWAP()
			}
			if shortOK && longOK && sigOK {
				minDur := time.Duration(mm.RegimeGateMinDurationSec) * time.Second
				active, dir, activeDur := regime.IsTrending(now,
					mm.RegimeAutocorrThreshold, mm.RegimeVWAPDivergenceSigma,
					minDur, shortV, longV, sigma)
				prev := m.regimeGatePrev[coin]
				if active {
					regimeFactor := 1.0 - mm.RegimeGateMaxSuppression
					// Determine entry side: trend up → buys add adverse; trend down → sells add adverse.
					entrySide := "buy"
					if dir == -1 {
						entrySide = "sell"
					}
					// Stage the per-side regime factor; composition with
					// trend_guard and flow_gate happens below via min().
					switch entrySide {
					case "buy":
						buyRegimeFactor = regimeFactor
					case "sell":
						sellRegimeFactor = regimeFactor
					}
					// Edge logging: activated / direction flip / heartbeat.
					autocorrVal, _ := regime.Autocorrelation()
					divVal := math.Abs(shortV-longV) / sigma
					switch {
					case !prev.Active:
						slog.Info("regime gate activated",
							"coin", coin,
							"dir", dir,
							"autocorr", autocorrVal,
							"divergence_sigma", divVal,
							"duration_sec", int(minDur.Seconds()),
							"suppression_factor", mm.RegimeGateMaxSuppression,
						)
						prev = regimeGateState{Active: true, Dir: dir, ActivatedAt: now, LastHeartbeat: now}
					case prev.Dir != dir:
						slog.Info("regime gate deactivated",
							"coin", coin,
							"dir", prev.Dir,
							"active_duration_sec", now.Sub(prev.ActivatedAt).Seconds(),
						)
						slog.Info("regime gate activated",
							"coin", coin,
							"dir", dir,
							"autocorr", autocorrVal,
							"divergence_sigma", divVal,
							"duration_sec", int(minDur.Seconds()),
							"suppression_factor", mm.RegimeGateMaxSuppression,
						)
						prev = regimeGateState{Active: true, Dir: dir, ActivatedAt: now, LastHeartbeat: now}
					case now.Sub(prev.LastHeartbeat) >= 5*time.Minute:
						slog.Info("regime gate heartbeat",
							"coin", coin,
							"dir", dir,
							"autocorr", autocorrVal,
							"divergence_sigma", divVal,
							"active_duration_sec", activeDur.Seconds(),
							"suppression_factor", mm.RegimeGateMaxSuppression,
						)
						prev.LastHeartbeat = now
					}
					m.regimeGatePrev[coin] = prev
				} else if prev.Active {
					slog.Info("regime gate deactivated",
						"coin", coin,
						"dir", prev.Dir,
						"active_duration_sec", now.Sub(prev.ActivatedAt).Seconds(),
					)
					m.regimeGatePrev[coin] = regimeGateState{}
				}
			}
		}
	}

	// Apply gate composition: for each side, pick the most conservative
	// (smallest) factor staged by trend_guard / flow_gate / regime_gate, and
	// multiply the side's notional by it exactly once. This eliminates
	// double-suppression, ordering dependencies, and divide-by-zero reapply
	// tricks that the prior multiplicative composition suffered from.
	buyGateFactor, sellGateFactor := composeGateFactors(
		buyTrendGuardFactor, buyFlowGateFactor, buyRegimeFactor,
		sellTrendGuardFactor, sellFlowGateFactor, sellRegimeFactor,
	)
	buyNotional *= buyGateFactor
	sellNotional *= sellGateFactor

	wickBidSuppressed := wickSuppressBid
	wickAskSuppressed := wickSuppressAsk
	if mm.PriceShockFreezeEntry && priceShock.Active && sideIncreasesPosition(priceShock.EntrySide, pos.Side) {
		switch priceShock.EntrySide {
		case "buy":
			buyNotional = 0
			wickBidSuppressed = wickBidSuppressed || bidMult > 1.0
		case "sell":
			sellNotional = 0
			wickAskSuppressed = wickAskSuppressed || askMult > 1.0
		}
	}
	// V24: asymmetric price-shock response. Only applies when the freeze above
	// fired — i.e. the bot holds or is flat on a side where the entry side
	// increases position. Gating on the same condition as the freeze keeps this
	// coupled to the scenario the V23 03:02 event describes: freeze zeroed buys
	// while sells kept filling into the rally. When the freeze does not fire
	// (e.g. bot holds opposing inventory and the shock side reduces it), we
	// let both sides quote at full size — the position-reducing shock side is
	// doing useful work. Factor is in [0, 1]; 1.0 or unset = disabled.
	if mm.PriceShockFreezeEntry && priceShock.Active &&
		sideIncreasesPosition(priceShock.EntrySide, pos.Side) &&
		mm.PriceShockExitSizeFactor > 0 && mm.PriceShockExitSizeFactor < 1.0 {
		exitFactor := mm.PriceShockExitSizeFactor
		// The exit side is the opposite of the frozen entry side.
		switch priceShock.EntrySide {
		case "buy":
			sellNotional *= exitFactor
		case "sell":
			buyNotional *= exitFactor
		}
	}
	// V30: favorable shock profit-taking. When shock is active AND the shock's
	// EntrySide does NOT increase our position (i.e. the shock direction favors
	// existing inventory), that same side is our profit-taking/cover side.
	// Boost its notional so we can actually realize gains during the cooldown
	// window instead of sitting passively while the market runs. Scenario from
	// V29 07:46:21: short 2.055 CL, CL drops -40 bps (favorable), cover side
	// squeezed to ~$17 by flow_gate + entry_size — no covers filled, unrealized
	// gain evaporated. Only applies when non-flat AND factor > 1.0; symmetric
	// to the adverse exit_size_factor but in the opposite direction.
	if priceShock.Active && pos.Side != "" &&
		!sideIncreasesPosition(priceShock.EntrySide, pos.Side) &&
		mm.PriceShockFavorableExitFactor > 1.0 {
		favFactor := mm.PriceShockFavorableExitFactor
		switch priceShock.EntrySide {
		case "buy":
			buyNotional *= favFactor
		case "sell":
			sellNotional *= favFactor
		}
	}
	if mm.WickTrendSuppressEnabled {
		suppressBuyWick := false
		suppressSellWick := false
		if priceShock.Active && sideIncreasesPosition(priceShock.EntrySide, pos.Side) {
			switch priceShock.EntrySide {
			case "buy":
				suppressBuyWick = true
			case "sell":
				suppressSellWick = true
			}
		}
		if trendGuardActive && trendGuardSuppress >= mm.WickTrendSuppressThreshold {
			switch pos.Side {
			case "long":
				suppressBuyWick = true
			case "short":
				suppressSellWick = true
			}
		}
		if flowGateActive && (1.0-flowGateFactor) >= mm.WickTrendSuppressThreshold {
			switch pos.Side {
			case "long":
				suppressBuyWick = true
			case "short":
				suppressSellWick = true
			}
		}
		if suppressBuyWick && bidMult > 1.0 {
			buyNotional /= bidMult
			bidMult = 1.0
			wickBidSuppressed = true
		}
		if suppressSellWick && askMult > 1.0 {
			sellNotional /= askMult
			askMult = 1.0
			wickAskSuppressed = true
		}
	}

	if pairState.Active {
		tighten := mm.PairReduceTightenFactor
		switch pairState.ExitSide {
		case "sell":
			buyNotional = 0
			ask = fairValue + (ask-fairValue)*tighten
		case "buy":
			sellNotional = 0
			bid = fairValue - (fairValue-bid)*tighten
		}
	}
	// Fixed-sizing mode (signal_pauses_enabled=false) intentionally ignores the
	// sizing gates so both sides keep quoting at the configured notional. The
	// price-based edge gate below still applies its cost floor.
	if !m.cfg.SignalPauses() {
		buyNotional = mm.QuoteNotional
		sellNotional = mm.QuoteNotional
	}

	// Edge gate: floor the exposure-increasing side(s) at measured fee +
	// adverse selection + target net edge. Exit-side quotes keep the normal
	// pipeline (skew, position-age, idle breaker) so inventory can be reduced.
	var edgeReqBid, edgeReqAsk, edgeHalfBid, edgeHalfAsk, edgeFeeBps float64
	var edgeSkipBid, edgeSkipAsk bool
	if mm.EdgeGateEnabled {
		edgeFeeBps = m.measuredFeeBpsFor(coin, mm)
		if pos.Side != "long" {
			edgeReqBid = requiredHalfSpreadBps(edgeFeeBps, m.adverseBpsFor(coin, "buy", mm.EdgeAdverseFloorBps), mm.EdgeTargetBps)
			bid, edgeSkipBid = applyEdgeFloor(bid, fairValue, true, edgeReqBid, mm.EdgeMaxWidthBps)
			if edgeSkipBid {
				buyNotional = 0
			}
		}
		if pos.Side != "short" {
			edgeReqAsk = requiredHalfSpreadBps(edgeFeeBps, m.adverseBpsFor(coin, "sell", mm.EdgeAdverseFloorBps), mm.EdgeTargetBps)
			ask, edgeSkipAsk = applyEdgeFloor(ask, fairValue, false, edgeReqAsk, mm.EdgeMaxWidthBps)
			if edgeSkipAsk {
				sellNotional = 0
			}
		}
		if fairValue > 0 {
			edgeHalfBid = (fairValue - bid) / fairValue * 10000.0
			edgeHalfAsk = (ask - fairValue) / fairValue * 10000.0
		}
	}
	bid, ask = applyCrossingProtection(bid, ask, fairValue, pos.Side, pos.Notional, book.BestBid, book.BestAsk, float64(mm.MinCrossingSpreadBps))

	// Exposure limits
	exposure := exposures[coin]
	// Only apply cross-asset spread exposure deduction when spread engine is active
	// (correlated pair trading). With independent assets, each asset's spread exposure
	// is governed solely by its own per-asset maxSpreadExposure limit.
	// nil = auto: enabled only with 2+ assets (matches main.go logic).
	var otherPair float64
	spreadEngineActive := len(m.cfg.Strategy.Assets) >= 2
	if m.cfg.Strategy.SpreadEngineEnabled != nil {
		spreadEngineActive = *m.cfg.Strategy.SpreadEngineEnabled
	}
	if spreadEngineActive {
		otherPair = pairExposureExcluding(exposures, coin)
	}
	if maxSpreadExposure <= 0 {
		maxSpreadExposure = math.MaxFloat64
	}
	allowedBuy := minFloat(buyNotional, maxPositionNotional-exposure.Long, maxSpreadExposure-otherPair-exposure.Long)
	allowedSell := minFloat(sellNotional, maxPositionNotional-exposure.Short, maxSpreadExposure-otherPair-exposure.Short)
	if allowedBuy < 0 {
		allowedBuy = 0
	}
	if allowedSell < 0 {
		allowedSell = 0
	}

	// Rate limit Degraded: halve notional to conserve rate limit budget.
	if m.scratch.rlTier >= RLTierDegraded {
		allowedBuy *= 0.5
		allowedSell *= 0.5
	}

	szDec := m.szDecimals[coin]
	pow10 := m.pow10Cache[szDec]
	reason := m.cachedReason
	if m.cachedReason == "" || math.Abs(sig.ZScore-m.lastReasonZ) > 0.005 {
		reason = "mm z=" + strconv.FormatFloat(sig.ZScore, 'f', 2, 64)
		m.cachedReason = reason
		m.lastReasonZ = sig.ZScore
	}

	// Volume burst ladders: compute burst state (used by lazy ladder logic below).
	effectiveLadderLevels := mm.LadderLevels
	var volBurstRatio float64
	var volBurstActive bool
	var burstEffectiveLevels int
	if vt := m.velocityTrackers[coin]; vt != nil && !warmupActive && !recoveryActive && !health.Degraded && m.scratch.rlTier < RLTierCaution {
		snap := vt.Snapshot(mm.LadderLevels)
		burstEffectiveLevels = snap.EffectiveLevels
		volBurstRatio = snap.RateRatio
		volBurstActive = snap.IsBurst
	}

	// Lazy ladders: start at 1 level, expand to full ladder_levels on recent fill.
	// Volume burst only amplifies when lazy ladder is already expanded (post-fill),
	// preventing burst from bypassing lazy ladder's idle savings.
	if mm.LazyLadderEnabled {
		lazyExpanded := false
		if ft, ok := m.scratch.lastFillTimes[coin]; ok && !ft.IsZero() {
			if time.Since(ft) <= time.Duration(mm.LazyLadderDecaySec)*time.Second {
				lazyExpanded = true
			}
		}
		if lazyExpanded {
			if volBurstActive {
				effectiveLadderLevels = burstEffectiveLevels
			} else {
				effectiveLadderLevels = mm.LadderLevels
			}
		} else {
			effectiveLadderLevels = 1
		}
	} else if burstEffectiveLevels > 0 {
		// No lazy ladders: burst applies directly (original behavior)
		effectiveLadderLevels = burstEffectiveLevels
	}

	// Quiet period ladder reduction: when no fills for quiet_ladder_reduce_sec,
	// reduce ladder levels by 1 to save rate limit tokens. Minimum 2 levels per side.
	if !volBurstActive && effectiveLadderLevels > 2 && mm.QuietLadderReduceSec > 0 {
		if ft, ok := m.scratch.lastFillTimes[coin]; ok && !ft.IsZero() {
			if time.Since(ft) > time.Duration(mm.QuietLadderReduceSec)*time.Second {
				effectiveLadderLevels = effectiveLadderLevels - 1
			}
		}
	}

	// Rate limit Degraded: collapse to 1 ladder level per side.
	if m.scratch.rlTier >= RLTierDegraded {
		effectiveLadderLevels = 1
	}

	// Lazy ladder notional scaling: when fewer levels are active than configured,
	// scale per-side notional proportionally so a single lazy-level order doesn't
	// concentrate the entire quote_notional budget into one fill.
	if effectiveLadderLevels < mm.LadderLevels && mm.LadderLevels > 0 {
		lazyScale := float64(effectiveLadderLevels) / float64(mm.LadderLevels)
		allowedBuy *= lazyScale
		allowedSell *= lazyScale
	}

	var orders []types.OrderRequest
	wallFrontBuyEnabled := mm.WallFrontEnabled
	wallFrontSellEnabled := mm.WallFrontEnabled
	if wq := m.wallQualityTrackers[coin]; wq != nil {
		wallFrontBuyEnabled = wallFrontBuyEnabled && wq.SideEnabled("buy", now)
		wallFrontSellEnabled = wallFrontSellEnabled && wq.SideEnabled("sell", now)
	}
	// ALWAYS quote both sides (no directional suppression)
	orders = append(orders, buildSideOrders(coin, "buy", bid, mid, spacingBps, allowedBuy, effectiveLadderLevels,
		book.BestBid, book.BestAsk, pow10, reason, mm.LadderSizeGraduation, mm.LadderSpacingExponent,
		book.Bids, wallFrontBuyEnabled, mm.WallMinSizeMultiplier, m.wallFrontLog)...)
	orders = append(orders, buildSideOrders(coin, "sell", ask, mid, spacingBps, allowedSell, effectiveLadderLevels,
		book.BestBid, book.BestAsk, pow10, reason, mm.LadderSizeGraduation, mm.LadderSpacingExponent,
		book.Asks, wallFrontSellEnabled, mm.WallMinSizeMultiplier, m.wallFrontLog)...)

	// Count orders per side to track how often stacked entry-side suppression
	// collapses one side entirely. buildSideOrders returns nil when allowed
	// notional < minOrderNotional, so an empty side means that side is absent
	// from the book for this cycle.
	if tracker := m.sidePresence[coin]; tracker != nil {
		var buyCount, sellCount int
		for _, o := range orders {
			if o.Side == "buy" {
				buyCount++
			} else if o.Side == "sell" {
				sellCount++
			}
		}
		tracker.Record(buyCount, sellCount)
	}

	if maxOrders := ac.Risk.MaxOpenOrders; maxOrders > 0 {
		perCoinCap := maxOrders / len(m.cfg.Strategy.Assets)
		if perCoinCap < 1 {
			perCoinCap = 1
		}
		// Cap each side independently. Truncating the concatenated slice
		// (buys first, then sells) silently removed the whole sell side as
		// soon as ladder_levels > 1.
		perSideCap := perCoinCap / 2
		if perSideCap < 1 {
			perSideCap = 1
		}
		orders = capOrdersPerSide(orders, perSideCap)
	}

	// Cache pipeline intermediates for STATUS dump.
	// Collect diagnostic values for snapshot.
	var toxBuyEWMA, toxSellEWMA float64
	if st := m.toxicityTrackers[coin]; st != nil {
		toxBuyEWMA, toxSellEWMA = st.SideEWMAs()
	}
	optEvalSeq := 0
	if optV2 := m.optimizersV2[coin]; optV2 != nil {
		optEvalSeq = optV2.EvalCount()
	}
	var bboSpreadBps float64
	if mid > 0 {
		bboSpreadBps = (book.BestAsk - book.BestBid) / mid * 10000.0
	}

	m.logQuoteAttribution(
		coin, mm, now,
		fairValue, bid, ask,
		baseBidSpreadBps, baseAskSpreadBps,
		optOverlay.SpreadDeltaBuy, optOverlay.SpreadDeltaSell,
		skewShiftBps, biasBps,
		bidMult, askMult,
		toxBuyMult, toxSellMult,
		trendGuardActive, trendGuardSuppress, trendGuardDevPct,
		flowGateActive, flowGateFactor,
		priceShock.Active, priceShock.MoveBps,
		allowedBuy, allowedSell,
		trendFVShiftBps,
	)

	m.diagMu.Lock()
	m.diagSnaps[coin] = DiagnosticSnapshot{
		FairValue:             fairValue,
		EffectiveSpreadBps:    effectiveSpreadBps,
		SkewShiftBps:          skewShiftBps,
		CrossLegSkewBps:       crossLegBps,
		BiasShiftBps:          biasBps,
		NotionalMult:          notionalMult,
		SkewFactor:            skewFactor,
		VWAPAlpha:             effectiveAlpha,
		Autocorrelation:       autocorr,
		WickBidMult:           bidMult,
		WickAskMult:           askMult,
		ZWideningMult:         zWideningMult,
		WarmupActive:          warmupActive,
		RecoveryActive:        recoveryActive,
		HealthDegraded:        health.Degraded,
		HealthHalted:          health.Halted,
		HealthReason:          health.Reason,
		PairReduceOnly:        pairState.Active,
		Timestamp:             time.Now(),
		ToxBuyMult:            toxBuyMult,
		ToxSellMult:           toxSellMult,
		ToxBuyEWMA:            toxBuyEWMA,
		ToxSellEWMA:           toxSellEWMA,
		OptSpreadDelta:        optOverlay.SpreadDelta,
		OptSpreadDeltaBuy:     optOverlay.SpreadDeltaBuy,
		OptSpreadDeltaSell:    optOverlay.SpreadDeltaSell,
		OptNotionalMult:       optOverlay.NotionalMult,
		OptSkewMult:           optOverlay.SkewMult,
		OptAlphaDelta:         optOverlay.AlphaDelta,
		OptImbalanceShiftMult: optOverlay.ImbalanceShiftMult,
		OptFillDecayMult:      optOverlay.FillDecayMult,
		OptRefreshMult:        optOverlay.RefreshMult,
		OptEvalSeq:            optEvalSeq,
		BurstBuySpreadMult:    burstBuySpreadMult,
		BurstSellSpreadMult:   burstSellSpreadMult,
		BurstBuySizeFactor:    burstBuySizeFactor,
		BurstSellSizeFactor:   burstSellSizeFactor,
		VolBurstRatio:         volBurstRatio,
		VolBurstLevels:        effectiveLadderLevels,
		VolBurstActive:        volBurstActive,

		BookMid:             mid,
		BBOSpreadBps:        bboSpreadBps,
		TradeImbalanceShift: tradeImbalanceShiftBps,
		FillDecayShift:      fillDecayShiftBps,
		InventoryUtil:       invUtil,
		EntrySizeFactor:     entrySizeFactor,
		AllowedBuyNotional:  allowedBuy,
		AllowedSellNotional: allowedSell,
		TrendGuardActive:    trendGuardActive,
		TrendGuardSuppress:  trendGuardSuppress,
		TrendGuardDevPct:    trendGuardDevPct,
		FlowGateActive:      flowGateActive,
		FlowGateFactor:      flowGateFactor,
		PriceShockActive:    priceShock.Active,
		PriceShockSide:      priceShock.EntrySide,
		PriceShockMoveBps:   priceShock.MoveBps,
		WickBidSuppressed:   wickBidSuppressed,
		WickAskSuppressed:   wickAskSuppressed,
		ScheduleMult:        scheduleMult,
		EdgeRequiredBidBps:  edgeReqBid,
		EdgeRequiredAskBps:  edgeReqAsk,
		QuoteHalfBidBps:     edgeHalfBid,
		QuoteHalfAskBps:     edgeHalfAsk,
		EdgeSkipBid:         edgeSkipBid,
		EdgeSkipAsk:         edgeSkipAsk,
		MeasuredFeeBps:      edgeFeeBps,
	}
	m.diagMu.Unlock()

	if m.cfg.ZeusMode {
		orders = capZeusOrders(orders, pos, maxPositionNotional, pow10)
	}
	return orders
}

func (m *Manager) logQuoteAttribution(
	coin string,
	mm config.MarketMakingConfig,
	now time.Time,
	fairValue, bid, ask float64,
	baseBidSpreadBps, baseAskSpreadBps float64,
	optSpreadBuy, optSpreadSell float64,
	skewShiftBps, biasBps float64,
	bidMult, askMult float64,
	toxBuyMult, toxSellMult float64,
	trendActive bool,
	trendSuppress float64,
	trendDevPct float64,
	flowActive bool,
	flowFactor float64,
	shockActive bool,
	shockMoveBps float64,
	allowedBuy, allowedSell float64,
	trendFVShiftBps float64,
) {
	if !mm.QuoteAttributionEnabled {
		return
	}
	intervalSec := mm.QuoteAttributionMinIntervalSec
	if intervalSec <= 0 {
		intervalSec = 30
	}
	// Log on: (a) guard-state edges (any of trend/flow/shock just flipped on/off),
	// or (b) the normal interval cap. The prior code bypassed the interval for the
	// entire duration that any guard was active, producing 20+ logs within 300ms
	// during rapid L2Book churn while flow_gate was engaged.
	guardActive := trendActive || flowActive || shockActive
	m.mu.Lock()
	transitioned := guardActive != m.lastGuardActive[coin]
	m.lastGuardActive[coin] = guardActive
	last := m.lastQuoteAttributionAt[coin]
	shouldLog := transitioned || last.IsZero() || now.Sub(last) >= time.Duration(intervalSec)*time.Second
	if shouldLog {
		m.lastQuoteAttributionAt[coin] = now
	}
	m.mu.Unlock()
	if !shouldLog {
		return
	}
	slog.Info("quote attribution",
		"coin", coin,
		"fair_value", fmt.Sprintf("%.4f", fairValue),
		"bid", fmt.Sprintf("%.4f", bid),
		"ask", fmt.Sprintf("%.4f", ask),
		"base_bid_bps", fmt.Sprintf("%.2f", baseBidSpreadBps),
		"base_ask_bps", fmt.Sprintf("%.2f", baseAskSpreadBps),
		"opt_bid_bps", fmt.Sprintf("%.2f", optSpreadBuy),
		"opt_ask_bps", fmt.Sprintf("%.2f", optSpreadSell),
		"skew_entry_bps", fmt.Sprintf("%.2f", skewShiftBps),
		"bias_bps", fmt.Sprintf("%.2f", biasBps),
		"wick_bid", fmt.Sprintf("%.2f", bidMult),
		"wick_ask", fmt.Sprintf("%.2f", askMult),
		"tox_buy", fmt.Sprintf("%.2f", toxBuyMult),
		"tox_sell", fmt.Sprintf("%.2f", toxSellMult),
		"trend_guard", trendActive,
		"tg_suppress", fmt.Sprintf("%.2f", trendSuppress),
		"tg_dev_pct", fmt.Sprintf("%.4f", trendDevPct),
		"flow_gate", flowActive,
		"fg_factor", fmt.Sprintf("%.2f", flowFactor),
		"price_shock", shockActive,
		"price_shock_bps", fmt.Sprintf("%.1f", shockMoveBps),
		"allowed_buy", fmt.Sprintf("%.1f", allowedBuy),
		"allowed_sell", fmt.Sprintf("%.1f", allowedSell),
		"trend_fv_shift_bps", fmt.Sprintf("%.2f", trendFVShiftBps),
	)
}

// computeLevelNotionals distributes totalNotional across n levels using
// geometric weighting. Level 0 (closest to mid) gets the smallest allocation.
// graduation is the ratio between consecutive levels (1.0 = uniform).
// If the innermost level would fall below minNotional, graduation is iteratively
// reduced toward 1.0 until the constraint is met.
func computeLevelNotionals(totalNotional float64, n int, graduation, minNotional float64) []float64 {
	if n <= 0 {
		return nil
	}
	if n == 1 {
		return []float64{totalNotional}
	}

	g := graduation
	for attempts := 0; attempts < 50; attempts++ {
		// Compute geometric weights: g^0, g^1, ..., g^(n-1)
		weights := make([]float64, n)
		sum := 0.0
		for i := 0; i < n; i++ {
			weights[i] = math.Pow(g, float64(i))
			sum += weights[i]
		}
		// Normalize so allocations sum to totalNotional.
		notionals := make([]float64, n)
		for i := 0; i < n; i++ {
			notionals[i] = totalNotional * weights[i] / sum
		}
		// Check innermost level meets minimum.
		if notionals[0] >= minNotional || g <= 1.0 {
			return notionals
		}
		// Halve the excess above 1.0 and retry.
		g = 1.0 + (g-1.0)/2.0
	}
	// Fallback: uniform.
	notionals := make([]float64, n)
	for i := range notionals {
		notionals[i] = totalNotional / float64(n)
	}
	return notionals
}

// oneTick computes the minimum tick increment for a price using Hyperliquid's 5-sig-fig rule.
func oneTick(price float64) float64 {
	if price == 0 {
		return 0
	}
	d := math.Ceil(math.Log10(math.Abs(price)))
	return math.Pow(10, d-5)
}

// findWallPrice scans L2 levels within the ladder's price range and returns the price
// of the largest level if it exceeds minSize. Levels are sorted best-first.
func findWallPrice(side string, levels []types.BookLevel, bestBookPrice, worstLadderPrice, wallMinSizeMultiplier float64) (float64, bool) {
	// First pass: collect in-range non-BBO levels, find largest.
	var totalSize float64
	var count int
	var wallPrice float64
	var wallSize float64
	for _, lv := range levels {
		if side == "buy" {
			if lv.Price < worstLadderPrice {
				break
			}
		} else {
			if lv.Price > worstLadderPrice {
				break
			}
		}
		// Skip BBO — fronting it just duplicates the clamped price.
		if lv.Price == bestBookPrice {
			continue
		}
		totalSize += lv.Size
		count++
		if lv.Size > wallSize {
			wallSize = lv.Size
			wallPrice = lv.Price
		}
	}
	if count == 0 {
		return 0, false
	}
	avgSize := totalSize / float64(count)
	if wallSize >= avgSize*wallMinSizeMultiplier {
		return wallPrice, true
	}
	return 0, false
}

func buildSideOrders(
	coin, side string,
	startPrice, mid, spacingBps, allowedNotional float64,
	levels int,
	bestBid, bestAsk, pow10Mult float64,
	reason string,
	graduation float64,
	spacingExponent float64,
	bookLevels []types.BookLevel,
	wallFrontEnabled bool,
	wallMinSizeMultiplier float64,
	wallLog map[string]*wallFrontLogEntry,
) []types.OrderRequest {
	if allowedNotional < minOrderNotional || levels < 1 {
		return nil
	}

	sideLevels := int(allowedNotional / minOrderNotional)
	if sideLevels < 1 {
		return nil
	}
	if sideLevels > levels {
		sideLevels = levels
	}

	levelNotionals := computeLevelNotionals(allowedNotional, sideLevels, graduation, minOrderNotional)

	// Wall-fronting: find the snap level and price if applicable.
	wallSnapLevel := -1
	wallSnapPrice := 0.0
	if wallFrontEnabled && len(bookLevels) > 0 && sideLevels >= 2 {
		// Compute worst ladder price (outermost level using fixed spacing).
		worstOffset := mid * math.Pow(float64(sideLevels-1), spacingExponent) * spacingBps / 10000.0
		var worstLadderPrice float64
		if side == "buy" {
			worstLadderPrice = startPrice - worstOffset
		} else {
			worstLadderPrice = startPrice + worstOffset
		}

		var bestBookPrice float64
		if side == "buy" {
			bestBookPrice = bestBid
		} else {
			bestBookPrice = bestAsk
		}
		if wp, ok := findWallPrice(side, bookLevels, bestBookPrice, worstLadderPrice, wallMinSizeMultiplier); ok {
			// Snap price: one tick in front of the wall.
			tick := oneTick(wp)
			if side == "buy" {
				wallSnapPrice = types.RoundToTickSize(wp + tick)
				// Don't cross the spread.
				if bestAsk > 0 && wallSnapPrice >= bestAsk {
					wallSnapPrice = types.RoundToTickSize(bestAsk - tick)
				}
			} else {
				wallSnapPrice = types.RoundToTickSize(wp - tick)
				if bestBid > 0 && wallSnapPrice <= bestBid {
					wallSnapPrice = types.RoundToTickSize(bestBid + tick)
				}
			}

			// Find the closest ladder level to the wall price.
			minDist := math.MaxFloat64
			for i := 0; i < sideLevels; i++ {
				offset := mid * math.Pow(float64(i), spacingExponent) * spacingBps / 10000.0
				var levelPrice float64
				if side == "buy" {
					levelPrice = startPrice - offset
				} else {
					levelPrice = startPrice + offset
				}
				dist := math.Abs(levelPrice - wp)
				if dist < minDist {
					minDist = dist
					wallSnapLevel = i
				}
			}
			// Rate-limit wall-front snap logs: Info on change or every 30s, Debug otherwise.
			wallKey := coin + ":" + side
			logLevel := slog.LevelDebug
			if wallLog != nil {
				now := time.Now()
				if entry := wallLog[wallKey]; entry == nil || entry.price != wp || now.Sub(entry.lastAt) >= wallFrontLogInterval {
					logLevel = slog.LevelInfo
					if entry == nil {
						wallLog[wallKey] = &wallFrontLogEntry{price: wp, lastAt: now}
					} else {
						entry.price = wp
						entry.lastAt = now
					}
				}
			}
			slog.Log(context.Background(), logLevel, "wall-front snap",
				"coin", coin,
				"side", side,
				"wall_price", wp,
				"snap_price", wallSnapPrice,
				"snap_level", wallSnapLevel,
			)
		}
	}

	orders := make([]types.OrderRequest, 0, sideLevels)
	for i := 0; i < sideLevels; i++ {
		levelSize := computeSize(levelNotionals[i], mid, pow10Mult)
		if levelSize == 0 {
			continue
		}
		var price float64
		wallFront := false
		if i == wallSnapLevel {
			price = wallSnapPrice
			wallFront = true
		} else {
			offset := mid * math.Pow(float64(i), spacingExponent) * spacingBps / 10000.0
			if side == "buy" {
				price = types.RoundToTickSize(startPrice - offset)
				if bestBid > 0 && price > bestBid {
					price = bestBid
				}
			} else {
				price = types.RoundToTickSize(startPrice + offset)
				if bestAsk > 0 && price < bestAsk {
					price = bestAsk
				}
			}
		}
		orders = append(orders, types.OrderRequest{
			Coin:        coin,
			Side:        side,
			Price:       price,
			Size:        levelSize,
			Reason:      reason,
			TimeInForce: "ALO",
			WallFront:   wallFront,
		})
	}
	return orders
}

func quotesMateriallyDifferent(
	current []types.OrderRequest,
	desired []types.OrderRequest,
	priceThresholdBps float64,
	sizeThresholdPct float64,
) bool {
	current = normalizeOrders(current)
	desired = normalizeOrders(desired)
	if len(current) != len(desired) {
		return true
	}

	for i := range current {
		if current[i].Side != desired[i].Side {
			return true
		}
		if priceDeltaBps(current[i].Price, desired[i].Price) > priceThresholdBps {
			return true
		}
		if sizeDeltaPct(current[i].Size, desired[i].Size) > sizeThresholdPct {
			return true
		}
	}
	return false
}

// capOrdersPerSide keeps at most perSide orders for each side while preserving
// the original order (inner levels first). Both sides always survive when present.
func capOrdersPerSide(orders []types.OrderRequest, perSide int) []types.OrderRequest {
	if perSide <= 0 {
		return orders
	}
	out := make([]types.OrderRequest, 0, len(orders))
	buys, sells := 0, 0
	for _, order := range orders {
		switch normalizeOrderSide(order.Side) {
		case "buy":
			if buys >= perSide {
				continue
			}
			buys++
		case "sell":
			if sells >= perSide {
				continue
			}
			sells++
		}
		out = append(out, order)
	}
	return out
}

func normalizeOrders(orders []types.OrderRequest) []types.OrderRequest {
	result := copyOrders(orders)
	for i := range result {
		result[i].Side = normalizeOrderSide(result[i].Side)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Side != result[j].Side {
			return result[i].Side < result[j].Side
		}
		if result[i].Side == "buy" {
			return result[i].Price > result[j].Price
		}
		return result[i].Price < result[j].Price
	})
	return result
}

func normalizeOrderSide(side string) string {
	switch side {
	case "B", "b", "buy":
		return "buy"
	case "A", "a", "ask", "S", "s", "sell":
		return "sell"
	default:
		return side
	}
}

func hasOrderSide(orders []types.OrderRequest, side string) bool {
	side = normalizeOrderSide(side)
	for _, order := range orders {
		if normalizeOrderSide(order.Side) == side {
			return true
		}
	}
	return false
}

func removeOrdersBySide(orders []types.OrderRequest, side string) []types.OrderRequest {
	side = normalizeOrderSide(side)
	filtered := orders[:0]
	for _, order := range orders {
		if normalizeOrderSide(order.Side) == side {
			continue
		}
		filtered = append(filtered, order)
	}
	return filtered
}

func copyOrders(orders []types.OrderRequest) []types.OrderRequest {
	result := make([]types.OrderRequest, len(orders))
	copy(result, orders)
	return result
}

func copyOrdersByCoin(src map[string][]types.OrderRequest) map[string][]types.OrderRequest {
	dst := make(map[string][]types.OrderRequest, len(src))
	for coin, orders := range src {
		dst[coin] = copyOrders(orders)
	}
	return dst
}

func accumulatePositionExposure(exposures map[string]sideExposure, pos types.PositionUpdate) {
	exp := exposures[pos.Coin]
	switch pos.Side {
	case "long":
		exp.Long += pos.Notional
	case "short":
		exp.Short += pos.Notional
	}
	exposures[pos.Coin] = exp
}

func accumulateOrderExposure(exposures map[string]sideExposure, orders []types.OrderRequest) {
	for _, order := range orders {
		exp := exposures[order.Coin]
		notional := order.Price * order.Size
		switch normalizeOrderSide(order.Side) {
		case "buy":
			exp.Long += notional
		case "sell":
			exp.Short += notional
		}
		exposures[order.Coin] = exp
	}
}

func pairExposureExcluding(exposures map[string]sideExposure, exclude string) float64 {
	var total float64
	for coin, exp := range exposures {
		if coin == exclude {
			continue
		}
		total += math.Max(exp.Long, exp.Short)
	}
	return total
}

func priceDeltaBps(a, b float64) float64 {
	if a <= 0 || b <= 0 {
		if a == b {
			return 0
		}
		return math.MaxFloat64
	}
	return math.Abs(a-b) / math.Max(a, b) * 10000.0
}

func sizeDeltaPct(a, b float64) float64 {
	if a == 0 || b == 0 {
		if a == b {
			return 0
		}
		return 1.0
	}
	return math.Abs(a-b) / math.Max(a, b)
}

// composeGateFactors picks the most-conservative (smallest) staged factor per
// side from trend_guard, flow_gate, and regime_gate. Each argument is a factor
// in [0, 1] where 1.0 means "no suppression". This replaces multiplicative
// composition — which would stack suppressions from independent gates that
// happen to align on the same side — with a single min() application so that
// the dominant gate governs without compounding.
func composeGateFactors(
	buyTrendGuard, buyFlowGate, buyRegime,
	sellTrendGuard, sellFlowGate, sellRegime float64,
) (buyFactor, sellFactor float64) {
	return minFloat(buyTrendGuard, buyFlowGate, buyRegime),
		minFloat(sellTrendGuard, sellFlowGate, sellRegime)
}

func minFloat(values ...float64) float64 {
	if len(values) == 0 {
		return 0
	}
	min := values[0]
	for _, v := range values[1:] {
		if v < min {
			min = v
		}
	}
	return min
}
