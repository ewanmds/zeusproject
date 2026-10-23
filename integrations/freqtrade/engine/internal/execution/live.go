package execution

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"hyperliquid-market-maker/internal/zeusbridge"
	"io"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

// RPSTracker counts SDK requests using lock-free atomics.
type RPSTracker struct {
	exchange atomic.Int64 // Buy, Sell, Cancel, CancelAll, ClosePosition
	info     atomic.Int64 // ClearinghouseState, OpenOrders, SpotClearinghouseState
}

// RPSSnapshot holds a point-in-time request count snapshot.
type RPSSnapshot struct {
	Exchange int64
	Info     int64
	Total    int64
}

func (t *RPSTracker) RecordExchange()         { t.exchange.Add(1) }
func (t *RPSTracker) RecordExchangeN(n int64) { t.exchange.Add(n) }
func (t *RPSTracker) RecordInfo()             { t.info.Add(1) }

func (t *RPSTracker) Snapshot() RPSSnapshot {
	ex := t.exchange.Swap(0)
	inf := t.info.Swap(0)
	return RPSSnapshot{Exchange: ex, Info: inf, Total: ex + inf}
}

// LiveExecutor executes real orders on Hyperliquid via the SDK.
type LiveExecutor struct {
	sdk      *hyperliquid.SDK
	dexes    map[string]string // coin → dex name ("xyz" or "")
	hasDex   bool              // true if any asset is HIP-3
	hasPerps bool              // true if any asset is a standard perp
	dexName  string            // the HIP-3 dex name (e.g. "xyz"), empty if no HIP-3 assets
	cfg      *config.Config    // optional; used for per-asset skip thresholds in diffOrders
	fillCh   <-chan types.FillEvent
	posCh    chan types.PositionUpdate

	reconcileCh <-chan struct{} // external trigger for immediate reconciliation (e.g. WS reconnect)

	positions       map[string]*livePosition
	mu              sync.Mutex
	cachedEquity    float64
	equityMu        sync.RWMutex
	equityRingBuf   [5]float64 // rolling window for median filter
	equityRingCount int        // samples seen so far (0..5+)

	cachedOpenOrders map[string][]types.OrderRequest
	openOrdersMu     sync.RWMutex
	assets           []string // traded assets, set during Init

	latestBook          map[string]types.BookUpdate // latest book per coin for slippage-protected closes
	bookMu              sync.RWMutex
	maxCloseSlippageBps int // max slippage bps for limit IOC close; 0 = market close only

	szDecimals map[string]int // coin -> size decimal places for rounding

	stats RPSTracker

	upstreamErrCount int
	upstreamLastErr  time.Time
	upstreamMu       sync.Mutex

	liveOrders            map[string][]trackedOrder // coin -> resting orders we placed
	orderEpoch            map[string]uint64         // coin -> incremented on fill; detects stale refresh writes
	orderGeneration       map[string]uint64
	lastOrderGenerationAt map[string]time.Time

	transport Transport // abstracts SDK vs Direct API for order operations

	openOrdersFn func(string) []types.OrderRequest

	bookSeq           atomic.Int64 // incremented on each UpdateBook; used for stale retry guard
	refreshInProgress atomic.Int32 // >0 while Refresh/RefreshMulti is executing; suppresses order-count reconciliation

	totalFees        float64 // cumulative exchange fees (USD)
	totalBuilderFees float64 // cumulative builder fees (USD)
	feeMu            sync.Mutex

	onFillCallback func(types.FillEvent)

	lastFillSignFlip map[string]time.Time // coin -> timestamp of last fill that flipped position sign
	lastFillTime     map[string]time.Time // coin -> timestamp of most recent fill (any side)

	// Fill poller state: polls UserFillsByTime every 3s for reliable fill detection.
	seenFillIDs     map[string]int64 // "oid:timeMs:coin:price:size:side" -> fill timestamp (poller self-dedup)
	seenFillContent map[string]int64 // "oid:coin:price:size:side" -> insertion time (cross-source dedup)
	lastPollTime    int64            // epoch ms of last poll window start
	seenMu          sync.Mutex       // guards seenFillIDs, seenFillContent, lastPollTime

	initTime          time.Time // when Init() completed; WS fills within wsReplayDrainDuration are skipped
	lastReconnectTime time.Time // reset on each WS reconnect to re-enable drain window

	healthMu                  sync.RWMutex
	lastFillPollAt            time.Time
	lastFillPollSuccessAt     time.Time
	fillPollConsecutiveErrors int
	coinHealth                map[string]*coinHealthState

	reconDiagMu      sync.Mutex
	reconDiagCounter map[string][]time.Time

	// Rate-limit snapshot, set by main.go alongside quoting.Manager.UpdateRateLimit.
	// Used to annotate recon mismatch diagnostics so exchange-side auto-cancel
	// under budget pressure is distinguishable from other disappearance causes.
	rateLimitMu        sync.RWMutex
	rateLimitRemaining int
	rateLimitCap       int
}

// UpdateRateLimit records the latest cumulative (since-inception) API request
// budget so the recon mismatch diagnostic can correlate silent order losses
// with low-budget conditions. Mirrors quoting.Manager.UpdateRateLimit — call
// both together from the main loop's rate-limit poller.
func (le *LiveExecutor) UpdateRateLimit(remaining, cap int) {
	le.rateLimitMu.Lock()
	le.rateLimitRemaining = remaining
	le.rateLimitCap = cap
	le.rateLimitMu.Unlock()
}

func (le *LiveExecutor) rateLimitSnapshot() (remaining, cap int) {
	le.rateLimitMu.RLock()
	defer le.rateLimitMu.RUnlock()
	return le.rateLimitRemaining, le.rateLimitCap
}

type livePosition struct {
	side     string
	size     float64
	avgEntry float64
}

type coinHealthState struct {
	QuoteHalt                bool
	Reason                   string
	LastMismatchAt           time.Time
	MatchingReconciles       int
	LastHealthyReconcileAt   time.Time
	ConsecutiveOrderMismatch int
	// Consecutive soft (non-dust) position mismatches for cancel-on-drift.
	ConsecutiveSoftMismatch int
	FirstSoftMismatchAt     time.Time
}

type placeOrderResult struct {
	size         float64 // actual submitted quantity after a Zeus fill-race cap
	benignReject bool
	oid          int64 // OID of resting order, 0 if not resting
}

// cancelTarget pairs an OID with its coin for cross-asset batch cancels.
type cancelTarget struct {
	oid  int64
	coin string
}

// trackedOrder represents a resting order we placed and are tracking by OID.
type trackedOrder struct {
	OID       int64
	Side      string // "buy" or "sell"
	Price     float64
	Size      float64
	WallFront bool
}

// modifyTarget pairs a tracked order with the desired replacement for batch modify.
type modifyTarget struct {
	tracked *trackedOrder
	desired *types.OrderRequest
}

// orderAction classifies what to do with a tracked/desired order pair.
type orderAction int

const (
	actionSkip   orderAction = iota // tracked and desired are close enough, no API call
	actionModify                    // modify tracked order in-place to match desired
	actionCancel                    // cancel tracked order (no corresponding desired)
	actionPlace                     // place new order (no corresponding tracked)
)

// orderDiff pairs a tracked order with a desired order and an action.
type orderDiff struct {
	action  orderAction
	tracked *trackedOrder       // non-nil for skip, modify, cancel
	desired *types.OrderRequest // non-nil for skip, modify, place
}

// Default thresholds for skipping modify calls — used when no config is set.
const (
	defaultSkipPriceThresholdBps = 5.0
	defaultSkipSizeThresholdPct  = 0.25
)

func (le *LiveExecutor) publishPositionUpdate(pu types.PositionUpdate) {
	select {
	case le.posCh <- pu:
	default:
		slog.Warn("dropping position update, channel full",
			"coin", pu.Coin, "side", pu.Side, "size", pu.Size)
	}
}

func (le *LiveExecutor) markOrderGenerationLocked(coin string) {
	if le.orderGeneration == nil {
		le.orderGeneration = make(map[string]uint64)
	}
	if le.lastOrderGenerationAt == nil {
		le.lastOrderGenerationAt = make(map[string]time.Time)
	}
	le.orderGeneration[coin]++
	le.lastOrderGenerationAt[coin] = time.Now()
}

func desiredOrdersForce(orders []types.OrderRequest) bool {
	for _, o := range orders {
		if o.Force {
			return true
		}
	}
	return false
}

func trackedFromOrder(oid int64, order types.OrderRequest) trackedOrder {
	return trackedOrder{
		OID:       oid,
		Side:      normalizeSide(order.Side),
		Price:     order.Price,
		Size:      order.Size,
		WallFront: order.WallFront,
	}
}

// NewLiveExecutor creates a live executor.
// fillCh receives FillEvent from the WebSocket feed.
// assets is the list of traded assets (e.g. ["xyz:CL", "BTC"]); per-asset dex
// is derived from the asset name prefix.
func NewLiveExecutor(sdk *hyperliquid.SDK, fillCh <-chan types.FillEvent, assets []string, maxCloseSlippageBps int) *LiveExecutor {
	dexes := make(map[string]string, len(assets))
	var hasDex, hasPerps bool
	var dexName string
	for _, asset := range assets {
		dex, _ := types.ParseAsset(asset)
		dexes[asset] = dex
		if dex != "" {
			hasDex = true
			dexName = dex
		} else {
			hasPerps = true
		}
	}

	le := &LiveExecutor{
		sdk:                   sdk,
		dexes:                 dexes,
		hasDex:                hasDex,
		hasPerps:              hasPerps,
		dexName:               dexName,
		fillCh:                fillCh,
		posCh:                 make(chan types.PositionUpdate, 100),
		positions:             make(map[string]*livePosition),
		latestBook:            make(map[string]types.BookUpdate),
		liveOrders:            make(map[string][]trackedOrder),
		orderEpoch:            make(map[string]uint64),
		orderGeneration:       make(map[string]uint64),
		lastOrderGenerationAt: make(map[string]time.Time),
		cachedOpenOrders:      make(map[string][]types.OrderRequest),
		maxCloseSlippageBps:   maxCloseSlippageBps,
		seenFillIDs:           make(map[string]int64),
		seenFillContent:       make(map[string]int64),
		coinHealth:            make(map[string]*coinHealthState),
		lastFillSignFlip:      make(map[string]time.Time),
		lastFillTime:          make(map[string]time.Time),
		reconDiagCounter:      make(map[string][]time.Time),
	}
	le.transport = NewSDKTransport(le)
	return le
}

// dexForCoin returns the HIP-3 dex name for a coin, or "" for standard perps.
func (le *LiveExecutor) dexForCoin(coin string) string {
	return resolveDexForCoin(le.dexes, coin)
}

// resolveDexForCoin returns the HIP-3 dex name for a coin, falling back to
// parsing the coin name when the dexes map has no entry.
func resolveDexForCoin(dexes map[string]string, coin string) string {
	if d, ok := dexes[coin]; ok {
		return d
	}
	dex, _ := types.ParseAsset(coin)
	return dex
}

// SetConfig sets the config reference used for per-asset skip thresholds in diffOrders.
func (le *LiveExecutor) SetConfig(cfg *config.Config) {
	le.cfg = cfg
}

// SetSzDecimals sets the size decimals map used for rounding close order sizes.
func (le *LiveExecutor) SetSzDecimals(szDec map[string]int) {
	le.szDecimals = szDec
}

// SetOnFill sets a callback invoked after each fill is processed.
// Must be called during initialization, before Run() starts processing fills.
func (le *LiveExecutor) SetOnFill(fn func(types.FillEvent)) {
	le.onFillCallback = fn
}

// roundSizeForCoin rounds a size value to the exchange-required decimal precision
// for the given coin. This prevents 422 "Failed to deserialize" errors caused by
// floating-point artifacts (e.g., 1.9240000000000002 instead of 1.924).
func (le *LiveExecutor) roundSizeForCoin(coin string, size float64) float64 {
	if le.szDecimals == nil {
		return size
	}
	dec, ok := le.szDecimals[coin]
	if !ok {
		return size
	}
	pow10 := math.Pow(10, float64(dec))
	return math.Round(size*pow10) / pow10
}

// SetReconnectCh sets the channel that triggers immediate position reconciliation
// (e.g. after a WebSocket reconnect where fills may have been missed).
func (le *LiveExecutor) SetReconnectCh(ch <-chan struct{}) {
	le.reconcileCh = ch
}

// HealthSnapshot returns a copy of live execution health telemetry for quote gating.
func (le *LiveExecutor) HealthSnapshot() HealthSnapshot {
	le.healthMu.RLock()
	defer le.healthMu.RUnlock()

	out := HealthSnapshot{
		LastFillPollAt:            le.lastFillPollAt,
		LastFillPollSuccessAt:     le.lastFillPollSuccessAt,
		FillPollConsecutiveErrors: le.fillPollConsecutiveErrors,
		Coins:                     make(map[string]CoinHealth, len(le.coinHealth)),
	}
	for coin, state := range le.coinHealth {
		out.Coins[coin] = CoinHealth{
			QuoteHalt:          state.QuoteHalt,
			Reason:             state.Reason,
			LastMismatchAt:     state.LastMismatchAt,
			MatchingReconciles: state.MatchingReconciles,
		}
	}
	return out
}

// UpdateBook stores the latest book snapshot for slippage-protected position closes.
func (le *LiveExecutor) UpdateBook(book types.BookUpdate) {
	le.bookMu.Lock()
	le.latestBook[book.Coin] = book
	le.bookMu.Unlock()
	le.bookSeq.Add(1)
}

// Init bootstraps positions and equity from on-chain state.
func (le *LiveExecutor) Init(ctx context.Context, assets []string) error {
	le.mu.Lock()
	defer le.mu.Unlock()

	// Query HIP-3 dex clearinghouse if any assets are on a dex.
	if le.hasDex {
		state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address(), hyperliquid.WithDex(le.dexName))
		le.stats.RecordInfo()
		if err != nil {
			return fmt.Errorf("init: query dex clearinghouse: %w", err)
		}
		parsePositionsInto(state, le.dexName, le.positions)
	}

	// Query standard perps clearinghouse if any assets are standard perps.
	if le.hasPerps {
		state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address())
		le.stats.RecordInfo()
		if err != nil {
			return fmt.Errorf("init: query perps clearinghouse: %w", err)
		}
		parsePositionsInto(state, "", le.positions)
	}

	// Emit position updates for all bootstrapped positions.
	for fullName, pos := range le.positions {
		pu := types.PositionUpdate{
			Coin:     fullName,
			Side:     pos.side,
			Size:     pos.size,
			AvgEntry: pos.avgEntry,
			Notional: pos.size * pos.avgEntry,
		}

		slog.Info("bootstrapped position",
			"coin", fullName,
			"side", pos.side,
			"size", pos.size,
			"avg_entry", pos.avgEntry,
		)

		le.publishPositionUpdate(pu)
	}

	le.assets = assets
	le.healthMu.Lock()
	for _, asset := range assets {
		if _, ok := le.coinHealth[asset]; !ok {
			le.coinHealth[asset] = &coinHealthState{}
		}
	}
	le.healthMu.Unlock()

	// Seed equity cache and ring buffer so median filter is active from first poll.
	eq := le.queryEquityAllSources()
	le.equityMu.Lock()
	le.cachedEquity = eq
	le.equityRingBuf = [5]float64{eq}
	le.equityRingCount = 1
	le.equityMu.Unlock()
	slog.Info("bootstrapped equity", "equity", eq)

	// Seed open orders cache.
	le.openOrdersMu.Lock()
	for _, asset := range assets {
		le.cachedOpenOrders[asset] = le.fetchOpenOrders(asset)
	}
	le.openOrdersMu.Unlock()

	// Seed fill dedup maps with recent fills so the WS fill replay on connect
	// doesn't corrupt the position state we just bootstrapped from chain.
	le.seedFillDedup()

	le.initTime = time.Now()

	return nil
}

