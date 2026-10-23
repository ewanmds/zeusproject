package hyperliquid

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/ethereum/go-ethereum/crypto"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func TestNativeDigestMatchesOfficialPythonSDK(t *testing.T) {
	// Generated offline by installed official hyperliquid.utils.signing.action_hash
	// and eth_account encode_typed_data(l1_payload(...)), nonce 1700000000000.
	wire := object{{"type", "order"}, {"orders", []any{object{{"a", 110001}, {"b", true}, {"p", "100.28"}, {"s", "0.103"}, {"r", false}, {"t", object{{"limit", object{{"tif", "Alo"}}}}}}}}, {"grouping", "na"}}
	digest, err := nativeDigest(wire, 1700000000000, false)
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(digest) != "fa21ea453524bcc078d810da899f1c3034916323926a3d9b67bfbe6e061cac8d" {
		t.Fatalf("signature digest differs from official SDK: %x", digest)
	}
	other, _ := nativeDigest(wire, 1700000000000, true)
	if reflect.DeepEqual(digest, other) {
		t.Fatal("testnet domain must differ")
	}
}

func TestNativeManagementDigestsMatchOfficialPythonSDK(t *testing.T) {
	order := object{{"a", 110000}, {"b", false}, {"p", "100.29"}, {"s", "0.103"}, {"r", false}, {"t", object{{"limit", object{{"tif", "Alo"}}}}}}
	for _, tc := range []struct {
		action   object
		expected string
	}{
		{object{{"type", "cancel"}, {"cancels", []any{object{{"a", 110000}, {"o", int64(42)}}}}}, "cee8c566b537258845301220453f382df4c1a05b623b1895cf8f9d9d4f4448b7"},
		{object{{"type", "updateLeverage"}, {"asset", 110000}, {"isCross", false}, {"leverage", 5}}, "add7593530999c13c3bb13df3d555d6b350a0dae9b6eafb06bb33f0cd4ea4ab7"},
		{object{{"type", "batchModify"}, {"modifies", []any{object{{"oid", int64(42)}, {"order", order}}}}}, "d2534f972a4a6c0cf0d5e605ea7274df7cefcc24d2617b8d3a5214ec2d2b3acf"},
	} {
		digest, err := nativeDigest(tc.action, 1700000000000, false)
		if err != nil {
			t.Fatal(err)
		}
		if hex.EncodeToString(digest) != tc.expected {
			t.Fatalf("digest mismatch: %x", digest)
		}
	}
}

