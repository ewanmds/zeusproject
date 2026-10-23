package main

import "time"

// RingBuffer is a fixed-capacity circular buffer. Items() returns newest-first.
type RingBuffer[T any] struct {
	items []T
	head  int // next write position
	count int
	cap   int
}

func NewRingBuffer[T any](capacity int) *RingBuffer[T] {
	return &RingBuffer[T]{
		items: make([]T, capacity),
		cap:   capacity,
	}
}

func (rb *RingBuffer[T]) Add(item T) {
	rb.items[rb.head] = item
	rb.head = (rb.head + 1) % rb.cap
	if rb.count < rb.cap {
		rb.count++
	}
}

func (rb *RingBuffer[T]) Len() int {
	return rb.count
}

// Items returns all items newest-first.
func (rb *RingBuffer[T]) Items() []T {
	out := make([]T, rb.count)
	for i := 0; i < rb.count; i++ {
		idx := (rb.head - 1 - i + rb.cap) % rb.cap
		out[i] = rb.items[idx]
	}
	return out
}

// EventType classifies events in the events feed.
type EventType int

const (
	EventFill EventType = iota
	EventWarn
	EventError
	EventRequote
	EventWallSnap
	EventPositionFlip
	EventOptimizer
	EventBurst
	EventRisk          // risk halt, position close, rate limit critical (red)
	EventRecovery      // resume, health recovered, feed recovered (green)
	EventTrendGuard    // trend guard activation/deactivation
	EventFlowGate      // flow gate activation/deactivation
	EventPriceShock    // price shock guard activation/deactivation
	EventReconMismatch // reconciliation mismatch diagnostic (INFO-level forensic log)
	EventOptimizerDeadband
	EventWallQualityState   // P7 wall_quality enabled/disabled transitions
	EventScheduleWindow     // P12 trading schedule window entered/exited
	EventPostMortem         // P13 post-mortem dump written
	EventVWAPAlphaFade      // P2 drift-aware vwap_alpha fade
	EventSessionStopLoss    // P3 session stop-loss trigger / pause expiry / reset
	EventIdleFillBreaker    // P9 idle-fill circuit breaker fired
	EventPerSideGate        // P6 per-side winrate / profit factor gate fired
	EventQuantileNormalizer // P14 empirical-quantile sub-dim normalizer warm / snapshot
	EventRegimeGate         // P1 regime-gated quoting activation/deactivation
	EventShockChain         // V30 shock-chain cascade detection: threshold crossed, pause expired, recovery
	EventMarkoutPersistExit // V32.2 markout-persistence exit accelerator fired (active position killer)
)

type Event struct {
	Time    time.Time
	Level   string
	Coin    string
	Type    EventType
	Side    string  // "buy" or "sell" for fill events
	NetPnL  float64 // net PnL for fill events (0 = not set)
	Summary string
}

type Markout struct {
	Side   string
	Size   string
	T500ms string
	T1s    string
	T2s    string
	T5s    string
}

type Position struct {
	Side     string
	Size     float64
	Notional float64
}

type QuotingState struct {
	BookMid        string
	FairValue      string
	BBOSpread      string
	Spread         string
	Skew           string
	Bias           string
	ImbalanceShift string
	FillDecayShift string
	WickBid        string
	WickAsk        string
	ZWide          string
	Warmup         bool
	Recovery       bool
	Degraded       bool
	Halted         bool
	PairReduceOnly bool
}

type SizingState struct {
	InvUtil           string
	EntryFactor       string
	AllowedBuy        string
	AllowedSell       string
	NotionalMult      string
	ScheduleMult      string // time-of-day notional schedule multiplier ("1.00" when inactive)
	ToxBuyEwma        string
	ToxSellEwma       string
	ToxBuyMult        string
	ToxSellMult       string
	TrendGuardActive  bool
	TrendGuardSuppPct string // suppress factor as string (e.g. "0.45")
	TrendGuardDevPct  string // long VWAP deviation %
	FlowGateActive    bool
	FlowGateFactor    string // flow gate factor (1.0 = none)
	PriceShockActive  bool
	PriceShockSide    string
	PriceShockBps     string
	WickSuppBid       bool
	WickSuppAsk       bool
	BidAbsentPct      string // % of quote cycles in last STATUS window with no buy orders built
	AskAbsentPct      string // % of quote cycles in last STATUS window with no sell orders built
	BothAbsentPct     string // % of quote cycles in last STATUS window with both sides absent
	PresenceCycles    int    // number of quote cycles observed in the window (0 = no data)
}

type AdaptiveState struct {
	SkewFactor       string
	VwapAlpha        string
	Autocorr         string
	OptSpreadBuy     string
	OptSpreadSell    string
	OptNotionalMult  string
	OptSkewMult      string
	OptAlphaDelta    string
	OptImbShiftMult  string
	OptFillDecayMult string
	OptRefreshMult   string
	OptEval          int
	BurstBuySpread   string
	BurstSellSpread  string
	VolBurstRatio    string
	VolBurstLevels   int
	VolBurstActive   bool
}

type VWAPState struct {
	ShortVwap string
	LongVwap  string
	Trades    int
	TradeAge  string
}

type OptimizerState struct {
	Eval      int
	Policy    string
	Utility   string
	Ready     bool
	Tox       float64
	Opp       float64
	Inv       float64
	Budget    float64
	FourthDim string // label for 4th axis: "budget_pressure" (default) or "session_pnl"
}

// TrendMechState holds V31/V32 trend-bias mechanism diagnostics.
// FVShiftBps is the latest shift from the quote attribution log; the three
// *Fired counters come from the "  markout" status line and accumulate since
// the last status tick (bot resets them on each snapshot).
type TrendMechState struct {
	FVShiftBps       string
	FVShiftFired     int
	AlphaBoostFired  int
	EntryAccelFired  int
	CompositeBuyBps  string
	CompositeSellBps string
}

