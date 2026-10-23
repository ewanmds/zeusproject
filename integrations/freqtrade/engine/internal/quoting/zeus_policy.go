package quoting

import (
	"fmt"
	"hyperliquid-market-maker/internal/types"
	"math"
)

// Hyperliquid's nRequestsCap is lifetime cumulative credit, not the size of
// a reusable request bucket. In Zeus, measure percentage depletion against
// the session's initial available credit. Keep absolute and TTE guards intact.
// Caller holds m.mu. Standalone percentage semantics remain unchanged.
func (m *Manager) rateLimitRemainingPctLocked(remaining int) float64 {
	denominator := m.rateLimitCap
	if m.cfg.ZeusMode && m.rlBootSeeded {
		denominator = max(m.rlBootRemaining, m.cfg.RateLimit.AbsoluteFloor)
	}
	if denominator <= 0 {
		return 0
	}
	return float64(remaining) / float64(denominator) * 100
}

// ZeusQuoteStatus reports actual quoting gates, rather than equating a fresh
// book with active orders. No account identifiers or strategy parameters leave.
func (m *Manager) ZeusQuoteStatus(coin string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.supervisor.IsRLCritical() {
		return fmt.Sprintf("budget API insuffisant (%d requetes restantes)", m.rateLimitRemaining), true
	}
	if !m.supervisor.CanQuote() {
		return "quotes suspendues : " + m.supervisor.State().String(), true
	}
	if m.executor.Equity() <= 0 {
		return "fonds disponibles non confirmes", true
	}
	m.diagMu.RLock()
	d := m.diagSnaps[coin]
	m.diagMu.RUnlock()
	if d.HealthHalted {
		return "quotes suspendues : " + d.HealthReason, true
	}
	if n := len(m.executor.OpenOrders(coin)); n > 0 {
		return fmt.Sprintf("%d quote(s) active(s)", n), false
	}
	if !d.Timestamp.IsZero() && d.AllowedBuyNotional == 0 && d.AllowedSellNotional == 0 {
		return "attente capacite inventaire / montant executable", true
	}
	return "preparation des ordres", false
}

// Cap at each quote's actual price, not entry price or book mid.
func capZeusOrders(orders []types.OrderRequest, pos types.PositionUpdate, capUSD, pow10 float64) []types.OrderRequest {
	out := make([]types.OrderRequest, 0, len(orders))
	remaining := map[string]float64{"buy": capUSD, "sell": capUSD}
	for _, order := range orders {
		if order.Price <= 0 || order.Size <= 0 {
			continue
		}
		reducing := (pos.Side == "long" && order.Side == "sell") || (pos.Side == "short" && order.Side == "buy")
		available := remaining[order.Side] / order.Price
		if reducing {
			available = pos.Size
			order.ReduceOnly = true
		} else {
			available -= pos.Size
		}
		order.Size = math.Floor(math.Max(0, math.Min(order.Size, available))*pow10+1e-9) / pow10
		if order.Size <= 0 || (!reducing && order.Price*order.Size < minOrderNotional) {
			continue
		}
		remaining[order.Side] -= order.Size * order.Price
		out = append(out, order)
	}
	return out
}
