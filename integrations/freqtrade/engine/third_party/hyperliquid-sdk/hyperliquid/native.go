package hyperliquid

// Native L1 signing follows hyperliquid-dex/hyperliquid-python-sdk signing.py.
// No remote builder, remote hash service, or builder field is used.
import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/ethereum/go-ethereum/crypto"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
)

type nativeState struct {
	observer  func(string, int)
	metaMu    sync.Mutex
	assets    map[string][2]int
	nonceMu   sync.Mutex
	lastNonce int64
}

// SetNativeObserver counts actual signed submissions, including failed attempts.
// Install before starting the executor; it never receives credentials or signatures.
func (s *SDK) SetNativeObserver(fn func(string, int)) { s.native.observer = fn }

type field struct {
	key   string
	value any
}
type object []field

func (o object) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, f := range o {
		if i > 0 {
			b.WriteByte(',')
		}
		k, _ := json.Marshal(f.key)
		v, err := json.Marshal(f.value)
		if err != nil {
			return nil, err
		}
		b.Write(k)
		b.WriteByte(':')
		b.Write(v)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

// encodePack deliberately preserves wire field order and Python msgpack's
// compact integer encodings. Unsupported values fail instead of being signed.
func encodePack(b *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case object:
		if len(x) >= 16 {
			return fmt.Errorf("oversized native map")
		}
		b.WriteByte(0x80 | byte(len(x)))
		for _, f := range x {
			if err := encodePack(b, f.key); err != nil {
				return err
			}
			if err := encodePack(b, f.value); err != nil {
				return err
			}
		}
	case []any:
		if len(x) < 16 {
			b.WriteByte(0x90 | byte(len(x)))
		} else if len(x) <= 65535 {
			b.WriteByte(0xdc)
			binary.Write(b, binary.BigEndian, uint16(len(x)))
		} else {
			return fmt.Errorf("oversized native array")
		}
		for _, v := range x {
			if err := encodePack(b, v); err != nil {
				return err
			}
		}
	case string:
		n := len(x)
		if n < 32 {
			b.WriteByte(0xa0 | byte(n))
		} else if n < 256 {
			b.WriteByte(0xd9)
			b.WriteByte(byte(n))
		} else if n <= 65535 {
			b.WriteByte(0xda)
			binary.Write(b, binary.BigEndian, uint16(n))
		} else {
			return fmt.Errorf("oversized native string")
		}
		b.WriteString(x)
	case bool:
		if x {
			b.WriteByte(0xc3)
		} else {
			b.WriteByte(0xc2)
		}
	case int:
		return encodePack(b, int64(x))
	case int64:
		if x < 0 {
			return fmt.Errorf("negative wire integer")
		}
		switch {
		case x < 128:
			b.WriteByte(byte(x))
		case x <= 255:
			b.WriteByte(0xcc)
			b.WriteByte(byte(x))
		case x <= 65535:
			b.WriteByte(0xcd)
			binary.Write(b, binary.BigEndian, uint16(x))
		case x <= math.MaxUint32:
			b.WriteByte(0xce)
			binary.Write(b, binary.BigEndian, uint32(x))
		default:
			b.WriteByte(0xcf)
			binary.Write(b, binary.BigEndian, uint64(x))
		}
	default:
		return fmt.Errorf("unsupported wire value %T", v)
	}
	return nil
}

func nativeDigest(action object, nonce int64, testnet bool) ([]byte, error) {
	var packed bytes.Buffer
	if err := encodePack(&packed, action); err != nil {
		return nil, err
	}
	binary.Write(&packed, binary.BigEndian, uint64(nonce))
	packed.WriteByte(0) // no vault; main account is inferred from API signer
	connection := crypto.Keccak256(packed.Bytes())
	domainType := crypto.Keccak256([]byte("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"))
	chain := make([]byte, 32)
	binary.BigEndian.PutUint64(chain[24:], 1337)
	domain := crypto.Keccak256(domainType, crypto.Keccak256([]byte("Exchange")), crypto.Keccak256([]byte("1")), chain, make([]byte, 32))
	source := "a"
	if testnet {
		source = "b"
	}
	agent := crypto.Keccak256(crypto.Keccak256([]byte("Agent(string source,bytes32 connectionId)")), crypto.Keccak256([]byte(source)), connection)
	return crypto.Keccak256([]byte{0x19, 0x01}, domain, agent), nil
}

