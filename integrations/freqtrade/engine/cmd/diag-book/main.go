// Diagnostic tool: streams L2Book for configured assets and prints book depth.
// Usage: go run ./cmd/diag-book/ -config config.cl.yaml
package main

import (
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"hyperliquid-market-maker/internal/config"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

func main() {
	configPath := flag.String("config", "config.cl.yaml", "path to config file")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	fmt.Printf("Subscribing to L2Book for: %v\n", cfg.Strategy.Assets)
	fmt.Printf("Endpoint: %s\n\n", cfg.QuickNodeEndpoint)

	var mu sync.Mutex
	seen := make(map[string]bool)
	total := len(cfg.Strategy.Assets)

	done := make(chan struct{})

	grpcConfig := &hyperliquid.GRPCStreamConfig{
		Secure:       true,
		Reconnect:    true,
		MaxReconnect: 3,
		OnError: func(err error) {
			fmt.Fprintf(os.Stderr, "gRPC error: %v\n", err)
		},
		OnConnect: func() {
			fmt.Println("gRPC stream connected, waiting for book updates...")
		},
	}

	stream := hyperliquid.NewGRPCStream(cfg.QuickNodeEndpoint, grpcConfig)

	for _, coin := range cfg.Strategy.Assets {
		c := coin
		stream.L2Book(c, func(data map[string]any) {
			mu.Lock()
			if seen[c] {
				mu.Unlock()
				return
			}
			seen[c] = true
			mu.Unlock()

			printBook(c, data)

			mu.Lock()
			if len(seen) >= total {
				mu.Unlock()
				select {
				case <-done:
				default:
					close(done)
				}
				return
			}
			mu.Unlock()
		}, hyperliquid.L2BookNLevels(10))
	}

	if err := stream.Start(); err != nil {
		log.Fatalf("gRPC start: %v", err)
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Wait up to 15 seconds.
	timer := time.NewTimer(15 * time.Second)
	select {
	case <-done:
		fmt.Println("\nGot books for all assets.")
	case <-timer.C:
		fmt.Println("\nTimeout waiting for all assets.")
	case sig := <-sigCh:
		fmt.Printf("\nReceived %v, exiting.\n", sig)
	}
	stream.Stop()
}

func printBook(coin string, data map[string]any) {
	bids, asks := parseLevels(data)

	fmt.Printf("=== %s L2 Book ===\n", coin)
	fmt.Printf("Time: %s\n\n", time.Now().Format(time.RFC3339))

	if len(bids) == 0 && len(asks) == 0 {
		fmt.Println("  (empty book)")
		fmt.Println()
		return
	}

	// Print asks in reverse (highest first at top).
	fmt.Println("  ASKS (sell side):")
	for i := len(asks) - 1; i >= 0; i-- {
		fmt.Printf("    %10.2f  x  %10.3f  ($%.2f)\n", asks[i].px, asks[i].sz, asks[i].px*asks[i].sz)
	}

	if len(bids) > 0 && len(asks) > 0 {
		spread := asks[0].px - bids[0].px
		mid := (asks[0].px + bids[0].px) / 2
		spreadBps := (spread / mid) * 10000
		fmt.Printf("  --- SPREAD: $%.2f (%.1f bps) | Mid: $%.3f ---\n", spread, spreadBps, mid)
	}

	fmt.Println("  BIDS (buy side):")
	for i := 0; i < len(bids); i++ {
		fmt.Printf("    %10.2f  x  %10.3f  ($%.2f)\n", bids[i].px, bids[i].sz, bids[i].px*bids[i].sz)
	}

	// Totals.
	var bidTotal, askTotal float64
	for _, b := range bids {
		bidTotal += b.px * b.sz
	}
	for _, a := range asks {
		askTotal += a.px * a.sz
	}
	fmt.Printf("\n  Bid depth (10 levels): $%.2f | Ask depth (10 levels): $%.2f\n", bidTotal, askTotal)
	if bidTotal+askTotal > 0 {
		imbalance := (bidTotal - askTotal) / (bidTotal + askTotal)
		fmt.Printf("  Imbalance: %.1f%% (%s heavy)\n", math.Abs(imbalance)*100, func() string {
			if imbalance > 0 {
				return "bid"
			}
			return "ask"
		}())
	}
	fmt.Println()
}

type level struct {
	px, sz float64
}

func parseLevels(data map[string]any) (bids, asks []level) {
	// Try [][]any first
	if b, ok := data["bids"].([][]any); ok {
		a, _ := data["asks"].([][]any)
		bids = convertSliceSlice(b)
		asks = convertSliceSlice(a)
		return
	}
	// Try []any
	if b, ok := data["bids"].([]any); ok {
		a, _ := data["asks"].([]any)
		bids = convertAnySlice(b)
		asks = convertAnySlice(a)
		return
	}
	// Try levels
	if levels, ok := data["levels"].([]any); ok && len(levels) >= 2 {
		bidMaps, _ := levels[0].([]any)
		askMaps, _ := levels[1].([]any)
		bids = convertMapLevels(bidMaps)
		asks = convertMapLevels(askMaps)
	}
	return
}

func convertSliceSlice(levels [][]any) []level {
	out := make([]level, 0, len(levels))
	for _, l := range levels {
		if len(l) < 2 {
			continue
		}
		px := toFloat(l[0])
		sz := toFloat(l[1])
		if px > 0 {
			out = append(out, level{px, sz})
		}
	}
	return out
}

func convertAnySlice(levels []any) []level {
	out := make([]level, 0, len(levels))
	for _, raw := range levels {
		l, ok := raw.([]any)
		if !ok || len(l) < 2 {
			continue
		}
		px := toFloat(l[0])
		sz := toFloat(l[1])
		if px > 0 {
			out = append(out, level{px, sz})
		}
	}
	return out
}

func convertMapLevels(levels []any) []level {
	out := make([]level, 0, len(levels))
	for _, raw := range levels {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		px := toFloat(m["px"])
		sz := toFloat(m["sz"])
		if px > 0 {
			out = append(out, level{px, sz})
		}
	}
	return out
}

func toFloat(v any) float64 {
	switch val := v.(type) {
	case string:
		f, _ := strconv.ParseFloat(val, 64)
		return f
	case float64:
		return val
	case float32:
		return float64(val)
	case int:
		return float64(val)
	}
	return 0
}