// parsePositionsInto extracts positions from a clearinghouse state response
// and adds them to the target map. dex is the HIP-3 dex prefix ("xyz") or "" for perps.
func parsePositionsInto(state map[string]any, dex string, target map[string]*livePosition) {
	assetPositions, _ := state["assetPositions"].([]any)
	for _, ap := range assetPositions {
		apMap, ok := ap.(map[string]any)
		if !ok {
			continue
		}
		posMap, ok := apMap["position"].(map[string]any)
		if !ok {
			continue
		}

		coin, _ := posMap["coin"].(string)
		if coin == "" {
			continue
		}

		// Reconstruct full asset name: HIP-3 chain may return bare "CL" → "xyz:CL".
		fullName := coin
		if dex != "" && !strings.HasPrefix(coin, dex+":") {
			fullName = dex + ":" + coin
		}

		sziStr, _ := posMap["szi"].(string)
		entryStr, _ := posMap["entryPx"].(string)
		szi := parseFloatFromAPI(sziStr, "szi", fullName)
		entryPx := parseFloatFromAPI(entryStr, "entryPx", fullName)

		if szi == 0 {
			continue
		}

		pos := &livePosition{avgEntry: entryPx}
		if szi > 0 {
			pos.side = "long"
			pos.size = szi
		} else {
			pos.side = "short"
			pos.size = -szi
		}
		target[fullName] = pos
	}
}

// seedFillDedup queries recent fills and pre-populates the dedup maps so that
// historical fills replayed by the WebSocket on connect are correctly skipped.
// Without this, WS fill replay corrupts the position state bootstrapped by Init().
func (le *LiveExecutor) seedFillDedup() {
	// Query fills from the last 60 minutes — wide enough to cover the WS replay
	// window which can include fills well beyond the 5-minute dedup eviction window.
	startTime := time.Now().Add(-60 * time.Minute).UnixMilli()
	fills, err := le.queryFillsByTime(startTime)
	le.stats.RecordInfo()
	if err != nil {
		slog.Warn("seed fill dedup: query failed, WS fill replay may cause position drift", "error", err)
		return
	}

	le.seenMu.Lock()
	le.lastPollTime = time.Now().UnixMilli()
	count := 0
	for _, f := range fills {
		fm, ok := f.(map[string]any)
		if !ok {
			continue
		}

		timeMs := int64(0)
		if v, ok := fm["time"].(float64); ok {
			timeMs = int64(v)
		}
		fill := le.parseFillFromAPI(fm)
		key := buildFillDedupKey(fill, timeMs)

		le.seenFillIDs[key] = timeMs
		contentKey := buildFillContentKey(fill)
		le.seenFillContent[contentKey] = le.lastPollTime
		count++
	}
	le.seenMu.Unlock()

	slog.Info("seeded fill dedup maps", "recent_fills", count, "window_minutes", 60)
}

// Run processes incoming fill events. Blocks until ctx is cancelled.
func (le *LiveExecutor) Run(ctx context.Context) {
	slog.Info("live executor started")

	reconcileTicker := time.NewTicker(30 * time.Second) // V1 analysis: reduced from 60s — faster drift detection
	defer reconcileTicker.Stop()

	// reconcileCh may be nil if no reconnect signal is wired up;
	// a nil channel never fires in select, which is the desired default.
	reconnectCh := le.reconcileCh

	go le.pollEquity(ctx)
	go le.pollOpenOrders(ctx)
	go le.pollFills(ctx)

	const (
		normalReconcileInterval = 30 * time.Second // V1 analysis: reduced from 60s — faster drift detection
		haltedReconcileInterval = 15 * time.Second
	)

	for {
		select {
		case <-ctx.Done():
			slog.Info("live executor stopped")
			return
		case fill := <-le.fillCh:
			le.dedupAndHandleFill(fill)
		case <-reconcileTicker.C:
			le.reconcilePositions()

			// Accelerate reconciliation when any coin is halted so
			// recovery (2 consecutive healthy ticks) happens in ~30s
			// instead of ~120s.
			anyHalted := le.anyCoinHalted()
			if anyHalted {
				reconcileTicker.Reset(haltedReconcileInterval)
			} else {
				reconcileTicker.Reset(normalReconcileInterval)
			}
		case <-reconnectCh:
			slog.Info("immediate reconciliation triggered by WebSocket reconnect, cancelling all orders first")
			le.lastReconnectTime = time.Now()
			cancelCtx, cancelCancel := context.WithTimeout(ctx, 5*time.Second)
			for _, asset := range le.assets {
				if err := le.CancelAll(cancelCtx, asset); err != nil {
					slog.Error("reconnect: cancel-all failed", "coin", asset, "err", err)
				}
			}
			cancelCancel()
			le.reconcilePositions()
		}
	}
}

// pollEquity periodically refreshes the cached equity value.
//
// Uses a median-of-5 filter to reject transient outliers caused by non-atomic
// multi-source API queries (dex + perps + spot). Settlement can move funds
// between namespaces during the sequential HTTP calls, creating single-sample
// spikes or drops that would otherwise inflate the high-water mark or trigger
// false drawdowns. Median-of-5 requires 3+ consecutive bad readings to defeat,
// a significant improvement over median-of-3 (which only needs 2).
// A warmup path exists (first 4 samples use raw values) but Init seeds the
// ring buffer, so median filtering is active from the first poll.
func (le *LiveExecutor) pollEquity(ctx context.Context) {
	interval := 15
	if le.cfg != nil && le.cfg.Execution.PollEquitySec > 0 {
		interval = le.cfg.Execution.PollEquitySec
	}
	// V24: no startup jitter — equity is the "anchor" poller (phase 0).
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			eq := le.queryEquityAllSources()
			if eq <= 0 {
				slog.Warn("equity poll returned zero, keeping previous value")
				continue
			}

			// Push into ring buffer.
			le.equityMu.Lock()
			idx := le.equityRingCount % 5
			le.equityRingBuf[idx] = eq
			le.equityRingCount++

			if le.equityRingCount < 5 {
				// Warmup: not enough samples for median yet, use raw value.
				le.cachedEquity = eq
				le.equityMu.Unlock()
				continue
			}

			// Compute median of the 5 most recent samples.
			med := median5(le.equityRingBuf)

			prev := le.cachedEquity
			le.cachedEquity = med
			le.equityMu.Unlock()

			// Log when median rejects an outlier (raw differs from median by >2%).
			if prev > 0 {
				rawDriftPct := (eq - med) / prev * 100.0
				if rawDriftPct > 2.0 || rawDriftPct < -2.0 {
					slog.Warn("equity poll: median filter rejected outlier",
						"raw", fmt.Sprintf("%.2f", eq),
						"median", fmt.Sprintf("%.2f", med),
						"previous", fmt.Sprintf("%.2f", prev),
						"drift_pct", fmt.Sprintf("%.1f", rawDriftPct),
					)
				}
			}
		}
	}
}

// median5 returns the median of a 5-element array using a sorting network.
func median5(v [5]float64) float64 {
	a, b, c, d, e := v[0], v[1], v[2], v[3], v[4]
	// Sorting network for 5 elements (9 comparisons), then return middle.
	if a > b {
		a, b = b, a
	}
	if c > d {
		c, d = d, c
	}
	if a > c {
		a, c = c, a
		b, d = d, b
	}
	// Now a <= c <= d, a <= b.
	if b > e {
		b, e = e, b
	}
	if b > c {
		b, c = c, b
	}
	if d > e {
		d, e = e, d
	}
	// Median is min(c, d) after above network.
	if c > d {
		return d
	}
	return c
}

// pollOpenOrders periodically refreshes the cached open orders for each asset.
func (le *LiveExecutor) pollOpenOrders(ctx context.Context) {
	interval := 15
	if le.cfg != nil && le.cfg.Execution.PollOpenOrdersSec > 0 {
		interval = le.cfg.Execution.PollOpenOrdersSec
	}
	// V24: deterministic 5s startup offset to de-phase from pollEquity/pollFills;
	// reduces the 4-poller pile-ups observed in V23 top-burst buckets.
	select {
	case <-ctx.Done():
		return
	case <-time.After(5 * time.Second):
	}
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			for _, asset := range le.assets {
				orders := le.fetchOpenOrders(asset)
				le.openOrdersMu.Lock()
				le.cachedOpenOrders[asset] = orders
				le.openOrdersMu.Unlock()
			}
		}
	}
}

// wsReplayDrainDuration is how long after Init() we treat unknown WS fills as
// replays rather than new fills. The WS replays historical fills on connect;
// during this window we only process fills the seed already identified.
const wsReplayDrainDuration = 5 * time.Second

// dedupAndHandleFill processes a WS fill, skipping it if the poller already handled it
// or if it arrived during the post-init WS replay drain window.
func (le *LiveExecutor) dedupAndHandleFill(fill types.FillEvent) {
	fill.Side = normalizeSide(fill.Side)

	// During replay drain window (after init or after each WS reconnect),
	// skip all WS fills — the poller will pick up any genuinely new fills.
	drainRef := le.lastReconnectTime
	if drainRef.IsZero() {
		drainRef = le.initTime
	}
	if !drainRef.IsZero() && time.Since(drainRef) < wsReplayDrainDuration {
		slog.Debug("WS fill during replay drain, skipping",
			"coin", fill.Coin, "oid", fill.OID, "side", fill.Side, "size", fill.Size)
		return
	}

	// Cross-source dedup: skip if poller (or a prior WS delivery) already processed this fill.
	contentKey := buildFillContentKey(fill)
	le.seenMu.Lock()
	if _, seen := le.seenFillContent[contentKey]; seen {
		le.seenMu.Unlock()
		slog.Debug("WS fill already processed, skipping", "coin", fill.Coin, "oid", fill.OID)
		return
	}
	le.seenFillContent[contentKey] = time.Now().UnixMilli()
	le.seenMu.Unlock()

	le.handleFill(fill)
}

// pollFills polls UserFillsByTime for reliable fill detection (WS is primary source).
func (le *LiveExecutor) pollFills(ctx context.Context) {
	interval := 10
	if le.cfg != nil && le.cfg.Execution.PollFillsSec > 0 {
		interval = le.cfg.Execution.PollFillsSec
	}
	// V24: deterministic 10s startup offset to de-phase from pollEquity (phase 0)
	// and pollOpenOrders (phase 5s). WS fill feed is primary — REST poller is a
	// safety net — so a small offset does not delay fill detection meaningfully.
	select {
	case <-ctx.Done():
		return
	case <-time.After(10 * time.Second):
	}
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	// Initialize lastPollTime to now (we only care about fills from this point forward).
	le.seenMu.Lock()
	le.lastPollTime = time.Now().UnixMilli()
	le.seenMu.Unlock()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			le.doPollFills()
		}
	}
}

func (le *LiveExecutor) doPollFills() {
	le.seenMu.Lock()
	startTime := le.lastPollTime
	le.seenMu.Unlock()
	le.recordFillPollAttempt()

	fills, err := le.queryFillsByTime(startTime)
	le.stats.RecordInfo()
	if err != nil {
		le.recordFillPollResult(false)
		slog.Warn("fill poller: query failed", "error", err)
		return
	}
	le.recordFillPollResult(true)

	now := time.Now().UnixMilli()
	var newFills []types.FillEvent

	le.seenMu.Lock()
	for _, f := range fills {
		fm, ok := f.(map[string]any)
		if !ok {
			continue
		}

		timeMs := int64(0)
		if v, ok := fm["time"].(float64); ok {
			timeMs = int64(v)
		}
		fill := le.parseFillFromAPI(fm)
		key := buildFillDedupKey(fill, timeMs)

		if _, seen := le.seenFillIDs[key]; seen {
			continue
		}
		// Cross-source dedup: skip if WS already processed this fill.
		contentKey := buildFillContentKey(fill)
		if _, seen := le.seenFillContent[contentKey]; seen {
			continue
		}
		le.seenFillIDs[key] = timeMs
		le.seenFillContent[contentKey] = now
		newFills = append(newFills, fill)
	}

	// Advance poll window: 1s overlap for boundary fills (dedup prevents double-processing).
	le.lastPollTime = now - 1000

	// Evict dedup entries older than 5 minutes.
	evictBefore := now - 5*60*1000
	for key, ts := range le.seenFillIDs {
		if ts < evictBefore {
			delete(le.seenFillIDs, key)
		}
	}
	for key, ts := range le.seenFillContent {
		if ts < evictBefore {
			delete(le.seenFillContent, key)
		}
	}
	le.seenMu.Unlock()

	// Process new fills outside the seenMu lock.
	for _, fill := range newFills {
		slog.Info("fill poller: new fill",
			"coin", fill.Coin,
			"side", fill.Side,
			"size", fill.Size,
			"price", fill.Price,
			"oid", fill.OID,
			"fee", fill.Fee,
			"builder_fee", fill.BuilderFee,
		)
		le.handleFill(fill)
	}
}

// fillPollerInfoURL uses api.hyperliquid.xyz directly because neither QuickNode
// nor send.hyperliquidapi.com support userFillsByTime (both return 422).
const fillPollerInfoURL = "https://api.hyperliquid.xyz/info"

// queryFillsByTime queries UserFillsByTime via the direct Hyperliquid API.
// The SDK routes this to send.hyperliquidapi.com which doesn't support it,
// and QuickNode nodes don't support fill queries at all.
func (le *LiveExecutor) queryFillsByTime(startTime int64) ([]any, error) {
	body := map[string]any{
		"type":      "userFillsByTime",
		"user":      le.sdk.Address(),
		"startTime": startTime,
	}

	jsonData, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fillPollerInfoURL, bytes.NewReader(jsonData))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("info endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var result []any
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("unmarshal response: %w", err)
	}

	return result, nil
}

// parseFillFromAPI parses a fill from the UserFillsByTime REST response.
func (le *LiveExecutor) parseFillFromAPI(fm map[string]any) types.FillEvent {
	zeusbridge.RecordFill(fm)
	event := types.FillEvent{}

	if coin, ok := fm["coin"].(string); ok {
		// HIP-3 dex prefix reconstruction: API returns bare "CL", we need "xyz:CL".
		// Check if this coin matches a known HIP-3 asset and add the dex prefix.
		if le.hasDex && le.dexName != "" && !strings.HasPrefix(coin, le.dexName+":") {
			candidate := le.dexName + ":" + coin
			if _, known := le.dexes[candidate]; known {
				coin = candidate
			}
		}
		event.Coin = coin
	}

	if side, ok := fm["side"].(string); ok {
		event.Side = normalizeSide(side)
	}

	if px, ok := fm["px"].(string); ok {
		event.Price, _ = strconv.ParseFloat(px, 64)
	}

	if sz, ok := fm["sz"].(string); ok {
		event.Size, _ = strconv.ParseFloat(sz, 64)
	}

	if oid, ok := fm["oid"].(float64); ok {
		event.OID = int64(oid)
	}

	if fee, ok := fm["fee"].(string); ok {
		event.Fee, _ = strconv.ParseFloat(fee, 64)
	}

	if builderFee, ok := fm["builderFee"].(string); ok {
		event.BuilderFee, _ = strconv.ParseFloat(builderFee, 64)
	}

	return event
}

