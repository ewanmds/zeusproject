package config

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func writeTestConfig(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	os.WriteFile(path, []byte(content), 0644)
	return path
}

const validConfigYAML = `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

logging:
  verbose: false

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`

func TestPublicDataSourceWithoutQuickNode(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "")
	_, err := Load(writeTestConfig(t, "data_source: public\n"+validConfigYAML))
	if err != nil {
		t.Fatal(err)
	}
}

func TestLoadValidConfig(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}

	if len(cfg.Strategy.Assets) != 2 {
		t.Errorf("expected 2 assets, got %d", len(cfg.Strategy.Assets))
	}
	if cfg.Strategy.SpreadType != "difference" {
		t.Errorf("expected spread_type difference, got %s", cfg.Strategy.SpreadType)
	}
	if cfg.Strategy.LookbackWindow != 500 {
		t.Errorf("expected lookback_window 500, got %d", cfg.Strategy.LookbackWindow)
	}
	if cfg.MarketMaking.QuoteSpreadBps != 50 {
		t.Errorf("expected quote_spread_bps 50, got %g", cfg.MarketMaking.QuoteSpreadBps)
	}
	if cfg.MarketMaking.QuoteNotional != 100 {
		t.Errorf("expected quote_notional 100, got %f", cfg.MarketMaking.QuoteNotional)
	}

	// Verify new defaults applied
	if cfg.MarketMaking.VWAPShortWindowMinutes != 5 {
		t.Errorf("expected vwap_short_window_minutes default 5, got %d", cfg.MarketMaking.VWAPShortWindowMinutes)
	}
	if cfg.MarketMaking.VWAPLongWindowMinutes != 30 {
		t.Errorf("expected vwap_long_window_minutes default 30, got %d", cfg.MarketMaking.VWAPLongWindowMinutes)
	}
	if cfg.MarketMaking.VWAPAlpha != 0.40 {
		t.Errorf("expected vwap_alpha default 0.40, got %f", cfg.MarketMaking.VWAPAlpha)
	}
	if cfg.MarketMaking.SpreadBiasScale != 2.0 {
		t.Errorf("expected spread_bias_scale default 2.0, got %f", cfg.MarketMaking.SpreadBiasScale)
	}
	if cfg.MarketMaking.SpreadBiasMaxBps != 8.0 {
		t.Errorf("expected spread_bias_max_bps default 8.0, got %f", cfg.MarketMaking.SpreadBiasMaxBps)
	}
	if cfg.MarketMaking.ZWideningThreshold != 4.0 {
		t.Errorf("expected z_widening_threshold default 4.0, got %f", cfg.MarketMaking.ZWideningThreshold)
	}
	if cfg.MarketMaking.WickSizeMultiplier != 2.0 {
		t.Errorf("expected wick_size_multiplier default 2.0, got %f", cfg.MarketMaking.WickSizeMultiplier)
	}

	expectedEndpoint := "https://my-node.quiknode.pro/abc123token"
	if cfg.QuickNodeEndpoint != expectedEndpoint {
		t.Errorf("expected QuickNodeEndpoint %q, got %q", expectedEndpoint, cfg.QuickNodeEndpoint)
	}
}

func TestLoadEnvVarPrivateKey(t *testing.T) {
	t.Setenv("WALLET_ADDRESS", "0x1111111111111111111111111111111111111111")
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")
	t.Setenv("PRIVATE_KEY", "0xdeadbeef")

	liveConfig := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: true
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, liveConfig)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}
	if cfg.PrivateKey != "0xdeadbeef" {
		t.Errorf("expected PrivateKey 0xdeadbeef, got %q", cfg.PrivateKey)
	}
}

func TestLiveAccountAddressValidation(t *testing.T) {
	t.Setenv("PRIVATE_KEY", "test-placeholder-not-a-real-key")
	t.Setenv("WALLET_ADDRESS", "")
	content := "data_source: public\n" + strings.Replace(validConfigYAML, "live_mode: false", "live_mode: true", 1)
	if _, err := Load(writeTestConfig(t, content)); err == nil {
		t.Fatal("live must require the trading account address")
	}
	t.Setenv("WALLET_ADDRESS", "not-an-address")
	if _, err := Load(writeTestConfig(t, content)); err == nil {
		t.Fatal("invalid address accepted")
	}
	t.Setenv("WALLET_ADDRESS", "0x1111111111111111111111111111111111111111")
	if _, err := Load(writeTestConfig(t, content)); err != nil {
		t.Fatal(err)
	}
}

