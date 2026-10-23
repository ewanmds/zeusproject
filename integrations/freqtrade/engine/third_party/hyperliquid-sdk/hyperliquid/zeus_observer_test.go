package hyperliquid

import (
	"encoding/hex"
	"encoding/json"
	"github.com/ethereum/go-ethereum/crypto"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNativeObserverCountsActualFailedSubmission(t *testing.T) {
	key, _ := crypto.GenerateKey()
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		action, _ := body["action"].(map[string]any)
		if _, ok := action["builder"]; ok {
			t.Error("unexpected builder")
		}
		if r.URL.Path != "/exchange" {
			t.Error("unexpected endpoint")
		}
		http.Error(w, "unavailable", 503)
	}))
	defer server.Close()
	sdk, err := New(server.URL, WithPrivateKey(hex.EncodeToString(crypto.FromECDSA(key))), WithDirectExchange())
	if err != nil {
		t.Fatal(err)
	}
	defer sdk.Close()
	calls, actions := 0, 0
	sdk.SetNativeObserver(func(kind string, count int) {
		if kind != "cancel" {
			t.Error(kind)
		}
		calls++
		actions += count
	})
	_, err = sdk.nativeSend(map[string]any{"type": "cancel", "cancels": []any{map[string]any{"a": 0, "o": int64(1)}, map[string]any{"a": 0, "o": int64(2)}}}, nil)
	if err == nil || requests != 1 || calls != 1 || actions != 2 {
		t.Fatalf("wrong actual attempt accounting: %v %d %d %d", err, requests, calls, actions)
	}
}
