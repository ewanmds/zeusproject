package risk

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/types"
)

// SessionPnLReader is the minimal interface the risk manager needs to observe
// session PnL for the stop-loss subsystem. *quoting.SessionPnLTracker satisfies
// this interface; passing nil disables the session stop-loss check.
type SessionPnLReader interface {
	PnL() (absUSDC float64, bps float64)
	ElapsedSec() float64
}

// Manager monitors positions and equity, emitting risk actions when limits are breached.
type Manager struct {
	cfg        *config.Config
	posCh      <-chan types.PositionUpdate
	riskCh     chan<- types.RiskAction
	equityFn   func() float64
	sessionPnL SessionPnLReader
	ctx        context.Context

	positions        map[string]types.PositionUpdate
	highWater        float64
	lastAction       types.RiskAction
	drawdownHalted   bool      // true when halted due to drawdown breach
	drawdownHaltedAt time.Time // when drawdown halt was triggered
	haltCount        int       // consecutive drawdown halts without a successful run
	resumedAt        time.Time // when last resume was emitted (for successful run detection)
	permanentHalt    bool      // true when permanently halted (equity floor or max consecutive halts)

	sessionStopHalted   bool      // true when halted due to session stop-loss breach
	sessionStopHaltedAt time.Time // when session stop-loss halt was triggered

	equityEMA            float64 // EMA-smoothed equity for drawdown checks
	equityEMAInitialized bool

	floorBreachCount int // consecutive smoothed-equity readings below absolute floor

	mu sync.Mutex
}

// New creates a new risk Manager. sessionPnL may be nil (disables session stop-loss).
func New(
	cfg *config.Config,
	posCh <-chan types.PositionUpdate,
	riskCh chan<- types.RiskAction,
	equityFn func() float64,
	sessionPnL SessionPnLReader,
) *Manager {
	return &Manager{
		cfg:        cfg,
		posCh:      posCh,
		riskCh:     riskCh,
		equityFn:   equityFn,
		sessionPnL: sessionPnL,
		positions:  make(map[string]types.PositionUpdate),
		lastAction: types.RiskResume,
	}
}

// HighWater returns the current high-water mark (smoothed equity).
func (m *Manager) HighWater() float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.highWater
}

// SeedPositions loads initial positions before the event loop starts.
// Call this after executor Init() to ensure the risk manager knows about
// existing on-chain positions from a previous session.
func (m *Manager) SeedPositions(positions []types.PositionUpdate) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, pos := range positions {
		m.positions[pos.Coin] = pos
	}
}

// Run processes position updates and periodically checks equity.
// Blocks until ctx is cancelled.
func (m *Manager) Run(ctx context.Context) {
	m.ctx = ctx

	for _, asset := range m.cfg.Strategy.Assets {
		ac := m.cfg.ForAsset(asset)
		slog.Info("risk manager started",
			"coin", asset,
			"max_position_notional", ac.Risk.MaxPositionNotional,
			"max_spread_exposure", ac.Risk.MaxSpreadExposure,
		)
	}
	aggLimit := m.cfg.Risk.MaxAggregateExposure
	if aggLimit <= 0 {
		aggLimit = m.cfg.Risk.MaxSpreadExposure
	}
	slog.Info("risk manager global limits",
		"max_aggregate_exposure", aggLimit,
		"max_drawdown_pct", m.cfg.Risk.MaxDrawdownPct,
	)

	// Record initial high-water mark.
	m.highWater = m.equityFn()

	equityTicker := time.NewTicker(5 * time.Second)
	defer equityTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("risk manager stopped")
			return

		case pos := <-m.posCh:
			m.mu.Lock()
			m.positions[pos.Coin] = pos
			m.mu.Unlock()

			m.evaluate()

		case <-equityTicker.C:
			m.evaluate()
		}
	}
}