func TestValidationMissingAssets(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: []
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for empty assets, got nil")
	}
}

func TestValidationInvalidSpreadType(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "logarithmic"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for invalid spread_type, got nil")
	}
}

func TestValidationLiveModeNoPrivateKey(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")
	t.Setenv("PRIVATE_KEY", "")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: true
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for live_mode without PRIVATE_KEY, got nil")
	}
}

func TestValidationMissingQuickNodeSubdomain(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "")

	path := writeTestConfig(t, validConfigYAML)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for missing QuickNode credentials, got nil")
	}
}

// SkipCredentialCheck lets `bot -validate` verify schema/topology against a
// template config (e.g. config.example.yaml) even when QN creds are empty.
func TestLoadSkipCredentialCheck(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "")

	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path, SkipCredentialCheck())
	if err != nil {
		t.Fatalf("expected Load with SkipCredentialCheck to succeed without creds, got %v", err)
	}
	if cfg.QuickNodeEndpoint != "" {
		t.Errorf("expected empty QuickNodeEndpoint when creds missing, got %q", cfg.QuickNodeEndpoint)
	}
}

// Live-mode private-key requirement is also a credential check — skipping
// should let config.example.yaml validate even with live_mode:true.
func TestLoadSkipCredentialCheckAllowsLiveModeWithoutPrivateKey(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "")
	t.Setenv("PRIVATE_KEY", "")

	liveYAML := strings.Replace(validConfigYAML, "live_mode: false", "live_mode: true", 1)
	path := writeTestConfig(t, liveYAML)
	if _, err := Load(path, SkipCredentialCheck()); err != nil {
		t.Fatalf("expected Load with SkipCredentialCheck to succeed in live mode without private key, got %v", err)
	}
}

func TestLoadFromYAMLEnvSection(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "")

	content := validConfigYAML + `
env:
  quicknode_subdomain: "my-yaml-node.quiknode.pro"
  quicknode_auth_token: "yaml-token-123"
`
	path := writeTestConfig(t, content)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}
	expected := "https://my-yaml-node.quiknode.pro/yaml-token-123"
	if cfg.QuickNodeEndpoint != expected {
		t.Errorf("expected QuickNodeEndpoint %q, got %q", expected, cfg.QuickNodeEndpoint)
	}
}

func TestEnvVarsOverrideYAMLEnvSection(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "env-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "env-token")

	content := validConfigYAML + `
env:
  quicknode_subdomain: "yaml-node.quiknode.pro"
  quicknode_auth_token: "yaml-token"
`
	path := writeTestConfig(t, content)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}
	expected := "https://env-node.quiknode.pro/env-token"
	if cfg.QuickNodeEndpoint != expected {
		t.Errorf("expected env vars to override YAML, got %q", cfg.QuickNodeEndpoint)
	}
}

func TestValidationNegativeQuoteSpread(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: -10
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for negative quote_spread_bps, got nil")
	}
}

func TestVWAPAlphaValidation(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	tests := []struct {
		name  string
		alpha float64
	}{
		{"alpha negative", -0.1},
		{"alpha > 1", 1.5},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			content := fmt.Sprintf(`
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5
  vwap_alpha: %g

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`, tt.alpha)
			path := writeTestConfig(t, content)
			_, err := Load(path)
			if err == nil {
				t.Fatalf("expected error for %s, got nil", tt.name)
			}
		})
	}
}

func TestNewConfigFieldValidation(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	tests := []struct {
		name  string
		field string
		value string
	}{
		{"spread_bias_scale negative", "spread_bias_scale", "-1"},
		{"z_widening_threshold negative", "z_widening_threshold", "-1"},
		{"z_max_width_multiplier < 1", "z_max_width_multiplier", "0.5"},
		{"wick_size_multiplier < 1", "wick_size_multiplier", "0.5"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			content := fmt.Sprintf(`
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5
  %s: %s

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`, tt.field, tt.value)
			path := writeTestConfig(t, content)
			_, err := Load(path)
			if err == nil {
				t.Fatalf("expected error for %s, got nil", tt.name)
			}
		})
	}
}

