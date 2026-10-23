package quoting

// OptimizerOverlay holds the current overlay values applied to base config.
type OptimizerOverlay struct {
	SpreadDelta        float64 // added to base spread bps (backward compat: max of per-side deltas)
	SpreadDeltaBuy     float64 // per-side buy spread adjustment (added to base spread for buy side)
	SpreadDeltaSell    float64 // per-side sell spread adjustment (added to base spread for sell side)
	NotionalMult       float64 // multiplied with base notional (1.0 = neutral)
	SkewMult           float64 // multiplied with effective skew factor (1.0 = neutral)
	AlphaDelta         float64 // added to base vwap_alpha
	ImbalanceShiftMult float64 // multiplied with trade imbalance max shift (1.0 = neutral)
	FillDecayMult      float64 // multiplied with fill adverse shift (1.0 = neutral)
	RefreshMult        float64 // multiplied with refresh interval (1.0 = neutral)
}

// neutralOverlay returns the no-op overlay value.
func neutralOverlay() OptimizerOverlay {
	return OptimizerOverlay{
		NotionalMult:       1.0,
		SkewMult:           1.0,
		ImbalanceShiftMult: 1.0,
		FillDecayMult:      1.0,
		RefreshMult:        1.0,
	}
}
