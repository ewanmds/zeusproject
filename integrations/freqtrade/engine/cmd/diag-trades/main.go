// Diagnostic tool: connects to gRPC trade stream and dumps raw messages.
// Usage: go run ./cmd/diag-trades/ -config config.yaml
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"

	"hyperliquid-market-maker/internal/config"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

func main() {
	configPath := flag.String("config", "config.yaml", "path to config file")
	maxMessages := flag.Int("n", 5, "number of messages to print before exiting")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	fmt.Printf("Subscribing to trades for: %v\n", cfg.Strategy.Assets)
	fmt.Printf("Endpoint: %s (gRPC)\n\n", cfg.QuickNodeEndpoint)

	var count atomic.Int32
	max := int32(*maxMessages)

	done := make(chan struct{})
	var closed atomic.Bool

	grpcConfig := &hyperliquid.GRPCStreamConfig{
		Secure:       true,
		Reconnect:    true,
		MaxReconnect: 3,
		OnError: func(err error) {
			fmt.Fprintf(os.Stderr, "stream error: %v\n", err)
		},
		OnConnect: func() {
			fmt.Println("gRPC stream connected, waiting for trades...")
		},
	}

	stream := hyperliquid.NewGRPCStream(cfg.QuickNodeEndpoint, grpcConfig)
	stream.Trades(cfg.Strategy.Assets, func(data map[string]any) {
		n := count.Add(1)
		fmt.Printf("=== Trade %d ===\n", n)
		b, _ := json.MarshalIndent(data, "", "  ")
		fmt.Println(string(b))

		// Show key fields.
		coin, _ := data["coin"].(string)
		px, _ := data["px"].(string)
		sz, _ := data["sz"].(string)
		side, _ := data["side"].(string)
		fmt.Printf("  coin=%s px=%s sz=%s side=%s\n\n", coin, px, sz, side)

		if n >= max && closed.CompareAndSwap(false, true) {
			close(done)
		}
	})

	if err := stream.Start(); err != nil {
		log.Fatalf("stream start: %v", err)
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	select {
	case <-done:
		fmt.Println("received enough messages, exiting")
	case sig := <-sigCh:
		fmt.Printf("received %v, exiting\n", sig)
	}

	stream.Stop()
}