// reconcilePositions re-queries clearinghouse state and corrects internal tracking.
func (le *LiveExecutor) reconcilePositions() {
	onChain := make(map[string]*livePosition)

	if le.hasDex {
		state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address(), hyperliquid.WithDex(le.dexName))
		le.stats.RecordInfo()
		if err != nil {
			slog.Warn("reconciliation: failed to query dex clearinghouse", "error", err)
			return
		}
		parsePositionsInto(state, le.dexName, onChain)
	}
	if le.hasPerps {
		state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address())
		le.stats.RecordInfo()
		if err != nil {
			slog.Warn("reconciliation: failed to query perps clearinghouse", "error", err)
			return
		}
		parsePositionsInto(state, "", onChain)
	}

	// Capture book mids before acquiring position lock (bookMu and mu have separate ordering).
	le.bookMu.RLock()
	bookMids := make(map[string]float64, len(onChain))
	for coin := range onChain {
		if book, ok := le.latestBook[coin]; ok {
			bookMids[coin] = book.Mid
		}
	}
	// Also capture mids for locally-tracked positions (for closed-on-chain detection).
	for coin := range le.positions {
		if _, ok := bookMids[coin]; !ok {
			if book, ok := le.latestBook[coin]; ok {
				bookMids[coin] = book.Mid
			}
		}
	}
	le.bookMu.RUnlock()

	var synthFills []types.FillEvent
	var cancelCoins []string // coins needing cancel-all due to consecutive soft mismatches
	now := time.Now()

	// Post-fill grace period: suppress hard-mismatch-to-halt classification for coins
	// that had ANY fill within the grace period. Previously only checked sign-flip
	// fills, which missed the case where rapid fills on one side (e.g., multiple
	// buys reducing a short) flip the on-chain position but the WS/poller hasn't
	// delivered all fills yet. With the broadened check, any recent fill activity
	// suppresses the halt — the next reconciliation tick will re-check.
	reconGracePeriod := 3 * time.Second // default
	if le.cfg != nil {
		reconGracePeriod = time.Duration(le.cfg.Execution.ReconGracePeriodMs) * time.Millisecond
	}

	le.mu.Lock()

	for coin, chainPos := range onChain {
		local := le.positions[coin] // nil if not tracked locally
		var localSide string
		var localSize float64
		if local != nil {
			localSide = local.side
			localSize = local.size
		}
		if localSide != chainPos.side || localSize != chainPos.size {
			// Suppress log noise for floating-point rounding mismatches (e.g., 0.19999 vs 0.2).
			sizeEpsilon := math.Abs(localSize-chainPos.size) < 0.001 && localSide == chainPos.side
			if sizeEpsilon {
				// Silently accept rounding-level corrections without logging WARN.
				le.positions[coin] = chainPos
				le.markHealthyReconcile(coin, now)
				le.resetSoftMismatch(coin)
				continue
			}
			slog.Warn("reconciliation: position mismatch, updating",
				"coin", coin,
				"local_side", localSide,
				"local_size", localSize,
				"chain_side", chainPos.side,
				"chain_size", chainPos.size,
			)

			deltaNotional, dustMismatch, hardMismatch := le.classifyReconcileDrift(
				coin, localSide, localSize, chainPos.side, chainPos.size, bookMids[coin], chainPos.avgEntry,
			)
			switch {
			case hardMismatch:
				// Check both sign-flip time and any-fill time for grace period.
				// The V10 incident showed that rapid fills can flip on-chain position
				// without a local sign flip if the WS drops intermediate fills.
				graceRef := le.lastFillSignFlip[coin]
				if fillTime := le.lastFillTime[coin]; fillTime.After(graceRef) {
					graceRef = fillTime
				}
				if !graceRef.IsZero() && now.Sub(graceRef) < reconGracePeriod {
					slog.Info("reconciliation: suppressing hard drift during post-fill grace period",
						"coin", coin,
						"delta_notional", fmt.Sprintf("%.2f", deltaNotional),
						"grace_elapsed_ms", now.Sub(graceRef).Milliseconds(),
						"trigger", func() string {
							if le.lastFillTime[coin].After(le.lastFillSignFlip[coin]) {
								return "recent_fill"
							}
							return "sign_flip"
						}(),
					)
					// Treat as soft correction: adopt chain state but don't halt.
					// Synthesize a fill so adaptive notional / fill stats stay updated.
					makerFeeBps := 0.0
					if le.cfg != nil {
						makerFeeBps = le.cfg.ForAsset(coin).MarketMaking.MakerFeeBps
					}
					if sf := inferFillFromDelta(localSide, localSize, chainPos.side, chainPos.size, coin, bookMids[coin], makerFeeBps); sf != nil {
						synthFills = append(synthFills, *sf)
					}
				} else {
					le.markHardMismatch(coin, "position_mismatch", now)
					slog.Error("reconciliation: hard drift detected, halting coin quoting",
						"coin", coin,
						"delta_notional", fmt.Sprintf("%.2f", deltaNotional),
					)
				}
			case dustMismatch:
				le.markHealthyReconcile(coin, now)
				le.resetSoftMismatch(coin)
			default:
				makerFeeBps := 0.0
				if le.cfg != nil {
					makerFeeBps = le.cfg.ForAsset(coin).MarketMaking.MakerFeeBps
				}
				if sf := inferFillFromDelta(localSide, localSize, chainPos.side, chainPos.size, coin, bookMids[coin], makerFeeBps); sf != nil {
					synthFills = append(synthFills, *sf)
				}
				// Track consecutive soft mismatches. If 3+ within 30s, escalate
				// to cancel-all to prevent stale-state accumulation.
				cancelCoins = append(cancelCoins, le.trackSoftMismatch(coin, now)...)
			}

			le.positions[coin] = chainPos

			pu := types.PositionUpdate{
				Coin:     coin,
				Side:     chainPos.side,
				Size:     chainPos.size,
				AvgEntry: chainPos.avgEntry,
				Notional: chainPos.size * chainPos.avgEntry,
			}
			le.publishPositionUpdate(pu)
		} else {
			le.markHealthyReconcile(coin, now)
			le.resetSoftMismatch(coin)
		}
	}

	// Detect positions closed on-chain but still tracked locally.
	for coin, local := range le.positions {
		if local.side == "" {
			continue
		}
		if _, onChainExists := onChain[coin]; !onChainExists {
			slog.Warn("reconciliation: local position not on chain, clearing",
				"coin", coin,
				"local_side", local.side,
				"local_size", local.size,
			)

			deltaNotional, dustMismatch, hardMismatch := le.classifyReconcileDrift(
				coin, local.side, local.size, "", 0, bookMids[coin], local.avgEntry,
			)
			switch {
			case hardMismatch:
				graceRef2 := le.lastFillSignFlip[coin]
				if fillTime := le.lastFillTime[coin]; fillTime.After(graceRef2) {
					graceRef2 = fillTime
				}
				if !graceRef2.IsZero() && now.Sub(graceRef2) < reconGracePeriod {
					slog.Info("reconciliation: suppressing hard drift during post-fill grace period",
						"coin", coin,
						"delta_notional", fmt.Sprintf("%.2f", deltaNotional),
						"grace_elapsed_ms", now.Sub(graceRef2).Milliseconds(),
					)
					// Treat as soft correction: adopt chain state but don't halt.
					makerFeeBps := 0.0
					if le.cfg != nil {
						makerFeeBps = le.cfg.ForAsset(coin).MarketMaking.MakerFeeBps
					}
					if sf := inferFillFromDelta(local.side, local.size, "", 0, coin, bookMids[coin], makerFeeBps); sf != nil {
						synthFills = append(synthFills, *sf)
					}
				} else {
					le.markHardMismatch(coin, "position_missing_on_chain", now)
					slog.Error("reconciliation: hard drift detected, halting coin quoting",
						"coin", coin,
						"delta_notional", fmt.Sprintf("%.2f", deltaNotional),
					)
				}
			case dustMismatch:
				le.markHealthyReconcile(coin, now)
				le.resetSoftMismatch(coin)
			default:
				makerFeeBps := 0.0
				if le.cfg != nil {
					makerFeeBps = le.cfg.ForAsset(coin).MarketMaking.MakerFeeBps
				}
				if sf := inferFillFromDelta(local.side, local.size, "", 0, coin, bookMids[coin], makerFeeBps); sf != nil {
					synthFills = append(synthFills, *sf)
				}
				cancelCoins = append(cancelCoins, le.trackSoftMismatch(coin, now)...)
			}

			le.positions[coin] = &livePosition{}
			pu := types.PositionUpdate{Coin: coin}
			le.publishPositionUpdate(pu)
		}
	}

	// Health recovery check: for halted coins where both local and chain are flat,
	// call markHealthyReconcile so the 2-consecutive-healthy-reconcile recovery path
	// can clear the halt. Without this, coins halted by position_missing_on_chain
	// or hard drift are never re-evaluated because the main loops above skip coins
	// with empty local positions (side == "").
	for _, coin := range le.assets {
		local := le.positions[coin]
		localFlat := local == nil || local.side == "" || local.size == 0
		_, chainExists := onChain[coin]
		if localFlat && !chainExists {
			// Both sides agree the position is flat — this is a healthy state.
			le.markHealthyReconcile(coin, now)
		}
	}

	// Collect coins with tracked orders for order count reconciliation.
	coinsToCheck := make(map[string]int) // coin -> tracked count
	for coin, tracked := range le.liveOrders {
		coinsToCheck[coin] = len(tracked)
	}
	le.mu.Unlock()

	// Fire synthetic fill callbacks outside the lock.
	if cb := le.onFillCallback; cb != nil && len(synthFills) > 0 {
		for _, sf := range synthFills {
			slog.Info("reconciliation: inferred synthetic fill",
				"coin", sf.Coin,
				"side", sf.Side,
				"size", sf.Size,
				"price", sf.Price,
			)
			cb(sf)
		}
	}

	// Cancel orders for coins with too many consecutive soft mismatches.
	if len(cancelCoins) > 0 {
		cancelCtx, cancelCancel := context.WithTimeout(context.Background(), 5*time.Second)
		for _, coin := range cancelCoins {
			slog.Warn("reconciliation: consecutive soft mismatches exceeded threshold, cancelling orders",
				"coin", coin,
			)
			if err := le.CancelAll(cancelCtx, coin); err != nil {
				slog.Error("reconciliation: cancel-on-drift failed", "coin", coin, "err", err)
			}
		}
		cancelCancel()
	}

	// Skip order-count reconciliation while a refresh is in progress.
	// During Refresh/RefreshMulti, the exchange order state is transitional
	// (cancels/modifies in flight), so any count comparison is unreliable
	// and would cause spurious epoch bumps that fail the active refresh.
	if le.refreshInProgress.Load() > 0 {
		return
	}

	// Query actual open orders outside the lock (network I/O).
	if len(coinsToCheck) == 0 {
		return
	}
	exchangeOrders := make(map[string]int)         // coin -> exchange count
	exchangeOIDsByCoin := make(map[string][]int64) // coin -> exchange OIDs (diagnostic only)
	exchangeFetchOKByCoin := make(map[string]bool) // coin -> whether OID fetch succeeded
	diagEnabled := le.cfg != nil && le.cfg.Execution.ReconMismatchDiagnosticEnabled
	for coin := range coinsToCheck {
		orders := le.fetchOpenOrders(coin)
		exchangeOrders[coin] = len(orders)
		if diagEnabled {
			oids, ok := le.queryOpenOrderOIDs(coin)
			exchangeOIDsByCoin[coin] = oids
			exchangeFetchOKByCoin[coin] = ok
		}
	}

	// Re-acquire lock and reconcile order tracking.
	le.mu.Lock()
	defer le.mu.Unlock()
	for coin, trackedCount := range coinsToCheck {
		exchangeCount := exchangeOrders[coin]
		// Re-check tracking hasn't changed from a concurrent fill/refresh.
		currentTracked, exists := le.liveOrders[coin]
		if !exists || len(currentTracked) != trackedCount {
			continue
		}
		if exchangeCount != trackedCount {
			// Use a shorter grace window for the unambiguous "exchange=0 && tracked>0"
			// desync signal (all orders silently auto-cancelled or ALO-rejected). Partial
			// drift mismatches still use the normal grace period since they're usually
			// transient mid-refresh skew.
			effectiveGrace := reconGracePeriod
			graceReason := "normal"
			if exchangeCount == 0 && trackedCount > 0 && le.cfg != nil && le.cfg.Execution.ReconEmptyExchangeGraceMs > 0 {
				emptyGrace := time.Duration(le.cfg.Execution.ReconEmptyExchangeGraceMs) * time.Millisecond
				if emptyGrace < effectiveGrace {
					effectiveGrace = emptyGrace
					graceReason = "empty_exchange"
				}
			}
			if genAt := le.lastOrderGenerationAt[coin]; !genAt.IsZero() && now.Sub(genAt) < effectiveGrace {
				slog.Info("reconciliation: deferring order count mismatch during order generation grace",
					"coin", coin,
					"tracked", trackedCount,
					"exchange", exchangeCount,
					"generation", le.orderGeneration[coin],
					"age_ms", now.Sub(genAt).Milliseconds(),
					"grace_ms", effectiveGrace.Milliseconds(),
					"grace_reason", graceReason,
				)
				continue
			}
			if diagEnabled {
				maxPerHour := le.cfg.Execution.ReconMismatchDiagnosticMaxPerHour
				if le.reconDiagAllow(coin, maxPerHour, now) {
					trackedOIDs := make([]int64, 0, len(currentTracked))
					for _, t := range currentTracked {
						trackedOIDs = append(trackedOIDs, t.OID)
					}
					rlRem, rlCap := le.rateLimitSnapshot()
					le.bookMu.RLock()
					reconBookMid := le.latestBook[coin].Mid
					le.bookMu.RUnlock()
					payload := buildReconDiagnostic(
						trackedOIDs,
						exchangeOIDsByCoin[coin],
						le.orderGeneration[coin],
						le.lastOrderGenerationAt[coin],
						now,
						currentTracked,
						exchangeFetchOKByCoin[coin],
						le.lastFillTime[coin], // V24: surface WS-race causality
						rlRem, rlCap,          // V26: budget pressure correlation
						reconBookMid, // V27: stale-order hypothesis
					)
					emitReconDiagnostic(coin, payload)
				}
			}
			slog.Warn("reconciliation: order count mismatch, clearing tracking",
				"coin", coin,
				"tracked", trackedCount,
				"exchange", exchangeCount,
			)
			le.markOrderCountMismatch(coin, now)
			delete(le.liveOrders, coin)
			le.orderEpoch[coin]++
			le.markOrderGenerationLocked(coin)
		} else {
			le.resetOrderMismatch(coin)
		}
	}
}

// inferFillFromDelta synthesizes a FillEvent from a position reconciliation mismatch.
// This allows adaptive notional and fill stats to track fills missed by WebSocket disconnects.
// makerFeeBps is used to estimate the exchange fee on the synthetic fill (better than $0).
func inferFillFromDelta(localSide string, localSize float64, chainSide string, chainSize float64, coin string, bookMid float64, makerFeeBps float64) *types.FillEvent {
	// Convert to signed positions: long = positive, short = negative.
	var localSigned, chainSigned float64
	switch localSide {
	case "long":
		localSigned = localSize
	case "short":
		localSigned = -localSize
	}
	switch chainSide {
	case "long":
		chainSigned = chainSize
	case "short":
		chainSigned = -chainSize
	}

	delta := chainSigned - localSigned
	if delta == 0 {
		return nil
	}

	side := "buy"
	size := delta
	if delta < 0 {
		side = "sell"
		size = -delta
	}

	notional := size * bookMid
	estimatedFee := notional * makerFeeBps / 10000.0

	return &types.FillEvent{
		Coin:  coin,
		Side:  side,
		Size:  size,
		Price: bookMid,
		Fee:   estimatedFee,
	}
}

