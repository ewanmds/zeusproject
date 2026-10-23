package quoting

import (
	"hyperliquid-market-maker/internal/types"
	"math"
)

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