func (m *Manager) evaluate() {
	m.mu.Lock()
	defer m.mu.Unlock()

	equity := m.equityFn()

	// Compute EMA-smoothed equity for drawdown checks, floor, and high-water mark.
	smoothedEquity := equity
	if samples := m.cfg.Risk.DrawdownSmoothingSamples; samples > 1 {
		alpha := 2.0 / (float64(samples) + 1.0)
		if !m.equityEMAInitialized {
			m.equityEMA = equity
			m.equityEMAInitialized = true
		} else {
			m.equityEMA = alpha*equity + (1.0-alpha)*m.equityEMA
		}
		smoothedEquity = m.equityEMA
	}

	// Absolute equity floor: permanent halt if smoothed equity drops below floor
	// for 3 consecutive readings (confirmation window filters phantom spikes).
	if floor := m.cfg.Risk.AbsoluteEquityFloor; floor > 0 {
		if smoothedEquity < floor {
			m.floorBreachCount++
			if m.floorBreachCount >= 3 {
				m.permanentHalt = true
				slog.Error("equity below absolute floor, permanent halt",
					"smoothed_equity", smoothedEquity,
					"raw_equity", equity,
					"floor", floor,
					"consecutive_breaches", m.floorBreachCount,
				)
				m.emitActionForce(types.RiskHalt)
				return
			}
			slog.Warn("equity below floor, awaiting confirmation",
				"smoothed_equity", smoothedEquity,
				"raw_equity", equity,
				"floor", floor,
				"breach_count", m.floorBreachCount,
			)
		} else {
			if m.floorBreachCount > 0 {
				slog.Info("equity recovered above floor, resetting breach count",
					"smoothed_equity", smoothedEquity,
					"floor", floor,
					"previous_breach_count", m.floorBreachCount,
				)
			}
			m.floorBreachCount = 0
		}
	}
	if m.permanentHalt {
		m.emitActionForce(types.RiskHalt)
		return
	}

	// P3: Session stop-loss. When session PnL breaches the configured floor,
	// cancel all + close positions, then pause for SessionStopLossPauseSec
	// seconds before emitting RiskResumeFromSessionStop (recovery mode).
	if m.cfg.Risk.SessionStopLossBps < 0 && m.sessionPnL != nil {
		if m.sessionStopHalted {
			pause := time.Duration(m.cfg.Risk.SessionStopLossPauseSec) * time.Second
			if pause > 0 && time.Since(m.sessionStopHaltedAt) >= pause {
				slog.Info("session stop-loss pause expired",
					"pause_sec", m.cfg.Risk.SessionStopLossPauseSec,
					"recovery_duration_sec", m.cfg.Risk.SessionStopLossRecoveryDurationSec,
					"recovery_notional_pct", m.cfg.Risk.SessionStopLossRecoveryNotionalPct,
				)
				m.sessionStopHalted = false
				m.resumedAt = time.Now()
				m.emitAction(types.RiskResumeFromSessionStop)
				return
			}
			// Still in pause: keep the halt asserted.
			m.emitActionForce(types.RiskHalt)
			return
		}
		_, bps := m.sessionPnL.PnL()
		if bps <= m.cfg.Risk.SessionStopLossBps {
			slog.Error("session stop-loss triggered",
				"session_pnl_bps", bps,
				"elapsed_sec", m.sessionPnL.ElapsedSec(),
				"equity", equity,
				"floor_bps", m.cfg.Risk.SessionStopLossBps,
			)
			m.sessionStopHalted = true
			m.sessionStopHaltedAt = time.Now()
			m.emitAction(types.RiskSessionStopLoss)
			return
		}
	}

	// Check per-leg position notional (only for configured assets).
	managedAssets := make(map[string]bool, len(m.cfg.Strategy.Assets))
	for _, a := range m.cfg.Strategy.Assets {
		managedAssets[a] = true
	}

	maxNotional := 0.0
	totalNotional := 0.0
	allFlat := true

	for _, pos := range m.positions {
		if !managedAssets[pos.Coin] {
			continue
		}
		if pos.Side != "" && pos.Size > 0 {
			allFlat = false
		}
		if pos.Notional > maxNotional {
			maxNotional = pos.Notional
		}
		totalNotional += pos.Notional
	}

	// If we were in a HALT state and positions are now flat, handle resume.
	// This must run before the drawdown check so that the sticky halt/cooldown
	// logic executes instead of re-triggering halt.
	if m.lastAction == types.RiskHalt && allFlat {
		if m.drawdownHalted {
			// Drawdown-triggered flatten: stay halted or wait for cooldown.
			cooldown := time.Duration(m.cfg.Risk.DrawdownCooldownSec) * time.Second
			if cooldown > 0 && time.Since(m.drawdownHaltedAt) >= cooldown {
				// Check equity threshold: equity must be >= highWater * MinResumeEquityPct/100.
				if m.cfg.Risk.MinResumeEquityPct > 0 {
					minEquity := m.highWater * m.cfg.Risk.MinResumeEquityPct / 100.0
					if equity < minEquity {
						slog.Warn("equity below resume threshold, extending halt",
							"equity", equity,
							"min_equity", minEquity,
							"high_water", m.highWater,
							"min_resume_equity_pct", m.cfg.Risk.MinResumeEquityPct,
						)
						m.haltCount++
						m.drawdownHaltedAt = time.Now() // extend cooldown
						m.emitActionForce(types.RiskHalt)
						return
					}
				}

				// Check consecutive halt limit.
				if m.cfg.Risk.MaxConsecutiveHalts > 0 && m.haltCount >= m.cfg.Risk.MaxConsecutiveHalts {
					m.permanentHalt = true
					slog.Error("max consecutive halts reached, permanent halt",
						"halt_count", m.haltCount,
						"max_consecutive_halts", m.cfg.Risk.MaxConsecutiveHalts,
					)
					m.emitActionForce(types.RiskHalt)
					return
				}

				slog.Info("drawdown cooldown expired, resuming",
					"cooldown_sec", m.cfg.Risk.DrawdownCooldownSec,
					"halt_count", m.haltCount,
				)
				m.drawdownHalted = false
				m.highWater = equity // reset high-water to current equity
				m.resumedAt = time.Now()
				m.emitAction(types.RiskResumeFromDrawdown)
				return
			}
			// Still in cooldown or cooldown==0 (stay halted forever).
			m.emitActionForce(types.RiskHalt)
			return
		}
		// Non-drawdown halt (e.g. position-limit): auto-resume immediately.
		slog.Info("positions cleared, resuming")
		m.emitAction(types.RiskResume)
		return
	}

	// Update high-water mark using smoothed equity to prevent phantom spikes
	// from inflating the drawdown threshold.
	if smoothedEquity > m.highWater {
		m.highWater = smoothedEquity
		// Equity exceeded previous peak — the bot has fully recovered.
		if m.haltCount > 0 {
			slog.Info("equity exceeded high-water mark, resetting halt count",
				"smoothed_equity", smoothedEquity,
				"previous_halt_count", m.haltCount,
			)
			m.haltCount = 0
		}
	}

	// Check equity drawdown (using smoothed equity to filter transient MTM spikes).
	if m.highWater > 0 {
		drawdownPct := (m.highWater - smoothedEquity) / m.highWater * 100
		if drawdownPct > m.cfg.Risk.MaxDrawdownPct {
			// If there was a successful run since last resume, reset haltCount.
			cooldown := time.Duration(m.cfg.Risk.DrawdownCooldownSec) * time.Second
			if !m.resumedAt.IsZero() && cooldown > 0 && time.Since(m.resumedAt) >= cooldown {
				m.haltCount = 0
			}
			m.haltCount++

			slog.Error("max drawdown exceeded",
				"drawdown_pct", drawdownPct,
				"high_water", m.highWater,
				"equity", equity,
				"halt_count", m.haltCount,
			)
			m.drawdownHalted = true
			m.drawdownHaltedAt = time.Now()
			m.emitAction(types.RiskHalt)
			return
		}
	}

	// Check per-asset position limits.
	for _, pos := range m.positions {
		if !managedAssets[pos.Coin] {
			continue
		}
		ac := m.cfg.ForAsset(pos.Coin)
		haltCap := positionHaltCap(ac.Risk.MaxPositionNotional, ac.Risk.MaxPositionHaltTolerancePct)
		if pos.Notional >= haltCap {
			slog.Warn("position notional at/above limit",
				"coin", pos.Coin,
				"notional", pos.Notional,
				"limit", haltCap,
				"configured_limit", ac.Risk.MaxPositionNotional,
				"tolerance_pct", ac.Risk.MaxPositionHaltTolerancePct,
			)
			m.emitActionForce(types.RiskHalt)
			return
		}
	}

	// Check per-asset spread exposure, plus aggregate safety net.
	for _, pos := range m.positions {
		if !managedAssets[pos.Coin] {
			continue
		}
		ac := m.cfg.ForAsset(pos.Coin)
		haltCap := positionHaltCap(ac.Risk.MaxSpreadExposure, ac.Risk.MaxPositionHaltTolerancePct)
		if pos.Notional >= haltCap {
			slog.Warn("spread exposure at/above per-asset limit",
				"coin", pos.Coin,
				"notional", pos.Notional,
				"limit", haltCap,
				"configured_limit", ac.Risk.MaxSpreadExposure,
			)
			m.emitActionForce(types.RiskHalt)
			return
		}
	}
	// Aggregate safety net: total portfolio exposure capped by max_aggregate_exposure
	// (falls back to global max_spread_exposure when max_aggregate_exposure is 0).
	aggLimit := m.cfg.Risk.MaxAggregateExposure
	if aggLimit <= 0 {
		aggLimit = m.cfg.Risk.MaxSpreadExposure
	}
	if totalNotional >= aggLimit {
		slog.Warn("aggregate spread exposure at/above limit",
			"total_notional", totalNotional,
			"limit", aggLimit,
		)
		m.emitActionForce(types.RiskHalt)
		return
	}

	// If no limits are breached and we're not already in RESUME, resume.
	if m.lastAction != types.RiskResume {
		m.emitAction(types.RiskResume)
	}
}