func (s *SDK) nativeAsset(coin string) (int, int, error) {
	s.native.metaMu.Lock()
	defer s.native.metaMu.Unlock()
	if a, ok := s.native.assets[coin]; ok {
		return a[0], a[1], nil
	}
	dex := ""
	if i := strings.IndexByte(coin, ':'); i >= 0 {
		dex = coin[:i]
	}
	offset := 0
	if dex != "" {
		raw, err := s.http.PostRaw(context.Background(), s.infoURL, map[string]any{"type": "perpDexs"})
		if err != nil {
			return 0, 0, err
		}
		rows, ok := raw.([]any)
		if !ok {
			return 0, 0, fmt.Errorf("invalid perpDexs response")
		}
		found := false
		for i, row := range rows {
			m, _ := row.(map[string]any)
			if m["name"] == dex && i > 0 {
				offset = 100000 + i*10000
				found = true
				break
			}
		}
		if !found {
			return 0, 0, fmt.Errorf("unknown HIP-3 DEX %q", dex)
		}
	}
	raw, err := s.http.Post(context.Background(), s.infoURL, map[string]any{"type": "meta", "dex": dex})
	if err != nil {
		return 0, 0, err
	}
	rows, ok := raw["universe"].([]any)
	if !ok {
		return 0, 0, fmt.Errorf("invalid metadata response")
	}
	if s.native.assets == nil {
		s.native.assets = map[string][2]int{}
	}
	for i, row := range rows {
		m, _ := row.(map[string]any)
		name, _ := m["name"].(string)
		dec, ok := m["szDecimals"].(float64)
		if !ok || name == "" {
			continue
		}
		if dex != "" && !strings.Contains(name, ":") {
			name = dex + ":" + name
		}
		s.native.assets[name] = [2]int{offset + i, int(dec)}
	}
	if a, ok := s.native.assets[coin]; ok {
		return a[0], a[1], nil
	}
	return 0, 0, fmt.Errorf("unknown exact market %q", coin)
}

func nativeNumber(v any) (float64, error) { return strconv.ParseFloat(fmt.Sprint(v), 64) }
func nativePrice(v float64, dec int) string {
	significant, _ := strconv.ParseFloat(strconv.FormatFloat(v, 'g', 5, 64), 64)
	places := 6 - dec
	if places < 0 {
		places = 0
	}
	scale := math.Pow10(places)
	return strconv.FormatFloat(math.Round(significant*scale)/scale, 'f', -1, 64)
}

