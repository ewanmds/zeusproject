package config

import (
	"reflect"
	"testing"
)

func TestMergeOverrideAppliesNonNilPointers(t *testing.T) {
	type Config struct {
		SpreadBps int
		Notional  float64
		Enabled   bool
	}
	type Override struct {
		SpreadBps *int
		Notional  *float64
		Enabled   *bool
	}

	cfg := Config{SpreadBps: 5, Notional: 100.0, Enabled: false}
	newSpread := 10
	newEnabled := true
	ov := Override{SpreadBps: &newSpread, Enabled: &newEnabled}

	applyOverride(&cfg, &ov)

	if cfg.SpreadBps != 10 {
		t.Errorf("SpreadBps: expected 10, got %d", cfg.SpreadBps)
	}
	if cfg.Notional != 100.0 {
		t.Errorf("Notional: expected 100.0 (unchanged), got %f", cfg.Notional)
	}
	if cfg.Enabled != true {
		t.Errorf("Enabled: expected true, got %v", cfg.Enabled)
	}
}

func TestMergeOverrideSkipsNilPointers(t *testing.T) {
	type Config struct {
		Alpha float64
		Beta  float64
	}
	type Override struct {
		Alpha *float64
		Beta  *float64
	}

	cfg := Config{Alpha: 0.4, Beta: 0.8}
	ov := Override{} // all nil

	applyOverride(&cfg, &ov)

	if cfg.Alpha != 0.4 {
		t.Errorf("Alpha: expected 0.4, got %f", cfg.Alpha)
	}
	if cfg.Beta != 0.8 {
		t.Errorf("Beta: expected 0.8, got %f", cfg.Beta)
	}
}

func TestMergeOverrideIgnoresUnmatchedFields(t *testing.T) {
	type Config struct {
		X int
	}
	type Override struct {
		X *int
		Y *int // not in Config
	}

	cfg := Config{X: 1}
	newX := 2
	newY := 3
	ov := Override{X: &newX, Y: &newY}

	applyOverride(&cfg, &ov)

	if cfg.X != 2 {
		t.Errorf("X: expected 2, got %d", cfg.X)
	}
}

func TestMergeOverrideWorksWithMarketMakingTypes(t *testing.T) {
	cfg := MarketMakingConfig{}
	cfg.QuoteSpreadBps = 5
	cfg.QuoteNotional = 100
	cfg.VWAPAlpha = 0.4

	newSpread := 10.0
	newAlpha := 0.6
	ov := MarketMakingOverride{
		QuoteSpreadBps: &newSpread,
		VWAPAlpha:      &newAlpha,
	}

	applyOverride(&cfg, &ov)

	if cfg.QuoteSpreadBps != 10 {
		t.Errorf("QuoteSpreadBps: expected 10, got %g", cfg.QuoteSpreadBps)
	}
	if cfg.QuoteNotional != 100 {
		t.Errorf("QuoteNotional: expected 100 (unchanged), got %f", cfg.QuoteNotional)
	}
	if cfg.VWAPAlpha != 0.6 {
		t.Errorf("VWAPAlpha: expected 0.6, got %f", cfg.VWAPAlpha)
	}
}

// Test that applyOverride handles a partial MarketMakingOverride correctly.
func TestApplyOverridePartialMarketMaking(t *testing.T) {
	base := MarketMakingConfig{
		QuoteSpreadBps: 5, QuoteNotional: 100, RefreshIntervalMs: 5000,
		VWAPAlpha: 0.4, InventorySkewFactor: 0.3, LadderLevels: 3,
	}

	spread := 10.0
	alpha := 0.6
	levels := 5
	ov := MarketMakingOverride{
		QuoteSpreadBps: &spread,
		VWAPAlpha:      &alpha,
		LadderLevels:   &levels,
	}

	cfg := base
	applyOverride(&cfg, &ov)

	if cfg.QuoteSpreadBps != 10 {
		t.Errorf("QuoteSpreadBps: expected 10, got %g", cfg.QuoteSpreadBps)
	}
	if cfg.QuoteNotional != 100 {
		t.Errorf("QuoteNotional: expected 100 (unchanged), got %f", cfg.QuoteNotional)
	}
	if cfg.RefreshIntervalMs != 5000 {
		t.Errorf("RefreshIntervalMs: expected 5000 (unchanged), got %d", cfg.RefreshIntervalMs)
	}
	if cfg.VWAPAlpha != 0.6 {
		t.Errorf("VWAPAlpha: expected 0.6, got %f", cfg.VWAPAlpha)
	}
	if cfg.InventorySkewFactor != 0.3 {
		t.Errorf("InventorySkewFactor: expected 0.3 (unchanged), got %f", cfg.InventorySkewFactor)
	}
	if cfg.LadderLevels != 5 {
		t.Errorf("LadderLevels: expected 5, got %d", cfg.LadderLevels)
	}
}