func TestFeeConfigDefaults(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if cfg.MarketMaking.MakerFeeBps != 0 {
		t.Errorf("expected default maker_fee_bps=0, got %f", cfg.MarketMaking.MakerFeeBps)
	}
	if cfg.MarketMaking.BuilderFeeBps != 0 {
		t.Errorf("expected default builder_fee_bps=0, got %f", cfg.MarketMaking.BuilderFeeBps)
	}
	if cfg.MarketMaking.FeeFloorEnabled != false {
		t.Errorf("expected default fee_floor_enabled=false, got true")
	}
}

func TestFeeConfigExplicit(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")
	t.Setenv("PRIVATE_KEY", "0xdeadbeef")

	yaml := strings.Replace(validConfigYAML,
		"  inventory_skew_factor: 0.5",
		"  inventory_skew_factor: 0.5\n  maker_fee_bps: 0.9\n  builder_fee_bps: 4.0\n  fee_floor_enabled: true",
		1)

	path := writeTestConfig(t, yaml)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if cfg.MarketMaking.MakerFeeBps != 0.9 {
		t.Errorf("expected maker_fee_bps=0.9, got %f", cfg.MarketMaking.MakerFeeBps)
	}
	if cfg.MarketMaking.BuilderFeeBps != 4.0 {
		t.Errorf("expected builder_fee_bps=4.0, got %f", cfg.MarketMaking.BuilderFeeBps)
	}
	if !cfg.MarketMaking.FeeFloorEnabled {
		t.Errorf("expected fee_floor_enabled=true")
	}
}

// --- Per-asset override tests ---

const perAssetConfigYAML = `
strategy:
  assets: ["xyz:NVDA", "xyz:MU"]
  spread_engine_enabled: false
  spread_type: "difference"
  lookback_window: 400

market_making:
  quote_spread_bps: 3
  quote_notional: 350
  refresh_interval_ms: 5000
  inventory_skew_factor: 0.40
  ladder_levels: 4
  ladder_spacing_bps: 3
  max_quote_age_ms: 10000
  max_book_staleness_ms: 15000
  vwap_alpha: 0.50
  wick_dev_threshold_pct: 0.05
  width_max_multiplier: 2.0
  ladder_size_graduation: 1.25
  requote_threshold_bps: 2

  per_asset:
    "xyz:MU":
      quote_spread_bps: 4
      quote_notional: 400
      inventory_skew_factor: 0.45
      ladder_levels: 5
      ladder_spacing_bps: 4
      ladder_size_graduation: 1.30
      requote_threshold_bps: 2
      width_max_multiplier: 2.5
      vwap_alpha: 0.55
      wick_dev_threshold_pct: 0.06

risk:
  max_position_notional: 3500
  max_spread_exposure: 3500
  max_drawdown_pct: 25.0
  max_open_orders: 60

  per_asset:
    "xyz:MU":
      max_position_notional: 3000
      max_spread_exposure: 3000

execution:
  live_mode: false
  paper_starting_equity: 375
  leverage: 12

  per_asset:
    "xyz:MU":
      leverage: 10
`