func (le *LiveExecutor) handleFill(fill types.FillEvent) {
	fill.Side = normalizeSide(fill.Side)
	if fill.Side != "buy" && fill.Side != "sell" {
		slog.Error("ignoring fill with unknown side", "coin", fill.Coin, "side", fill.Side, "oid", fill.OID)
		le.markHardMismatch(fill.Coin, "unknown_fill_side", time.Now())
		return
	}

	// If this coin is in QuoteHalt due to a position mismatch, suppress
	// incremental position updates. The chain state was adopted by
	// reconcilePositions(); any fill arriving now is either already reflected
	// on-chain or is a dedup leak. Letting it through would re-corrupt the
	// local position and prevent recovery. We still track fees and invoke the
	// fill callback so markout/toxicity systems stay updated.
	le.healthMu.RLock()
	halted := false
	if state, ok := le.coinHealth[fill.Coin]; ok && state.QuoteHalt {
		halted = true
	}
	le.healthMu.RUnlock()

	if halted {
		slog.Warn("suppressing fill position update during quote halt",
			"coin", fill.Coin, "side", fill.Side, "size", fill.Size, "oid", fill.OID)

		le.feeMu.Lock()
		le.totalFees += fill.Fee
		le.totalBuilderFees += fill.BuilderFee
		le.feeMu.Unlock()

		if cb := le.onFillCallback; cb != nil {
			cb(fill)
		}
		return
	}

	le.mu.Lock()

	// Surgical OID-based tracking update: only remove the filled order,
	// keep remaining orders tracked for efficient diff-based refresh.
	if fill.OID != 0 {
		tracked := le.liveOrders[fill.Coin]
		if len(tracked) > 0 {
			newTracked := make([]trackedOrder, 0, len(tracked))
			for _, t := range tracked {
				if t.OID == fill.OID {
					fill.WallFront = t.WallFront
					remaining := t.Size - fill.Size
					if remaining > 1e-12 {
						// Partial fill: keep with reduced size
						t.Size = remaining
						newTracked = append(newTracked, t)
					}
					// else: fully filled, drop
				} else {
					newTracked = append(newTracked, t)
				}
			}
			if len(newTracked) > 0 {
				le.liveOrders[fill.Coin] = newTracked
			} else {
				delete(le.liveOrders, fill.Coin)
			}
			le.markOrderGenerationLocked(fill.Coin)
			// Do NOT bump epoch — tracking is still valid for remaining orders.
		}
	} else {
		// No OID — fall back to old behavior
		le.orderEpoch[fill.Coin]++
		delete(le.liveOrders, fill.Coin)
		le.markOrderGenerationLocked(fill.Coin)
	}

	pos, ok := le.positions[fill.Coin]
	if !ok {
		pos = &livePosition{}
		le.positions[fill.Coin] = pos
	}

	// Save old side for sign-flip detection.
	oldSide := pos.side

	// Compute realized PnL before updating position state.
	isReducing := (pos.side == "long" && fill.Side == "sell") || (pos.side == "short" && fill.Side == "buy")
	if isReducing && pos.size > 0 {
		closedSize := math.Min(fill.Size, pos.size)
		if pos.side == "long" {
			fill.RealizedPnL = (fill.Price - pos.avgEntry) * closedSize
		} else {
			fill.RealizedPnL = (pos.avgEntry - fill.Price) * closedSize
		}
		fill.ClosedSize = closedSize
	}

	if pos.side == "" || pos.size == 0 {
		// New position.
		if fill.Side == "buy" {
			pos.side = "long"
		} else {
			pos.side = "short"
		}
		pos.size = fill.Size
		pos.avgEntry = fill.Price
	} else if (pos.side == "long" && fill.Side == "buy") || (pos.side == "short" && fill.Side == "sell") {
		// Adding to position.
		totalCost := pos.avgEntry*pos.size + fill.Price*fill.Size
		pos.size += fill.Size
		pos.avgEntry = totalCost / pos.size
	} else {
		// Reducing/closing.
		if fill.Size >= pos.size {
			remaining := fill.Size - pos.size
			if remaining > 0 {
				if fill.Side == "buy" {
					pos.side = "long"
				} else {
					pos.side = "short"
				}
				pos.size = remaining
				pos.avgEntry = fill.Price
			} else {
				pos.side = ""
				pos.size = 0
				pos.avgEntry = 0
			}
		} else {
			pos.size -= fill.Size
		}
	}

	// Detect position sign flip: old side was long/short and new side is the opposite.
	if oldSide != "" && pos.side != "" && oldSide != pos.side {
		if le.lastFillSignFlip == nil {
			le.lastFillSignFlip = make(map[string]time.Time)
		}
		le.lastFillSignFlip[fill.Coin] = time.Now()
		slog.Info("fill caused position sign flip",
			"coin", fill.Coin,
			"old_side", oldSide,
			"new_side", pos.side,
		)
	}

	// Track most recent fill time for any fill (not just sign flips).
	// Used by reconciliation to suppress hard drift classification when
	// fills are actively being processed — the poller may not have caught
	// all fills yet, so chain state can temporarily diverge from local.
	if le.lastFillTime == nil {
		le.lastFillTime = make(map[string]time.Time)
	}
	le.lastFillTime[fill.Coin] = time.Now()

	pu := types.PositionUpdate{
		Coin:     fill.Coin,
		Side:     pos.side,
		Size:     pos.size,
		AvgEntry: pos.avgEntry,
		Notional: pos.size * pos.avgEntry,
	}

	slog.Info("position updated from fill",
		"coin", fill.Coin,
		"side", pos.side,
		"size", pos.size,
		"avg_entry", pos.avgEntry,
	)

	le.publishPositionUpdate(pu)

	// Accumulate fees from fill.
	le.feeMu.Lock()
	le.totalFees += fill.Fee
	le.totalBuilderFees += fill.BuilderFee
	le.feeMu.Unlock()

	// Release mu before invoking fill callback to prevent deadlock
	// if the callback ever calls back into LiveExecutor methods.
	cb := le.onFillCallback
	le.mu.Unlock()

	if cb != nil {
		cb(fill)
	}
}

func (le *LiveExecutor) recordFillPollAttempt() {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	le.lastFillPollAt = time.Now()
}

func (le *LiveExecutor) recordFillPollResult(success bool) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	if success {
		le.lastFillPollSuccessAt = time.Now()
		le.fillPollConsecutiveErrors = 0
		return
	}
	le.fillPollConsecutiveErrors++
}

func (le *LiveExecutor) ensureCoinHealthLocked(coin string) *coinHealthState {
	state, ok := le.coinHealth[coin]
	if !ok {
		state = &coinHealthState{}
		le.coinHealth[coin] = state
	}
	return state
}

// anyCoinHalted returns true if any coin is currently in QuoteHalt.
func (le *LiveExecutor) anyCoinHalted() bool {
	le.healthMu.RLock()
	defer le.healthMu.RUnlock()
	for _, state := range le.coinHealth {
		if state.QuoteHalt {
			return true
		}
	}
	return false
}

func (le *LiveExecutor) markHardMismatch(coin, reason string, now time.Time) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)
	state.QuoteHalt = true
	state.Reason = reason
	state.LastMismatchAt = now
	state.MatchingReconciles = 0
	state.LastHealthyReconcileAt = time.Time{}
}

func (le *LiveExecutor) markHealthyReconcile(coin string, now time.Time) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)
	state.ConsecutiveOrderMismatch = 0
	if !state.QuoteHalt {
		state.MatchingReconciles = 0
		state.LastHealthyReconcileAt = now
		return
	}
	if !state.LastHealthyReconcileAt.IsZero() && now.Sub(state.LastHealthyReconcileAt) >= 3*time.Second {
		state.MatchingReconciles++
	} else if state.LastHealthyReconcileAt.IsZero() {
		state.MatchingReconciles = 1
	}
	state.LastHealthyReconcileAt = now
	if state.MatchingReconciles >= 2 {
		slog.Info("health halt auto-recovered after 2 consecutive healthy reconciliations",
			"coin", coin,
			"previous_reason", state.Reason,
		)
		state.QuoteHalt = false
		state.Reason = ""
		state.MatchingReconciles = 0
	}
}

func (le *LiveExecutor) markOrderCountMismatch(coin string, now time.Time) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)
	state.ConsecutiveOrderMismatch++
	state.LastMismatchAt = now
	if state.ConsecutiveOrderMismatch >= 2 {
		state.QuoteHalt = true
		state.Reason = "order_count_mismatch"
		state.MatchingReconciles = 0
		state.LastHealthyReconcileAt = time.Time{}
	}
}

func (le *LiveExecutor) resetOrderMismatch(coin string) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)
	state.ConsecutiveOrderMismatch = 0
}

const (
	softMismatchCancelThreshold = 3  // consecutive soft mismatches before cancel-all
	softMismatchWindowSec       = 30 // time window for consecutive soft mismatches
)

// trackSoftMismatch increments the consecutive soft mismatch counter for a coin.
// If 3+ soft mismatches occur within 30s, returns the coin for cancel-all.
// REQUIRES: caller holds le.mu.
func (le *LiveExecutor) trackSoftMismatch(coin string, now time.Time) []string {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)

	// Reset if window expired.
	if !state.FirstSoftMismatchAt.IsZero() && now.Sub(state.FirstSoftMismatchAt) > softMismatchWindowSec*time.Second {
		state.ConsecutiveSoftMismatch = 0
		state.FirstSoftMismatchAt = time.Time{}
	}

	state.ConsecutiveSoftMismatch++
	if state.ConsecutiveSoftMismatch == 1 {
		state.FirstSoftMismatchAt = now
	}

	if state.ConsecutiveSoftMismatch >= softMismatchCancelThreshold {
		slog.Error("reconciliation: consecutive soft mismatch threshold reached",
			"coin", coin,
			"count", state.ConsecutiveSoftMismatch,
			"window_sec", softMismatchWindowSec,
		)
		state.ConsecutiveSoftMismatch = 0
		state.FirstSoftMismatchAt = time.Time{}
		return []string{coin}
	}
	return nil
}

// resetSoftMismatch clears the consecutive soft mismatch counter.
// REQUIRES: caller holds le.mu.
func (le *LiveExecutor) resetSoftMismatch(coin string) {
	le.healthMu.Lock()
	defer le.healthMu.Unlock()
	state := le.ensureCoinHealthLocked(coin)
	state.ConsecutiveSoftMismatch = 0
	state.FirstSoftMismatchAt = time.Time{}
}

func (le *LiveExecutor) classifyReconcileDrift(
	coin, localSide string,
	localSize float64,
	chainSide string,
	chainSize float64,
	bookMid float64,
	fallbackPrice float64,
) (deltaNotional float64, dustMismatch bool, hardMismatch bool) {
	refPrice := bookMid
	if refPrice <= 0 {
		refPrice = fallbackPrice
	}
	if refPrice <= 0 {
		refPrice = 1
	}

	var localSigned, chainSigned float64
	switch localSide {
	case "long":
		localSigned = localSize
	case "short":
		localSigned = -localSize
	}
	switch chainSide {
	case "long":
		chainSigned = chainSize
	case "short":
		chainSigned = -chainSize
	}

	deltaNotional = math.Abs(chainSigned-localSigned) * refPrice
	if deltaNotional < 10.0 {
		return deltaNotional, true, false
	}

	threshold := 50.0
	if le.cfg != nil {
		threshold = math.Max(50.0, 0.25*le.cfg.ForAsset(coin).MarketMaking.QuoteNotional)
	}
	sideFlip := localSide != "" && chainSide != "" && localSide != chainSide

	// A side flip with delta within max_spread_exposure is likely caused by
	// missed fills on our own resting orders (WS drop + poller latency), not
	// by external manipulation. Treat as soft correction, not hard drift.
	// Only escalate side flips to hard when the delta exceeds exposure limits,
	// which would indicate something truly anomalous.
	if sideFlip && le.cfg != nil {
		maxExposure := le.cfg.ForAsset(coin).Risk.MaxSpreadExposure
		if maxExposure > 0 && deltaNotional <= maxExposure {
			slog.Info("reconciliation: side flip within exposure limits, treating as soft correction",
				"coin", coin,
				"delta_notional", fmt.Sprintf("%.2f", deltaNotional),
				"max_exposure", fmt.Sprintf("%.2f", maxExposure),
			)
			return deltaNotional, false, false
		}
	}

	return deltaNotional, false, sideFlip || deltaNotional > threshold
}

func buildFillDedupKey(fill types.FillEvent, timeMs int64) string {
	return fmt.Sprintf("%d:%d:%s:%0.8f:%0.8f:%s", fill.OID, timeMs, fill.Coin, fill.Price, fill.Size, normalizeSide(fill.Side))
}

func buildFillContentKey(fill types.FillEvent) string {
	return fmt.Sprintf("%d:%s:%.8f:%.8f:%s", fill.OID, fill.Coin, fill.Price, fill.Size, normalizeSide(fill.Side))
}

// FeeSnapshot returns cumulative fee totals.
func (le *LiveExecutor) FeeSnapshot() (fees, builderFees float64) {
	le.feeMu.Lock()
	defer le.feeMu.Unlock()
	return le.totalFees, le.totalBuilderFees
}

// diffOrders compares tracked (resting) orders against desired orders and returns
// a list of actions. Orders are partitioned by side, sorted identically (buys desc,
// sells asc by price), and paired positionally. Pairs within thresholds are skipped.
func diffOrders(tracked []trackedOrder, desired []types.OrderRequest, priceThresholdBps, sizeThresholdPct float64) []orderDiff {
	trackedBuys, trackedSells := partitionTracked(tracked)
	desiredBuys, desiredSells := partitionDesired(desired)

	sort.Slice(trackedBuys, func(i, j int) bool { return trackedBuys[i].Price > trackedBuys[j].Price })
	sort.Slice(trackedSells, func(i, j int) bool { return trackedSells[i].Price < trackedSells[j].Price })
	sort.Slice(desiredBuys, func(i, j int) bool { return desiredBuys[i].Price > desiredBuys[j].Price })
	sort.Slice(desiredSells, func(i, j int) bool { return desiredSells[i].Price < desiredSells[j].Price })

	var diffs []orderDiff
	diffs = append(diffs, matchSide(trackedBuys, desiredBuys, priceThresholdBps, sizeThresholdPct)...)
	diffs = append(diffs, matchSide(trackedSells, desiredSells, priceThresholdBps, sizeThresholdPct)...)
	return diffs
}

func matchSide(tracked []trackedOrder, desired []types.OrderRequest, priceThresholdBps, sizeThresholdPct float64) []orderDiff {
	minLen := len(tracked)
	if len(desired) < minLen {
		minLen = len(desired)
	}

	var diffs []orderDiff
	for i := 0; i < minLen; i++ {
		t := tracked[i]
		d := desired[i]
		if priceDiffBps(t.Price, d.Price) <= priceThresholdBps && sizeDiffPct(t.Size, d.Size) <= sizeThresholdPct {
			diffs = append(diffs, orderDiff{action: actionSkip, tracked: &t, desired: &d})
		} else {
			diffs = append(diffs, orderDiff{action: actionModify, tracked: &t, desired: &d})
		}
	}
	for i := minLen; i < len(tracked); i++ {
		t := tracked[i]
		diffs = append(diffs, orderDiff{action: actionCancel, tracked: &t})
	}
	for i := minLen; i < len(desired); i++ {
		d := desired[i]
		diffs = append(diffs, orderDiff{action: actionPlace, desired: &d})
	}
	return diffs
}

func partitionTracked(orders []trackedOrder) (buys, sells []trackedOrder) {
	for _, o := range orders {
		if o.Side == "buy" {
			buys = append(buys, o)
		} else {
			sells = append(sells, o)
		}
	}
	return
}

func partitionDesired(orders []types.OrderRequest) (buys, sells []types.OrderRequest) {
	for _, o := range orders {
		side := normalizeSide(o.Side)
		if side == "buy" {
			buys = append(buys, o)
		} else {
			sells = append(sells, o)
		}
	}
	return
}

func priceDiffBps(a, b float64) float64 {
	if a <= 0 || b <= 0 {
		if a == b {
			return 0
		}
		return math.MaxFloat64
	}
	return math.Abs(a-b) / math.Max(a, b) * 10000.0
}

func sizeDiffPct(a, b float64) float64 {
	if a == 0 || b == 0 {
		if a == b {
			return 0
		}
		return 1.0
	}
	return math.Abs(a-b) / math.Max(a, b)
}