// Exhaustive: set every override field and verify all are applied correctly.
func TestApplyOverrideAllMarketMakingFields(t *testing.T) {
	base := MarketMakingConfig{
		QuoteSpreadBps: 1, QuoteNotional: 1.0, RefreshIntervalMs: 1,
		VWAPAlpha: 0.1, InventorySkewFactor: 0.1, LadderLevels: 1,
	}

	// Build an override with every single field set.
	ov := buildFullMarketMakingOverride()

	cfg := base
	applyOverride(&cfg, &ov)

	// Verify that every overrideable field was applied by checking that
	// each override field's pointed-to value is reflected in the config.
	ovVal := reflect.ValueOf(ov)
	ovType := ovVal.Type()
	cfgVal := reflect.ValueOf(cfg)

	for i := 0; i < ovVal.NumField(); i++ {
		ovField := ovVal.Field(i)
		fieldName := ovType.Field(i).Name
		if ovField.Kind() != reflect.Ptr || ovField.IsNil() {
			continue
		}
		cfgField := cfgVal.FieldByName(fieldName)
		if !cfgField.IsValid() {
			continue
		}
		expected := ovField.Elem().Interface()
		actual := cfgField.Interface()
		if !reflect.DeepEqual(expected, actual) {
			t.Errorf("field %s: expected %v, got %v", fieldName, expected, actual)
		}
	}
}

