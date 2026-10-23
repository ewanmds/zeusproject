package hyperliquid

import (
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"
)

func TestAccountOverrideRetainsAPISigner(t *testing.T) {
	// Ephemeral, unfunded key generated only for this offline test.
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	account := "0x1111111111111111111111111111111111111111"
	sdk, err := New("http://unused.invalid", WithPrivateKey(hex.EncodeToString(crypto.FromECDSA(key))), WithAccountAddress(account), WithAutoApprove(false))
	if err != nil {
		t.Fatal(err)
	}
	defer sdk.Close()
	signer := crypto.PubkeyToAddress(key.PublicKey).Hex()
	if sdk.Address() != account || sdk.SignerAddress() != signer {
		t.Fatal("account and signer not separated")
	}
	hash := crypto.Keccak256([]byte("local signing test"))
	sig, err := sdk.wallet.SignHash(hex.EncodeToString(hash))
	if err != nil {
		t.Fatal(err)
	}
	r, _ := hex.DecodeString(strings.TrimPrefix(sig.R, "0x"))
	s, _ := hex.DecodeString(strings.TrimPrefix(sig.S, "0x"))
	raw := append(append(r, s...), byte(sig.V-27))
	pub, err := crypto.SigToPub(hash, raw)
	if err != nil {
		t.Fatal(err)
	}
	if crypto.PubkeyToAddress(*pub).Hex() != signer {
		t.Fatal("signature must remain from the API wallet")
	}
	queries := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["user"] != account {
			t.Error("query did not use trading account")
		}
		queries++
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"orders":[]}`))
	}))
	defer server.Close()
	sdk.publicWorkerURL = server.URL
	if _, err := sdk.CancelAll("BTC"); err != nil {
		t.Fatal(err)
	}
	if queries != 1 {
		t.Fatal("expected account order query")
	}
}
