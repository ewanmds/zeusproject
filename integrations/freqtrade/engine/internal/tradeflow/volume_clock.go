package tradeflow

import (
	"math"
	"sync"
	"time"
)

type volumeBucket struct {
	buyVol  float64
	sellVol float64
}

// VolumeClockCalculator implements ImbalanceCalculator using VPIN-lite
// volume-clock buckets instead of time windows. Trades are grouped into
// equal-notional volume buckets; imbalance is computed over completed buckets.
type VolumeClockCalculator struct {
	bucketSize   float64 // notional per bucket (e.g., $5000)
	numBuckets   int     // max completed buckets to keep
	sizeExponent float64 // power-law trade size weighting

	buckets      []volumeBucket // circular buffer of completed buckets
	writeIdx     int
	count        int          // completed buckets in buffer
	current      volumeBucket // in-progress bucket
	currentTotal float64      // buyVol + sellVol in current

	// Saturation ring buffer (same pattern as Calculator).
	saturation saturationRing

	mu sync.RWMutex
}

// NewVolumeClockCalculator creates a volume-clock imbalance calculator.
// bucketSize is the notional per volume bucket (e.g. 5000 for $5000).
// numBuckets is the number of completed buckets retained in the circular buffer.
// NOTE: When sizeExponent != 1.0, trade notionals are transformed (pow(notional, exp))
// before accumulation. bucketSize must be calibrated to the chosen exponent — e.g.,
// with exponent=1.2 a $5000 trade produces pow(5000,1.2) ≈ 19307 weighted units.
// sizeExponent is the power-law trade size weighting (1.0 = raw).
func NewVolumeClockCalculator(bucketSize float64, numBuckets int, sizeExponent float64) *VolumeClockCalculator {
	if sizeExponent <= 0 {
		sizeExponent = 1.0
	}
	if numBuckets <= 0 {
		numBuckets = 20
	}
	if bucketSize <= 0 {
		bucketSize = 5000.0
	}
	return &VolumeClockCalculator{
		bucketSize:   bucketSize,
		numBuckets:   numBuckets,
		sizeExponent: sizeExponent,
		buckets:      make([]volumeBucket, numBuckets),
	}
}

// AddTrade records a trade. size should be notional (price * qty) in volume mode.
// The side is "buy" or "sell". ts is ignored (volume-clock is not time-based).
func (v *VolumeClockCalculator) AddTrade(size float64, side string, _ time.Time) {
	v.mu.Lock()
	defer v.mu.Unlock()

	weighted := math.Pow(size, v.sizeExponent)
	remaining := weighted

	for remaining > 0 {
		space := v.bucketSize - v.currentTotal
		chunk := remaining
		if chunk > space {
			chunk = space
		}

		if side == "buy" {
			v.current.buyVol += chunk
		} else {
			v.current.sellVol += chunk
		}
		v.currentTotal += chunk
		remaining -= chunk

		// If bucket is full, push to circular buffer.
		if v.currentTotal >= v.bucketSize {
			v.buckets[v.writeIdx] = v.current
			v.writeIdx = (v.writeIdx + 1) % v.numBuckets
			if v.count < v.numBuckets {
				v.count++
			}
			v.current = volumeBucket{}
			v.currentTotal = 0
		}
	}
}

// imbalanceUnlocked computes imbalance without acquiring lock.
// Caller must hold at least v.mu.RLock().
func (v *VolumeClockCalculator) imbalanceUnlocked() (float64, bool) {
	if v.count == 0 {
		return 0, false
	}

	var totalBuy, totalSell float64
	n := v.count
	// Read the last 'count' entries from the circular buffer.
	for i := 0; i < n; i++ {
		idx := (v.writeIdx - n + i + v.numBuckets) % v.numBuckets
		totalBuy += v.buckets[idx].buyVol
		totalSell += v.buckets[idx].sellVol
	}

	total := totalBuy + totalSell
	if total <= 0 {
		return 0, false
	}

	imb := (totalBuy - totalSell) / total
	// Defensive clamp.
	if imb > 1 {
		imb = 1
	} else if imb < -1 {
		imb = -1
	}
	return imb, true
}

// Imbalance returns the buy/sell imbalance across all completed volume buckets.
// Returns (0, false) if no buckets have been completed yet.
func (v *VolumeClockCalculator) Imbalance() (float64, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	return v.imbalanceUnlocked()
}

// ImbalanceAndRecord is like Imbalance but also records saturation status
// into the ring buffer. Uses a write lock since it mutates ring buffer state.
func (v *VolumeClockCalculator) ImbalanceAndRecord() (float64, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()

	imb, ready := v.imbalanceUnlocked()
	if !ready {
		return 0, false
	}

	v.saturation.Record(imb > 0.95 || imb < -0.95)
	return imb, true
}

// SaturationRatio returns the fraction of recent imbalance reads that were
// at the +/-cap (|imbalance| > 0.95). Returns 0 if no readings recorded.
func (v *VolumeClockCalculator) SaturationRatio() float64 {
	v.mu.RLock()
	defer v.mu.RUnlock()
	return v.saturation.Fraction()
}