// buildFullMarketMakingOverride returns an override with every field set to a non-zero value.
func buildFullMarketMakingOverride() MarketMakingOverride {
	intVal := func(v int) *int { return &v }
	floatVal := func(v float64) *float64 { return &v }
	boolVal := func(v bool) *bool { return &v }
	strVal := func(v string) *string { return &v }

	arrVal := func(v [4]float64) *[4]float64 { return &v }

	v2Cfg := &OptimizerV2Config{
		Enabled:         true,
		EvalIntervalSec: 99,
		WarmupEvals:     99,
	}

	return MarketMakingOverride{
		QuoteSpreadBps:          floatVal(99.0),
		QuoteSpreadBpsBuy:       floatVal(99.0),
		QuoteSpreadBpsSell:      floatVal(99.0),
		QuoteNotional:           floatVal(99.0),
		RefreshIntervalMs:       intVal(99),
		RefreshPostFillMs:       intVal(99),
		RefreshAdverseMs:        intVal(99),
		RefreshQuietMs:          intVal(99),
		RefreshQuietThresholdMs: intVal(99),
		InventorySkewFactor:     floatVal(0.99),
		InventorySkewGamma:      floatVal(1.99),
		LadderLevels:            intVal(99),
		LadderSpacingBps:        intVal(99),
		DynamicWidth:            boolVal(true),
		WidthVolWindow:          intVal(99),
		WidthMaxMultiplier:      floatVal(9.9),
		WidthImbalanceLevels:    intVal(99),
		RequoteThresholdBps:     intVal(99),
		SizeRequotePct:          floatVal(0.99),
		MaxQuoteAgeMs:           intVal(99),
		MaxBookStalenessMs:      intVal(99),

		VWAPShortWindowMinutes: intVal(99),
		VWAPLongWindowMinutes:  intVal(99),
		VWAPAlpha:              floatVal(0.99),

		WickSizeMultiplier:      floatVal(9.9),
		WickDevThresholdPct:     floatVal(0.99),
		WickLongDevThresholdPct: floatVal(0.99),
		WickMaxInventoryUtil:    floatVal(0.99),
		LadderSizeGraduation:    floatVal(1.99),
		LadderSpacingDynamic:    boolVal(true),
		LadderSpacingExponent:   floatVal(1.99),

		WallFrontEnabled:      boolVal(true),
		WallMinSizeMultiplier: floatVal(9.9),

		MakerFeeBps:     floatVal(0.99),
		BuilderFeeBps:   floatVal(0.99),
		FeeFloorEnabled: boolVal(true),

		TradeImbalanceWindowSec:    intVal(99),
		TradeImbalanceMaxShiftBps:  floatVal(9.9),
		TradeImbalanceSizeExponent: floatVal(1.99),
		TradeImbalanceMode:         strVal("volume"),
		TradeImbalanceBucketSize:   floatVal(9999.0),
		TradeImbalanceBucketCount:  intVal(99),
		FillAdverseShiftBps:        floatVal(9.9),
		FillDecayHalfLifeMs:        intVal(99),
		MicropriceLevels:           intVal(99),
		ZWideningAsymmetry:         floatVal(0.99),
		InventorySkewAsymmetry:     floatVal(0.99),
		CrossLegSkewEnabled:        boolVal(true),
		CrossLegSkewFactor:         floatVal(0.99),
		PairReduceEntryFraction:    floatVal(0.99),
		PairReduceExitFraction:     floatVal(0.99),
		PairReduceTightenFactor:    floatVal(0.99),
		MinCrossingSpreadBps:       floatVal(9.9),
		PairReduceMaxDurationSec:   intVal(99),
		SpreadBiasMaxFraction:      floatVal(0.99),
		SpreadBiasAsymmetry:        floatVal(0.99),
		MaxNoBookUpdateMs:          intVal(99),

		AdaptiveNotionalEnabled:      boolVal(true),
		AdaptiveNotionalTargetPerMin: floatVal(9.9),
		AdaptiveNotionalMin:          floatVal(9.9),
		AdaptiveNotionalMax:          floatVal(99.9),
		AdaptiveNotionalHalfLifeSec:  intVal(99),
		RegimeWindow:                 intVal(99),
		MarkoutEnabled:               boolVal(true),
		MarkoutEWMAHalfLifeSec:       intVal(99),
		WarmupNotionalPct:            floatVal(0.99),
		WarmupSpreadMultiplier:       floatVal(0.99),
		WarmupDurationSec:            intVal(99),
		TradeStaleDegradedSec:        intVal(99),
		TradeStaleHaltSec:            intVal(99),
		FillRefreshCooldownMs:        intVal(99),
		FillBurstWindowSec:           floatVal(9.9),
		FillBurstThreshold:           intVal(99),
		FillBurstSpreadMult:          floatVal(9.9),
		FillBurstSizeFactor:          floatVal(0.99),
		FillBurstDecaySec:            floatVal(9.9),
		VolumeBurstEnabled:           boolVal(true),
		BurstVelocityThreshold:       floatVal(9.9),
		BurstVelocityFastSec:         floatVal(9.9),
		BurstVelocitySlowSec:         floatVal(99.9),
		BurstVelocityMinFloor:        floatVal(0.99),
		BurstLevelMultiplier:         intVal(99),
		BurstDecaySec:                floatVal(99.9),
		ToxicityHalfLifeSec:          intVal(99),
		ToxicityMaxWiden:             floatVal(9.9),
		ToxicityBaselineBps:          floatVal(9.9),

		InventorySkewExitPreserve:   boolVal(true),
		EntryReductionRate:          floatVal(0.99),
		EntryReductionFloor:         floatVal(0.99),
		PositionAgeTightenMinSec:    intVal(99),
		PositionAgeTightenBps:       floatVal(0.99),
		TodSpreadStartHourUTC:       intVal(9),
		TodSpreadEndHourUTC:         intVal(17),
		TodSpreadExtraBps:           floatVal(9.9),
		ToxicityPerSide:             boolVal(true),
		ToxicitySizeReduceThreshold: floatVal(-9.9),
		ToxicitySizeReduceMax:       floatVal(0.99),
		ToxicitySizeExponent:        floatVal(1.99),
		ToxicityIdleDecayHalfLives:  floatVal(9.9),
		ToxicityWarmupFills:         intVal(9),
		ToxicityT5sWeight:           floatVal(0.99),

		MarkoutFVScale:                floatVal(0.99),
		MarkoutFVMaxShiftBps:          floatVal(9.9),
		MaxTotalSpreadMultiplier:      floatVal(9.9),
		RateLimitAllocatorEnabled:     boolVal(true),
		RateLimitROIEWMAHalfLifeSec:   intVal(99),
		RateLimitMinRefreshMultiplier: floatVal(0.99),
		RateLimitMaxRefreshMultiplier: floatVal(9.9),
		MarkoutCompositeWeights:       arrVal([4]float64{0.1, 0.2, 0.3, 0.4}),
		MicropriceDecay:               floatVal(0.99),
		EmergencyWidenThresholdBps:    floatVal(-9.9),
		EmergencyWidenAmountBps:       floatVal(9.9),
		QuietLadderReduceSec:          intVal(99),
		LazyLadderEnabled:             boolVal(true),
		LazyLadderDecaySec:            intVal(99),
		ToxicitySizeWeightCap:         floatVal(9.9),
		VelocityInitialSeed:           floatVal(9.9),

		MaxFVDeviationPct:            floatVal(9.9),
		FundingUrgencyEnabled:        boolVal(true),
		FundingUrgencyScale:          floatVal(1.99),
		FundingUrgencyHorizonMinutes: floatVal(29.9),
		FundingPollIntervalSec:       intVal(99),
		FundingIntervalHours:         floatVal(1.0),

		InventoryHalfLifeMinutes:   floatVal(9.9),
		InventoryControllerSkewMin: floatVal(0.99),
		InventoryControllerSkewMax: floatVal(9.9),
		InventoryControllerGainP:   floatVal(0.99),
		InventoryControllerGainI:   floatVal(0.99),
		InventoryControllerGainD:   floatVal(0.99),

		// V21/V24/V30: price-shock guard + favorable branch overrides.
		PriceShockEnabled:                   boolVal(true),
		PriceShockWindowMs:                  intVal(99),
		PriceShockThresholdBps:              floatVal(9.9),
		PriceShockCooldownMs:                intVal(99),
		PriceShockFreezeEntry:               boolVal(true),
		PriceShockExitSizeFactor:            floatVal(0.99),
		PriceShockFavorableExitFactor:       floatVal(1.99),
		PriceShockFavorableExitTightenBps:   floatVal(1.99),
		PriceShockFavorableIocFrac:          floatVal(0.99),
		PriceShockFavorableIocMinInvUtil:    floatVal(0.99),
		PriceShockFavorableIocMinCaptureBps: floatVal(9.9),
		PriceShockFavorableIocSlippageBps:   floatVal(9.9),

		// V30 part 3: shock-chain overrides.
		ShockChainEnabled:     boolVal(true),
		ShockChainWindowSec:   intVal(99),
		ShockChainThreshold:   intVal(9),
		ShockChainPauseSec:    intVal(99),
		ShockChainRecoverySec: intVal(99),

		OptimizerV2: v2Cfg,
	}
}