// Refresh updates orders for the coin. Modifies existing orders in-place when
// possible, cancels removed orders, and places new ones. Falls back to full
// cancel-and-replace when order tracking is unavailable.
func (le *LiveExecutor) Refresh(ctx context.Context, coin string, orders []types.OrderRequest) error {
	le.refreshInProgress.Add(1)
	defer le.refreshInProgress.Add(-1)

	le.mu.Lock()
	tracked := le.liveOrders[coin]
	epoch := le.orderEpoch[coin]
	le.mu.Unlock()

	// No tracking → full cancel+replace to establish tracking.
	if len(tracked) == 0 {
		return le.refreshFull(ctx, coin, orders)
	}

	// Use per-asset config thresholds when available, else defaults.
	priceThr := defaultSkipPriceThresholdBps
	sizeThr := defaultSkipSizeThresholdPct
	if le.cfg != nil {
		ac := le.cfg.ForAsset(coin)
		priceThr = float64(ac.MarketMaking.RequoteThresholdBps)
		sizeThr = ac.MarketMaking.SizeRequotePct
	}
	if desiredOrdersForce(orders) {
		priceThr = 0
		sizeThr = 0
	}

	// Diff tracked vs desired.
	diffs := diffOrders(tracked, orders, priceThr, sizeThr)

	// Batch path: partition diffs and execute as batch operations.
	if le.transport.SupportsBatch() {
		return le.refreshBatch(ctx, coin, epoch, diffs)
	}

	// Single-order path (SDK mode): two-phase execution.
	// Phase 1: cancels + modifies. Phase 2: places (after phase 1 completes).
	var (
		newTracked    []trackedOrder
		mu            sync.Mutex
		wg1           sync.WaitGroup
		clearTracking atomic.Bool
		pendingPlaces []orderDiff
	)

	for _, d := range diffs {
		d := d // capture loop var
		switch d.action {
		case actionSkip:
			mu.Lock()
			newTracked = append(newTracked, *d.tracked)
			mu.Unlock()

		case actionModify:
			wg1.Add(1)
			go func() {
				defer wg1.Done()
				result, err := le.modifyOrder(ctx, coin, d.tracked, d.desired)
				if err != nil {
					slog.Warn("modify failed, falling back to cancel+place",
						"oid", d.tracked.OID, "coin", coin, "error", err)
					// Fallback: cancel OID + place fresh.
					le.cancelSingleOID(ctx, d.tracked.OID, coin)
					o := *d.desired
					o.TimeInForce = normalizedTIF(o.TimeInForce, "ALO")
					if o.Intent == "" {
						o.Intent = "quote"
					}
					result2, err2 := le.placeOrder(ctx, o)
					if err2 != nil {
						slog.Error("fallback place after modify failure also failed",
							"coin", coin, "side", o.Side, "error", err2)
						clearTracking.Store(true)
						return
					}
					if !result2.benignReject && result2.oid > 0 {
						mu.Lock()
						newTracked = append(newTracked, trackedFromOrder(result2.oid, o))
						mu.Unlock()
					}
					return
				}
				if result.benignReject {
					slog.Warn("modify rejected benignly (ALO would cross)",
						"oid", d.tracked.OID, "coin", coin)
					// Original order is still resting — keep tracking it.
					mu.Lock()
					newTracked = append(newTracked, *d.tracked)
					mu.Unlock()
					return
				}
				oid := result.oid
				if oid == 0 {
					oid = d.tracked.OID // keep old OID if response didn't include new one
				}
				mu.Lock()
				newTracked = append(newTracked, trackedFromOrder(oid, *d.desired))
				mu.Unlock()
				slog.Info("order modified",
					"oid", d.tracked.OID, "new_oid", oid,
					"coin", coin, "side", d.desired.Side,
					"price", d.desired.Price, "size", d.desired.Size,
				)
			}()

		case actionCancel:
			wg1.Add(1)
			go func() {
				defer wg1.Done()
				if err := le.cancelSingleOID(ctx, d.tracked.OID, coin); err != nil {
					slog.Error("cancel of tracked order failed",
						"oid", d.tracked.OID, "coin", coin, "error", err)
					clearTracking.Store(true)
				}
			}()

		case actionPlace:
			pendingPlaces = append(pendingPlaces, d)
		}
	}

	wg1.Wait()

	// Epoch guard between phases: if a fill arrived during Phase 1,
	// abort before placing new orders.
	le.mu.Lock()
	if clearTracking.Load() || le.orderEpoch[coin] != epoch {
		delete(le.liveOrders, coin)
		le.mu.Unlock()
		return fmt.Errorf("refresh %s: epoch changed during phase 1, tracking cleared", coin)
	}
	le.mu.Unlock()

	cancelCount := 0
	modifyCount := 0
	for _, d := range diffs {
		if d.action == actionCancel {
			cancelCount++
		}
		if d.action == actionModify {
			modifyCount++
		}
	}
	slog.Debug("refresh: phase 1 complete",
		"coin", coin,
		"cancels", cancelCount,
		"modifies", modifyCount,
		"places_pending", len(pendingPlaces),
	)

	// Phase 2: place new orders.
	var wg2 sync.WaitGroup
	for _, d := range pendingPlaces {
		d := d
		wg2.Add(1)
		go func() {
			defer wg2.Done()
			o := *d.desired
			o.TimeInForce = normalizedTIF(o.TimeInForce, "ALO")
			if o.Intent == "" {
				o.Intent = "quote"
			}
			result, err := le.placeOrder(ctx, o)
			if err != nil {
				slog.Error("place new order failed during diff refresh",
					"coin", coin, "side", o.Side, "error", err)
				clearTracking.Store(true)
				return
			}
			if result.benignReject {
				slog.Warn("new order rejected benignly during diff refresh",
					"coin", coin, "side", o.Side)
				return
			}
			if result.oid > 0 {
				mu.Lock()
				newTracked = append(newTracked, trackedFromResult(result, o))
				mu.Unlock()
			}
			slog.Info("order placed",
				"coin", o.Coin, "side", o.Side,
				"price", o.Price, "size", o.Size,
				"reason", o.Reason,
			)
		}()
	}

	wg2.Wait()

	le.mu.Lock()
	if clearTracking.Load() || le.orderEpoch[coin] != epoch {
		delete(le.liveOrders, coin)
		le.markOrderGenerationLocked(coin)
	} else {
		le.liveOrders[coin] = newTracked
		le.markOrderGenerationLocked(coin)
	}
	le.mu.Unlock()

	if clearTracking.Load() {
		return fmt.Errorf("refresh %s: fallback failures, tracking cleared", coin)
	}

	le.syncCachedOrders(coin)
	return nil
}

// RefreshMulti refreshes quotes for multiple coins in coalesced batch API calls.
// Each coin's orders are diffed independently, then all modifies/cancels/places
// are batched into at most 3 API calls total (vs 2N for N coins).
func (le *LiveExecutor) RefreshMulti(ctx context.Context, ordersByCoin map[string][]types.OrderRequest) error {
	le.refreshInProgress.Add(1)
	defer le.refreshInProgress.Add(-1)

	// Fallback: if transport doesn't support batch, delegate to per-coin Refresh.
	// Per-coin isolation: if one coin fails, log and continue with others.
	if !le.transport.SupportsBatch() {
		var lastErr error
		for coin, orders := range ordersByCoin {
			if err := le.Refresh(ctx, coin, orders); err != nil {
				slog.Warn("non-batch Refresh failed, skipping coin",
					"coin", coin, "error", err)
				lastErr = err
			}
		}
		return lastErr
	}

	// Partition: for each coin, check if tracking exists. Coins without tracking
	// get delegated to refreshFull individually. Coins with tracking get diffed.
	type coinEpoch struct {
		coin  string
		epoch uint64
	}
	type taggedModify struct {
		coin   string
		target modifyTarget
	}
	type taggedPlace struct {
		coin  string
		order types.OrderRequest
	}

	var (
		allModifies []taggedModify
		allCancels  []cancelTarget
		allPlaces   []taggedPlace
		skipByCoin  = make(map[string][]trackedOrder)
		epochs      []coinEpoch
	)

	le.mu.Lock()
	for coin, orders := range ordersByCoin {
		tracked := le.liveOrders[coin]
		if len(tracked) == 0 {
			le.mu.Unlock()
			// No tracking → full cancel+replace to establish tracking.
			// Per-coin isolation: if one coin's bootstrap fails, log and
			// continue with other coins instead of failing the entire batch.
			if err := le.refreshFull(ctx, coin, orders); err != nil {
				slog.Warn("bootstrap refreshFull failed, skipping coin",
					"coin", coin, "error", err)
			}
			le.mu.Lock()
			continue
		}

		epoch := le.orderEpoch[coin]
		epochs = append(epochs, coinEpoch{coin: coin, epoch: epoch})

		// Diff tracked vs desired for this coin.
		priceThr := defaultSkipPriceThresholdBps
		sizeThr := defaultSkipSizeThresholdPct
		if le.cfg != nil {
			ac := le.cfg.ForAsset(coin)
			priceThr = float64(ac.MarketMaking.RequoteThresholdBps)
			sizeThr = ac.MarketMaking.SizeRequotePct
		}
		if desiredOrdersForce(orders) {
			priceThr = 0
			sizeThr = 0
		}

		diffs := diffOrders(tracked, orders, priceThr, sizeThr)

		for _, d := range diffs {
			switch d.action {
			case actionSkip:
				skipByCoin[coin] = append(skipByCoin[coin], *d.tracked)
			case actionModify:
				allModifies = append(allModifies, taggedModify{
					coin:   coin,
					target: modifyTarget{tracked: d.tracked, desired: d.desired},
				})
			case actionCancel:
				allCancels = append(allCancels, cancelTarget{oid: d.tracked.OID, coin: coin})
			case actionPlace:
				o := *d.desired
				o.TimeInForce = normalizedTIF(o.TimeInForce, "ALO")
				if o.Intent == "" {
					o.Intent = "quote"
				}
				allPlaces = append(allPlaces, taggedPlace{coin: coin, order: o})
			}
		}
	}
	le.mu.Unlock()

	// If nothing to do after diffs, just commit skips.
	if len(allModifies) == 0 && len(allCancels) == 0 && len(allPlaces) == 0 {
		le.mu.Lock()
		for coin, skipped := range skipByCoin {
			le.liveOrders[coin] = skipped
		}
		le.mu.Unlock()
		for coin := range skipByCoin {
			le.syncCachedOrders(coin)
		}
		return nil
	}

	var (
		clearTracking atomic.Bool
		newTracked    = make(map[string][]trackedOrder)
		mu            sync.Mutex
		wg1           sync.WaitGroup
	)

	// Seed newTracked with skipped orders.
	for coin, skipped := range skipByCoin {
		newTracked[coin] = append(newTracked[coin], skipped...)
	}

	// Phase 1: batch modify + batch cancel (concurrent with each other).
	if len(allModifies) > 0 {
		wg1.Add(1)
		go func() {
			defer wg1.Done()
			targets := make([]modifyTarget, len(allModifies))
			for i, tm := range allModifies {
				targets[i] = tm.target
			}
			results, err := le.transport.ModifyBatch(ctx, targets)
			if err != nil {
				slog.Warn("batch modify failed in RefreshMulti",
					"count", len(targets), "error", err)
				clearTracking.Store(true)
				return
			}
			for i, r := range results {
				coin := allModifies[i].coin
				if r.benignReject {
					mu.Lock()
					newTracked[coin] = append(newTracked[coin], *allModifies[i].target.tracked)
					mu.Unlock()
					continue
				}
				oid := r.oid
				if oid == 0 {
					oid = allModifies[i].target.tracked.OID
				}
				mu.Lock()
				newTracked[coin] = append(newTracked[coin], trackedFromOrder(oid, *allModifies[i].target.desired))
				mu.Unlock()
			}
		}()
	}

	if len(allCancels) > 0 {
		wg1.Add(1)
		go func() {
			defer wg1.Done()
			if err := le.transport.CancelBatch(ctx, allCancels); err != nil {
				slog.Error("batch cancel failed in RefreshMulti", "error", err)
				clearTracking.Store(true)
			}
		}()
	}

	wg1.Wait()

	// Epoch guard: verify no fills arrived during phase 1.
	le.mu.Lock()
	epochChanged := clearTracking.Load()
	for _, ce := range epochs {
		if le.orderEpoch[ce.coin] != ce.epoch {
			epochChanged = true
			break
		}
	}
	if epochChanged {
		for _, ce := range epochs {
			delete(le.liveOrders, ce.coin)
			le.markOrderGenerationLocked(ce.coin)
		}
		le.mu.Unlock()
		return fmt.Errorf("RefreshMulti: epoch changed during phase 1, tracking cleared")
	}
	le.mu.Unlock()

	slog.Debug("RefreshMulti: phase 1 complete",
		"modifies", len(allModifies),
		"cancels", len(allCancels),
		"places_pending", len(allPlaces),
	)

	// Phase 2: batch place.
	if len(allPlaces) > 0 {
		orders := make([]types.OrderRequest, len(allPlaces))
		for i, tp := range allPlaces {
			orders[i] = tp.order
		}
		results, err := le.transport.PlaceBatch(ctx, orders)
		if err != nil {
			slog.Error("batch place failed in RefreshMulti", "error", err)
			clearTracking.Store(true)
		} else {
			for i, r := range results {
				if r.benignReject {
					o := allPlaces[i].order
					slog.Info("ALO batch place rejected (would cross book)",
						"coin", allPlaces[i].coin,
						"side", o.Side,
						"price", o.Price,
						"size", o.Size,
					)
					continue
				}
				if r.oid > 0 {
					coin := allPlaces[i].coin
					mu.Lock()
					newTracked[coin] = append(newTracked[coin], trackedFromOrder(r.oid, allPlaces[i].order))
					mu.Unlock()
				}
			}
		}
	}

	// Commit tracking.
	le.mu.Lock()
	epochChangedFinal := clearTracking.Load()
	for _, ce := range epochs {
		if le.orderEpoch[ce.coin] != ce.epoch {
			epochChangedFinal = true
			break
		}
	}
	if epochChangedFinal {
		for _, ce := range epochs {
			delete(le.liveOrders, ce.coin)
			le.markOrderGenerationLocked(ce.coin)
		}
	} else {
		for _, ce := range epochs {
			if tracked, ok := newTracked[ce.coin]; ok {
				le.liveOrders[ce.coin] = tracked
				le.markOrderGenerationLocked(ce.coin)
			}
		}
	}
	le.mu.Unlock()

	if clearTracking.Load() {
		return fmt.Errorf("RefreshMulti: batch failures, tracking cleared")
	}

	for _, ce := range epochs {
		le.syncCachedOrders(ce.coin)
	}

	slog.Debug("RefreshMulti complete",
		"coins", len(epochs),
		"modifies", len(allModifies),
		"cancels", len(allCancels),
		"places", len(allPlaces),
	)
	return nil
}

// refreshBatch executes diff actions using batch API calls.
// Partitions diffs into modifies, cancels, and places, then executes up to 3
// concurrent batches. Falls back to single-order for any batch fn that is nil.
func (le *LiveExecutor) refreshBatch(ctx context.Context, coin string, epoch uint64, diffs []orderDiff) error {
	var (
		newTracked []trackedOrder
		modifies   []modifyTarget
		cancelOIDs []int64
		places     []types.OrderRequest
	)

	for _, d := range diffs {
		switch d.action {
		case actionSkip:
			newTracked = append(newTracked, *d.tracked)
		case actionModify:
			modifies = append(modifies, modifyTarget{tracked: d.tracked, desired: d.desired})
		case actionCancel:
			cancelOIDs = append(cancelOIDs, d.tracked.OID)
		case actionPlace:
			o := *d.desired
			o.TimeInForce = normalizedTIF(o.TimeInForce, "ALO")
			if o.Intent == "" {
				o.Intent = "quote"
			}
			places = append(places, o)
		}
	}

	var (
		wg1           sync.WaitGroup
		mu            sync.Mutex
		clearTracking atomic.Bool
	)

	// Phase 1: Batch modifies + cancels (concurrent with each other).
	if len(modifies) > 0 {
		wg1.Add(1)
		go func() {
			defer wg1.Done()
			results, err := le.transport.ModifyBatch(ctx, modifies)
			if err != nil {
				slog.Warn("batch modify failed, falling back to cancel+place per entry",
					"coin", coin, "count", len(modifies), "error", err)
				// Fall back to individual cancel+place per failed entry.
				for _, m := range modifies {
					le.cancelSingleOID(ctx, m.tracked.OID, coin)
					o := *m.desired
					o.TimeInForce = normalizedTIF(o.TimeInForce, "ALO")
					if o.Intent == "" {
						o.Intent = "quote"
					}
					result, err := le.placeOrder(ctx, o)
					if err != nil {
						clearTracking.Store(true)
						continue
					}
					if !result.benignReject && result.oid > 0 {
						mu.Lock()
						newTracked = append(newTracked, trackedFromResult(result, o))
						mu.Unlock()
					}
				}
				return
			}
			for i, r := range results {
				if r.benignReject {
					// Original order still resting.
					mu.Lock()
					newTracked = append(newTracked, *modifies[i].tracked)
					mu.Unlock()
					continue
				}
				oid := r.oid
				if oid == 0 {
					oid = modifies[i].tracked.OID
				}
				mu.Lock()
				newTracked = append(newTracked, trackedFromOrder(oid, *modifies[i].desired))
				mu.Unlock()
			}
		}()
	}

	if len(cancelOIDs) > 0 {
		wg1.Add(1)
		go func() {
			defer wg1.Done()
			targets := make([]cancelTarget, len(cancelOIDs))
			for i, oid := range cancelOIDs {
				targets[i] = cancelTarget{oid: oid, coin: coin}
			}
			if err := le.transport.CancelBatch(ctx, targets); err != nil {
				slog.Error("batch cancel failed", "coin", coin, "error", err)
				clearTracking.Store(true)
			}
		}()
	}

	wg1.Wait()

	// Epoch guard between phases: if a fill arrived during Phase 1,
	// abort before placing new orders.
	le.mu.Lock()
	if clearTracking.Load() || le.orderEpoch[coin] != epoch {
		delete(le.liveOrders, coin)
		le.markOrderGenerationLocked(coin)
		le.mu.Unlock()
		return fmt.Errorf("refresh %s: epoch changed during batch phase 1, tracking cleared", coin)
	}
	le.mu.Unlock()

	slog.Debug("refresh: phase 1 complete",
		"coin", coin,
		"cancels", len(cancelOIDs),
		"modifies", len(modifies),
		"places_pending", len(places),
	)

	// Phase 2: Batch places.
	var wg2 sync.WaitGroup
	if len(places) > 0 {
		wg2.Add(1)
		go func() {
			defer wg2.Done()
			results, err := le.transport.PlaceBatch(ctx, places)
			if err != nil {
				slog.Error("batch place failed during diff refresh", "coin", coin, "error", err)
				clearTracking.Store(true)
				return
			}
			for i, r := range results {
				if r.benignReject {
					continue
				}
				if r.oid > 0 {
					mu.Lock()
					newTracked = append(newTracked, trackedFromOrder(r.oid, places[i]))
					mu.Unlock()
				}
			}
		}()
	}

	wg2.Wait()

	le.mu.Lock()
	if clearTracking.Load() || le.orderEpoch[coin] != epoch {
		delete(le.liveOrders, coin)
		le.markOrderGenerationLocked(coin)
	} else {
		le.liveOrders[coin] = newTracked
		le.markOrderGenerationLocked(coin)
	}
	le.mu.Unlock()

	if clearTracking.Load() {
		return fmt.Errorf("refresh %s: batch failures, tracking cleared", coin)
	}

	le.syncCachedOrders(coin)

	slog.Debug("batch refresh complete",
		"coin", coin,
		"modifies", len(modifies),
		"cancels", len(cancelOIDs),
		"places", len(places),
		"tracked", len(newTracked),
	)
	return nil
}