// positionHaltCap applies the configured tolerance to a notional limit. A zero
// tolerance keeps the historical behaviour (halt exactly at the limit).
func positionHaltCap(limit, tolerancePct float64) float64 {
	if limit <= 0 {
		return limit
	}
	if tolerancePct <= 0 {
		return limit
	}
	return limit * (1.0 + tolerancePct/100.0)
}

func (m *Manager) emitAction(action types.RiskAction) {
	// Only emit if action changed, to avoid flooding the channel.
	if action == m.lastAction {
		return
	}

	slog.Info("risk action", "action", action, "previous", m.lastAction)
	m.lastAction = action
	select {
	case m.riskCh <- action:
	default:
		slog.Error("dropping risk action, channel full", "action", action)
	}
}

// emitActionForce bypasses deduplication for critical signals that must always
// reach the quote manager (e.g. permanent halt, equity threshold rejection).
// Retries with a timeout rather than silently dropping the action.
func (m *Manager) emitActionForce(action types.RiskAction) {
	slog.Info("risk action (forced)", "action", action, "previous", m.lastAction)
	m.lastAction = action

	// Fast path: try non-blocking send.
	select {
	case m.riskCh <- action:
		return
	default:
	}

	// Channel full: retry with a timeout to give the consumer time to drain.
	slog.Warn("forced risk action channel full, retrying with timeout", "action", action)
	select {
	case m.riskCh <- action:
	case <-time.After(1 * time.Second):
		slog.Error("CRITICAL: forced risk action could not send after 1s timeout",
			"action", action)
	case <-m.ctx.Done():
	}
}