type AssetState struct {
	Coin            string
	Position        Position
	Quoting         QuotingState
	Sizing          SizingState
	Adaptive        AdaptiveState
	VWAP            VWAPState
	Optimizer       OptimizerState
	TrendMech       TrendMechState
	Markouts        *RingBuffer[Markout]
	OrderCount      int
	PrevBurstActive bool // for detecting burst transitions
	PrevTrendGuard  bool // for detecting trend guard transitions
	PrevFlowGate    bool // for detecting flow gate transitions
	PrevPriceShock  bool // for detecting price shock transitions
	// Live mid-price tracking (for real-time equity estimation between STATUS blocks).
	LastBuyWall   float64   // latest snap_price from buy-side wall-front snap
	LastSellWall  float64   // latest snap_price from sell-side wall-front snap
	LastFillPrice float64   // latest fill price (buy or sell)
	LastMidUpdate time.Time // time of most recent live price update
	StatusBookMid float64   // book_mid captured at last STATUS
	StatusPosSize float64   // signed position size captured at last STATUS (negative for short)
}

func NewAssetState(coin string) *AssetState {
	return &AssetState{
		Coin:     coin,
		Markouts: NewRingBuffer[Markout](20),
	}
}

type RateLimitState struct {
	StartRemaining   int // from boot "rate limit status" log
	Remaining        int
	Pct              string
	Tier             string
	Halted           bool
	SessionUsed      int
	SessionGenerated int
	SessionNet       int
	BurnRate         string
	Headroom         string
	Depletion        string
	CumVolumeUSD     string
	VolumeRateUSD    string
	TteMin           string // time-to-exhaustion from bot's rate_limit log
}

type APIRequestsState struct {
	WindowSec int
	Total     int
	Exchange  int
	Info      int
	AvgRPS    string
}

type Portfolio struct {
	Equity            float64
	StatusEquity      float64 // equity snapshot at last STATUS, baseline for live MTM
	StartEquity       float64
	HighWater         string
	LowWater          float64
	EquityFloor       float64
	MaxDrawdownPct    float64
	SessionPnL        string
	SessionPnLBps     string
	WindowPnLBps      float64
	EquityDelta       string
	TotalExchangeFees string
	TotalBuilderFees  string
	TotalAllFees      string
	TotalExposure     float64
	MaxExposure       float64

	// P12: Trading schedule window state.
	ScheduleActive     bool
	ScheduleLabel      string
	ScheduleMultiplier float64

	// P2: Most recent effective vwap_alpha after drift fade (0 if never logged).
	EffectiveVWAPAlpha float64

	// P3: Session stop-loss state.
	SessionStopActive bool
	SessionStopReason string

	// P1: Regime-gated quoting state.
	RegimeGateActive bool
	RegimeDir        int

	// V30: Shock-chain cascade state.
	ShockChainActive bool   // true during pause/recovery after cascade
	ShockChainReason string // "paused", "recovery", ""
}

type Session struct {
	StartTime  time.Time
	LogFile    string
	Mode       string // "live" or "static"
	CommitHash string
	Assets     []string
	BotMode    string // "LIVE" or "PAPER"
}

type FillCounts struct {
	Total        int
	Buy          int
	Sell         int
	BuyNotional  float64
	SellNotional float64
}

type State struct {
	Session        Session
	Portfolio      Portfolio
	Assets         map[string]*AssetState
	AssetOrder     []string
	RateLimit      RateLimitState
	APIRequests    APIRequestsState
	Events         *RingBuffer[Event]
	Fills          FillCounts
	UnmatchedCount int
	LastUpdate     time.Time
}

func NewState() *State {
	return &State{
		Assets: make(map[string]*AssetState),
		Events: NewRingBuffer[Event](200),
	}
}

func (s *State) GetOrCreateAsset(coin string) *AssetState {
	if a, ok := s.Assets[coin]; ok {
		return a
	}
	a := NewAssetState(coin)
	s.Assets[coin] = a
	s.AssetOrder = append(s.AssetOrder, coin)
	return a
}

// LatestMid returns the freshest mid-price estimate available for this asset.
// Prefers the average of buy/sell wall snaps (BBO proxy); falls back to last fill
// price, then to the book_mid captured at the most recent STATUS block.
func (a *AssetState) LatestMid() float64 {
	if a.LastBuyWall > 0 && a.LastSellWall > 0 {
		return (a.LastBuyWall + a.LastSellWall) / 2
	}
	if a.LastFillPrice > 0 {
		return a.LastFillPrice
	}
	if a.LastBuyWall > 0 {
		return a.LastBuyWall
	}
	if a.LastSellWall > 0 {
		return a.LastSellWall
	}
	return a.StatusBookMid
}

// LiveEquity returns a real-time estimate of equity by marking open positions
// to the latest available mid-price, using the last STATUS equity as baseline.
// Falls back to Portfolio.Equity if no snapshot is available yet.
func (s *State) LiveEquity() float64 {
	if s.Portfolio.StatusEquity == 0 {
		return s.Portfolio.Equity
	}
	delta := 0.0
	for _, coin := range s.AssetOrder {
		a := s.Assets[coin]
		if a.StatusBookMid == 0 || a.StatusPosSize == 0 {
			continue
		}
		cur := a.LatestMid()
		if cur == 0 {
			continue
		}
		delta += a.StatusPosSize * (cur - a.StatusBookMid)
	}
	return s.Portfolio.StatusEquity + delta
}