func TestPerAssetOverrideMerging(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	path := writeTestConfig(t, perAssetConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// NVDA should use global defaults (no overrides).
	nvda := cfg.ForAsset("xyz:NVDA")
	if nvda.MarketMaking.QuoteSpreadBps != 3 {
		t.Errorf("NVDA: expected quote_spread_bps=3, got %g", nvda.MarketMaking.QuoteSpreadBps)
	}
	if nvda.MarketMaking.QuoteNotional != 350 {
		t.Errorf("NVDA: expected quote_notional=350, got %f", nvda.MarketMaking.QuoteNotional)
	}
	if nvda.Execution.Leverage != 12 {
		t.Errorf("NVDA: expected leverage=12, got %d", nvda.Execution.Leverage)
	}
	if nvda.Risk.MaxPositionNotional != 3500 {
		t.Errorf("NVDA: expected max_position_notional=3500, got %f", nvda.Risk.MaxPositionNotional)
	}
	if nvda.MarketMaking.LadderLevels != 4 {
		t.Errorf("NVDA: expected ladder_levels=4, got %d", nvda.MarketMaking.LadderLevels)
	}

	// MU should have overrides applied.
	mu := cfg.ForAsset("xyz:MU")
	if mu.MarketMaking.QuoteSpreadBps != 4 {
		t.Errorf("MU: expected quote_spread_bps=4, got %g", mu.MarketMaking.QuoteSpreadBps)
	}
	if mu.MarketMaking.QuoteNotional != 400 {
		t.Errorf("MU: expected quote_notional=400, got %f", mu.MarketMaking.QuoteNotional)
	}
	if mu.MarketMaking.InventorySkewFactor != 0.45 {
		t.Errorf("MU: expected inventory_skew_factor=0.45, got %f", mu.MarketMaking.InventorySkewFactor)
	}
	if mu.MarketMaking.LadderLevels != 5 {
		t.Errorf("MU: expected ladder_levels=5, got %d", mu.MarketMaking.LadderLevels)
	}
	if mu.MarketMaking.VWAPAlpha != 0.55 {
		t.Errorf("MU: expected vwap_alpha=0.55, got %f", mu.MarketMaking.VWAPAlpha)
	}
	if mu.MarketMaking.WidthMaxMultiplier != 2.5 {
		t.Errorf("MU: expected width_max_multiplier=2.5, got %f", mu.MarketMaking.WidthMaxMultiplier)
	}
	if mu.Execution.Leverage != 10 {
		t.Errorf("MU: expected leverage=10, got %d", mu.Execution.Leverage)
	}
	if mu.Risk.MaxPositionNotional != 3000 {
		t.Errorf("MU: expected max_position_notional=3000, got %f", mu.Risk.MaxPositionNotional)
	}
	if mu.Risk.MaxSpreadExposure != 3000 {
		t.Errorf("MU: expected max_spread_exposure=3000, got %f", mu.Risk.MaxSpreadExposure)
	}

	// Fields NOT overridden should inherit from global.
	if mu.MarketMaking.RefreshIntervalMs != 5000 {
		t.Errorf("MU: expected refresh_interval_ms=5000 (inherited), got %d", mu.MarketMaking.RefreshIntervalMs)
	}
}

func TestPerAssetInvalidKey(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	// per_asset references an asset not in strategy.assets.
	content := `
strategy:
  assets: ["xyz:NVDA"]
  spread_type: "difference"
  lookback_window: 400

market_making:
  quote_spread_bps: 3
  quote_notional: 350
  refresh_interval_ms: 5000
  inventory_skew_factor: 0.40
  max_quote_age_ms: 10000
  max_book_staleness_ms: 15000

  per_asset:
    "xyz:FAKECOIN":
      quote_spread_bps: 5

risk:
  max_position_notional: 3500
  max_spread_exposure: 3500
  max_drawdown_pct: 25.0
  max_open_orders: 60

execution:
  live_mode: false
  paper_starting_equity: 375
  leverage: 12
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for per_asset key not in assets list")
	}
	if !strings.Contains(err.Error(), "xyz:FAKECOIN") {
		t.Fatalf("expected error mentioning FAKECOIN, got: %v", err)
	}
}

func TestPerAssetInvalidOverrideValue(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	// Invalid per-asset leverage.
	content := `
strategy:
  assets: ["xyz:NVDA", "xyz:MU"]
  spread_engine_enabled: false
  spread_type: "difference"
  lookback_window: 400

market_making:
  quote_spread_bps: 3
  quote_notional: 350
  refresh_interval_ms: 5000
  inventory_skew_factor: 0.40
  max_quote_age_ms: 10000
  max_book_staleness_ms: 15000

risk:
  max_position_notional: 3500
  max_spread_exposure: 3500
  max_drawdown_pct: 25.0
  max_open_orders: 60

execution:
  live_mode: false
  paper_starting_equity: 375
  leverage: 12
  per_asset:
    "xyz:MU":
      leverage: 0
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for invalid per-asset leverage=0")
	}
	if !strings.Contains(err.Error(), "xyz:MU") {
		t.Fatalf("expected error mentioning MU, got: %v", err)
	}
}