// refreshFull does a full cancel-all → place-all cycle, capturing OIDs
// into liveOrders for subsequent diff-based refreshes.
func (le *LiveExecutor) refreshFull(ctx context.Context, coin string, orders []types.OrderRequest) error {
	// Cancel existing orders first.
	if err := le.cancelOrders(ctx, coin); err != nil {
		slog.Error("cancel before refresh failed", "coin", coin, "error", err)
		return fmt.Errorf("cancel before refresh: %w", err)
	}

	// Clear tracking since we just cancelled everything.
	le.mu.Lock()
	delete(le.liveOrders, coin)
	le.markOrderGenerationLocked(coin)
	le.mu.Unlock()

	// Normalize all orders before placement.
	for i := range orders {
		orders[i].TimeInForce = normalizedTIF(orders[i].TimeInForce, "ALO")
		if orders[i].Intent == "" {
			orders[i].Intent = "quote"
		}
	}

	// Batch path: single API call for all orders.
	if le.transport.SupportsBatch() {
		results, err := le.transport.PlaceBatch(ctx, orders)
		if err != nil {
			return fmt.Errorf("batch place %s: %w", coin, err)
		}

		var newTracked []trackedOrder
		benignRejects := 0
		for i, r := range results {
			if r.benignReject {
				benignRejects++
				continue
			}
			if r.oid > 0 {
				newTracked = append(newTracked, trackedFromOrder(r.oid, orders[i]))
			}
		}

		if len(newTracked) > 0 {
			le.mu.Lock()
			le.liveOrders[coin] = newTracked
			le.markOrderGenerationLocked(coin)
			le.mu.Unlock()
		}

		le.syncCachedOrders(coin)

		slog.Info("batch place complete",
			"coin", coin,
			"total", len(orders),
			"placed", len(newTracked),
			"benign_rejects", benignRejects,
		)
		return nil
	}

	// Single-order path (SDK mode): place concurrently.
	var wg sync.WaitGroup
	var resultMu sync.Mutex
	var hardFailures []error
	var newTracked []trackedOrder
	benignRejects := 0
	for _, o := range orders {
		wg.Add(1)
		go func(o types.OrderRequest) {
			defer wg.Done()
			result, err := le.placeOrder(ctx, o)
			if err != nil {
				resultMu.Lock()
				hardFailures = append(hardFailures, err)
				resultMu.Unlock()
				slog.Error("failed to place order",
					"coin", o.Coin,
					"side", o.Side,
					"price", o.Price,
					"size", o.Size,
					"error", err,
				)
				return
			}
			if result.benignReject {
				resultMu.Lock()
				benignRejects++
				resultMu.Unlock()
				slog.Warn("order placement rejected benignly during refresh",
					"coin", o.Coin,
					"side", o.Side,
					"price", o.Price,
					"size", o.Size,
				)
				return
			}
			if result.oid > 0 {
				resultMu.Lock()
				newTracked = append(newTracked, trackedFromResult(result, o))
				resultMu.Unlock()
			}
			slog.Info("order placed",
				"coin", o.Coin,
				"side", o.Side,
				"price", o.Price,
				"size", o.Size,
				"reason", o.Reason,
			)
		}(o)
	}
	wg.Wait()

	if len(hardFailures) > 0 {
		lastErr := hardFailures[len(hardFailures)-1]
		return fmt.Errorf("refresh %s: %d/%d placements failed: %w", coin, len(hardFailures), len(orders), lastErr)
	}

	// Store tracked orders for diff-based refresh next cycle.
	if len(newTracked) > 0 {
		le.mu.Lock()
		le.liveOrders[coin] = newTracked
		le.markOrderGenerationLocked(coin)
		le.mu.Unlock()
	}

	le.syncCachedOrders(coin)

	if benignRejects > 0 {
		slog.Warn("refresh completed with benign ALO rejections",
			"coin", coin,
			"benign_rejects", benignRejects,
			"total_orders", len(orders),
			"placed", len(orders)-benignRejects-len(hardFailures),
		)
	}

	return nil
}

func (le *LiveExecutor) modifyOrder(ctx context.Context, coin string, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
	return le.transport.ModifyOrder(ctx, tracked, desired)
}

func (le *LiveExecutor) modifyWithRetry(ctx context.Context, coin string, tracked *trackedOrder, desired *types.OrderRequest) (placeOrderResult, error) {
	priceStr := strconv.FormatFloat(desired.Price, 'f', -1, 64)
	sizeStr := strconv.FormatFloat(desired.Size, 'f', -1, 64)
	side := normalizeSide(desired.Side)

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		// sdk.Modify passes the asset name directly in the action (no resolveAssetIndex),
		// so HIP-3 assets need the full dex-qualified name (e.g. "xyz:CL", not "CL").
		capped, capErr := le.capZeusOrder(*desired, tracked.OID)
		if capErr != nil {
			return placeOrderResult{}, capErr
		}
		if le.cfg != nil && le.cfg.ZeusMode {
			sizeStr = strconv.FormatFloat(capped.Size, 'f', -1, 64)
		}
		opts := []hyperliquid.ModifyOption{hyperliquid.ModifyWithTIF(hyperliquid.TIFALO)}
		desired.Size = capped.Size
		desired.ReduceOnly = capped.ReduceOnly
		if capped.ReduceOnly {
			opts = append(opts, hyperliquid.ModifyWithReduceOnly())
		}
		order, err := le.sdk.Modify(tracked.OID, coin, side, priceStr, sizeStr, opts...)
		le.stats.RecordExchange()
		if err == nil {
			le.recordUpstreamSuccess()
			if order != nil && order.IsError() {
				statusLower := strings.ToLower(order.Status)
				if strings.Contains(statusLower, "alo") || strings.Contains(statusLower, "would cross") || strings.Contains(statusLower, "post only") || strings.Contains(statusLower, "immediately matched") {
					slog.Debug("ALO modify rejected (would cross book)", "oid", tracked.OID, "status", order.Status)
					return placeOrderResult{benignReject: true}, nil
				}
				return placeOrderResult{}, fmt.Errorf("modify rejected: status=%s", order.Status)
			}
			result := placeOrderResult{}
			if order != nil && order.OID > 0 {
				result.oid = order.OID
			}
			return result, nil
		}

		if le.cfg != nil && le.cfg.ZeusMode {
			return placeOrderResult{}, err
		}
		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			return placeOrderResult{}, err
		}

		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		slog.Warn("retrying order modify",
			"oid", tracked.OID,
			"attempt", attempt+1,
			"backoff", backoff,
			"error", err,
		)

		select {
		case <-ctx.Done():
			return placeOrderResult{}, ctx.Err()
		case <-time.After(backoff):
		}
	}

	le.recordUpstreamError()
	return placeOrderResult{}, fmt.Errorf("order modify after retries: %w", lastErr)
}

// cancelSingleOID cancels a single order by OID. Failure is benign (order may already be gone).
func (le *LiveExecutor) cancelSingleOID(ctx context.Context, oid int64, coin string) error {
	return le.transport.CancelOrder(ctx, oid, coin)
}

func (le *LiveExecutor) cancelOrders(ctx context.Context, coin string) error {
	return le.transport.CancelAll(ctx, coin)
}

func (le *LiveExecutor) fetchOpenOrders(coin string) []types.OrderRequest {
	if le.openOrdersFn != nil {
		return le.openOrdersFn(coin)
	}
	return le.queryOpenOrders(coin)
}

// coinMatchesDex returns true if a and b refer to the same asset,
// handling both "CL" and "xyz:CL" formats for HIP-3 dex orders.
func (le *LiveExecutor) coinMatchesDex(a, b string) bool {
	if a == b {
		return true
	}
	if le.dexName == "" {
		return false
	}
	prefix := le.dexName + ":"
	return strings.TrimPrefix(a, prefix) == strings.TrimPrefix(b, prefix)
}

func (le *LiveExecutor) queryOpenOrders(coin string) []types.OrderRequest {
	var rawOrders []any
	coinDex := le.dexForCoin(coin)
	if coinDex != "" {
		orders, err := le.sdk.Info().OpenOrders(le.sdk.Address(), hyperliquid.WithDex(coinDex))
		le.stats.RecordInfo()
		if err != nil {
			slog.Error("failed to query dex open orders", "error", err)
			return nil
		}
		rawOrders = orders
	} else {
		orders, err := le.sdk.Info().OpenOrders(le.sdk.Address())
		le.stats.RecordInfo()
		if err != nil {
			slog.Error("failed to query perps open orders", "error", err)
			return nil
		}
		rawOrders = orders
	}

	var result []types.OrderRequest
	for _, raw := range rawOrders {
		o, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		oCoin, _ := o["coin"].(string)
		if !le.coinMatchesDex(oCoin, coin) {
			continue
		}
		side, _ := o["side"].(string)
		side = normalizeSide(side)
		px, _ := o["limitPx"].(string)
		sz, _ := o["sz"].(string)
		price := parseFloatFromAPI(px, "limitPx", oCoin)
		size := parseFloatFromAPI(sz, "sz", oCoin)
		result = append(result, types.OrderRequest{
			Coin:  oCoin,
			Side:  side,
			Price: price,
			Size:  size,
		})
	}
	return result
}

// reconDiagnosticPayload is the computed symmetric-difference payload between
// tracked and exchange OIDs, used by emitReconDiagnostic.
type reconDiagnosticPayload struct {
	trackedOIDs     []int64
	exchangeOIDs    []int64
	onlyLocal       []int64
	onlyLocalSides  []string
	onlyLocalPrices []float64
	onlyExchange    []int64
	trackedAgesMs   []int64
	exchangeAgesMs  []int64
	generation      uint64
	generationAgeMs int64
	exchangeFetchOK bool
	// V24: WS-race causality fields. lastFillMsAgo surfaces the time since the
	// most recent WS fill; large values = stale; small values + hard mismatch =
	// race. -1 = no fill recorded for this coin in this session.
	lastFillMsAgo int64

	// V26: cumulative-since-inception rate-limit snapshot. Correlates silent
	// exchange-side order cancellations with budget pressure. -1 when the
	// executor hasn't yet received a rate-limit update.
	rateLimitRemaining int
	rateLimitCap       int
	rateLimitPct       float64 // remaining / cap, -1 when cap is 0

	// V27: stale-order hypothesis. bookMid is the latest mid at mismatch time;
	// onlyLocalDriftBps[i] is the signed drift of onlyLocalPrices[i] vs bookMid
	// in bps. Negative = order price below current mid; positive = above.
	// Tests the theory that the exchange auto-cancels orders whose price has
	// drifted too far from the current market.
	bookMid           float64
	onlyLocalDriftBps []float64
}

// buildReconDiagnostic computes the OID symmetric difference and per-OID ages.
// tracked ages are relative to generationAt; exchange ages are 0 (no first-seen
// data is tracked). If tracked is non-nil, each entry must align index-by-index
// with trackedOIDs so onlyLocal entries can be enriched with side/price.
// V24: lastFillAt is the timestamp of the most recent WS fill for this coin
// (zero-value time.Time if none).
func buildReconDiagnostic(trackedOIDs, exchangeOIDs []int64, generation uint64, generationAt time.Time, now time.Time, tracked []trackedOrder, exchangeFetchOK bool, lastFillAt time.Time, rateLimitRemaining, rateLimitCap int, bookMid float64) reconDiagnosticPayload {
	p := reconDiagnosticPayload{
		trackedOIDs:        append([]int64(nil), trackedOIDs...),
		exchangeOIDs:       append([]int64(nil), exchangeOIDs...),
		trackedAgesMs:      make([]int64, len(trackedOIDs)),
		exchangeAgesMs:     make([]int64, len(exchangeOIDs)),
		generation:         generation,
		exchangeFetchOK:    exchangeFetchOK,
		lastFillMsAgo:      -1,
		rateLimitRemaining: rateLimitRemaining,
		rateLimitCap:       rateLimitCap,
		rateLimitPct:       -1,
		bookMid:            bookMid,
	}
	if rateLimitCap > 0 {
		p.rateLimitPct = float64(rateLimitRemaining) / float64(rateLimitCap)
	}
	if !lastFillAt.IsZero() {
		p.lastFillMsAgo = now.Sub(lastFillAt).Milliseconds()
	}
	if !generationAt.IsZero() {
		p.generationAgeMs = now.Sub(generationAt).Milliseconds()
		for i := range p.trackedAgesMs {
			p.trackedAgesMs[i] = p.generationAgeMs
		}
	}
	exchSet := make(map[int64]struct{}, len(exchangeOIDs))
	for _, o := range exchangeOIDs {
		exchSet[o] = struct{}{}
	}
	trackedSet := make(map[int64]struct{}, len(trackedOIDs))
	for _, o := range trackedOIDs {
		trackedSet[o] = struct{}{}
	}
	oidMeta := make(map[int64]trackedOrder, len(tracked))
	for _, t := range tracked {
		oidMeta[t.OID] = t
	}
	for _, o := range trackedOIDs {
		if _, ok := exchSet[o]; !ok {
			p.onlyLocal = append(p.onlyLocal, o)
			if meta, ok := oidMeta[o]; ok {
				p.onlyLocalSides = append(p.onlyLocalSides, meta.Side)
				p.onlyLocalPrices = append(p.onlyLocalPrices, meta.Price)
			} else {
				p.onlyLocalSides = append(p.onlyLocalSides, "")
				p.onlyLocalPrices = append(p.onlyLocalPrices, 0)
			}
		}
	}
	for _, o := range exchangeOIDs {
		if _, ok := trackedSet[o]; !ok {
			p.onlyExchange = append(p.onlyExchange, o)
		}
	}
	// V27: per-orphaned-order drift from current mid in bps. Allocated even
	// when bookMid is unknown so the log field is always the same length as
	// onlyLocalPrices; entries are 0 when mid is unavailable.
	p.onlyLocalDriftBps = make([]float64, len(p.onlyLocalPrices))
	if bookMid > 0 {
		for i, px := range p.onlyLocalPrices {
			if px > 0 {
				p.onlyLocalDriftBps[i] = (px - bookMid) / bookMid * 10000
			}
		}
	}
	return p
}

