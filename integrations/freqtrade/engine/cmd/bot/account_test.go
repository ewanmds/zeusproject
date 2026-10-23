package main

import (
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"
	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
	"hyperliquid-market-maker/internal/config"
)

func TestAgentBelongsToTradingAccount(t *testing.T) {
	account := "0x1111111111111111111111111111111111111111"
	signer := "0x2222222222222222222222222222222222222222"
	for _, tc := range []struct {
		name            string
		account, signer string
		role            map[string]any
		ok              bool
	}{
		{"authorized", account, signer, map[string]any{"role": "agent", "data": map[string]any{"user": account}}, true},
		{"wrong owner", account, signer, map[string]any{"role": "agent", "data": map[string]any{"user": signer}}, false},
		{"master key", account, account, map[string]any{"role": "user"}, false},
		{"expired or missing", account, signer, map[string]any{"role": "missing"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateAgentRole(tc.account, tc.signer, tc.role)
			if (err == nil) != tc.ok {
				t.Fatalf("unexpected result: %v", err)
			}
		})
	}
}

func TestAccountPreflightOnlyUsesInfo(t *testing.T) {
	account := "0x1111111111111111111111111111111111111111"
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	signer := crypto.PubkeyToAddress(key.PublicKey).Hex()
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/info" {
			t.Error("preflight attempted a non-info operation")
			http.Error(w, "blocked", 403)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		calls++
		w.Header().Set("Content-Type", "application/json")
		switch body["type"] {
		case "userRole":
			if body["user"] != signer {
				t.Error("role must check signer")
			}
			json.NewEncoder(w).Encode(map[string]any{"role": "agent", "data": map[string]string{"user": account}})
		case "clearinghouseState":
			if body["user"] != account || body["dex"] != "xyz" {
				t.Error("balance must query main account and DEX")
			}
			w.Write([]byte(`{"marginSummary":{"accountValue":"150"}}`))
		case "openOrders":
			if body["user"] != account || body["dex"] != "xyz" {
				t.Error("orders must query main account and DEX")
			}
			w.Write([]byte(`[]`))
		default:
			t.Error("unexpected preflight query")
			http.Error(w, "blocked", 403)
		}
	}))
	defer server.Close()
	sdk, err := hyperliquid.New(server.URL, hyperliquid.WithPrivateKey(hex.EncodeToString(crypto.FromECDSA(key))), hyperliquid.WithAccountAddress(account), hyperliquid.WithAutoApprove(false))
	if err != nil {
		t.Fatal(err)
	}
	defer sdk.Close()
	cfg := &config.Config{WalletAddress: account, Strategy: config.StrategyConfig{Assets: []string{"xyz:CL"}}}
	if err := checkTradingAccount(sdk, cfg); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatalf("expected three read-only queries, got %d", calls)
	}
}