func TestBackwardsCompatNoPerAsset(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	// Original config with no per_asset — should load identically.
	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Both assets should resolve to global config.
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.QuoteSpreadBps != cfg.MarketMaking.QuoteSpreadBps {
			t.Errorf("%s: spread_bps mismatch: %g vs %g", asset, ac.MarketMaking.QuoteSpreadBps, cfg.MarketMaking.QuoteSpreadBps)
		}
		if ac.Execution.Leverage != cfg.Execution.Leverage {
			t.Errorf("%s: leverage mismatch: %d vs %d", asset, ac.Execution.Leverage, cfg.Execution.Leverage)
		}
		if ac.Risk.MaxPositionNotional != cfg.Risk.MaxPositionNotional {
			t.Errorf("%s: max_position_notional mismatch: %f vs %f", asset, ac.Risk.MaxPositionNotional, cfg.Risk.MaxPositionNotional)
		}
	}
}

func TestForAssetUnknownFallback(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Unknown asset should return global defaults.
	ac := cfg.ForAsset("xyz:UNKNOWN")
	if ac.MarketMaking.QuoteSpreadBps != cfg.MarketMaking.QuoteSpreadBps {
		t.Errorf("expected global spread_bps for unknown asset, got %g", ac.MarketMaking.QuoteSpreadBps)
	}
	if ac.Execution.Leverage != cfg.Execution.Leverage {
		t.Errorf("expected global leverage for unknown asset, got %d", ac.Execution.Leverage)
	}
}

func TestSpreadEngineDisabledWithTwoAssets(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	// Two assets with spread_engine_enabled: false — should not require valid spread_type.
	content := `
strategy:
  assets: ["xyz:NVDA", "xyz:MU"]
  spread_engine_enabled: false
  spread_type: "" # would normally fail validation with 2 assets
  lookback_window: 400

market_making:
  quote_spread_bps: 3
  quote_notional: 350
  refresh_interval_ms: 5000
  inventory_skew_factor: 0.40
  max_quote_age_ms: 10000
  max_book_staleness_ms: 15000

risk:
  max_position_notional: 3500
  max_spread_exposure: 3500
  max_drawdown_pct: 25.0
  max_open_orders: 60

execution:
  live_mode: false
  paper_starting_equity: 375
  leverage: 12
`
	path := writeTestConfig(t, content)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error with spread_engine_enabled=false, got: %v", err)
	}

	// Verify SpreadEngineEnabled is correctly set.
	if cfg.Strategy.SpreadEngineEnabled == nil {
		t.Fatal("expected SpreadEngineEnabled to be non-nil")
	}
	if *cfg.Strategy.SpreadEngineEnabled != false {
		t.Error("expected SpreadEngineEnabled to be false")
	}
}

func TestLadderSpacingDefaults(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	path := writeTestConfig(t, validConfigYAML)
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.MarketMaking.LadderSpacingExponent != 1.0 {
		t.Errorf("expected default ladder_spacing_exponent=1.0, got %f", cfg.MarketMaking.LadderSpacingExponent)
	}
	if cfg.MarketMaking.LadderSpacingDynamic != false {
		t.Errorf("expected default ladder_spacing_dynamic=false, got true")
	}
}