// reconDiagAllow returns true if a diagnostic emission is allowed for coin under
// the sliding-hour rate limit. Prunes stale entries and records the emission
// timestamp when allowed.
func (le *LiveExecutor) reconDiagAllow(coin string, maxPerHour int, now time.Time) bool {
	if maxPerHour <= 0 {
		return false
	}
	le.reconDiagMu.Lock()
	defer le.reconDiagMu.Unlock()
	stamps := le.reconDiagCounter[coin]
	cutoff := now.Add(-time.Hour)
	kept := stamps[:0]
	for _, t := range stamps {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= maxPerHour {
		le.reconDiagCounter[coin] = kept
		return false
	}
	kept = append(kept, now)
	le.reconDiagCounter[coin] = kept
	return true
}

// emitReconDiagnostic logs the diagnostic payload at INFO level.
func emitReconDiagnostic(coin string, p reconDiagnosticPayload) {
	slog.Info("reconciliation mismatch diagnostic",
		"coin", coin,
		"tracked_count", len(p.trackedOIDs),
		"exchange_count", len(p.exchangeOIDs),
		"exchange_fetch_ok", p.exchangeFetchOK,
		"only_local", p.onlyLocal,
		"only_local_sides", p.onlyLocalSides,
		"only_local_prices", p.onlyLocalPrices,
		"only_exchange", p.onlyExchange,
		"tracked_ages_ms", p.trackedAgesMs,
		"exchange_ages_ms", p.exchangeAgesMs,
		"generation", p.generation,
		"generation_age_ms", p.generationAgeMs,
		"last_fill_ms_ago", p.lastFillMsAgo, // V24: WS-race causality signal (-1 = none)
		"rate_limit_remaining", p.rateLimitRemaining, // V26: budget correlation
		"rate_limit_cap", p.rateLimitCap,
		"rate_limit_pct", p.rateLimitPct, // -1 when cap unknown
		"book_mid", p.bookMid, // V27: stale-order hypothesis — mid at mismatch time (0 when unknown)
		"only_local_drift_bps", p.onlyLocalDriftBps, // V27: per-orphaned-order drift from mid in bps
	)
}

// queryOpenOrderOIDs returns OIDs of open orders for coin. Used for the
// reconciliation mismatch diagnostic. The second return value is false when
// the underlying OpenOrders query failed (a warning is logged); callers should
// propagate this to the diagnostic so readers can distinguish empty-exchange
// from failed-fetch.
func (le *LiveExecutor) queryOpenOrderOIDs(coin string) ([]int64, bool) {
	var rawOrders []any
	coinDex := le.dexForCoin(coin)
	if coinDex != "" {
		orders, err := le.sdk.Info().OpenOrders(le.sdk.Address(), hyperliquid.WithDex(coinDex))
		le.stats.RecordInfo()
		if err != nil {
			slog.Warn("reconciliation diagnostic: OID fetch failed", "coin", coin, "err", err)
			return nil, false
		}
		rawOrders = orders
	} else {
		orders, err := le.sdk.Info().OpenOrders(le.sdk.Address())
		le.stats.RecordInfo()
		if err != nil {
			slog.Warn("reconciliation diagnostic: OID fetch failed", "coin", coin, "err", err)
			return nil, false
		}
		rawOrders = orders
	}

	var oids []int64
	for _, raw := range rawOrders {
		o, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		oCoin, _ := o["coin"].(string)
		if !le.coinMatchesDex(oCoin, coin) {
			continue
		}
		oidFloat, ok := o["oid"].(float64)
		if !ok {
			continue
		}
		oids = append(oids, int64(oidFloat))
	}
	return oids, true
}

// CancelAll cancels all open orders for the coin.
func (le *LiveExecutor) CancelAll(ctx context.Context, coin string) error {
	le.mu.Lock()
	delete(le.liveOrders, coin)
	le.markOrderGenerationLocked(coin)
	le.mu.Unlock()
	err := le.cancelOrders(ctx, coin)
	if err != nil {
		return err
	}
	le.syncCachedOrders(coin)
	return nil
}

// CancelSide cancels only one side of tracked open orders for the coin.
func (le *LiveExecutor) CancelSide(ctx context.Context, coin string, side string, reason string) error {
	side = normalizeSide(side)
	if side != "buy" && side != "sell" {
		return fmt.Errorf("cancel side %s: invalid side %q", coin, side)
	}

	le.refreshInProgress.Add(1)
	defer le.refreshInProgress.Add(-1)

	le.mu.Lock()
	tracked := le.liveOrders[coin]
	if len(tracked) == 0 {
		le.mu.Unlock()
		return nil
	}
	targets := make([]cancelTarget, 0, len(tracked))
	for _, order := range tracked {
		if normalizeSide(order.Side) == side {
			targets = append(targets, cancelTarget{oid: order.OID, coin: coin})
		}
	}
	if len(targets) == 0 {
		le.mu.Unlock()
		return nil
	}
	le.mu.Unlock()

	var err error
	if le.transport.SupportsBatch() {
		err = le.transport.CancelBatch(ctx, targets)
	} else {
		for _, target := range targets {
			if cancelErr := le.cancelSingleOID(ctx, target.oid, target.coin); cancelErr != nil {
				err = cancelErr
			}
		}
	}
	if err != nil {
		slog.Error("cancel side failed", "coin", coin, "side", side, "reason", reason, "error", err)
		return err
	}
	cancelled := make(map[int64]struct{}, len(targets))
	for _, target := range targets {
		cancelled[target.oid] = struct{}{}
	}
	le.mu.Lock()
	current := le.liveOrders[coin]
	kept := make([]trackedOrder, 0, len(current))
	for _, order := range current {
		if _, ok := cancelled[order.OID]; ok && normalizeSide(order.Side) == side {
			continue
		}
		kept = append(kept, order)
	}
	if len(kept) > 0 {
		le.liveOrders[coin] = kept
	} else {
		delete(le.liveOrders, coin)
	}
	le.orderEpoch[coin]++
	le.markOrderGenerationLocked(coin)
	le.mu.Unlock()
	le.syncCachedOrders(coin)
	slog.Info("cancel side complete",
		"coin", coin,
		"side", side,
		"cancelled", len(targets),
		"reason", reason,
	)
	return nil
}

// ClosePosition closes any open position for the coin. Three-tier strategy:
// 1. Passive close: post aggressive ALO at best bid/ask, wait up to 5s for fill
// 2. Limit IOC with slippage: try 1x and 2x slippage multipliers
// 3. Market close: SDK market close as last resort
func (le *LiveExecutor) ClosePosition(ctx context.Context, coin string, reason string) error {
	if _, _, isFlat := le.positionSnapshot(coin); isFlat {
		return nil
	}

	le.bookMu.RLock()
	book := le.latestBook[coin]
	le.bookMu.RUnlock()

	bookFresh := book.Mid > 0 && time.Since(book.Timestamp) <= 5*time.Second

	// Entry log: single structured line that captures the tier-decision context.
	// When a close fails, the pair of this line + the final error line is
	// enough to reconstruct why a specific tier fired or was skipped (P2-1
	// instrumentation added after v18 showed 144 failed-close cycles that
	// were not diagnosable from the existing per-tier warnings alone).
	posSide, posSize, _ := le.positionSnapshot(coin)
	slog.Info("close position requested",
		"coin", coin,
		"reason", reason,
		"pos_side", posSide,
		"pos_size", posSize,
		"book_fresh", bookFresh,
		"book_age_ms", time.Since(book.Timestamp).Milliseconds(),
		"book_mid", book.Mid,
		"max_close_slippage_bps", le.maxCloseSlippageBps,
	)

	// Tier 1: Passive close — post at best bid/ask and wait for fill.
	// This avoids crossing the spread on thin books.
	if bookFresh {
		posSide, posSize, isFlat := le.positionSnapshot(coin)
		if isFlat {
			return nil
		}
		posSize = le.roundSizeForCoin(coin, posSize)

		var side string
		var price float64
		if posSide == "long" {
			side = "sell"
			price = book.BestAsk // post on the ask side
		} else {
			side = "buy"
			price = book.BestBid // post on the bid side
		}

		if price > 0 {
			order := types.OrderRequest{
				Coin:        coin,
				Side:        side,
				Price:       types.RoundToTickSize(price),
				Size:        posSize,
				TimeInForce: "ALO",
				Intent:      "flatten",
				Reason:      reason + "_passive",
			}

			result, err := le.placeOrder(ctx, order)
			if err == nil && result.oid != 0 {
				slog.Info("passive close order posted",
					"coin", coin,
					"side", side,
					"price", price,
					"size", posSize,
					"oid", result.oid,
				)

				// Wait up to 5 seconds for a passive fill.
				passiveDeadline := time.After(5 * time.Second)
				passiveTicker := time.NewTicker(500 * time.Millisecond)
				filled := false
			passiveLoop:
				for {
					select {
					case <-ctx.Done():
						// Cancel the passive order before returning.
						_ = le.cancelSingleOID(context.Background(), result.oid, coin)
						return ctx.Err()
					case <-passiveDeadline:
						break passiveLoop
					case <-passiveTicker.C:
						if _, _, isFlat := le.positionSnapshot(coin); isFlat {
							filled = true
							break passiveLoop
						}
					}
				}

				passiveTicker.Stop()

				if filled {
					slog.Info("position closed via passive order",
						"coin", coin,
						"reason", reason,
					)
					return nil
				}

				// Cancel the passive order before escalating.
				if err := le.cancelSingleOID(ctx, result.oid, coin); err != nil {
					slog.Warn("failed to cancel passive close order, proceeding to IOC",
						"coin", coin,
						"oid", result.oid,
						"error", err,
					)
				}
			} else if err != nil {
				slog.Warn("passive close order failed, skipping to IOC tier",
					"coin", coin,
					"error", err,
				)
			}
		}
	}

	if _, _, isFlat := le.positionSnapshot(coin); isFlat {
		return nil
	}

	// Tier 2: Limit IOC with slippage protection.
	if le.maxCloseSlippageBps > 0 {
		if !bookFresh && book.Mid > 0 {
			slog.Warn("book data stale for slippage-protected close, falling through to market close",
				"coin", coin,
				"age_ms", time.Since(book.Timestamp).Milliseconds(),
			)
		} else if bookFresh {
			for _, multiplier := range []int{1, 2} {
				posSide, posSize, isFlat := le.positionSnapshot(coin)
				if isFlat {
					slog.Info("position closed via limit IOC",
						"coin", coin,
						"reason", reason,
						"slippage_multiplier", multiplier-1,
					)
					return nil
				}
				posSize = le.roundSizeForCoin(coin, posSize)

				slippage := book.Mid * float64(le.maxCloseSlippageBps*multiplier) / 10000.0
				var side string
				var price float64
				if posSide == "long" {
					side = "sell"
					price = book.BestBid - slippage
				} else {
					side = "buy"
					price = book.BestAsk + slippage
				}
				if price <= 0 {
					break
				}

				order := types.OrderRequest{
					Coin:        coin,
					Side:        side,
					Price:       types.RoundToTickSize(price),
					Size:        posSize,
					TimeInForce: "IOC",
					Intent:      "flatten",
					Reason:      reason,
				}

				_, err := le.placeOrder(ctx, order)
				if err != nil {
					slog.Warn("limit IOC close attempt failed",
						"coin", coin,
						"multiplier", multiplier,
						"error", err,
					)
					continue
				}

				// Allow time for fill to process through WebSocket.
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(200 * time.Millisecond):
				}

				if _, _, isFlat := le.positionSnapshot(coin); isFlat {
					slog.Info("position closed via limit IOC",
						"coin", coin,
						"reason", reason,
						"slippage_multiplier", multiplier,
					)
					return nil
				}
			}

			slog.Warn("limit IOC did not fully close position, falling back to market close",
				"coin", coin,
				"reason", reason,
			)
		}
	}

	// Tier 3: Market close fallback (existing behavior with retries).
	return le.marketClosePosition(ctx, coin, reason)
}

func (le *LiveExecutor) positionSnapshot(coin string) (side string, size float64, isFlat bool) {
	le.mu.Lock()
	defer le.mu.Unlock()

	pos := le.positions[coin]
	if pos == nil || pos.side == "" || pos.size == 0 {
		return "", 0, true
	}
	return pos.side, pos.size, false
}

// marketClosePosition uses sdk.ClosePosition with retry for market-price close.
// When all retries fail, logs position + book state before returning so the
// caller's generic "failed to close position on halt" error line is diagnosable
// (P2-1 instrumentation from v18 analysis).
func (le *LiveExecutor) marketClosePosition(ctx context.Context, coin string, reason string) error {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		_, err := le.sdk.ClosePosition(coin)
		le.stats.RecordExchange()
		if err == nil {
			slog.Info("position closed", "coin", coin, "reason", reason)

			// Reconcile from on-chain state instead of optimistically zeroing,
			// which could mask partial fills or concurrent position changes.
			// Delay briefly to let WS fill events arrive before querying on-chain state.
			go func() {
				time.Sleep(500 * time.Millisecond)
				le.reconcilePositions()
			}()

			return nil
		}

		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			posSide, posSize, _ := le.positionSnapshot(coin)
			slog.Error("market close non-retryable, giving up",
				"coin", coin,
				"reason", reason,
				"attempt", attempt+1,
				"pos_side", posSide,
				"pos_size", posSize,
				"error", err,
			)
			return fmt.Errorf("close position %s: %w", coin, err)
		}

		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		slog.Warn("retrying close position",
			"coin", coin,
			"attempt", attempt+1,
			"backoff", backoff,
			"error", err,
		)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}

	// All retries exhausted — log rich context so the subsequent
	// "failed to close position on halt" error can be diagnosed.
	posSide, posSize, _ := le.positionSnapshot(coin)
	le.bookMu.RLock()
	book := le.latestBook[coin]
	le.bookMu.RUnlock()
	slog.Error("market close failed after all retries",
		"coin", coin,
		"reason", reason,
		"pos_side", posSide,
		"pos_size", posSize,
		"book_mid", book.Mid,
		"book_age_ms", time.Since(book.Timestamp).Milliseconds(),
		"error", lastErr,
	)
	return fmt.Errorf("close position %s after retries: %w", coin, lastErr)
}

