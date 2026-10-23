package spread

import (
	"context"
	"log/slog"
	"math"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

// recomputeEveryRotations triggers a full recomputation of sums to eliminate
// floating-point drift from incremental add/subtract over many rotations.
const recomputeEveryRotations = 1000

// rollingStats maintains a circular buffer for O(1) incremental mean/stddev.
type rollingStats struct {
	window    int
	buf       []float64
	pos       int // next write position
	count     int // number of values added (capped at window)
	sum       float64
	sumSq     float64
	rotations int // number of full buffer rotations
}

func newRollingStats(window int) *rollingStats {
	return &rollingStats{
		window: window,
		buf:    make([]float64, window),
	}
}

func (rs *rollingStats) add(v float64) {
	if rs.count == rs.window {
		// Remove the oldest value.
		old := rs.buf[rs.pos]
		rs.sum -= old
		rs.sumSq -= old * old
	} else {
		rs.count++
	}
	rs.buf[rs.pos] = v
	rs.sum += v
	rs.sumSq += v * v
	rs.pos = (rs.pos + 1) % rs.window
	if rs.pos == 0 && rs.count == rs.window {
		rs.rotations++
		if rs.rotations%recomputeEveryRotations == 0 {
			rs.recompute()
		}
	}
}

func (rs *rollingStats) recompute() {
	var sum, sumSq float64
	for i := 0; i < rs.count; i++ {
		sum += rs.buf[i]
		sumSq += rs.buf[i] * rs.buf[i]
	}
	rs.sum = sum
	rs.sumSq = sumSq
}

func (rs *rollingStats) ready() bool {
	return rs.count == rs.window
}

func (rs *rollingStats) mean() float64 {
	if !rs.ready() {
		return 0
	}
	return rs.sum / float64(rs.count)
}

func (rs *rollingStats) variance() float64 {
	if !rs.ready() {
		return 0
	}
	m := rs.mean()
	return rs.sumSq/float64(rs.count) - m*m
}

func (rs *rollingStats) stddev() float64 {
	if !rs.ready() {
		return 0
	}
	v := rs.variance()
	if v < 0 {
		// Floating-point rounding can cause tiny negative values.
		v = 0
	}
	return math.Sqrt(v)
}

// rollingOLS computes incremental OLS beta using circular buffers.
type rollingOLS struct {
	window    int
	xBuf      []float64 // first asset prices (CL)
	yBuf      []float64 // second asset prices (BRENT)
	pos       int
	count     int
	sumX      float64
	sumY      float64
	sumXY     float64
	sumXX     float64
	rotations int
}

func newRollingOLS(window int) *rollingOLS {
	return &rollingOLS{
		window: window,
		xBuf:   make([]float64, window),
		yBuf:   make([]float64, window),
	}
}

func (r *rollingOLS) add(x, y float64) {
	if r.count == r.window {
		oldX := r.xBuf[r.pos]
		oldY := r.yBuf[r.pos]
		r.sumX -= oldX
		r.sumY -= oldY
		r.sumXY -= oldX * oldY
		r.sumXX -= oldX * oldX
	} else {
		r.count++
	}
	r.xBuf[r.pos] = x
	r.yBuf[r.pos] = y
	r.sumX += x
	r.sumY += y
	r.sumXY += x * y
	r.sumXX += x * x
	r.pos = (r.pos + 1) % r.window
	if r.pos == 0 && r.count == r.window {
		r.rotations++
		if r.rotations%recomputeEveryRotations == 0 {
			r.recompute()
		}
	}
}

func (r *rollingOLS) recompute() {
	var sumX, sumY, sumXY, sumXX float64
	for i := 0; i < r.count; i++ {
		sumX += r.xBuf[i]
		sumY += r.yBuf[i]
		sumXY += r.xBuf[i] * r.yBuf[i]
		sumXX += r.xBuf[i] * r.xBuf[i]
	}
	r.sumX = sumX
	r.sumY = sumY
	r.sumXY = sumXY
	r.sumXX = sumXX
}

func (r *rollingOLS) ready() bool {
	return r.count == r.window
}

// beta returns raw Cov(X,Y)/Var(X).
func (r *rollingOLS) beta() float64 {
	if !r.ready() {
		return 1.0
	}
	n := float64(r.count)
	meanX := r.sumX / n
	meanY := r.sumY / n
	cov := r.sumXY/n - meanX*meanY
	varX := r.sumXX/n - meanX*meanX
	if varX == 0 {
		return 1.0
	}
	return cov / varX
}

// Engine computes rolling spread statistics and emits trading signals.
type Engine struct {
	cfg      *config.Config
	bookCh   <-chan types.BookUpdate
	signalCh chan<- types.SpreadSignal
	stats    *rollingStats
	ols      *rollingOLS
	zStats   *rollingStats // rolling z-score tracker for Feature 5 (nil when disabled)
	ctx      context.Context

	lastCLMid    float64
	lastBrentMid float64
	lastCLTs     time.Time
	lastBrentTs  time.Time
	hasCL        bool
	hasBrent     bool
	clDirty      bool
	brentDirty   bool
	lastSignal   types.SpreadSignal
	mu           sync.Mutex
}

// LatestSignal returns the most recent spread signal.
func (e *Engine) LatestSignal() types.SpreadSignal {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.lastSignal
}

// WarmUpProgress returns (current count, window size).
func (e *Engine) WarmUpProgress() (int, int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.stats.count, e.stats.window
}

// New creates a new spread Engine.
func New(cfg *config.Config, bookCh <-chan types.BookUpdate, signalCh chan<- types.SpreadSignal) *Engine {
	e := &Engine{
		cfg:      cfg,
		bookCh:   bookCh,
		signalCh: signalCh,
		stats:    newRollingStats(cfg.Strategy.LookbackWindow),
		ols:      newRollingOLS(cfg.Strategy.LookbackWindow),
	}
	if cfg.MarketMaking.AdaptiveZEnabled {
		e.zStats = newRollingStats(cfg.MarketMaking.ZCalibrationWindow)
	}
	return e
}

// zStdDevLocked returns the rolling standard deviation of z-scores,
// floored at 0.1 to prevent degenerate scaling. Returns 1.0 when not ready or disabled.
// Must be called with e.mu held.
func (e *Engine) zStdDevLocked() float64 {
	if e.zStats == nil || !e.zStats.ready() {
		return 1.0
	}
	sd := e.zStats.stddev()
	if sd < 0.1 {
		return 0.1
	}
	return sd
}

// Run processes book updates and emits spread signals. Blocks until ctx is cancelled.
func (e *Engine) Run(ctx context.Context) {
	e.ctx = ctx

	slog.Info("spread engine started",
		"window", e.cfg.Strategy.LookbackWindow,
		"spread_type", e.cfg.Strategy.SpreadType,
	)

	for {
		select {
		case <-ctx.Done():
			slog.Info("spread engine stopped")
			return
		case update := <-e.bookCh:
			e.handleBookUpdate(update)
		}
	}
}

func (e *Engine) handleBookUpdate(update types.BookUpdate) {
	e.mu.Lock()

	switch update.Coin {
	case e.cfg.Strategy.Assets[0]: // CL
		e.lastCLMid = update.Mid
		e.lastCLTs = update.Timestamp
		e.hasCL = true
		e.clDirty = true
	case e.cfg.Strategy.Assets[1]: // BRENTOIL
		e.lastBrentMid = update.Mid
		e.lastBrentTs = update.Timestamp
		e.hasBrent = true
		e.brentDirty = true
	default:
		e.mu.Unlock()
		return
	}

	// Only sample when both legs have received a fresh update since the last
	// successful sample. This stays symmetric without overweighting one leg when
	// only that leg updates repeatedly.
	if !e.hasCL || !e.hasBrent || !e.clDirty || !e.brentDirty {
		e.mu.Unlock()
		return
	}

	maxCrossLegSkewMs := e.cfg.MarketMaking.MaxCrossLegSkewMs
	if maxCrossLegSkewMs == 0 {
		maxCrossLegSkewMs = 150
	}
	maxSkew := time.Duration(maxCrossLegSkewMs) * time.Millisecond
	if maxSkew > 0 {
		skew := e.lastCLTs.Sub(e.lastBrentTs)
		if skew < 0 {
			skew = -skew
		}
		if skew > maxSkew {
			slog.Debug("spread engine skipping stale sample",
				"skew_ms", skew.Milliseconds(),
				"max_skew_ms", maxCrossLegSkewMs,
			)
			e.mu.Unlock()
			return
		}
	}
	e.clDirty = false
	e.brentDirty = false

	// Add to OLS tracker.
	e.ols.add(e.lastCLMid, e.lastBrentMid)

	beta := 1.0
	if e.cfg.Strategy.UseBetaHedge && e.ols.ready() {
		beta = e.ols.beta()
		if beta < e.cfg.Strategy.BetaMin {
			beta = e.cfg.Strategy.BetaMin
		}
		if beta > e.cfg.Strategy.BetaMax {
			beta = e.cfg.Strategy.BetaMax
		}
	}

	// Compute spread.
	var spread float64
	switch e.cfg.Strategy.SpreadType {
	case "ratio":
		if e.lastBrentMid == 0 {
			e.mu.Unlock()
			return
		}
		spread = e.lastCLMid / e.lastBrentMid
	default: // "difference"
		if e.cfg.Strategy.UseBetaHedge {
			spread = e.lastCLMid - beta*e.lastBrentMid
		} else {
			spread = e.lastCLMid - e.lastBrentMid
		}
	}

	// Compute z-score against current window BEFORE adding new value.
	// This uses the established distribution to score the incoming spread.
	var z float64
	windowReady := e.stats.ready()
	if windowReady {
		sd := e.stats.stddev()
		if floor := e.cfg.Strategy.ZMinStdDev; floor > 0 && sd < floor {
			sd = floor
		}
		if sd > 0 {
			z = (spread - e.stats.mean()) / sd
		}
	}

	// Track z-score distribution for Feature 5.
	if e.zStats != nil && windowReady {
		e.zStats.add(z)
	}

	// Add to rolling window.
	e.stats.add(spread)

	if !windowReady {
		slog.Debug("spread engine warming up",
			"count", e.stats.count,
			"window", e.stats.window,
		)
		e.mu.Unlock()
		return
	}

	// Skip signal when raw stddev is zero and z is zero (all values identical).
	rawSD := e.stats.stddev()
	if rawSD == 0 && z == 0 {
		e.mu.Unlock()
		return
	}
	m := e.stats.mean()
	sd := rawSD
	if floor := e.cfg.Strategy.ZMinStdDev; floor > 0 && sd < floor {
		sd = floor
	}

	sig := types.SpreadSignal{
		ZScore:    z,
		Spread:    spread,
		Mean:      m,
		StdDev:    sd,
		Beta:      beta,
		ZStdDev:   e.zStdDevLocked(),
		Timestamp: time.Now(),
	}

	slog.Debug("spread engine signal",
		"z", z,
		"spread", spread,
		"mean", m,
		"stddev", sd,
		"beta", beta,
	)

	e.lastSignal = sig
	e.mu.Unlock()

	// Send with a timeout: freshness matters more than completeness for spread
	// signals. A stale signal is worse than a dropped one.
	select {
	case e.signalCh <- sig:
	case <-time.After(1 * time.Second):
		slog.Error("spread engine: signal send timed out, dropping signal",
			"z", sig.ZScore,
		)
	case <-e.ctx.Done():
		return
	}
}
