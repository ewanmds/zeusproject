package main

import (
	"context"
	"fmt"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/execution"
	"hyperliquid-market-maker/internal/quoting"
	"hyperliquid-market-maker/internal/zeusbridge"
	"math"
	"os"
	"strconv"
	"strings"
	"time"
)

func watchZeusStop(ctx context.Context, cancel context.CancelFunc, path string) {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := os.Stat(path); err == nil {
				cancel()
				return
			}
		}
	}
}
func emitZeusState(ctx context.Context, s *zeusbridge.State, exec *execution.LiveExecutor, manager *quoting.Manager, coin string) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			book := exec.ZeusBook(coin)
			pos := exec.Position(coin)
			health := exec.HealthSnapshot()
			qty := pos.Size
			if pos.Side == "short" {
				qty = -qty
			}
			fresh := book.Mid > 0 && time.Since(book.Timestamp) < 10*time.Second
			known := health.FillPollConsecutiveErrors == 0 && !health.Coins[coin].QuoteHalt
			phase := "connecting"
			action := "FREQTRADE | attente carnet valide"
			if fresh {
				phase = "running"
				action = "FREQTRADE | moteur actif"
				// L'erreur vient du dernier log ERROR et reste collée. Dès que le
				// moteur peut requoter normalement, on efface l'alerte périmée ;
				// en halt réel, blocked reste vrai et le message est conservé.
				if _, blocked := manager.ZeusQuoteStatus(coin); !blocked && !health.Coins[coin].QuoteHalt {
					s.Update(map[string]any{"error": ""})
				}
			}
			if h := health.Coins[coin]; h.QuoteHalt {
				action = "FREQTRADE | execution suspendue: " + h.Reason
			}
			spread := 0.0
			if book.Mid > 0 {
				spread = (book.BestAsk - book.BestBid) / book.Mid * 10000
			}
			// Measured quality: per-side markout and the edge gate's view of the
			// entry side. The entry side increases inventory; when flat both
			// sides do, so report the tighter one.
			markout := manager.MarkoutSnapshot(coin)
			diag := manager.Diagnostics()[coin]
			quoteHalf, required := 0.0, 0.0
			switch pos.Side {
			case "long":
				quoteHalf, required = diag.QuoteHalfBidBps, diag.EdgeRequiredBidBps
			case "short":
				quoteHalf, required = diag.QuoteHalfAskBps, diag.EdgeRequiredAskBps
			default:
				quoteHalf = math.Min(diag.QuoteHalfBidBps, diag.QuoteHalfAskBps)
				required = math.Max(diag.EdgeRequiredBidBps, diag.EdgeRequiredAskBps)
			}
			margin := quoteHalf - required
			s.Update(map[string]any{"phase": phase, "running": true, "position_known": known, "position": qty, "position_usd": qty * book.Mid, "entry": pos.AvgEntry, "mid": book.Mid, "spread_bps": spread, "upnl": qty * (book.Mid - pos.AvgEntry), "action": action,
				"markout_bid_bps": markout.BuyComposite, "markout_ask_bps": markout.SellComposite,
				"quote_half_spread_bps": quoteHalf, "edge_required_bps": required, "edge_margin_bps": margin,
				"measured_fee_bps": diag.MeasuredFeeBps})
			s.Emit()
		}
	}
}

// Verify independently after shutdown. Cached empty positions are never proof.
func verifyZeusFlat(sdk *hyperliquid.SDK, coin string) error {
	dex, bare := "", coin
	if parts := strings.SplitN(coin, ":", 2); len(parts) == 2 {
		dex, bare = parts[0], parts[1]
	}
	state, err := sdk.Info().ClearinghouseState(sdk.Address(), hyperliquid.WithDex(dex))
	if err != nil {
		return fmt.Errorf("position non confirmee")
	}
	rows, ok := state["assetPositions"].([]any)
	if !ok {
		return fmt.Errorf("reponse position invalide")
	}
	for _, row := range rows {
		m, ok := row.(map[string]any)
		if !ok {
			return fmt.Errorf("reponse position invalide")
		}
		p, ok := m["position"].(map[string]any)
		if !ok {
			return fmt.Errorf("reponse position invalide")
		}
		if name, valid := p["coin"].(string); !valid || name == "" {
			return fmt.Errorf("symbole de position invalide")
		}
		if p["coin"] == coin || p["coin"] == bare {
			size, parseErr := strconv.ParseFloat(fmt.Sprint(p["szi"]), 64)
			if parseErr != nil || math.IsNaN(size) || math.IsInf(size, 0) {
				return fmt.Errorf("taille de position invalide")
			}
			if size != 0 {
				return fmt.Errorf("position residuelle")
			}
		}
	}
	orders, err := sdk.Info().OpenOrders(sdk.Address(), hyperliquid.WithDex(dex))
	if err != nil {
		return fmt.Errorf("ordres non confirmes")
	}
	for _, raw := range orders {
		order, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("reponse ordres invalide")
		}
		if name, valid := order["coin"].(string); !valid || name == "" {
			return fmt.Errorf("symbole d'ordre invalide")
		}
		if order["coin"] == coin || order["coin"] == bare {
			return fmt.Errorf("ordres residuels")
		}
	}
	return nil
}

func recoverZeusClosingFills(sdk *hyperliquid.SDK, s *zeusbridge.State, start int64) error {
	if start < s.StartMillis() {
		start = s.StartMillis()
	}
	rows, err := sdk.Info().UserFillsByTime(sdk.Address(), start, nil)
	if err != nil {
		return err
	}
	for _, row := range rows {
		raw, ok := row.(map[string]any)
		if !ok {
			return fmt.Errorf("invalid fill response")
		}
		zeusbridge.RecordFill(raw)
	}
	if len(rows) >= 2000 {
		return fmt.Errorf("fill response truncated")
	}
	return nil
}
