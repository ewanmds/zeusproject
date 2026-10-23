package quoting

import (
	"sync"
	"testing"
	"time"
)

func TestShockChainTracker_BasicRecordAndCount(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(300*time.Second, 3)
	base := time.Unix(1_000_000, 0)

	count, crossed := trk.Record(base)
	if count != 1 || crossed {
		t.Fatalf("first record: want count=1 crossed=false, got count=%d crossed=%v", count, crossed)
	}

	count, crossed = trk.Record(base.Add(10 * time.Second))
	if count != 2 || crossed {
		t.Fatalf("second record: want count=2 crossed=false, got count=%d crossed=%v", count, crossed)
	}

	count, crossed = trk.Record(base.Add(20 * time.Second))
	if count != 3 || !crossed {
		t.Fatalf("threshold record: want count=3 crossed=true, got count=%d crossed=%v", count, crossed)
	}
}

func TestShockChainTracker_CrossedOnlyFiresOnce(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(300*time.Second, 3)
	base := time.Unix(1_000_000, 0)

	trk.Record(base)
	trk.Record(base.Add(10 * time.Second))
	_, crossed := trk.Record(base.Add(20 * time.Second))
	if !crossed {
		t.Fatalf("expected crossed on 3rd record")
	}

	// 4th activation with same events still in window: count 4, but "crossed"
	// (the edge-transition flag) is false because we already crossed on 3.
	count, crossed := trk.Record(base.Add(30 * time.Second))
	if count != 4 {
		t.Fatalf("4th record: want count=4, got %d", count)
	}
	if crossed {
		t.Fatalf("4th record: crossed should only fire once per threshold crossing")
	}
}

func TestShockChainTracker_WindowPruning(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(60*time.Second, 3)
	base := time.Unix(1_000_000, 0)

	trk.Record(base)
	trk.Record(base.Add(10 * time.Second))

	// Advance well past window. Old entries should age out; count resets.
	count, crossed := trk.Record(base.Add(120 * time.Second))
	if count != 1 {
		t.Fatalf("after window elapsed: want count=1, got %d", count)
	}
	if crossed {
		t.Fatalf("expected no threshold crossing after window reset")
	}
}

func TestShockChainTracker_CrossedReFiresAfterReset(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(300*time.Second, 3)
	base := time.Unix(1_000_000, 0)

	trk.Record(base)
	trk.Record(base.Add(10 * time.Second))
	_, crossed := trk.Record(base.Add(20 * time.Second))
	if !crossed {
		t.Fatalf("expected first crossing")
	}

	trk.Reset()

	// After reset, a fresh cascade should cross again.
	trk.Record(base.Add(30 * time.Second))
	trk.Record(base.Add(40 * time.Second))
	_, crossed = trk.Record(base.Add(50 * time.Second))
	if !crossed {
		t.Fatalf("expected second crossing after reset")
	}
}

func TestShockChainTracker_ThresholdZeroDisablesCrossed(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(300*time.Second, 0)
	base := time.Unix(1_000_000, 0)
	for i := 0; i < 10; i++ {
		_, crossed := trk.Record(base.Add(time.Duration(i) * time.Second))
		if crossed {
			t.Fatalf("threshold=0 must never report crossed (i=%d)", i)
		}
	}
}

func TestShockChainTracker_ConcurrentRecordSafe(t *testing.T) {
	t.Parallel()
	trk := NewShockChainTracker(10*time.Second, 1000)
	base := time.Unix(1_000_000, 0)

	var wg sync.WaitGroup
	const goroutines = 8
	const perGoroutine = 50

	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(offset int) {
			defer wg.Done()
			for i := 0; i < perGoroutine; i++ {
				trk.Record(base.Add(time.Duration(offset+i) * time.Millisecond))
				trk.Count(base.Add(time.Duration(offset+i) * time.Millisecond))
			}
		}(g * perGoroutine)
	}
	wg.Wait()

	got := trk.Count(base.Add(time.Second))
	want := goroutines * perGoroutine
	if got != want {
		t.Fatalf("concurrent record: want count=%d, got %d", want, got)
	}
}

func TestShockChainTracker_NilReceiverSafe(t *testing.T) {
	t.Parallel()
	var trk *ShockChainTracker
	count, crossed := trk.Record(time.Now())
	if count != 0 || crossed {
		t.Fatalf("nil Record: want (0,false), got (%d,%v)", count, crossed)
	}
	if got := trk.Count(time.Now()); got != 0 {
		t.Fatalf("nil Count: want 0, got %d", got)
	}
	trk.Reset() // must not panic
}
