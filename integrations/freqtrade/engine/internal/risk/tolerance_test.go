package risk

import (
	"math"
	"testing"
)

func TestPositionHaltCap(t *testing.T) {
	cases := []struct {
		limit, tolerance, want float64
	}{
		{100, 0, 100},
		{100, 10, 110},
		{40, 5, 42},
		{0, 10, 0},
		{100, -5, 100},
	}
	for _, c := range cases {
		if got := positionHaltCap(c.limit, c.tolerance); math.Abs(got-c.want) > 1e-9 {
			t.Fatalf("positionHaltCap(%g,%g)=%g want %g", c.limit, c.tolerance, got, c.want)
		}
	}
}