func (s *SDK) nativeOrder(input map[string]any, slippage *float64) (object, error) {
	coin, _ := input["asset"].(string)
	if coin == "" {
		coin, _ = input["a"].(string)
	}
	idx, dec, err := s.nativeAsset(coin)
	if err != nil {
		return nil, err
	}
	buy := input["side"] == "buy"
	if b, ok := input["b"].(bool); ok {
		buy = b
	}
	size := input["size"]
	if size == nil {
		size = input["s"]
	}
	qty, err := nativeNumber(size)
	if err != nil || qty <= 0 || math.IsNaN(qty) || math.IsInf(qty, 0) {
		return nil, fmt.Errorf("invalid order size")
	}
	scaled := qty * math.Pow10(dec)
	if math.Abs(scaled-math.Round(scaled)) > 1e-7 {
		return nil, fmt.Errorf("size exceeds market precision")
	}
	price := input["price"]
	if price == nil {
		price = input["p"]
	}
	tif, _ := input["tif"].(string)
	if t, ok := input["t"].(map[string]any); ok {
		if limit, ok := t["limit"].(map[string]any); ok {
			tif, _ = limit["tif"].(string)
		}
	}
	if strings.EqualFold(tif, "market") {
		mid, err := s.GetMid(coin)
		if err != nil || mid <= 0 {
			return nil, fmt.Errorf("cannot get executable market reference")
		}
		slip := s.config.Slippage
		if slippage != nil {
			slip = *slippage
		}
		if buy {
			price = mid * (1 + slip)
		} else {
			price = mid * (1 - slip)
		}
		tif = "Ioc"
	}
	px, err := nativeNumber(price)
	if err != nil || px <= 0 || math.IsNaN(px) || math.IsInf(px, 0) {
		return nil, fmt.Errorf("invalid order price")
	}
	switch strings.ToLower(tif) {
	case "alo":
		tif = "Alo"
	case "gtc":
		tif = "Gtc"
	case "ioc":
		tif = "Ioc"
	default:
		return nil, fmt.Errorf("unsupported native TIF %q", tif)
	}
	reduce, _ := input["reduceOnly"].(bool)
	if r, ok := input["r"].(bool); ok {
		reduce = r
	}
	wire := object{{"a", idx}, {"b", buy}, {"p", nativePrice(px, dec)}, {"s", strconv.FormatFloat(qty, 'f', -1, 64)}, {"r", reduce}, {"t", object{{"limit", object{{"tif", tif}}}}}}
	if cloid, ok := input["cloid"].(string); ok && cloid != "" {
		wire = append(wire, field{"c", cloid})
	}
	return wire, nil
}

func (s *SDK) nativeAction(action map[string]any, slippage *float64) (object, error) {
	if _, ok := action["builder"]; ok {
		return nil, fmt.Errorf("builder fields forbidden in direct mode")
	}
	kind, _ := action["type"].(string)
	switch kind {
	case "order":
		rows, ok := action["orders"].([]any)
		if !ok || len(rows) == 0 {
			return nil, fmt.Errorf("missing orders")
		}
		out := []any{}
		for _, row := range rows {
			m, ok := row.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("invalid order")
			}
			o, err := s.nativeOrder(m, slippage)
			if err != nil {
				return nil, err
			}
			out = append(out, o)
		}
		grouping := "na"
		if g, ok := action["grouping"].(string); ok {
			grouping = g
		}
		return object{{"type", "order"}, {"orders", out}, {"grouping", grouping}}, nil
	case "cancel":
		rows, _ := action["cancels"].([]any)
		out := []any{}
		for _, row := range rows {
			m, _ := row.(map[string]any)
			a, ok := m["a"].(int)
			if !ok || a < 0 {
				return nil, fmt.Errorf("invalid cancel asset")
			}
			oid, ok := m["o"].(int64)
			if !ok || oid <= 0 {
				return nil, fmt.Errorf("invalid cancel oid")
			}
			out = append(out, object{{"a", a}, {"o", oid}})
		}
		return object{{"type", "cancel"}, {"cancels", out}}, nil
	case "batchModify":
		rows, _ := action["modifies"].([]any)
		out := []any{}
		for _, row := range rows {
			m, _ := row.(map[string]any)
			o, _ := m["order"].(map[string]any)
			wire, err := s.nativeOrder(o, slippage)
			if err != nil {
				return nil, err
			}
			out = append(out, object{{"oid", m["oid"]}, {"order", wire}})
		}
		return object{{"type", "batchModify"}, {"modifies", out}}, nil
	case "updateLeverage":
		return object{{"type", kind}, {"asset", action["asset"]}, {"isCross", action["isCross"]}, {"leverage", action["leverage"]}}, nil
	case "closePosition":
		coin, _ := action["asset"].(string)
		dex := ""
		if i := strings.IndexByte(coin, ':'); i >= 0 {
			dex = coin[:i]
		}
		state, err := s.Info().ClearinghouseState(s.Address(), WithDex(dex))
		if err != nil {
			return nil, err
		}
		rows, _ := state["assetPositions"].([]any)
		for _, row := range rows {
			m, _ := row.(map[string]any)
			p, _ := m["position"].(map[string]any)
			if p["coin"] != coin {
				continue
			}
			qty, err := nativeNumber(p["szi"])
			if err != nil {
				return nil, err
			}
			if qty == 0 {
				break
			}
			side := "buy"
			if qty > 0 {
				side = "sell"
			}
			return s.nativeAction(map[string]any{"type": "order", "orders": []any{map[string]any{"asset": coin, "side": side, "size": math.Abs(qty), "tif": "market", "reduceOnly": true}}}, slippage)
		}
		return nil, fmt.Errorf("no position to close for %s", coin)
	default:
		return nil, fmt.Errorf("action %q unsupported in builder-free direct mode", kind)
	}
}