// ReducePositionIOC sends a single limit-IOC order sized to close `fraction`
// of the current position on the position-reducing side. Best-effort: if the
// book is stale or the IOC does not fill, returns 0 without error. Does not
// retry. Intended for fractional profit-taking on favorable price shocks
// (V30 part 4), where the signal is time-sensitive and a missed fill simply
// means continuing with the passive sizing+tightening path.
func (le *LiveExecutor) ReducePositionIOC(ctx context.Context, coin string, fraction float64, slippageBps float64, reason string) (float64, error) {
	if fraction <= 0 || fraction > 1 {
		return 0, fmt.Errorf("reduce fraction out of range: %g", fraction)
	}

	posSide, posSize, isFlat := le.positionSnapshot(coin)
	if isFlat {
		return 0, nil
	}

	le.bookMu.RLock()
	book := le.latestBook[coin]
	le.bookMu.RUnlock()

	bookFresh := book.Mid > 0 && time.Since(book.Timestamp) <= 3*time.Second
	if !bookFresh {
		slog.Warn("reduce IOC skipped: book stale",
			"coin", coin,
			"reason", reason,
			"book_age_ms", time.Since(book.Timestamp).Milliseconds(),
		)
		return 0, nil
	}

	reduceSize := le.roundSizeForCoin(coin, posSize*fraction)
	if reduceSize <= 0 {
		slog.Info("reduce IOC skipped: rounded size is zero",
			"coin", coin,
			"reason", reason,
			"pos_size", posSize,
			"fraction", fraction,
		)
		return 0, nil
	}

	slippage := book.Mid * slippageBps / 10000.0
	var side string
	var price float64
	if posSide == "long" {
		side = "sell"
		price = book.BestBid - slippage
	} else {
		side = "buy"
		price = book.BestAsk + slippage
	}
	if price <= 0 {
		return 0, nil
	}

	order := types.OrderRequest{
		Coin:        coin,
		Side:        side,
		Price:       types.RoundToTickSize(price),
		Size:        reduceSize,
		TimeInForce: "IOC",
		Intent:      "flatten",
		Reason:      reason,
	}

	slog.Info("reduce position IOC submitted",
		"coin", coin,
		"reason", reason,
		"pos_side", posSide,
		"pos_size", posSize,
		"fraction", fraction,
		"reduce_size", reduceSize,
		"side", side,
		"price", price,
		"slippage_bps", slippageBps,
	)

	sizeBefore := posSize
	// measureFilled takes a post-call snapshot and returns a non-negative
	// delta from sizeBefore. Always call this — even on placeOrder error or
	// ctx cancellation — because an IOC can partially fill before ack
	// timeout / network partition / ctx cancel, and reporting filled=0 in
	// that case would leave the caller with stale assumptions about
	// position state.
	measureFilled := func() float64 {
		_, posSizeAfter, _ := le.positionSnapshot(coin)
		delta := sizeBefore - posSizeAfter
		if delta < 0 {
			return 0
		}
		return delta
	}

	_, placeErr := le.placeOrder(ctx, order)

	// Always wait for fill-event processing so the snapshot reflects any
	// partial fill, even if placeOrder returned an error (the order may
	// have been accepted server-side before the error surfaced to us).
	select {
	case <-ctx.Done():
		filled := measureFilled()
		slog.Warn("reduce IOC ctx cancelled",
			"coin", coin,
			"reason", reason,
			"filled_size", filled,
			"place_err", placeErr,
		)
		return filled, ctx.Err()
	case <-time.After(250 * time.Millisecond):
	}

	filled := measureFilled()
	if placeErr != nil {
		slog.Warn("reduce IOC placement error (checked for partial fill)",
			"coin", coin,
			"reason", reason,
			"filled_size", filled,
			"error", placeErr,
		)
		return filled, placeErr
	}

	_, posSizeAfter, _ := le.positionSnapshot(coin)
	slog.Info("reduce position IOC completed",
		"coin", coin,
		"reason", reason,
		"target_size", reduceSize,
		"filled_size", filled,
		"pos_size_after", posSizeAfter,
	)
	return filled, nil
}

// Position returns the current position state for the coin.
func (le *LiveExecutor) Position(coin string) types.PositionUpdate {
	le.mu.Lock()
	defer le.mu.Unlock()

	pos, ok := le.positions[coin]
	if !ok || pos.side == "" {
		return types.PositionUpdate{Coin: coin}
	}

	return types.PositionUpdate{
		Coin:     coin,
		Side:     pos.side,
		Size:     pos.size,
		AvgEntry: pos.avgEntry,
		Notional: pos.size * pos.avgEntry,
	}
}

// PositionUpdates returns the channel for position change notifications.
func (le *LiveExecutor) PositionUpdates() <-chan types.PositionUpdate {
	return le.posCh
}

// Stats returns and resets the accumulated RPS counters.
func (le *LiveExecutor) Stats() RPSSnapshot {
	return le.stats.Snapshot()
}

// recordUpstreamError increments the upstream error count and sets the timestamp.
func (le *LiveExecutor) recordUpstreamError() {
	le.upstreamMu.Lock()
	defer le.upstreamMu.Unlock()
	le.upstreamErrCount++
	le.upstreamLastErr = time.Now()
}

// recordUpstreamSuccess resets the upstream error count if the last error
// was more than 30 seconds ago.
func (le *LiveExecutor) recordUpstreamSuccess() {
	le.upstreamMu.Lock()
	defer le.upstreamMu.Unlock()
	if time.Since(le.upstreamLastErr) > 30*time.Second {
		le.upstreamErrCount = 0
	}
}

// isUpstreamUnhealthy returns true if there have been >= 2 upstream errors
// within the last 30 seconds.
func (le *LiveExecutor) isUpstreamUnhealthy() bool {
	le.upstreamMu.Lock()
	defer le.upstreamMu.Unlock()
	return le.upstreamErrCount >= 2 && time.Since(le.upstreamLastErr) <= 30*time.Second
}

// Equity returns the cached account equity (refreshed every 15s by pollEquity).
func (le *LiveExecutor) Equity() float64 {
	le.equityMu.RLock()
	defer le.equityMu.RUnlock()
	return le.cachedEquity
}

// queryEquityAllSources computes total equity across Hyperliquid sub-accounts.
//
// Mixed-namespace mode (HIP-3 + perps): queries up to 3 sources:
//   - Dex: marginSummary.accountValue (margin + unrealized PnL + idle cash)
//   - Perps: marginSummary.accountValue
//   - Spot: free USDC not locked in any dex (total - hold)
//
// Pure dex mode skips perps (always $0). Pure perps mode skips dex.
func (le *LiveExecutor) queryEquityAllSources() float64 {
	// Parallelize queries to shrink the inter-call settlement race window.
	// Sequential calls had 300-1000ms gaps where USDC could move between
	// namespaces causing double-counting or missed reads (~$10 swings).
	var dexEq, perpsEq, spotEq float64
	var wg sync.WaitGroup

	if le.hasDex {
		wg.Add(1)
		go func() {
			defer wg.Done()
			dexEq = le.queryClearinghouseEquityRaw(hyperliquid.WithDex(le.dexName))
		}()
	}
	if le.hasPerps {
		wg.Add(1)
		go func() {
			defer wg.Done()
			perpsEq = le.queryClearinghouseEquityRaw()
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		spotEq = le.querySpotUSDC()
	}()

	wg.Wait()
	return dexEq + perpsEq + spotEq
}

// queryClearinghouseEquityRaw fetches total equity from marginSummary.accountValue.
func (le *LiveExecutor) queryClearinghouseEquityRaw(opts ...hyperliquid.InfoOption) float64 {
	state, err := le.sdk.Info().ClearinghouseState(le.sdk.Address(), opts...)
	le.stats.RecordInfo()
	if err != nil {
		slog.Error("failed to get clearinghouse state for equity", "error", err)
		return 0
	}

	// Parse marginSummary.accountValue from the response.
	if ms, ok := state["marginSummary"].(map[string]any); ok {
		if av, ok := ms["accountValue"].(string); ok {
			return parseFloatFromAPI(av, "accountValue", "equity")
		}
	}

	return 0
}

// querySpotUSDC fetches the unencumbered USDC balance from the spot
// clearinghouse. The "hold" field represents USDC locked in HIP-3 dex
// sub-accounts, which is already counted by the dex equity query,
// so we subtract it to avoid double-counting.
func (le *LiveExecutor) querySpotUSDC() float64 {
	state, err := le.sdk.Info().SpotClearinghouseState(le.sdk.Address())
	le.stats.RecordInfo()
	if err != nil {
		slog.Error("failed to get spot clearinghouse state", "error", err)
		return 0
	}

	balances, ok := state["balances"].([]any)
	if !ok {
		return 0
	}
	for _, b := range balances {
		bal, ok := b.(map[string]any)
		if !ok {
			continue
		}
		coin, _ := bal["coin"].(string)
		if coin != "USDC" {
			continue
		}
		totalStr, ok := bal["total"].(string)
		if !ok {
			slog.Warn("spot USDC: total field is not a string", "raw", bal["total"])
			return 0
		}
		total := parseFloatFromAPI(totalStr, "total", "USDC")
		hold := 0.0
		if holdStr, ok := bal["hold"].(string); ok {
			hold = parseFloatFromAPI(holdStr, "hold", "USDC")
		}
		return total - hold
	}
	return 0
}

// syncCachedOrders projects the executor's internal liveOrders tracking
// into cachedOpenOrders so that OpenOrders() returns a consistent view
// immediately after mutations, without waiting for the 5-second poll.
func (le *LiveExecutor) syncCachedOrders(coin string) {
	le.mu.Lock()
	tracked := le.liveOrders[coin]
	snapshot := make([]types.OrderRequest, 0, len(tracked))
	for _, to := range tracked {
		snapshot = append(snapshot, types.OrderRequest{
			Coin:      coin,
			Side:      to.Side,
			Price:     to.Price,
			Size:      to.Size,
			WallFront: to.WallFront,
		})
	}
	le.mu.Unlock()

	le.openOrdersMu.Lock()
	if le.cachedOpenOrders == nil {
		le.cachedOpenOrders = make(map[string][]types.OrderRequest)
	}
	le.cachedOpenOrders[coin] = snapshot
	le.openOrdersMu.Unlock()
}

// OpenOrders returns the cached open orders for a coin (refreshed every 15s by pollOpenOrders).
func (le *LiveExecutor) OpenOrders(coin string) []types.OrderRequest {
	le.openOrdersMu.RLock()
	defer le.openOrdersMu.RUnlock()
	return le.cachedOpenOrders[coin]
}

func (le *LiveExecutor) cancelWithRetry(ctx context.Context, coin string) error {
	coinDex := le.dexForCoin(coin)
	if coinDex != "" {
		// SDK's CancelAll internally calls OpenOrders without dex context,
		// so it silently finds zero HIP-3 orders and returns success.
		// Work around by querying orders with WithDex and cancelling by OID.
		return le.cancelDexOrders(ctx, coin)
	}

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		_, err := le.sdk.CancelAll(coin)
		le.stats.RecordExchange()
		if err == nil {
			slog.Info("cancelled all orders", "coin", coin)
			return nil
		}

		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			return fmt.Errorf("cancel all %s: %w", coin, err)
		}

		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		slog.Warn("retrying cancel all",
			"coin", coin,
			"attempt", attempt+1,
			"backoff", backoff,
			"error", err,
		)

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}

	return fmt.Errorf("cancel all %s after retries: %w", coin, lastErr)
}

// cancelDexOrders cancels HIP-3 orders by querying with dex context and
// cancelling each by OID. This works around sdk.CancelAll not supporting
// the WithDex option for HIP-3 markets.
func (le *LiveExecutor) cancelDexOrders(ctx context.Context, coin string) error {
	var rawOrders []any
	var lastErr error

	// Query open orders with dex context (with retry).
	for attempt := 0; attempt < 3; attempt++ {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		var err error
		rawOrders, err = le.sdk.Info().OpenOrders(le.sdk.Address(), hyperliquid.WithDex(le.dexName))
		le.stats.RecordInfo()
		if err == nil {
			break
		}
		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			return fmt.Errorf("query dex open orders: %w", err)
		}
		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}
	if rawOrders == nil && lastErr != nil {
		return fmt.Errorf("query dex open orders after retries: %w", lastErr)
	}

	// Parse OIDs to cancel.
	var targets []cancelTarget
	for _, raw := range rawOrders {
		o, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		oCoin, _ := o["coin"].(string)
		if coin != "" && !le.coinMatchesDex(oCoin, coin) {
			continue
		}
		oidFloat, ok := o["oid"].(float64)
		if !ok {
			continue
		}
		// Native execution requires exact DEX-qualified names. Never strip
		// the prefix: identical base names on different DEXes have different IDs.
		cancelCoin := oCoin
		if !strings.Contains(cancelCoin, ":") && le.dexName != "" {
			cancelCoin = le.dexName + ":" + cancelCoin
		}
		targets = append(targets, cancelTarget{oid: int64(oidFloat), coin: cancelCoin})
	}

	if len(targets) == 0 {
		return nil
	}

	// Cancel concurrently with bounded concurrency.
	const maxConcurrent = 5
	sem := make(chan struct{}, maxConcurrent)
	var failed atomic.Int64
	var attempted atomic.Int64
	var wg sync.WaitGroup

	for _, t := range targets {
		// Check context before acquiring semaphore.
		select {
		case <-ctx.Done():
			wg.Wait()
			return ctx.Err()
		default:
		}

		sem <- struct{}{}
		wg.Add(1)
		go func(t cancelTarget) {
			defer wg.Done()
			defer func() { <-sem }()
			attempted.Add(1)

			var cancelErr error
			for attempt := 0; attempt < 3; attempt++ {
				select {
				case <-ctx.Done():
					failed.Add(1)
					return
				default:
				}
				_, cancelErr = le.sdk.Cancel(t.oid, t.coin)
				le.stats.RecordExchange()
				if cancelErr == nil {
					le.recordUpstreamSuccess()
					return
				}
				if !hyperliquid.IsRetryable(cancelErr) {
					break
				}
				backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
				slog.Warn("retrying dex order cancel", "oid", t.oid, "coin", t.coin, "attempt", attempt+1, "error", cancelErr)
				select {
				case <-ctx.Done():
					failed.Add(1)
					return
				case <-time.After(backoff):
				}
			}
			if cancelErr != nil {
				le.recordUpstreamError()
				slog.Warn("failed to cancel dex order after retries", "oid", t.oid, "coin", t.coin, "error", cancelErr)
				failed.Add(1)
				lastErr = cancelErr
			}
		}(t)
	}
	wg.Wait()

	f := failed.Load()
	a := attempted.Load()
	if f > 0 {
		slog.Warn("some dex order cancels failed", "failed", f, "attempted", a, "coin", coin)
		return fmt.Errorf("cancel dex orders: %d/%d failed, last error: %w", f, a, lastErr)
	}

	slog.Info("cancelled dex orders", "coin", coin, "count", a)
	return nil
}

// parseFloatFromAPI parses a float64 from an API string, logging a warning on failure.
func parseFloatFromAPI(s, field, coin string) float64 {
	var v float64
	n, _ := fmt.Sscanf(s, "%f", &v)
	if n != 1 {
		slog.Warn("failed to parse API float", "field", field, "coin", coin, "raw", s)
	}
	return v
}

func normalizedTIF(value, fallback string) string {
	switch strings.ToUpper(value) {
	case "ALO", "IOC":
		return strings.ToUpper(value)
	default:
		return fallback
	}
}

func normalizeSide(side string) string {
	switch strings.ToLower(side) {
	case "b", "buy":
		return "buy"
	case "a", "ask", "s", "sell":
		return "sell"
	default:
		return strings.ToLower(side)
	}
}

func (le *LiveExecutor) placeOrder(ctx context.Context, o types.OrderRequest) (placeOrderResult, error) {
	return le.transport.PlaceOrder(ctx, o)
}

func timeInForceOpt(tif string) hyperliquid.TIF {
	if strings.EqualFold(tif, "IOC") {
		return hyperliquid.TIFIOC
	}
	return hyperliquid.TIFALO
}

func (le *LiveExecutor) placeWithRetry(ctx context.Context, tif string, fn func() (*hyperliquid.PlacedOrder, error)) (placeOrderResult, error) {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		order, err := fn()
		le.stats.RecordExchange()
		if err == nil {
			le.recordUpstreamSuccess()
			if order != nil && order.IsError() {
				// ALO rejection (would cross the book) — don't retry, next refresh will correct.
				statusLower := strings.ToLower(order.Status)
				if strings.EqualFold(tif, "ALO") && (strings.Contains(statusLower, "alo") || strings.Contains(statusLower, "would cross") || strings.Contains(statusLower, "post only") || strings.Contains(statusLower, "immediately matched")) {
					slog.Info("ALO order rejected (would cross book)", "status", order.Status)
					return placeOrderResult{benignReject: true}, nil
				}
				return placeOrderResult{}, fmt.Errorf("order rejected: status=%s", order.Status)
			}
			result := placeOrderResult{}
			if order != nil && order.OID > 0 {
				result.oid = order.OID
			}
			return result, nil
		}

		if le.cfg != nil && le.cfg.ZeusMode {
			return placeOrderResult{}, err
		}
		lastErr = err
		if !hyperliquid.IsRetryable(err) {
			return placeOrderResult{}, err
		}

		backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
		slog.Warn("retrying order placement",
			"attempt", attempt+1,
			"backoff", backoff,
			"error", err,
		)

		select {
		case <-ctx.Done():
			return placeOrderResult{}, ctx.Err()
		case <-time.After(backoff):
		}
	}

	le.recordUpstreamError()
	return placeOrderResult{}, fmt.Errorf("order placement after retries: %w", lastErr)
}
