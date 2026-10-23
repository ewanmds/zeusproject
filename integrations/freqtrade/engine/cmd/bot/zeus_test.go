package main

import (
	"context"
	"encoding/json"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestZeusStopBeforeFirstBook(t *testing.T) {
	path := filepath.Join(t.TempDir(), "stop.flag")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go watchZeusStop(ctx, cancel, path)
	if err := os.WriteFile(path, []byte("stop"), 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("stop was not honored")
	}
}
func TestZeusIndependentFlatVerification(t *testing.T) {
	for _, tc := range []struct {
		name, state, orders string
		fail                bool
	}{
		{"flat", `{"assetPositions":[]}`, `[]`, false},
		{"position", `{"assetPositions":[{"position":{"coin":"SP500","szi":"0.1"}}]}`, `[]`, true},
		{"invalid-size", `{"assetPositions":[{"position":{"coin":"SP500","szi":"bad"}}]}`, `[]`, true},
		{"order", `{"assetPositions":[]}`, `[{"coin":"xyz:SP500","oid":1}]`, true},
		{"malformed", `{}`, `[]`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/info" {
					t.Error("verification attempted exchange mutation")
					http.Error(w, "forbidden", 403)
					return
				}
				var payload map[string]any
				_ = json.NewDecoder(r.Body).Decode(&payload)
				w.Header().Set("Content-Type", "application/json")
				switch payload["type"] {
				case "clearinghouseState":
					w.Write([]byte(tc.state))
				case "openOrders":
					w.Write([]byte(tc.orders))
				default:
					http.Error(w, "bad query", 400)
				}
			}))
			defer server.Close()
			sdk, err := hyperliquid.New(server.URL, hyperliquid.WithAccountAddress("0x0000000000000000000000000000000000000001"))
			if err != nil {
				t.Fatal(err)
			}
			defer sdk.Close()
			err = verifyZeusFlat(sdk, "xyz:SP500")
			if (err != nil) != tc.fail {
				t.Fatalf("unexpected verification: %v", err)
			}
		})
	}
}