// TestMarketMakingOverrideCoverage uses reflection to verify that every field in
// MarketMakingConfig is either present in MarketMakingOverride (as a per-asset
// overridable pointer field) or explicitly listed in the globalOnly allowlist.
// This catches drift when someone adds a new config field but forgets to add the
// corresponding override or make a conscious decision to keep it global-only.
func TestMarketMakingOverrideCoverage(t *testing.T) {
	// Fields intentionally global-only — not overridable per-asset.
	// Each entry explains WHY the field is global.
	globalOnly := map[string]string{
		"MaxCrossLegSkewMs":   "cross-leg timing is a global coordination parameter",
		"SpreadBiasScale":     "spread engine parameter — z-score bias denominator is global",
		"SpreadBiasMaxBps":    "spread engine parameter — max bias cap is global",
		"ZWideningThreshold":  "z-score widening tied to global spread engine",
		"ZWideningRate":       "z-score widening rate tied to global spread engine",
		"ZMaxWidthMultiplier": "z-score max width cap tied to global spread engine",
		"AdaptiveZEnabled":    "z-score calibration is global (Feature 5)",
		"ZCalibrationWindow":  "z-score calibration window is global (Feature 5)",
		"PerAsset":            "meta-field — the per-asset override map itself",

		"TradingScheduleEnabled": "P12 time-of-day schedule is global (one clock for the whole bot)",
		"TradingSchedule":        "P12 time-of-day schedule is global (one clock for the whole bot)",
	}

	configType := reflect.TypeOf(MarketMakingConfig{})
	overrideType := reflect.TypeOf(MarketMakingOverride{})

	// Build set of override field names.
	overrideFields := make(map[string]bool, overrideType.NumField())
	for i := 0; i < overrideType.NumField(); i++ {
		overrideFields[overrideType.Field(i).Name] = true
	}

	// Check every MarketMakingConfig field.
	var missing []string
	for i := 0; i < configType.NumField(); i++ {
		name := configType.Field(i).Name
		if overrideFields[name] {
			continue
		}
		if _, ok := globalOnly[name]; ok {
			continue
		}
		missing = append(missing, name)
	}

	if len(missing) > 0 {
		t.Errorf("MarketMakingConfig fields not in MarketMakingOverride or globalOnly allowlist:\n")
		for _, name := range missing {
			t.Errorf("  - %s", name)
		}
		t.Errorf("\nFor each field, either:")
		t.Errorf("  1. Add a corresponding *pointer field to MarketMakingOverride (applyOverride handles it via reflection)")
		t.Errorf("  2. Add it to the globalOnly map in this test with a reason why it's not per-asset")
	}

	// Also verify the allowlist doesn't contain stale entries (fields removed from config).
	for name := range globalOnly {
		found := false
		for i := 0; i < configType.NumField(); i++ {
			if configType.Field(i).Name == name {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("globalOnly allowlist contains %q which no longer exists in MarketMakingConfig — remove it", name)
		}
	}
}

func TestLadderSpacingExponentValidation(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	tests := []struct {
		name  string
		value string
		valid bool
	}{
		{"too_low", "0.3", false},
		{"too_high", "4.0", false},
		{"valid_1.3", "1.3", true},
		{"valid_lower_bound", "0.5", true},
		{"valid_upper_bound", "3.0", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			content := fmt.Sprintf(`
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5
  ladder_spacing_exponent: %s

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`, tt.value)
			path := writeTestConfig(t, content)
			_, err := Load(path)
			if tt.valid && err != nil {
				t.Fatalf("expected valid config, got error: %v", err)
			}
			if !tt.valid && err == nil {
				t.Fatalf("expected validation error for exponent=%s, got nil", tt.value)
			}
		})
	}
}

// --- Asset topology validation tests ---

func TestValidationRejectsMoreThanSixAssets(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL", "xyz:GASOIL", "xyz:NATGAS", "xyz:COPPER", "xyz:GOLD", "xyz:SILVER"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for 7 assets, got nil")
	}
	if !strings.Contains(err.Error(), "at most 6 assets") {
		t.Fatalf("expected error about 'at most 6 assets', got: %v", err)
	}
}

func TestValidationAcceptsMixedDexAndPerps(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "BTC"]
  spread_engine_enabled: false
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error for mixed HIP-3 + perps, got: %v", err)
	}
}

func TestValidationRejectsDifferentHIP3Dexes(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "abc:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for different HIP-3 dexes, got nil")
	}
	if !strings.Contains(err.Error(), "multiple HIP-3 dex namespaces") {
		t.Fatalf("expected error about 'multiple HIP-3 dex namespaces', got: %v", err)
	}
}

func TestValidationRejectsDuplicateBaseSymbols(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:CL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for duplicate assets, got nil")
	}
	if !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected error about 'duplicate', got: %v", err)
	}
}

func TestValidationRejectsMalformedAsset(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	// Test "xyz:" — empty base symbol
	content := `
strategy:
  assets: ["xyz:"]
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected error for malformed asset 'xyz:', got nil")
	}
	if !strings.Contains(err.Error(), "malformed") && !strings.Contains(err.Error(), "empty") {
		t.Fatalf("expected error about 'malformed' or 'empty', got: %v", err)
	}

	// Test ":CL" — empty dex prefix with colon
	content2 := `
strategy:
  assets: [":CL"]
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path2 := writeTestConfig(t, content2)
	_, err2 := Load(path2)
	if err2 == nil {
		t.Fatal("expected error for malformed asset ':CL', got nil")
	}
	if !strings.Contains(err2.Error(), "malformed") && !strings.Contains(err2.Error(), "empty") {
		t.Fatalf("expected error about 'malformed' or 'empty' for ':CL', got: %v", err2)
	}
}

