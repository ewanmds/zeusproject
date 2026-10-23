package types

import (
	"math"
	"testing"
)

func TestRoundToTickSize(t *testing.T) {
	tests := []struct {
		name     string
		price    float64
		expected float64
	}{
		{"zero", 0, 0},
		{"round_up_CL", 105.99544, 106.00},
		{"round_down_BRENT", 99.37491, 99.375},
		{"small_price", 0.00012345, 0.00012345},
		{"exact_5_sig_figs", 70.000, 70.000},
		{"negative_price", -105.99544, -106.00},
		{"large_price", 12345.6, 12346.0},
		{"trailing_fives", 99.9995, 100.00},
		{"one_sig_fig_boundary", 1.0, 1.0},
		{"sub_dollar", 0.99375, 0.99375},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := RoundToTickSize(tt.price)
			if math.Abs(got-tt.expected) > 1e-10 {
				t.Errorf("RoundToTickSize(%f) = %f, want %f", tt.price, got, tt.expected)
			}
		})
	}
}

func TestParseAsset(t *testing.T) {
	tests := []struct {
		asset    string
		wantDex  string
		wantBase string
	}{
		{"xyz:CL", "xyz", "CL"},
		{"BTC", "", "BTC"},
		{"", "", ""},
		{"a:b:c", "a", "b:c"},
		{":CL", "", "CL"},
	}

	for _, tt := range tests {
		t.Run(tt.asset, func(t *testing.T) {
			dex, base := ParseAsset(tt.asset)
			if dex != tt.wantDex {
				t.Errorf("ParseAsset(%q) dex = %q, want %q", tt.asset, dex, tt.wantDex)
			}
			if base != tt.wantBase {
				t.Errorf("ParseAsset(%q) base = %q, want %q", tt.asset, base, tt.wantBase)
			}
		})
	}
}

func TestRiskActionString(t *testing.T) {
	tests := []struct {
		action   RiskAction
		expected string
	}{
		{RiskResume, "RESUME"},
		{RiskHalt, "HALT"},
		{RiskAction(99), "UNKNOWN"},
	}

	for _, tt := range tests {
		t.Run(tt.expected, func(t *testing.T) {
			got := tt.action.String()
			if got != tt.expected {
				t.Errorf("RiskAction(%d).String() = %q, want %q", tt.action, got, tt.expected)
			}
		})
	}
}