func TestApplyOverrideWorksWithExecutionTypes(t *testing.T) {
	cfg := ExecutionConfig{Leverage: 5, LiveMode: true}
	newLev := 10
	ov := ExecutionOverride{Leverage: &newLev}

	cfg2 := cfg
	applyOverride(&cfg2, &ov)

	if cfg2.Leverage != 10 {
		t.Errorf("Leverage: expected 10, got %d", cfg2.Leverage)
	}
	if cfg2.LiveMode != true {
		t.Errorf("LiveMode: expected true (unchanged), got %v", cfg2.LiveMode)
	}
}

func TestApplyOverrideWorksWithRiskTypes(t *testing.T) {
	cfg := RiskConfig{MaxPositionNotional: 1000, MaxSpreadExposure: 500, MaxOpenOrders: 10}
	newPos := 2000.0
	newOrders := 20
	ov := RiskOverride{MaxPositionNotional: &newPos, MaxOpenOrders: &newOrders}

	cfg2 := cfg
	applyOverride(&cfg2, &ov)

	if cfg2.MaxPositionNotional != 2000.0 {
		t.Errorf("MaxPositionNotional: expected 2000, got %f", cfg2.MaxPositionNotional)
	}
	if cfg2.MaxSpreadExposure != 500 {
		t.Errorf("MaxSpreadExposure: expected 500 (unchanged), got %f", cfg2.MaxSpreadExposure)
	}
	if cfg2.MaxOpenOrders != 20 {
		t.Errorf("MaxOpenOrders: expected 20, got %d", cfg2.MaxOpenOrders)
	}
}
