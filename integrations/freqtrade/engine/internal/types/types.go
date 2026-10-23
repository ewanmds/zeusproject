package types

import (
	"math"
	"strings"
	"time"
)

// BookLevel represents one price level in the order book.
type BookLevel struct {
	Price float64
	Size  float64
}

// BookUpdate represents a parsed L2 book snapshot for one coin.
type BookUpdate struct {
	Coin       string
	BestBid    float64
	BestAsk    float64
	Mid        float64
	Microprice float64
	Bids       []BookLevel // up to 10 levels, best first
	Asks       []BookLevel // up to 10 levels, best first
	Timestamp  time.Time
}

// SpreadSignal carries the spread engine's current assessment.
type SpreadSignal struct {
	ZScore    float64
	Spread    float64
	Mean      float64
	StdDev    float64
	Beta      float64 // rolling OLS beta (CL vs BRENT), 1.0 when not using beta hedge
	ZStdDev   float64 // rolling std dev of z-scores (Feature 5); 0 = not ready, 1.0 = default
	Timestamp time.Time
}

// RiskAction indicates what the risk manager wants the quote manager to do.
type RiskAction int

const (
	RiskResume                RiskAction = iota // Resume normal quoting (position-limit or other non-drawdown)
	RiskHalt                                    // Cancel all, close positions, cooldown
	RiskResumeFromDrawdown                      // Resume after drawdown halt (triggers recovery mode)
	RiskSessionStopLoss                         // Session PnL breached floor: cancel all + close positions + pause
	RiskResumeFromSessionStop                   // Resume after session-stop pause (triggers session-stop recovery mode)
)

// String returns a human-readable name for the risk action.
func (r RiskAction) String() string {
	switch r {
	case RiskResume:
		return "RESUME"
	case RiskHalt:
		return "HALT"
	case RiskResumeFromDrawdown:
		return "RESUME_FROM_DRAWDOWN"
	case RiskSessionStopLoss:
		return "SESSION_STOP_LOSS"
	case RiskResumeFromSessionStop:
		return "RESUME_FROM_SESSION_STOP"
	default:
		return "UNKNOWN"
	}
}

// PositionUpdate represents the current position state for one coin.
type PositionUpdate struct {
	Coin     string
	Side     string  // "long", "short", or "" (flat)
	Size     float64 // absolute size
	Notional float64 // size * avgEntry
	AvgEntry float64
}

// OrderRequest is what the quote manager sends to the executor.
type OrderRequest struct {
	ReduceOnly  bool // Zeus: never let an inventory exit reverse the position.
	Coin        string
	Side        string // "buy" or "sell"
	Price       float64
	Size        float64
	Reason      string // human-readable reason for logging
	TimeInForce string // "ALO" or "IOC"
	Intent      string // "quote", "hedge", or "flatten"
	Force       bool   // bypass normal skip thresholds during protective refreshes
	WallFront   bool   // true when this order was snapped in front of a detected wall
}

// FillEvent represents a parsed fill from the WebSocket UserFills stream.
type FillEvent struct {
	Coin       string
	Side       string // "buy" or "sell"
	Size       float64
	Price      float64
	OID        int64
	Fee        float64 // exchange fee in USD
	BuilderFee float64 // builder fee in USD
	WallFront  bool    // fill came from a wall-fronted quote

	// Realized PnL fields — populated by executor when fill reduces a position.
	RealizedPnL float64 // gross realized PnL (before fees)
	ClosedSize  float64 // portion of fill that closed existing position
}

// TradeUpdate represents a public trade on the exchange.
type TradeUpdate struct {
	Coin      string
	Price     float64
	Size      float64
	Side      string // "buy" or "sell"
	Crossed   bool   // true = taker (aggressor), false = maker; only taker side represents directional pressure
	Timestamp time.Time
}

// RoundToTickSize rounds a price to Hyperliquid's tick size (5 significant figures).
// e.g. 105.99544 → 106.00, 99.37491 → 99.375
func RoundToTickSize(price float64) float64 {
	if price == 0 {
		return 0
	}
	const sigFigs = 5
	d := math.Ceil(math.Log10(math.Abs(price)))
	pow := math.Pow(10, float64(sigFigs)-d)
	return math.Round(price*pow) / pow
}

// ParseAsset splits "xyz:CL" into dex="xyz", base="CL".
// For plain assets like "BTC", returns dex="", base="BTC".
func ParseAsset(asset string) (dex, base string) {
	if i := strings.Index(asset, ":"); i >= 0 {
		return asset[:i], asset[i+1:]
	}
	return "", asset
}
