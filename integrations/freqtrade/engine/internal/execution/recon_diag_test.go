package execution

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestBuildReconDiagnosticSymmetricDifference(t *testing.T) {
	tracked := []int64{1, 2, 3}
	exchange := []int64{2, 3, 4}
	gen := uint64(7)
	genAt := time.Now().Add(-5 * time.Second)
	now := time.Now()

	p := buildReconDiagnostic(tracked, exchange, gen, genAt, now, nil, true, time.Time{}, 0, 0, 0)

	if len(p.onlyLocal) != 1 || p.onlyLocal[0] != 1 {
		t.Fatalf("only_local = %v, want [1]", p.onlyLocal)
	}
	if len(p.onlyExchange) != 1 || p.onlyExchange[0] != 4 {
		t.Fatalf("only_exchange = %v, want [4]", p.onlyExchange)
	}
	if p.generation != gen {
		t.Fatalf("generation = %d, want %d", p.generation, gen)
	}
	if !p.exchangeFetchOK {
		t.Fatalf("exchangeFetchOK = false, want true")
	}
	if p.generationAgeMs < 4000 || p.generationAgeMs > 6000 {
		t.Fatalf("generationAgeMs = %d, want ~5000", p.generationAgeMs)
	}
	if len(p.trackedAgesMs) != len(tracked) {
		t.Fatalf("trackedAgesMs len = %d, want %d", len(p.trackedAgesMs), len(tracked))
	}
	for i, a := range p.trackedAgesMs {
		if a != p.generationAgeMs {
			t.Fatalf("trackedAgesMs[%d] = %d, want %d", i, a, p.generationAgeMs)
		}
	}
	if len(p.exchangeAgesMs) != len(exchange) {
		t.Fatalf("exchangeAgesMs len = %d, want %d", len(p.exchangeAgesMs), len(exchange))
	}
}

func TestBuildReconDiagnosticEmpty(t *testing.T) {
	p := buildReconDiagnostic(nil, nil, 0, time.Time{}, time.Now(), nil, false, time.Time{}, 0, 0, 0)
	if len(p.onlyLocal) != 0 || len(p.onlyExchange) != 0 {
		t.Fatalf("expected empty diffs, got %v / %v", p.onlyLocal, p.onlyExchange)
	}
	if p.generationAgeMs != 0 {
		t.Fatalf("expected zero generationAgeMs, got %d", p.generationAgeMs)
	}
	if p.exchangeFetchOK {
		t.Fatalf("exchangeFetchOK = true, want false")
	}
}

func TestBuildReconDiagnosticOnlyLocalEnrichment(t *testing.T) {
	trackedOIDs := []int64{10, 11, 12}
	exchange := []int64{11} // only OID 11 is on exchange; 10 and 12 are only_local
	tracked := []trackedOrder{
		{OID: 10, Side: "buy", Price: 100.5},
		{OID: 11, Side: "sell", Price: 101.5},
		{OID: 12, Side: "sell", Price: 102.0},
	}
	now := time.Now()

	p := buildReconDiagnostic(trackedOIDs, exchange, 1, now, now, tracked, true, time.Time{}, 0, 0, 0)

	if len(p.onlyLocal) != 2 || p.onlyLocal[0] != 10 || p.onlyLocal[1] != 12 {
		t.Fatalf("only_local = %v, want [10 12]", p.onlyLocal)
	}
	if len(p.onlyLocalSides) != 2 || p.onlyLocalSides[0] != "buy" || p.onlyLocalSides[1] != "sell" {
		t.Fatalf("only_local_sides = %v, want [buy sell]", p.onlyLocalSides)
	}
	if len(p.onlyLocalPrices) != 2 || p.onlyLocalPrices[0] != 100.5 || p.onlyLocalPrices[1] != 102.0 {
		t.Fatalf("only_local_prices = %v, want [100.5 102.0]", p.onlyLocalPrices)
	}
}