func TestValidationAcceptsNonDexAssets(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["BTC"]
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error for non-dex asset 'BTC', got: %v", err)
	}
}

func TestValidationAcceptsValidOneAsset(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL"]
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error for single valid asset, got: %v", err)
	}
}

func TestValidationAcceptsValidTwoAssets(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err != nil {
		t.Fatalf("expected no error for two valid same-dex assets, got: %v", err)
	}
}

// TestValidationFourthDimSessionPnLRequiresWindow verifies that enabling
// optimizer_v2.fourth_dimension="session_pnl" without a positive
// risk.session_pnl_window_minutes yields a validation error (P5).
func TestValidationFourthDimSessionPnLRequiresWindow(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	content := `
strategy:
  assets: ["xyz:CL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5
  optimizer_v2:
    enabled: true
    learning_rate: 0.1
    momentum: 0.5
    emergency_toxicity_threshold: 0.9
    rollback_rate: 0.5
    spread_bps: {min: -5, max: 10}
    notional_mult: {min: 0.3, max: 2.0}
    skew_mult: {min: 0.5, max: 3.0}
    vwap_alpha: {min: 0.1, max: 0.9}
    imbalance_shift_mult: {min: 0.5, max: 2.0}
    fill_decay_mult: {min: 0.5, max: 2.0}
    refresh_mult: {min: 0.5, max: 2.0}
    fourth_dimension: "session_pnl"

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20
  session_pnl_window_minutes: 0

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	path := writeTestConfig(t, content)
	_, err := Load(path)
	if err == nil {
		t.Fatalf("expected validation error, got nil")
	}
	if !strings.Contains(err.Error(), "session_pnl_window_minutes") {
		t.Errorf("expected error to mention session_pnl_window_minutes, got: %v", err)
	}
}

// TestValidationFourthDimSessionPnLBucketEdges verifies edge-list shape/order
// validation (P5).
func TestValidationFourthDimSessionPnLBucketEdges(t *testing.T) {
	t.Setenv("QUICKNODE_SUBDOMAIN", "my-node.quiknode.pro")
	t.Setenv("QUICKNODE_AUTH_TOKEN", "abc123token")

	tmpl := func(edges string) string {
		return `
strategy:
  assets: ["xyz:CL"]
  spread_type: "difference"
  lookback_window: 500

market_making:
  quote_spread_bps: 50
  quote_notional: 100
  refresh_interval_ms: 1000
  inventory_skew_factor: 0.5
  optimizer_v2:
    enabled: true
    learning_rate: 0.1
    momentum: 0.5
    emergency_toxicity_threshold: 0.9
    rollback_rate: 0.5
    spread_bps: {min: -5, max: 10}
    notional_mult: {min: 0.3, max: 2.0}
    skew_mult: {min: 0.5, max: 3.0}
    vwap_alpha: {min: 0.1, max: 0.9}
    imbalance_shift_mult: {min: 0.5, max: 2.0}
    fill_decay_mult: {min: 0.5, max: 2.0}
    refresh_mult: {min: 0.5, max: 2.0}
    fourth_dimension: "session_pnl"
    session_pnl_bucket_edges_bps: ` + edges + `

risk:
  max_position_notional: 500
  max_spread_exposure: 1000
  max_drawdown_pct: 5.0
  max_open_orders: 20
  session_pnl_window_minutes: 60

execution:
  live_mode: false
  paper_starting_equity: 10000
  leverage: 10
`
	}

	// Wrong length.
	path := writeTestConfig(t, tmpl("[-10, 10, 20]"))
	if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "exactly 4") {
		t.Errorf("expected 'exactly 4' error for 3-edge list, got: %v", err)
	}

	// Not strictly ascending.
	path = writeTestConfig(t, tmpl("[-20, -10, -10, 20]"))
	if _, err := Load(path); err == nil || !strings.Contains(err.Error(), "ascending") {
		t.Errorf("expected 'ascending' error, got: %v", err)
	}

	// Valid — should load.
	path = writeTestConfig(t, tmpl("[-40, -20, 20, 40]"))
	if _, err := Load(path); err != nil {
		t.Errorf("expected valid config with custom edges to load, got: %v", err)
	}
}