func TestNativeRejectionsNoRetryOrBuilderApproval(t *testing.T) {
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/exchange" {
			t.Error("unexpected remote build/approval request")
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"status":"ok","response":{"type":"cancel","data":{"statuses":[{"error":"invalid order"}]}}}`)
	}))
	defer server.Close()
	sdk, err := New(server.URL, WithPrivateKey(hex.EncodeToString(crypto.FromECDSA(key))), WithDirectExchange(), WithAutoApprove(true))
	if err != nil {
		t.Fatal(err)
	}
	defer sdk.Close()
	if calls != 0 || sdk.config.AutoApprove {
		t.Fatal("direct mode must never auto-approve builder fees")
	}
	sdk.native.assets = map[string][2]int{"xyz:CL": {110000, 3}}
	if _, err := sdk.Cancel(42, "xyz:CL"); err == nil {
		t.Fatal("cancel rejection treated as success")
	}
	if calls != 1 {
		t.Fatal("SDK retried rejected action")
	}
	order, err := sdk.Buy("xyz:CL", WithSize(0.103), WithPrice(100.28), WithTIF(TIFALO))
	if err != nil {
		t.Fatal(err)
	}
	if !order.IsError() {
		t.Fatal("order rejection treated as resting")
	}
	if calls != 2 {
		t.Fatal("SDK retried rejected order")
	}
}

func TestNativeRoutingNoBuilderAndAPISignatures(t *testing.T) {
	key, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	account := "0x1111111111111111111111111111111111111111"
	var sdk *SDK
	var mu sync.Mutex
	var expected object
	count := 0
	var lastNonce int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/info" {
			switch body["type"] {
			case "perpDexs":
				fmt.Fprint(w, `[null,{"name":"xyz"},{"name":"io"}]`)
			case "meta":
				dex, _ := body["dex"].(string)
				fmt.Fprintf(w, `{"universe":[{"name":%q,"szDecimals":3}]}`, dex+":CL")
			case "clearinghouseState":
				if body["user"] != account {
					t.Error("close queried wrong account")
				}
				fmt.Fprint(w, `{"assetPositions":[{"position":{"coin":"xyz:CL","szi":"0.103"}}]}`)
			case "openOrders":
				if body["user"] != account {
					t.Error("cancel queried wrong account")
				}
				fmt.Fprint(w, `[{"coin":"xyz:CL","oid":42}]`)
			case "allMids":
				fmt.Fprint(w, `{"xyz:CL":"100.28"}`)
			default:
				t.Errorf("unexpected info query %v", body["type"])
				http.Error(w, "unexpected", 400)
			}
			return
		}
		if r.URL.Path != "/exchange" {
			t.Errorf("remote builder route used: %s", r.URL.Path)
			http.Error(w, "blocked", 403)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		count++
		action, _ := body["action"].(map[string]any)
		raw, _ := json.Marshal(action)
		if strings.Contains(string(raw), "builder") {
			t.Error("builder leaked into action")
		}
		expectedJSON, _ := json.Marshal(expected)
		var want map[string]any
		json.Unmarshal(expectedJSON, &want)
		if !reflect.DeepEqual(action, want) {
			t.Errorf("wrong native wire action: %s; want %s", raw, expectedJSON)
		}
		nonce := int64(body["nonce"].(float64))
		if nonce <= lastNonce {
			t.Error("non-monotonic nonce")
		}
		lastNonce = nonce
		digest, err := nativeDigest(expected, nonce, false)
		if err != nil {
			t.Error(err)
		}
		sig, _ := body["signature"].(map[string]any)
		rBytes, _ := hex.DecodeString(strings.TrimPrefix(sig["r"].(string), "0x"))
		sBytes, _ := hex.DecodeString(strings.TrimPrefix(sig["s"].(string), "0x"))
		signature := append(append(rBytes, sBytes...), byte(sig["v"].(float64)-27))
		pub, err := crypto.SigToPub(digest, signature)
		if err != nil {
			t.Error(err)
		} else if crypto.PubkeyToAddress(*pub) != crypto.PubkeyToAddress(key.PublicKey) {
			t.Error("signature is not from API wallet")
		}
		if body["vaultAddress"] != nil {
			t.Error("main account must not be sent as a vault")
		}
		if action["type"] == "order" || action["type"] == "batchModify" {
			fmt.Fprint(w, `{"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":42}}]}}}`)
		} else {
			fmt.Fprint(w, `{"status":"ok","response":{"type":"cancel","data":{"statuses":["success"]}}}`)
		}
	}))
	defer server.Close()
	sdk, err = New(server.URL, WithPrivateKey(hex.EncodeToString(crypto.FromECDSA(key))), WithAccountAddress(account), WithDirectExchange())
	if err != nil {
		t.Fatal(err)
	}
	defer sdk.Close()
	// Must resolve identical base names on different HIP-3 DEXes independently.
	xyz, _, err := sdk.nativeAsset("xyz:CL")
	if err != nil {
		t.Fatal(err)
	}
	io, _, err := sdk.nativeAsset("io:CL")
	if err != nil {
		t.Fatal(err)
	}
	if xyz != 110000 || io != 120000 {
		t.Fatalf("wrong qualified IDs: %d %d", xyz, io)
	}
	wire := func(buy, reduce bool, price, tif string) object {
		return object{{"a", 110000}, {"b", buy}, {"p", price}, {"s", "0.103"}, {"r", reduce}, {"t", object{{"limit", object{{"tif", tif}}}}}}
	}
	expected = object{{"type", "order"}, {"orders", []any{wire(true, false, "100.28", "Alo")}}, {"grouping", "na"}}
	order, err := sdk.Buy("xyz:CL", WithSize(0.103), WithPrice(100.28), WithTIF(TIFALO))
	if err != nil || order.OID != 42 {
		t.Fatalf("buy: %v %+v", err, order)
	}
	expected = object{{"type", "batchModify"}, {"modifies", []any{object{{"oid", int64(42)}, {"order", wire(false, false, "100.29", "Alo")}}}}}
	if _, err := sdk.Modify(42, "xyz:CL", "sell", "100.29", "0.103", ModifyWithTIF(TIFALO)); err != nil {
		t.Fatal(err)
	}
	expected = object{{"type", "cancel"}, {"cancels", []any{object{{"a", 110000}, {"o", int64(42)}}}}}
	if _, err := sdk.Cancel(42, "xyz:CL"); err != nil {
		t.Fatal(err)
	}
	if _, err := sdk.CancelAll("xyz:CL"); err != nil {
		t.Fatal(err)
	}
	expected = object{{"type", "updateLeverage"}, {"asset", 110000}, {"isCross", false}, {"leverage", 5}}
	if _, err := sdk.UpdateLeverage("xyz:CL", 5, LeverageWithIsolated()); err != nil {
		t.Fatal(err)
	}
	expected = object{{"type", "order"}, {"orders", []any{wire(false, true, "97.272", "Ioc")}}, {"grouping", "na"}}
	if _, err := sdk.ClosePosition("xyz:CL"); err != nil {
		t.Fatal(err)
	}
	if count != 6 {
		t.Fatalf("expected six local native submissions, got %d", count)
	}
	if _, err := sdk.nativeAction(map[string]any{"type": "order", "builder": map[string]any{}}, nil); err == nil {
		t.Fatal("builder action accepted")
	}
	if _, _, err := sdk.nativeAsset("CL"); err == nil {
		t.Fatal("bare name silently mapped to a HIP-3 market")
	}
}
