// Diagnostic tool: queries clearinghouse state and dumps the raw response.
// Usage: go run ./cmd/diag/ -config config.yaml
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"

	"hyperliquid-market-maker/internal/config"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

func main() {
	configPath := flag.String("config", "config.yaml", "path to config file")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	var sdk *hyperliquid.SDK
	if cfg.PrivateKey != "" {
		sdk, err = hyperliquid.New(cfg.QuickNodeEndpoint,
			hyperliquid.WithPrivateKey(cfg.PrivateKey),
			hyperliquid.WithAutoApprove(true),
		)
	} else {
		sdk, err = hyperliquid.New(cfg.QuickNodeEndpoint)
	}
	if err != nil {
		log.Fatalf("sdk: %v", err)
	}

	addr := sdk.Address()
	fmt.Printf("Address: %s\n\n", addr)

	// Query perps clearinghouse state.
	fmt.Println("=== Perps Clearinghouse State ===")
	perpsState, err := sdk.Info().ClearinghouseState(addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "perps error: %v\n", err)
	} else {
		b, _ := json.MarshalIndent(perpsState, "", "  ")
		fmt.Println(string(b))
	}

	// Query HIP-3 dex clearinghouse state.
	fmt.Println("\n=== HIP-3 (xyz) Clearinghouse State ===")
	dexState, err := sdk.Info().ClearinghouseState(addr, hyperliquid.WithDex("xyz"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "dex error: %v\n", err)
	} else {
		b, _ := json.MarshalIndent(dexState, "", "  ")
		fmt.Println(string(b))
	}

	// Query HIP-3 open orders.
	fmt.Println("\n=== HIP-3 (xyz) Open Orders ===")
	openOrders, err := sdk.Info().OpenOrders(addr, hyperliquid.WithDex("xyz"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "open orders error: %v\n", err)
	} else {
		fmt.Printf("Count: %d\n", len(openOrders))
		b, _ := json.MarshalIndent(openOrders, "", "  ")
		fmt.Println(string(b))
	}

	// Query user rate limit.
	fmt.Println("\n=== User Rate Limit ===")
	rateLimit, err := sdk.Info().UserRateLimit(addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "rate limit error: %v\n", err)
	} else {
		b, _ := json.MarshalIndent(rateLimit, "", "  ")
		fmt.Println(string(b))
	}

	// Also try SpotClearinghouseState in case USDC is on spot.
	fmt.Println("\n=== Spot Clearinghouse State ===")
	spotState, err := sdk.Info().SpotClearinghouseState(addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "spot error: %v\n", err)
	} else {
		b, _ := json.MarshalIndent(spotState, "", "  ")
		fmt.Println(string(b))
	}
}