func TestBuildReconDiagnosticOnlyLocalMissingMeta(t *testing.T) {
	// tracked metadata missing for OID 99 — should emit placeholder side/price.
	p := buildReconDiagnostic([]int64{99}, nil, 0, time.Time{}, time.Now(), nil, true, time.Time{}, 0, 0, 0)
	if len(p.onlyLocalSides) != 1 || p.onlyLocalSides[0] != "" {
		t.Fatalf("onlyLocalSides = %v, want [\"\"]", p.onlyLocalSides)
	}
	if len(p.onlyLocalPrices) != 1 || p.onlyLocalPrices[0] != 0 {
		t.Fatalf("onlyLocalPrices = %v, want [0]", p.onlyLocalPrices)
	}
}

func TestReconDiagAllowRateLimit(t *testing.T) {
	le := &LiveExecutor{reconDiagCounter: make(map[string][]time.Time)}
	now := time.Now()
	coin := "CL"

	for i := 0; i < 3; i++ {
		if !le.reconDiagAllow(coin, 3, now) {
			t.Fatalf("emission %d should be allowed", i)
		}
	}
	if le.reconDiagAllow(coin, 3, now) {
		t.Fatalf("4th emission within hour should be blocked")
	}

	// Advance past 1h; stale entries should be pruned.
	future := now.Add(61 * time.Minute)
	if !le.reconDiagAllow(coin, 3, future) {
		t.Fatalf("emission after window should be allowed")
	}
}

func TestReconDiagAllowZeroMaxBlocks(t *testing.T) {
	le := &LiveExecutor{reconDiagCounter: make(map[string][]time.Time)}
	if le.reconDiagAllow("CL", 0, time.Now()) {
		t.Fatalf("maxPerHour=0 must block")
	}
	if le.reconDiagAllow("CL", -1, time.Now()) {
		t.Fatalf("negative maxPerHour must block")
	}
}

func TestEmitReconDiagnosticLogShape(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)

	tracked := []trackedOrder{
		{OID: 10, Side: "buy", Price: 100.5},
		{OID: 11, Side: "sell", Price: 101.5},
	}
	p := buildReconDiagnostic([]int64{10, 11}, []int64{11, 12}, 3, time.Now().Add(-2*time.Second), time.Now(), tracked, true, time.Now().Add(-4*time.Second), 1500, 10000, 0)
	emitReconDiagnostic("CL", p)

	line := buf.String()
	if !strings.Contains(line, `"msg":"reconciliation mismatch diagnostic"`) {
		t.Fatalf("missing msg: %s", line)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(line), &parsed); err != nil {
		t.Fatalf("parse log: %v", err)
	}
	if parsed["coin"] != "CL" {
		t.Fatalf("coin = %v, want CL", parsed["coin"])
	}
	if tc, ok := parsed["tracked_count"].(float64); !ok || int(tc) != 2 {
		t.Fatalf("tracked_count = %v", parsed["tracked_count"])
	}
	if ec, ok := parsed["exchange_count"].(float64); !ok || int(ec) != 2 {
		t.Fatalf("exchange_count = %v", parsed["exchange_count"])
	}
	if ok, _ := parsed["exchange_fetch_ok"].(bool); !ok {
		t.Fatalf("exchange_fetch_ok = %v, want true", parsed["exchange_fetch_ok"])
	}
	ol, ok := parsed["only_local"].([]any)
	if !ok || len(ol) != 1 || int(ol[0].(float64)) != 10 {
		t.Fatalf("only_local = %v", parsed["only_local"])
	}
	ols, ok := parsed["only_local_sides"].([]any)
	if !ok || len(ols) != 1 || ols[0].(string) != "buy" {
		t.Fatalf("only_local_sides = %v, want [buy]", parsed["only_local_sides"])
	}
	olp, ok := parsed["only_local_prices"].([]any)
	if !ok || len(olp) != 1 || olp[0].(float64) != 100.5 {
		t.Fatalf("only_local_prices = %v, want [100.5]", parsed["only_local_prices"])
	}
	oe, ok := parsed["only_exchange"].([]any)
	if !ok || len(oe) != 1 || int(oe[0].(float64)) != 12 {
		t.Fatalf("only_exchange = %v", parsed["only_exchange"])
	}
	// V24: last_fill_ms_ago must be present and near 4000 ms for this fixture.
	lf, ok := parsed["last_fill_ms_ago"].(float64)
	if !ok {
		t.Fatalf("last_fill_ms_ago missing or wrong type: %v", parsed["last_fill_ms_ago"])
	}
	if lf < 3500 || lf > 4500 {
		t.Fatalf("last_fill_ms_ago = %v, want ~4000", lf)
	}
}

