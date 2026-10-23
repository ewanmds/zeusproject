package quoting

import (
	"math"
	"testing"
)

func TestQuantileNormalizer_PreWarmupReturnsSentinel(t *testing.T) {
	q := newQuantileNormalizer(500, 5, 95)
	for i := 0; i < 50; i++ {
		q.Observe(float64(i))
	}
	if q.Ready() {
		t.Fatalf("expected not ready at count=%d window=%d", q.count, q.window)
	}
	if v := q.Normalize(10); v != -1 {
		t.Fatalf("expected sentinel -1 pre-warmup, got %v", v)
	}
}

func TestQuantileNormalizer_ReadyAtOneFifth(t *testing.T) {
	q := newQuantileNormalizer(500, 5, 95)
	for i := 0; i < 99; i++ {
		q.Observe(float64(i))
	}
	if q.Ready() {
		t.Fatalf("expected not ready at count=99")
	}
	q.Observe(100)
	if !q.Ready() {
		t.Fatalf("expected ready at count=100 (window/5=100)")
	}
}

func TestQuantileNormalizer_LinearInterpolation(t *testing.T) {
	q := newQuantileNormalizer(100, 5, 95)
	for i := 0; i < 100; i++ {
		q.Observe(float64(i))
	}
	got := q.Normalize(50)
	if math.Abs(got-0.5) > 0.1 {
		t.Fatalf("Normalize(50) with uniform [0,99] expected ~0.5, got %v", got)
	}
}

func TestQuantileNormalizer_OutliersClamp(t *testing.T) {
	q := newQuantileNormalizer(100, 5, 95)
	for i := 0; i < 100; i++ {
		q.Observe(float64(i))
	}
	if got := q.Normalize(-1000); got != 0 {
		t.Fatalf("extreme-low outlier should clamp to 0, got %v", got)
	}
	if got := q.Normalize(1e9); got != 1 {
		t.Fatalf("extreme-high outlier should clamp to 1, got %v", got)
	}
}

func TestQuantileNormalizer_DegenerateWindow(t *testing.T) {
	q := newQuantileNormalizer(100, 5, 95)
	for i := 0; i < 100; i++ {
		q.Observe(7.0)
	}
	got := q.Normalize(7.0)
	if got != 0.5 {
		t.Fatalf("degenerate window (pHigh==pLow) should return 0.5, got %v", got)
	}
}

func TestQuantileNormalizer_RingBufferRotation(t *testing.T) {
	q := newQuantileNormalizer(10, 10, 90)
	for i := 1; i <= 15; i++ {
		q.Observe(float64(i))
	}
	if q.count != 10 {
		t.Fatalf("expected count=10 after 15 observes with window=10, got %d", q.count)
	}
	sum := 0.0
	for _, v := range q.samples {
		sum += v
	}
	want := 6.0 + 7 + 8 + 9 + 10 + 11 + 12 + 13 + 14 + 15
	if sum != want {
		t.Fatalf("expected ring to retain last 10 values (sum=%v), got sum=%v", want, sum)
	}
}

func TestQuantileNormalizer_Snapshot(t *testing.T) {
	q := newQuantileNormalizer(100, 5, 95)
	for i := 0; i < 100; i++ {
		q.Observe(float64(i))
	}
	pLo, pMid, pHi, n := q.Snapshot()
	if n != 100 {
		t.Fatalf("snapshot samples=%d, want 100", n)
	}
	if pLo != 5 {
		t.Fatalf("p5 expected 5, got %v", pLo)
	}
	if pMid != 50 {
		t.Fatalf("p50 expected 50, got %v", pMid)
	}
	if pHi != 95 {
		t.Fatalf("p95 expected 95, got %v", pHi)
	}
}

func TestQuantileNormalizer_TransitionedToWarmFiresOnce(t *testing.T) {
	q := newQuantileNormalizer(100, 5, 95)
	for i := 0; i < 100; i++ {
		q.Observe(float64(i))
	}
	_ = q.Normalize(50)
	if !q.transitionedToWarm() {
		t.Fatalf("expected first transitionedToWarm to return true")
	}
	if q.transitionedToWarm() {
		t.Fatalf("expected subsequent transitionedToWarm to return false")
	}
}

func TestQuantileNormalizer_DefaultsWhenWindowZero(t *testing.T) {
	q := newQuantileNormalizer(0, 5, 95)
	if q.window != 500 {
		t.Fatalf("expected default window=500, got %d", q.window)
	}
}