func (s *SDK) nativeSend(action map[string]any, slippage *float64) (map[string]any, error) {
	wire, err := s.nativeAction(action, slippage)
	if err != nil {
		return nil, err
	}
	s.native.nonceMu.Lock()
	nonce := time.Now().UnixMilli()
	if nonce <= s.native.lastNonce {
		nonce = s.native.lastNonce + 1
	}
	s.native.lastNonce = nonce
	s.native.nonceMu.Unlock()
	digest, err := nativeDigest(wire, nonce, s.config.Testnet)
	if err != nil {
		return nil, err
	}
	signature, err := s.wallet.SignHash(hex.EncodeToString(digest))
	if err != nil {
		return nil, err
	}
	if s.native.observer != nil {
		kind := ""
		count := 0
		for _, f := range wire {
			if f.key == "type" {
				kind, _ = f.value.(string)
			}
			if rows, ok := f.value.([]any); ok {
				count = len(rows)
			}
		}
		s.native.observer(kind, count)
	}
	response, err := s.http.Post(context.Background(), strings.TrimSuffix(s.endpoint, "/")+"/exchange", map[string]any{"action": wire, "nonce": nonce, "signature": signature, "vaultAddress": nil})
	if err != nil {
		return nil, err
	}
	if response["status"] != "ok" {
		return nil, fmt.Errorf("exchange rejected action: %v", response["response"])
	}
	if action["type"] != "order" && action["type"] != "batchModify" && action["type"] != "closePosition" {
		resp, _ := response["response"].(map[string]any)
		data, _ := resp["data"].(map[string]any)
		statuses, _ := data["statuses"].([]any)
		for _, status := range statuses {
			if row, ok := status.(map[string]any); ok {
				if msg, ok := row["error"].(string); ok {
					return nil, fmt.Errorf("exchange rejected action: %s", msg)
				}
			}
		}
	}
	// Adapt the native reply to the upstream SDK's response parser.
	return map[string]any{"exchangeResponse": response}, nil
}

func (s *SDK) nativeCancelAll(coin string) (map[string]any, error) {
	if coin == "" {
		return nil, fmt.Errorf("explicit market required for cancel-all")
	}
	dex := ""
	if i := strings.IndexByte(coin, ':'); i >= 0 {
		dex = coin[:i]
	}
	rows, err := s.Info().OpenOrders(s.Address(), WithDex(dex))
	if err != nil {
		return nil, err
	}
	for _, raw := range rows {
		m, _ := raw.(map[string]any)
		if m["coin"] != coin {
			continue
		}
		oid, err := nativeNumber(m["oid"])
		if err != nil {
			return nil, err
		}
		if _, err = s.Cancel(int64(oid), coin); err != nil {
			return nil, err
		}
	}
	return map[string]any{"status": "ok"}, nil
}