func TestBuildReconDiagnosticLastFillMsAgoUnset(t *testing.T) {
	// V24: zero-value lastFillAt → lastFillMsAgo must be -1 sentinel.
	p := buildReconDiagnostic(nil, nil, 0, time.Time{}, time.Now(), nil, true, time.Time{}, 0, 0, 0)
	if p.lastFillMsAgo != -1 {
		t.Fatalf("lastFillMsAgo = %d, want -1 sentinel", p.lastFillMsAgo)
	}
}

func TestBuildReconDiagnosticRateLimitPopulated(t *testing.T) {
	// V26: rate-limit fields must be copied into the payload and pct computed.
	p := buildReconDiagnostic(nil, nil, 0, time.Time{}, time.Now(), nil, true, time.Time{}, 1500, 10000, 0)
	if p.rateLimitRemaining != 1500 {
		t.Fatalf("rateLimitRemaining = %d, want 1500", p.rateLimitRemaining)
	}
	if p.rateLimitCap != 10000 {
		t.Fatalf("rateLimitCap = %d, want 10000", p.rateLimitCap)
	}
	if p.rateLimitPct < 0.1499 || p.rateLimitPct > 0.1501 {
		t.Fatalf("rateLimitPct = %g, want ~0.15", p.rateLimitPct)
	}

	// When cap is 0, pct should be -1 sentinel rather than a NaN/Inf.
	p = buildReconDiagnostic(nil, nil, 0, time.Time{}, time.Now(), nil, true, time.Time{}, 0, 0, 0)
	if p.rateLimitPct != -1 {
		t.Fatalf("rateLimitPct with zero cap = %g, want -1", p.rateLimitPct)
	}
}

func TestBuildReconDiagnosticDriftBps(t *testing.T) {
	// V27: per-orphaned-order drift vs current mid. Three orders: one at mid,
	// one 10 bps below, one 5 bps above. Mid = 100.
	tracked := []trackedOrder{
		{OID: 1, Side: "buy", Price: 100.0},
		{OID: 2, Side: "buy", Price: 99.9},   // 10 bps below mid
		{OID: 3, Side: "sell", Price: 100.05}, // 5 bps above mid
	}
	p := buildReconDiagnostic([]int64{1, 2, 3}, nil, 0, time.Time{}, time.Now(), tracked, true, time.Time{}, 0, 0, 100.0)
	if len(p.onlyLocalDriftBps) != 3 {
		t.Fatalf("onlyLocalDriftBps len = %d, want 3", len(p.onlyLocalDriftBps))
	}
	if p.onlyLocalDriftBps[0] != 0 {
		t.Fatalf("drift[0] = %g, want 0", p.onlyLocalDriftBps[0])
	}
	if p.onlyLocalDriftBps[1] > -9.99 || p.onlyLocalDriftBps[1] < -10.01 {
		t.Fatalf("drift[1] = %g, want ~-10", p.onlyLocalDriftBps[1])
	}
	if p.onlyLocalDriftBps[2] < 4.99 || p.onlyLocalDriftBps[2] > 5.01 {
		t.Fatalf("drift[2] = %g, want ~5", p.onlyLocalDriftBps[2])
	}
	if p.bookMid != 100.0 {
		t.Fatalf("bookMid = %g, want 100.0", p.bookMid)
	}

	// bookMid = 0 → drift array allocated but all zeros.
	p2 := buildReconDiagnostic([]int64{1}, nil, 0, time.Time{}, time.Now(), tracked[:1], true, time.Time{}, 0, 0, 0)
	if len(p2.onlyLocalDriftBps) != 1 || p2.onlyLocalDriftBps[0] != 0 {
		t.Fatalf("bookMid=0 should zero drift; got %v", p2.onlyLocalDriftBps)
	}
}

func TestLiveExecutorUpdateRateLimit(t *testing.T) {
	le := &LiveExecutor{}
	le.UpdateRateLimit(2500, 50000)
	rem, cap := le.rateLimitSnapshot()
	if rem != 2500 || cap != 50000 {
		t.Fatalf("rateLimitSnapshot = %d/%d, want 2500/50000", rem, cap)
	}
}
