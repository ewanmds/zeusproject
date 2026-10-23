package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"hyperliquid-market-maker/internal/config"
	"hyperliquid-market-maker/internal/execution"
	"hyperliquid-market-maker/internal/feed"
	"hyperliquid-market-maker/internal/quoting"
	"hyperliquid-market-maker/internal/risk"
	"hyperliquid-market-maker/internal/spread"
	"hyperliquid-market-maker/internal/tradeflow"
	"hyperliquid-market-maker/internal/types"
	"hyperliquid-market-maker/internal/vwap"
	"hyperliquid-market-maker/internal/zeusbridge"

	"github.com/quiknode-labs/hyperliquid-sdk/go/hyperliquid"
)

// Set via -ldflags at build time.
var gitCommit string

func main() {
	configPath := flag.String("config", "config.yaml", "path to config file")
	validateOnly := flag.Bool("validate", false, "validate config and exit")
	checkAccount := flag.Bool("check-account", false, "read-only API wallet and account check; never starts trading")
	zeusSession := flag.String("zeus-session", "", "Zeus protocol session ID")
	zeusSymbol := flag.String("zeus-symbol", "", "Zeus qualified symbol")
	zeusStop := flag.String("zeus-stop-file", "", "Zeus cooperative stop sentinel")
	flag.Parse()
	var bridgeDone chan struct{}
	var bridge *zeusbridge.State
	if *zeusSession != "" {
		bridge = zeusbridge.Configure(*zeusSession, *zeusSymbol)
		slog.SetDefault(slog.New(zeusbridge.ErrorHandler{State: bridge}))
	}

	// Load config. In -validate mode, skip credential checks so schemas can
	// be verified offline (e.g. config.example.yaml with empty env).
	var loadOpts []config.LoadOption
	if *validateOnly {
		loadOpts = append(loadOpts, config.SkipCredentialCheck())
	}
	cfg, err := config.Load(*configPath, loadOpts...)
	if err != nil {
		slog.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	if *validateOnly {
		fmt.Printf("config %s: OK\n", *configPath)
		return
	}

	if bridge != nil {
		if !cfg.Execution.LiveMode || len(cfg.Strategy.Assets) != 1 || cfg.Strategy.Assets[0] != *zeusSymbol {
			slog.Error("invalid Zeus live session")
			os.Exit(1)
		}
		cfg.ZeusMode = true
	}
	// Configure logging.
	logLevel := slog.LevelInfo
	if cfg.Logging.Verbose {
		logLevel = slog.LevelDebug
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: logLevel}))
	if bridge == nil {
		slog.SetDefault(logger)
	}

	mode := "PAPER"
	if cfg.Execution.LiveMode {
		mode = "LIVE"
	}

	startupFields := []any{
		"mode", mode,
		"assets", cfg.Strategy.Assets,
		"max_drawdown_pct", cfg.Risk.MaxDrawdownPct,
	}
	if gitCommit != "" {
		startupFields = append(startupFields, "commit", gitCommit)
	}
	slog.Info("starting hyperliquid market maker", startupFields...)

	// Log per-asset resolved config (full dump for post-session audit).
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		mm := ac.MarketMaking
		r := ac.Risk
		ex := ac.Execution

		slog.Info("asset config",
			"coin", asset,
			"spread_bps", mm.QuoteSpreadBps,
			"spread_bps_buy", mm.QuoteSpreadBpsBuy,
			"spread_bps_sell", mm.QuoteSpreadBpsSell,
			"notional", mm.QuoteNotional,
			"vwap_alpha", mm.VWAPAlpha,
			"vwap_alpha_drift_scaling_enabled", mm.VWAPAlphaDriftScalingEnabled,
			"vwap_alpha_drift_k", mm.VWAPAlphaDriftK,
			"vwap_alpha_floor", mm.VWAPAlphaFloor,
			"vwap_alpha_trend_boost_enabled", mm.VWAPAlphaTrendBoostEnabled,
			"vwap_alpha_trend_boost_ceiling", mm.VWAPAlphaTrendBoostCeiling,
			"trend_fv_drift_bps", mm.TrendFVDriftBps,
			"trend_fv_drift_autocorr_min", mm.TrendFVDriftAutocorrMin,
			"leverage", ex.Leverage,
			"max_position_notional", r.MaxPositionNotional,
		)
		slog.Info("asset config: quoting",
			"coin", asset,
			"refresh_ms", mm.RefreshIntervalMs,
			"refresh_post_fill_ms", mm.RefreshPostFillMs,
			"refresh_adverse_ms", mm.RefreshAdverseMs,
			"refresh_quiet_ms", mm.RefreshQuietMs,
			"refresh_quiet_threshold_ms", mm.RefreshQuietThresholdMs,
			"ladder_levels", mm.LadderLevels,
			"ladder_spacing_bps", mm.LadderSpacingBps,
			"ladder_graduation", mm.LadderSizeGraduation,
			"ladder_spacing_exp", mm.LadderSpacingExponent,
			"ladder_spacing_dynamic", mm.LadderSpacingDynamic,
			"lazy_ladder_enabled", mm.LazyLadderEnabled,
			"lazy_ladder_decay_sec", mm.LazyLadderDecaySec,
			"requote_threshold_bps", mm.RequoteThresholdBps,
			"max_quote_age_ms", mm.MaxQuoteAgeMs,
			"dynamic_width", mm.DynamicWidth,
			"width_max_mult", mm.WidthMaxMultiplier,
		)
		slog.Info("asset config: skew & wick",
			"coin", asset,
			"skew_factor", mm.InventorySkewFactor,
			"skew_gamma", mm.InventorySkewGamma,
			"skew_asymmetry", mm.InventorySkewAsymmetry,
			"entry_reduction_rate", mm.EntryReductionRate,
			"entry_reduction_floor", mm.EntryReductionFloor,
			"entry_accel_threshold", mm.EntryAccelThreshold,
			"entry_accel_exponent", mm.EntryAccelExponent,
			"entry_reduction_trend_k", mm.EntryReductionTrendK,
			"trend_guard_enabled", mm.TrendGuardEnabled,
			"trend_guard_threshold_pct", mm.TrendGuardThresholdPct,
			"trend_guard_saturation_pct", mm.TrendGuardSaturationPct,
			"trend_guard_max_suppress", mm.TrendGuardMaxSuppress,
			"trend_guard_spread_widen_bps", mm.TrendGuardSpreadWidenBps,
			"trend_guard_spread_widen_asymmetry", mm.TrendGuardSpreadWidenAsymmetry,
			"trend_guard_freeze_threshold", mm.TrendGuardFreezeThreshold,
			"regime_gate_enabled", mm.RegimeGateEnabled,
			"regime_gate_mode", mm.RegimeGateMode,
			"regime_autocorr_threshold", mm.RegimeAutocorrThreshold,
			"regime_vwap_divergence_sigma", mm.RegimeVWAPDivergenceSigma,
			"regime_gate_min_duration_sec", mm.RegimeGateMinDurationSec,
			"regime_gate_max_suppression", mm.RegimeGateMaxSuppression,
			"flow_gate_enabled", mm.FlowGateEnabled,
			"flow_gate_threshold", mm.FlowGateThreshold,
			"flow_gate_max_suppress", mm.FlowGateMaxSuppress,
			"flow_gate_min_util", mm.FlowGateMinUtil,
			"price_shock_enabled", mm.PriceShockEnabled,
			"price_shock_window_ms", mm.PriceShockWindowMs,
			"price_shock_threshold_bps", mm.PriceShockThresholdBps,
			"price_shock_cooldown_ms", mm.PriceShockCooldownMs,
			"price_shock_freeze_entry", mm.PriceShockFreezeEntry,
			"price_shock_exit_size_factor", mm.PriceShockExitSizeFactor,
			"price_shock_favorable_exit_factor", mm.PriceShockFavorableExitFactor,
			"price_shock_favorable_exit_tighten_bps", mm.PriceShockFavorableExitTightenBps,
			"price_shock_favorable_ioc_frac", mm.PriceShockFavorableIocFrac,
			"price_shock_favorable_ioc_min_inv_util", mm.PriceShockFavorableIocMinInvUtil,
			"price_shock_favorable_ioc_min_capture_bps", mm.PriceShockFavorableIocMinCaptureBps,
			"price_shock_favorable_ioc_slippage_bps", mm.PriceShockFavorableIocSlippageBps,
			"shock_chain_enabled", mm.ShockChainEnabled,
			"shock_chain_window_sec", mm.ShockChainWindowSec,
			"shock_chain_threshold", mm.ShockChainThreshold,
			"shock_chain_pause_sec", mm.ShockChainPauseSec,
			"shock_chain_recovery_sec", mm.ShockChainRecoverySec,
			"position_age_tighten_min_sec", mm.PositionAgeTightenMinSec,
			"position_age_tighten_bps", mm.PositionAgeTightenBps,
			"idle_fill_detector_enabled", mm.IdleFillDetectorEnabled,
			"idle_fill_threshold_sec", mm.IdleFillThresholdSec,
			"idle_fill_min_inv_util", mm.IdleFillMinInvUtil,
			"idle_exit_tighten_bps", mm.IdleExitTightenBps,
			"idle_exit_size_multiplier", mm.IdleExitSizeMultiplier,
			"idle_fill_retry_interval_sec", mm.IdleFillRetryIntervalSec,
			"tod_spread_start_utc", mm.TodSpreadStartHourUTC,
			"tod_spread_end_utc", mm.TodSpreadEndHourUTC,
			"tod_spread_extra_bps", mm.TodSpreadExtraBps,
			"wick_size_mult", mm.WickSizeMultiplier,
			"wick_dev_pct", mm.WickDevThresholdPct,
			"wick_long_dev_pct", mm.WickLongDevThresholdPct,
			"wick_trend_suppress_enabled", mm.WickTrendSuppressEnabled,
			"wick_trend_suppress_threshold", mm.WickTrendSuppressThreshold,
			"wall_quality_enabled", mm.WallQualityEnabled,
			"wall_quality_half_life_sec", mm.WallQualityHalfLifeSec,
			"wall_quality_min_score", mm.WallQualityMinScore,
			"wall_quality_disable_sec", mm.WallQualityDisableSec,
			"wall_quality_hysteresis_sec", mm.WallQualityHysteresisSec,
			"wall_quality_reenable_threshold_bps", mm.WallQualityReenableThresholdBps,
			"quote_attribution_enabled", mm.QuoteAttributionEnabled,
			"quote_attribution_interval_sec", mm.QuoteAttributionMinIntervalSec,
		)
		slog.Info("asset config: adaptive",
			"coin", asset,
			"notional_enabled", mm.AdaptiveNotionalEnabled,
			"notional_target/min", mm.AdaptiveNotionalTargetPerMin,
			"notional_min", mm.AdaptiveNotionalMin,
			"notional_max", mm.AdaptiveNotionalMax,
			"notional_half_life_sec", mm.AdaptiveNotionalHalfLifeSec,
		)
		slog.Info("asset config: protection",
			"coin", asset,
			"warmup_sec", mm.WarmupDurationSec,
			"warmup_notional_pct", mm.WarmupNotionalPct,
			"warmup_spread_mult", mm.WarmupSpreadMultiplier,
			"fill_cooldown_ms", mm.FillRefreshCooldownMs,
			"tox_half_life", mm.ToxicityHalfLifeSec,
			"tox_max_widen", mm.ToxicityMaxWiden,
			"tox_warmup_fills", mm.ToxicityWarmupFills,
			"tox_size_exponent", mm.ToxicitySizeExponent,
			"fill_adverse_shift_bps", mm.FillAdverseShiftBps,
			"trade_imb_size_exp", mm.TradeImbalanceSizeExponent,
			"trade_imb_mode", mm.TradeImbalanceMode,
			"trade_imb_bucket_size", mm.TradeImbalanceBucketSize,
			"trade_imb_bucket_count", mm.TradeImbalanceBucketCount,
			"trade_stale_degraded_sec", mm.TradeStaleDegradedSec,
			"trade_stale_halt_sec", mm.TradeStaleHaltSec,
			"recon_grace_ms", ex.ReconGracePeriodMs,
			"recon_empty_exchange_grace_ms", ex.ReconEmptyExchangeGraceMs,
			"poll_equity_sec", ex.PollEquitySec,
			"poll_open_orders_sec", ex.PollOpenOrdersSec,
			"poll_fills_sec", ex.PollFillsSec,
			"poll_rate_limit_sec", ex.PollRateLimitSec,
			"min_crossing_spread_bps", mm.MinCrossingSpreadBps,
			"max_fv_deviation_pct", mm.MaxFVDeviationPct,
			"recon_diag_enabled", ex.ReconMismatchDiagnosticEnabled,
			"recon_diag_max_per_hour", ex.ReconMismatchDiagnosticMaxPerHour,
		)
		slog.Info("asset config: per-side winrate gate",
			"coin", asset,
			"per_side_winrate_enabled", mm.PerSideWinrateEnabled,
			"per_side_winrate_window_fills", mm.PerSideWinrateWindowFills,
			"per_side_winrate_threshold", mm.PerSideWinrateThreshold,
			"per_side_profit_factor_threshold", mm.PerSideProfitFactorThreshold,
			"per_side_winrate_widen_bps", mm.PerSideWinrateWidenBps,
		)
		slog.Info("asset config: fill burst & inventory controller",
			"coin", asset,
			"fill_burst_threshold", mm.FillBurstThreshold,
			"fill_burst_spread_mult", mm.FillBurstSpreadMult,
			"fill_burst_size_factor", mm.FillBurstSizeFactor,
			"fill_burst_decay_sec", mm.FillBurstDecaySec,
			"inv_half_life_min", mm.InventoryHalfLifeMinutes,
			"inv_controller_gain_p", mm.InventoryControllerGainP,
			"inv_controller_gain_i", mm.InventoryControllerGainI,
			"inv_controller_gain_d", mm.InventoryControllerGainD,
			"inv_controller_skew_min", mm.InventoryControllerSkewMin,
			"inv_controller_skew_max", mm.InventoryControllerSkewMax,
		)
		slog.Info("asset config: funding urgency",
			"coin", asset,
			"funding_urgency_enabled", mm.FundingUrgencyEnabled,
			"funding_urgency_scale", mm.FundingUrgencyScale,
			"funding_urgency_horizon_min", mm.FundingUrgencyHorizonMinutes,
			"funding_poll_interval_sec", mm.FundingPollIntervalSec,
			"funding_interval_hours", mm.FundingIntervalHours,
		)
		slog.Info("asset config: risk",
			"coin", asset,
			"max_position_notional", r.MaxPositionNotional,
			"max_spread_exposure", r.MaxSpreadExposure,
			"max_aggregate_exposure", cfg.Risk.MaxAggregateExposure,
			"max_drawdown_pct", r.MaxDrawdownPct,
			"absolute_equity_floor", r.AbsoluteEquityFloor,
			"max_open_orders", r.MaxOpenOrders,
			"drawdown_cooldown_sec", r.DrawdownCooldownSec,
			"max_consecutive_halts", r.MaxConsecutiveHalts,
			"recovery_sec", r.RecoveryDurationSec,
			"recovery_notional_pct", r.RecoveryNotionalPct,
			"recovery_spread_mult", r.RecoverySpreadMultiplier,
			"session_stop_loss_bps", cfg.Risk.SessionStopLossBps,
			"session_stop_loss_pause_sec", cfg.Risk.SessionStopLossPauseSec,
			"session_stop_loss_recovery_notional_pct", cfg.Risk.SessionStopLossRecoveryNotionalPct,
			"session_stop_loss_recovery_duration_sec", cfg.Risk.SessionStopLossRecoveryDurationSec,
			"session_stop_loss_warn_bps", cfg.Risk.SessionStopLossWarnBps,
			"session_stop_loss_warn_spread_mult", cfg.Risk.SessionStopLossWarnSpreadMult,
			"session_stop_loss_warn_notional_pct", cfg.Risk.SessionStopLossWarnNotionalPct,
			"session_flatten_on_shutdown", cfg.Risk.SessionFlattenOnShutdown,
			"session_flatten_timeout_sec", cfg.Risk.SessionFlattenTimeoutSec,
		)
	}

	// Initialize SDK.
	infoEndpoint := "https://api.hyperliquid.xyz"
	var sdk *hyperliquid.SDK
	if cfg.Execution.LiveMode {
		sdk, err = hyperliquid.New(infoEndpoint,
			hyperliquid.WithPrivateKey(cfg.PrivateKey),
			hyperliquid.WithAccountAddress(cfg.WalletAddress),
			hyperliquid.WithAutoApprove(false),
			hyperliquid.WithTimeout(5*time.Second),
			hyperliquid.WithDirectExchange(),
		)
	} else {
		sdk, err = hyperliquid.New(infoEndpoint, hyperliquid.WithAutoApprove(false), hyperliquid.WithDirectExchange())
	}
	if err != nil {
		slog.Error("failed to initialize SDK", "error", err)
		os.Exit(1)
	}
	defer sdk.Close()
	if bridge != nil {
		sdk.SetNativeObserver(zeusbridge.RecordAction)
	}
	if cfg.Execution.LiveMode || *checkAccount {
		if err := checkTradingAccount(sdk, cfg); err != nil {
			slog.Error("account check failed", "error", err)
			os.Exit(1)
		}
		if *checkAccount {
			slog.Info("read-only account check passed; no trading started")
			return
		}
	}

	// Fetch market metadata for szDecimals.
	szDecimals := make(map[string]int)
	var markets *hyperliquid.Markets
	if cfg.DataSource == "public" {
		markets, err = feed.PublicMarkets(infoEndpoint, cfg.Strategy.Assets)
	} else {
		markets, err = sdk.Markets()
	}
	if err != nil {
		slog.Warn("failed to fetch markets metadata, using default szDecimals=2", "error", err)
		for _, asset := range cfg.Strategy.Assets {
			szDecimals[asset] = 2
		}
	} else {
		// Search Perps markets.
		for _, m := range markets.Perps {
			for _, asset := range cfg.Strategy.Assets {
				if m.Name == asset {
					szDecimals[asset] = m.SzDecimals
					slog.Info("market metadata", "asset", asset, "szDecimals", m.SzDecimals, "source", "perps")
				}
			}
		}
		// Search Spot markets.
		for _, m := range markets.Spot {
			for _, asset := range cfg.Strategy.Assets {
				if _, found := szDecimals[asset]; !found && m.Name == asset {
					szDecimals[asset] = m.SzDecimals
					slog.Info("market metadata", "asset", asset, "szDecimals", m.SzDecimals, "source", "spot")
				}
			}
		}
		// Search HIP3 markets (e.g., "xyz:CL", "xyz:BRENTOIL").
		// API returns base names ("CL") keyed by dex ("xyz"), so reconstruct full name.
		for dex, hip3Markets := range markets.HIP3 {
			for _, m := range hip3Markets {
				fullName := dex + ":" + m.Name
				for _, asset := range cfg.Strategy.Assets {
					if _, found := szDecimals[asset]; !found && fullName == asset {
						szDecimals[asset] = m.SzDecimals
						slog.Info("market metadata", "asset", asset, "szDecimals", m.SzDecimals, "source", "hip3")
					}
				}
			}
		}
		// Fallback for assets not found in any market list.
		for _, asset := range cfg.Strategy.Assets {
			if _, ok := szDecimals[asset]; !ok {
				slog.Warn("market not found in metadata, using default szDecimals=2", "asset", asset)
				szDecimals[asset] = 2
			}
		}
	}

	// Check rate limit before proceeding (live mode only).
	// Save boot-time values to seed the quote manager later.
	var bootRLRemaining, bootRLCap int
	if cfg.RateLimit.Disabled {
		slog.Warn("local quota blocking disabled; exchange quota still applies")
	}
	if cfg.Execution.LiveMode {
		if rl, err := sdk.Info().UserRateLimit(sdk.Address()); err == nil {
			cap, _ := rl["nRequestsCap"].(float64)
			used, _ := rl["nRequestsUsed"].(float64)
			remaining := cap - used
			bootRLRemaining = int(remaining)
			bootRLCap = int(cap)
			slog.Info("rate limit status",
				"cap", int(cap),
				"used", int(used),
				"remaining", int(remaining),
			)
			if remaining <= 0 && !cfg.RateLimit.Disabled {
				slog.Error("FATAL: API request allowance exhausted — exchange will reject all actions",
					"cap", int(cap),
					"used", int(used),
					"hint", "trade more volume to increase cap, or wait for throttle recovery (1 req/10s)",
				)
				os.Exit(1)
			}
			if remaining < 500 {
				slog.Warn("API request allowance running low",
					"remaining", int(remaining),
					"cap", int(cap),
				)
			}
		} else {
			slog.Warn("could not check rate limit", "error", err)
		}
	}

	// Query exchange metadata for max leverage per asset (used to clamp below).
	maxLeverages := make(map[string]int)
	if cfg.Execution.LiveMode {
		if meta, err := sdk.Info().Meta(); err == nil {
			if universe, ok := meta["universe"].([]any); ok {
				for _, entry := range universe {
					m, ok := entry.(map[string]any)
					if !ok {
						continue
					}
					name, _ := m["name"].(string)
					if maxLev, ok := m["maxLeverage"].(float64); ok && maxLev > 0 {
						maxLeverages[name] = int(maxLev)
					}
				}
			}
			slog.Info("fetched exchange metadata for leverage limits", "assets_with_limits", len(maxLeverages))
		} else {
			slog.Warn("could not fetch exchange metadata for leverage limits, using configured values", "error", err)
		}
	}

	// Set leverage on each asset (live mode only).
	// SDK resolveAssetIndex matches against API names which omit the dex prefix,
	// so pass the base name (e.g. "CL" not "xyz:CL").
	// HIP-3 assets prefer isolated margin, but if an existing position is on cross
	// margin the exchange rejects the mode switch. On LEVERAGE_CONFLICT, retry
	// without specifying a margin mode (just set the leverage value).
	if cfg.Execution.LiveMode {
		for _, asset := range cfg.Strategy.Assets {
			ac := cfg.ForAsset(asset)
			dexName, base := types.ParseAsset(asset)

			// Clamp leverage to exchange maximum if known.
			targetLev := ac.Execution.Leverage
			if maxLev, ok := maxLeverages[base]; ok && targetLev > maxLev {
				slog.Warn("clamping leverage to exchange maximum",
					"asset", asset, "configured", targetLev, "max", maxLev)
				targetLev = maxLev
			}
			var levOpts []hyperliquid.LeverageOption
			if dexName != "" {
				levOpts = append(levOpts, hyperliquid.LeverageWithIsolated())
			}
			const maxRetries = 5
			var lastErr error
			for attempt := 0; attempt < maxRetries; attempt++ {
				if _, err := sdk.UpdateLeverage(base, targetLev, levOpts...); err != nil {
					lastErr = err
					errMsg := err.Error()

					// On LEVERAGE_CONFLICT with isolated mode, retry without margin
					// mode to preserve the existing cross/isolated setting.
					if strings.Contains(errMsg, "LEVERAGE_CONFLICT") && len(levOpts) > 0 {
						slog.Warn("leverage conflict with isolated mode, retrying without margin mode change",
							"asset", asset, "leverage", targetLev)
						if _, err2 := sdk.UpdateLeverage(base, targetLev); err2 != nil {
							lastErr = err2
							// If this also fails, the position's existing leverage
							// is likely already correct — just continue.
							break
						}
						lastErr = nil
						break
					}

					// Retry on SDK-retryable errors and upstream 530 failures.
					retryable := hyperliquid.IsRetryable(err) || strings.Contains(errMsg, "530")
					if !retryable {
						break
					}
					backoff := time.Duration(100<<uint(attempt)) * time.Millisecond
					slog.Warn("retrying set leverage", "asset", asset, "attempt", attempt+1, "backoff", backoff, "error", err)
					time.Sleep(backoff)
					continue
				}
				lastErr = nil
				break
			}
			if lastErr != nil {
				// Leverage persists across sessions; if the exchange is temporarily
				// rejecting the update, warn and continue with the existing setting.
				slog.Warn("could not set leverage, continuing with existing setting",
					"asset", asset, "leverage", targetLev, "error", lastErr)
			} else {
				slog.Info("leverage set", "asset", asset, "leverage", targetLev)
			}
		}
	}

	// Create context with signal handling.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if bridge != nil {
		go watchZeusStop(ctx, cancel, *zeusStop)
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Create channels (buffered to prevent blocking).
	bookChQuote := make(chan types.BookUpdate, 100)
	bookChPaper := make(chan types.BookUpdate, 100) // only used in paper mode
	signalCh := make(chan types.SpreadSignal, 100)
	riskCh := make(chan types.RiskAction, 100)
	fillCh := make(chan types.FillEvent, 100) // only used in live mode

	// Spread engine channel only needed when enabled.
	var bookChSpread chan types.BookUpdate
	useSpreadEngine := len(cfg.Strategy.Assets) >= 2
	if cfg.Strategy.SpreadEngineEnabled != nil {
		useSpreadEngine = *cfg.Strategy.SpreadEngineEnabled
	}
	if useSpreadEngine {
		bookChSpread = make(chan types.BookUpdate, 100)
	}

	// VWAP calculators (short + long per asset, using per-asset windows).
	shortVWAPs := make(map[string]*vwap.Calculator, len(cfg.Strategy.Assets))
	longVWAPs := make(map[string]*vwap.Calculator, len(cfg.Strategy.Assets))
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		shortWindow := time.Duration(ac.MarketMaking.VWAPShortWindowMinutes) * time.Minute
		longWindow := time.Duration(ac.MarketMaking.VWAPLongWindowMinutes) * time.Minute
		shortVWAPs[asset] = vwap.NewCalculator(shortWindow)
		longVWAPs[asset] = vwap.NewCalculator(longWindow)
	}
	tradeChVWAP := make(chan types.TradeUpdate, 100)

	// Trade-flow imbalance calculators (per asset).
	imbalanceCalcs := make(map[string]tradeflow.ImbalanceCalculator, len(cfg.Strategy.Assets))
	imbalanceVolumeMode := make(map[string]bool, len(cfg.Strategy.Assets))
	fillDecays := make(map[string]*tradeflow.FillDecay, len(cfg.Strategy.Assets))
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		exp := ac.MarketMaking.TradeImbalanceSizeExponent
		switch ac.MarketMaking.TradeImbalanceMode {
		case "volume":
			imbalanceCalcs[asset] = tradeflow.NewVolumeClockCalculator(
				ac.MarketMaking.TradeImbalanceBucketSize,
				ac.MarketMaking.TradeImbalanceBucketCount,
				exp,
			)
			imbalanceVolumeMode[asset] = true
		default: // "time"
			window := time.Duration(ac.MarketMaking.TradeImbalanceWindowSec) * time.Second
			imbalanceCalcs[asset] = tradeflow.NewCalculator(window, exp)
		}
		fillDecays[asset] = tradeflow.NewFillDecay()
	}

	// Create executor.
	var exec execution.Executor
	var liveExec *execution.LiveExecutor
	if cfg.Execution.LiveMode {
		liveExec = execution.NewLiveExecutor(sdk, fillCh, cfg.Strategy.Assets, cfg.Risk.MaxCloseSlippageBps)
		liveExec.SetConfig(cfg)
		liveExec.SetSzDecimals(szDecimals)
		exec = liveExec
		slog.Info("using SDK executor", "account", sdk.Address(), "signer", sdk.SignerAddress())
	} else {
		exec = execution.NewPaperExecutor(cfg.Execution.PaperStartingEquity, bookChPaper,
			cfg.MarketMaking.MakerFeeBps, cfg.MarketMaking.BuilderFeeBps)
	}

	// Bootstrap executor with on-chain state (positions, equity).
	if err := exec.Init(ctx, cfg.Strategy.Assets); err != nil {
		slog.Error("failed to initialize executor", "error", err)
		os.Exit(1)
	}

	// Session PnL tracker — shared by manager + status loop (P0).
	sessionPnL := quoting.NewSessionPnLTracker(exec.Equity(), cfg.Risk.SessionPnLWindowMinutes)

	// Create spread engine (only with 2+ assets).
	var spreadEngine *spread.Engine
	if useSpreadEngine {
		spreadEngine = spread.New(cfg, bookChSpread, signalCh)
	}

	// Create risk manager with equity function.
	posCh := exec.PositionUpdates()
	riskMgr := risk.New(cfg, posCh, riskCh, exec.Equity, sessionPnL)

	// Create quote manager.
	quoteMgr := quoting.New(cfg, bookChQuote, signalCh, riskCh, exec, szDecimals, shortVWAPs, longVWAPs, imbalanceCalcs, fillDecays, sessionPnL)
	if bootRLRemaining > 0 {
		quoteMgr.UpdateRateLimit(bootRLRemaining, bootRLCap)
	}
	if bridge != nil {
		bridgeDone = make(chan struct{})
		go func() { defer close(bridgeDone); emitZeusState(ctx, bridge, liveExec, quoteMgr, *zeusSymbol) }()
	}
	// P13: post-mortem dumper (no-op when disabled).
	pmDumper := quoting.NewPostMortemDumper(cfg.PostMortem)
	quoteMgr.SetPostMortemDumper(pmDumper)
	if spreadEngine != nil {
		quoteMgr.SetWarmupFn(spreadEngine.WarmUpProgress)
	}
	// Time-based warmup fallback when spread engine is disabled.
	if cfg.MarketMaking.WarmupDurationSec > 0 {
		quoteMgr.SetWarmupDuration(time.Duration(cfg.MarketMaking.WarmupDurationSec) * time.Second)
	}

	// P12: Time-of-day notional schedule (global). Default OFF.
	if cfg.MarketMaking.TradingScheduleEnabled {
		sched, err := risk.NewTradingSchedule(cfg.MarketMaking.TradingSchedule)
		if err != nil {
			slog.Error("failed to build trading schedule", "error", err)
			os.Exit(1)
		}
		quoteMgr.SetSchedule(sched)
		slog.Info("trading schedule",
			"enabled", true,
			"window_count", len(cfg.MarketMaking.TradingSchedule),
			"windows", sched.Windows(),
		)
	} else {
		slog.Info("trading schedule", "enabled", false)
	}

	// Funding rate session-end urgency: wire SDK info client.
	if cfg.MarketMaking.FundingUrgencyEnabled {
		quoteMgr.SetFundingInfo(sdk.Info())
	}

	// Wire fill callback for post-fill adverse decay tracking + adaptive notional.
	fillCallback := func(fill types.FillEvent) {
		ac := cfg.ForAsset(fill.Coin)
		if ac.MarketMaking.FillAdverseShiftBps > 0 {
			if fd, ok := fillDecays[fill.Coin]; ok {
				halfLife := time.Duration(ac.MarketMaking.FillDecayHalfLifeMs) * time.Millisecond
				fd.RecordFill(fill.Side, ac.MarketMaking.FillAdverseShiftBps, halfLife)
			}
		}
		quoteMgr.RecordFill(fill)
	}
	if liveExec != nil {
		liveExec.SetOnFill(fillCallback)
	} else if pe, ok := exec.(*execution.PaperExecutor); ok {
		pe.SetOnFill(fillCallback)
	}

	// Seed risk manager with any bootstrapped on-chain positions so it knows
	// about existing exposure before the event loop starts.
	var initialPositions []types.PositionUpdate
	for _, asset := range cfg.Strategy.Assets {
		pos := exec.Position(asset)
		if pos.Side != "" {
			initialPositions = append(initialPositions, pos)
		}
	}
	if len(initialPositions) > 0 {
		riskMgr.SeedPositions(initialPositions)
		slog.Info("seeded risk manager with initial positions", "count", len(initialPositions))
	}

	// Determine which channels to fan-out book updates to.
	var fanOutChannels []chan types.BookUpdate
	if useSpreadEngine {
		fanOutChannels = append(fanOutChannels, bookChSpread)
	}
	fanOutChannels = append(fanOutChannels, bookChQuote)
	var bookChExec chan types.BookUpdate
	if cfg.Execution.LiveMode {
		bookChExec = make(chan types.BookUpdate, 100)
		fanOutChannels = append(fanOutChannels, bookChExec)
	}
	if !cfg.Execution.LiveMode {
		fanOutChannels = append(fanOutChannels, bookChPaper)
	}

	// Create gRPC feed (always needed for market data).
	var grpcFeed interface {
		Start() error
		Stop()
	}
	if cfg.DataSource == "public" {
		grpcFeed = feed.NewPublicFeed(cfg.Strategy.Assets, fanOutChannels, nil, nil)
	} else {
		grpcFeed = feed.NewGRPCFeed(cfg.QuickNodeEndpoint, cfg.Strategy.Assets, fanOutChannels...)
	}
	slog.Info("market data source", "source", cfg.DataSource)

	// Create WebSocket feed (live mode only).
	// reconnectCh signals the executor to reconcile positions immediately after a
	// WebSocket reconnect (fills may have been missed during the disconnect window).
	// Fill poller (3s) is now the primary fill detection path; WS fills are secondary.
	//
	// WS feeds route to the public Hyperliquid endpoint instead of QuickNode because
	// the SDK's pingLoop sends {"method":"ping"} which QuickNode ignores (it expects
	// JSON-RPC hl_ping). Without pong responses, the SDK kills the connection every
	// 40 seconds. The public API understands the native ping format. gRPC stays on
	// QuickNode (uses native HTTP/2 PING frames, no issue).
	const publicWSEndpoint = "wss://api.hyperliquid.xyz/ws"
	var wsFeed *feed.WSFeed
	var reconnectCh chan struct{}         // raw signal from feeds
	var execReconnectCh chan struct{}     // fan-out to executor
	var quoteMgrReconnectCh chan struct{} // fan-out to quote manager
	if cfg.Execution.LiveMode {
		reconnectCh = make(chan struct{}, 1)
		execReconnectCh = make(chan struct{}, 1)
		quoteMgrReconnectCh = make(chan struct{}, 1)
		wsFeed = feed.NewWSFeed(publicWSEndpoint, sdk.Address(), fillCh, reconnectCh)
		liveExec.SetReconnectCh(execReconnectCh)

		// Fan-out reconnect signals to both executor and quote manager.
		go func() {
			for range reconnectCh {
				select {
				case execReconnectCh <- struct{}{}:
				default:
				}
				select {
				case quoteMgrReconnectCh <- struct{}{}:
				default:
				}
			}
		}()
		quoteMgr.SetReconnectCh(quoteMgrReconnectCh)
	}

	// Log full config at startup for post-session analysis.
	// JSON-serialize the resolved per-asset config so every field is captured,
	// including fields added in the future. No more hand-curated field lists.
	if stratJSON, err := json.Marshal(cfg.Strategy); err == nil {
		slog.Info("config: strategy", "json", string(stratJSON))
	}
	if logJSON, err := json.Marshal(cfg.Logging); err == nil {
		slog.Info("config: logging", "json", string(logJSON))
	}
	if rlJSON, err := json.Marshal(cfg.RateLimit); err == nil {
		slog.Info("config: rate_limit", "json", string(rlJSON))
	}
	{
		// P13: log the resolved (defaults-applied) post-mortem config, not the
		// raw cfg, so users see the effective output_dir and rotation count.
		pmCfg := pmDumper.Config()
		slog.Info("postmortem config",
			"enabled", pmCfg.Enabled,
			"output_dir", pmCfg.OutputDir,
			"filename_format", pmCfg.FilenameFormat,
			"include_last_n_evals", pmCfg.IncludeLastNEvals,
			"include_last_n_refreshes", pmCfg.IncludeLastNRefreshes,
			"include_all_fills", pmCfg.IncludeAllFills,
			"keep_count", pmCfg.RotateKeepCount,
		)
	}
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		// Zero out PerAsset from the per-asset dump (logged separately).
		ac.MarketMaking.PerAsset = nil
		ac.Risk.PerAsset = nil
		ac.Execution.PerAsset = nil
		if mmJSON, err := json.Marshal(ac.MarketMaking); err == nil {
			slog.Info("config: market_making", "coin", asset, "json", string(mmJSON))
		}
		if riskJSON, err := json.Marshal(ac.Risk); err == nil {
			slog.Info("config: risk", "coin", asset, "json", string(riskJSON))
		}
	}
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		if ac.MarketMaking.OptimizerV2.Enabled {
			if v2JSON, err := json.Marshal(ac.MarketMaking.OptimizerV2); err == nil {
				slog.Info("config: optimizer_v2", "coin", asset, "json", string(v2JSON))
			}
			// Plaintext enumerated print of operationally-tuned optimizer_v2
			// fields so they are greppable from logs without JSON parsing —
			// matches the style of the other `asset config: X` blocks above.
			v2 := ac.MarketMaking.OptimizerV2
			slog.Info("asset config: optimizer_v2",
				"coin", asset,
				"shadow_mode", v2.ShadowMode,
				"eval_interval_sec", v2.EvalIntervalSec,
				"warmup_evals", v2.WarmupEvals,
				"min_fill_samples", v2.MinFillSamples,
				"min_fill_sample_window_sec", v2.MinFillSampleWindowSec,
				"momentum", v2.Momentum,
				"learning_rate", v2.LearningRate,
				"max_knob_changes_per_eval", v2.MaxKnobChangesPerEval,
				"emergency_toxicity_threshold", v2.EmergencyToxicityThreshold,
				"rollback_window_evals", v2.RollbackWindowEvals,
				"rollback_rate", v2.RollbackRate,
				"rollback_cooldown_sec", v2.RollbackCooldownSec,
				"rollback_absolute_floor_bps", v2.RollbackAbsoluteFloorBps,
				"burst_eval_enabled", v2.BurstEvalEnabled,
				"burst_eval_min_interval_sec", v2.BurstEvalMinIntervalSec,
				"burst_eval_all_negative_cooldown_sec", v2.BurstEvalAllNegCooldownSec,
				"force_refresh_on_emergency", v2.ForceRefreshOnEmergency,
				"force_refresh_min_interval_ms", v2.ForceRefreshMinIntervalMs,
				"force_refresh_min_spread_delta_bps", v2.ForceRefreshMinSpreadDeltaBps,
				"subdim_normalization", v2.SubdimNormalization,
				"fourth_dimension", v2.FourthDimension,
				"fourth_dim_defensive_floor", v2.FourthDimDefensiveFloor,
			)
		}
	}

	// Start data feeds.
	slog.Info("starting data feeds...")

	if err := grpcFeed.Start(); err != nil {
		slog.Error("failed to start gRPC feed", "error", err)
		os.Exit(1)
	}

	if wsFeed != nil {
		if err := wsFeed.Start(); err != nil {
			slog.Error("failed to start WebSocket feed", "error", err)
			os.Exit(1)
		}
	}

	// Start trade feed for VWAP via gRPC (QuickNode).
	// The public WS endpoint rejects trade subscriptions for HIP-3 equity assets
	// (1006 close within 300ms), so gRPC is used for all trade data.
	// Pass reconnectCh so trade stream reconnects also trigger reconciliation.
	var tradeFeed interface {
		Start() error
		Stop()
	}
	if cfg.DataSource == "public" {
		tradeFeed = feed.NewPublicFeed(cfg.Strategy.Assets, nil, tradeChVWAP, reconnectCh)
	} else {
		tradeFeed = feed.NewGRPCTradeFeed(cfg.QuickNodeEndpoint, cfg.Strategy.Assets, reconnectCh, tradeChVWAP)
	}
	if err := tradeFeed.Start(); err != nil {
		slog.Error("failed to start trade feed", "error", err)
		os.Exit(1)
	}
	slog.Info("trade feed started", "assets", cfg.Strategy.Assets)
	for _, asset := range cfg.Strategy.Assets {
		ac := cfg.ForAsset(asset)
		slog.Info("  vwap windows", "coin", asset,
			"short_window_min", ac.MarketMaking.VWAPShortWindowMinutes,
			"long_window_min", ac.MarketMaking.VWAPLongWindowMinutes,
		)
	}

	// Fan-out: route trades to VWAP calculators and imbalance calculators.
	go func() {
		firstTradeSeen := make(map[string]bool)
		for {
			select {
			case <-ctx.Done():
				return
			case trade := <-tradeChVWAP:
				if !firstTradeSeen[trade.Coin] {
					firstTradeSeen[trade.Coin] = true
					slog.Info("first trade received for VWAP",
						"coin", trade.Coin,
						"price", trade.Price,
						"size", trade.Size,
					)
				}
				if calc, ok := shortVWAPs[trade.Coin]; ok {
					calc.AddTrade(trade.Price, trade.Size, trade.Timestamp)
				}
				if calc, ok := longVWAPs[trade.Coin]; ok {
					calc.AddTrade(trade.Price, trade.Size, trade.Timestamp)
				}
				if calc, ok := imbalanceCalcs[trade.Coin]; ok && trade.Crossed {
					if imbalanceVolumeMode[trade.Coin] {
						calc.AddTrade(trade.Price*trade.Size, trade.Side, trade.Timestamp)
					} else {
						calc.AddTrade(trade.Size, trade.Side, trade.Timestamp)
					}
				}
				// Volume burst: feed trade velocity tracker.
				quoteMgr.RecordMarketTrade(trade)
			}
		}
	}()

	// Forward book updates to live executor for slippage-protected closes.
	if liveExec != nil && bookChExec != nil {
		go func() {
			for {
				select {
				case <-ctx.Done():
					return
				case book := <-bookChExec:
					liveExec.UpdateBook(book)
				}
			}
		}()
	}

	// Start processing components.
	slog.Info("starting processing components...")

	if spreadEngine != nil {
		go spreadEngine.Run(ctx)
	}
	go exec.Run(ctx)
	go riskMgr.Run(ctx)
	go quoteMgr.Run(ctx)

	slog.Info("all components started, waiting for data...",
		"warm_up_ticks", cfg.Strategy.LookbackWindow,
	)

	// Rate limit circuit breaker polling — separate from STATUS dump.
	if cfg.Execution.LiveMode {
		go func() {
			rlInterval := cfg.Execution.PollRateLimitSec
			if rlInterval <= 0 {
				rlInterval = 30
			}
			// V24: deterministic 15s startup offset to de-phase the rate-limit poller
			// from pollEquity (0s) / pollOpenOrders (5s) / pollFills (10s). All four
			// previously piled up in the same 30s bucket (V23 top-burst analysis).
			select {
			case <-ctx.Done():
				return
			case <-time.After(15 * time.Second):
			}
			rlTicker := time.NewTicker(time.Duration(rlInterval) * time.Second)
			defer rlTicker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-rlTicker.C:
					if rl, err := sdk.Info().UserRateLimit(sdk.Address()); err == nil {
						cap, _ := rl["nRequestsCap"].(float64)
						used, _ := rl["nRequestsUsed"].(float64)
						remaining := cap - used
						quoteMgr.UpdateRateLimit(int(remaining), int(cap))
						if liveExec != nil {
							liveExec.UpdateRateLimit(int(remaining), int(cap))
						}
						if remaining < 500 {
							slog.Warn("API request allowance running low",
								"remaining", int(remaining),
								"cap", int(cap),
							)
						}
					}
				}
			}
		}()
	}

	// Periodic status logging.
	go func() {
		statusInterval := 15
		if cfg.Logging.StatusIntervalSec > 0 {
			statusInterval = cfg.Logging.StatusIntervalSec
		}
		ticker := time.NewTicker(time.Duration(statusInterval) * time.Second)
		defer ticker.Stop()
		prevEquity := 0.0
		var cumulativeExchangeReqs int64

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				equity := exec.Equity()

				// Spread engine status.
				hwm := riskMgr.HighWater()
				if spreadEngine != nil {
					sig := spreadEngine.LatestSignal()
					warmCount, warmWindow := spreadEngine.WarmUpProgress()
					slog.Info("=== STATUS ===",
						"equity", equity,
						"high_water", fmt.Sprintf("%.2f", hwm),
						"z_score", sig.ZScore,
						"spread", sig.Spread,
						"mean", sig.Mean,
						"stddev", sig.StdDev,
						"beta", fmt.Sprintf("%.4f", sig.Beta),
						"z_stddev", fmt.Sprintf("%.3f", sig.ZStdDev),
						"warm_up", fmt.Sprintf("%d/%d", warmCount, warmWindow),
					)
				} else {
					slog.Info("=== STATUS ===",
						"equity", equity,
						"high_water", fmt.Sprintf("%.2f", hwm),
					)
				}

				// VWAP fair value status (per asset).
				for _, asset := range cfg.Strategy.Assets {
					if calc, ok := shortVWAPs[asset]; ok {
						v, ready := calc.VWAP()
						trades := calc.TradeCount()
						lastT, hasLast := calc.LastTradeTime()
						if ready {
							tradeAgeMs := int64(-1)
							if hasLast {
								tradeAgeMs = time.Since(lastT).Milliseconds()
							}
							slog.Info("  vwap_short", "coin", asset, "vwap", fmt.Sprintf("%.4f", v), "trades", trades, "trade_age_ms", tradeAgeMs)
						} else {
							slog.Info("  vwap_short", "coin", asset, "vwap", "not_ready", "trades", trades)
						}
					}
					if calc, ok := longVWAPs[asset]; ok {
						v, ready := calc.VWAP()
						trades := calc.TradeCount()
						if ready {
							slog.Info("  vwap_long", "coin", asset, "vwap", fmt.Sprintf("%.4f", v), "trades", trades)
						}
					}
				}

				// Per-asset positions and orders.
				for _, asset := range cfg.Strategy.Assets {
					pos := exec.Position(asset)

					slog.Info("  position",
						"coin", pos.Coin,
						"side", pos.Side,
						"size", pos.Size,
						"notional", pos.Notional,
						"avg_entry", pos.AvgEntry,
					)

					orders := exec.OpenOrders(asset)
					if cfg.Logging.Verbose {
						for _, o := range orders {
							slog.Info("  order",
								"coin", o.Coin,
								"side", o.Side,
								"price", o.Price,
								"size", o.Size,
							)
						}
					}
					slog.Info("  orders", "coin", asset, "count", len(orders))
				}

				// Quoting pipeline diagnostics.
				diags := quoteMgr.Diagnostics()
				for _, asset := range cfg.Strategy.Assets {
					if d, ok := diags[asset]; ok && !d.Timestamp.IsZero() {
						slog.Info("  quoting",
							"coin", asset,
							"book_mid", fmt.Sprintf("%.4f", d.BookMid),
							"fair_value", fmt.Sprintf("%.4f", d.FairValue),
							"bbo_spread_bps", fmt.Sprintf("%.1f", d.BBOSpreadBps),
							"spread_bps", fmt.Sprintf("%.1f", d.EffectiveSpreadBps),
							"skew_entry_bps", fmt.Sprintf("%.2f", d.SkewShiftBps),
							"cross_leg_bps", fmt.Sprintf("%.2f", d.CrossLegSkewBps),
							"bias_bps", fmt.Sprintf("%.2f", d.BiasShiftBps),
							"imbalance_shift_bps", fmt.Sprintf("%.2f", d.TradeImbalanceShift),
							"fill_decay_shift_bps", fmt.Sprintf("%.2f", d.FillDecayShift),
							"wick_bid", fmt.Sprintf("%.2f", d.WickBidMult),
							"wick_ask", fmt.Sprintf("%.2f", d.WickAskMult),
							"z_wide", fmt.Sprintf("%.2f", d.ZWideningMult),
							"warmup", d.WarmupActive,
							"recovery", d.RecoveryActive,
							"degraded", d.HealthDegraded,
							"halted", d.HealthHalted,
							"health_reason", d.HealthReason,
							"pair_reduce_only", d.PairReduceOnly,
						)
						slog.Info("  sizing",
							"coin", asset,
							"inv_util", fmt.Sprintf("%.4f", d.InventoryUtil),
							"entry_size_factor", fmt.Sprintf("%.2f", d.EntrySizeFactor),
							"trend_guard", d.TrendGuardActive,
							"tg_suppress", fmt.Sprintf("%.2f", d.TrendGuardSuppress),
							"tg_dev_pct", fmt.Sprintf("%.3f", d.TrendGuardDevPct),
							"flow_gate", d.FlowGateActive,
							"fg_factor", fmt.Sprintf("%.2f", d.FlowGateFactor),
							"price_shock", d.PriceShockActive,
							"price_shock_side", d.PriceShockSide,
							"price_shock_bps", fmt.Sprintf("%.1f", d.PriceShockMoveBps),
							"wick_supp_bid", d.WickBidSuppressed,
							"wick_supp_ask", d.WickAskSuppressed,
							"allowed_buy", fmt.Sprintf("%.1f", d.AllowedBuyNotional),
							"allowed_sell", fmt.Sprintf("%.1f", d.AllowedSellNotional),
							"notional_mult", fmt.Sprintf("%.2f", d.NotionalMult),
							"schedule_mult", fmt.Sprintf("%.2f", d.ScheduleMult),
							"tox_buy_ewma", fmt.Sprintf("%.2f", d.ToxBuyEWMA),
							"tox_sell_ewma", fmt.Sprintf("%.2f", d.ToxSellEWMA),
							"tox_buy_mult", fmt.Sprintf("%.2f", d.ToxBuyMult),
							"tox_sell_mult", fmt.Sprintf("%.2f", d.ToxSellMult),
							"bid_absent_pct", fmt.Sprintf("%.1f", d.BidAbsentPct),
							"ask_absent_pct", fmt.Sprintf("%.1f", d.AskAbsentPct),
							"both_absent_pct", fmt.Sprintf("%.1f", d.BothAbsentPct),
							"presence_cycles", d.PresenceCycles,
						)
						slog.Info("  adaptive",
							"coin", asset,
							"skew_factor", fmt.Sprintf("%.3f", d.SkewFactor),
							"vwap_alpha", fmt.Sprintf("%.3f", d.VWAPAlpha),
							"autocorr", func() string {
								if math.IsNaN(d.Autocorrelation) {
									return "n/a"
								}
								return fmt.Sprintf("%.3f", d.Autocorrelation)
							}(),
							"opt_spread_delta", fmt.Sprintf("%.1f", d.OptSpreadDelta),
							"opt_spread_buy", fmt.Sprintf("%.1f", d.OptSpreadDeltaBuy),
							"opt_spread_sell", fmt.Sprintf("%.1f", d.OptSpreadDeltaSell),
							"opt_notional_mult", fmt.Sprintf("%.2f", d.OptNotionalMult),
							"opt_skew_mult", fmt.Sprintf("%.2f", d.OptSkewMult),
							"opt_alpha_delta", fmt.Sprintf("%.3f", d.OptAlphaDelta),
							"opt_imb_shift_mult", fmt.Sprintf("%.2f", d.OptImbalanceShiftMult),
							"opt_fill_decay_mult", fmt.Sprintf("%.2f", d.OptFillDecayMult),
							"opt_refresh_mult", fmt.Sprintf("%.2f", d.OptRefreshMult),
							"opt_eval", d.OptEvalSeq,
							"burst_buy_spread", fmt.Sprintf("%.2f", d.BurstBuySpreadMult),
							"burst_sell_spread", fmt.Sprintf("%.2f", d.BurstSellSpreadMult),
							"vol_burst_ratio", fmt.Sprintf("%.2f", d.VolBurstRatio),
							"vol_burst_levels", d.VolBurstLevels,
							"vol_burst_active", d.VolBurstActive,
						)
					}
				}

				// V30 part 6: per-asset markout EWMA snapshot. Previously the only
				// markout signal in logs was the fused composite inside
				// [OptimizerV2] inputs — post-mortems had to re-derive per-horizon
				// and per-side values from hundreds of individual fill events.
				// This single line exposes the running EWMA across all 4 horizons
				// (500ms / 1s / 2s / 5s) plus the per-side composites and their
				// fill counts, every status tick. No runtime cost beyond 8 floats.
				for _, asset := range cfg.Strategy.Assets {
					snap := quoteMgr.MarkoutSnapshot(asset)
					if snap.Composite.Count == 0 && snap.BuyFills == 0 && snap.SellFills == 0 {
						continue
					}
					slog.Info("  markout_ewma_snapshot",
						"coin", asset,
						"horizon_500ms_bps", fmt.Sprintf("%.2f", snap.Composite.Values[0]),
						"horizon_1s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[1]),
						"horizon_2s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[2]),
						"horizon_5s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[3]),
						"horizon_fills", snap.Composite.Count,
						"buy_ewma_bps", fmt.Sprintf("%.2f", snap.BuyComposite),
						"sell_ewma_bps", fmt.Sprintf("%.2f", snap.SellComposite),
						"buy_fills", snap.BuyFills,
						"sell_fills", snap.SellFills,
					)
				}

				// Fill window stats (resets on read).
				fillStats := quoteMgr.FillStatsSnapshot()
				equityDelta := 0.0
				if prevEquity > 0 {
					equityDelta = equity - prevEquity
				}
				prevEquity = equity
				for _, asset := range cfg.Strategy.Assets {
					if fs, ok := fillStats[asset]; ok && (fs.BuyCount+fs.SellCount) > 0 {
						avgCapture := 0.0
						if fs.SpreadCaptureCount > 0 {
							avgCapture = fs.SpreadCaptureBpsSum / float64(fs.SpreadCaptureCount)
						}
						slog.Info("  fills",
							"coin", asset,
							"buys", fs.BuyCount,
							"sells", fs.SellCount,
							"buy_notional", fmt.Sprintf("%.1f", fs.BuyNotional),
							"sell_notional", fmt.Sprintf("%.1f", fs.SellNotional),
							"fees", fmt.Sprintf("%.4f", fs.TotalFees),
							"avg_capture_bps", fmt.Sprintf("%.2f", avgCapture),
						)
					}
					if snap := quoteMgr.MarkoutSnapshot(asset); snap.Composite.Count > 0 {
						// V30 part 5: per-side composite EWMA appended so
						// post-mortems no longer have to grep hundreds of raw
						// per-fill `markout` lines to reconstruct side × horizon
						// behavior (V29 §3 gap).
						// V32: per-mechanism firing counters so a dormant
						// trend-bias mechanism is visible immediately instead
						// of requiring a full log grep (V31 trend_guard never
						// fired, noticed only post-session).
						tmc := quoteMgr.TrendMechCountsSnapshot(asset)
						slog.Info("  markout",
							"coin", asset,
							"fills", snap.Composite.Count,
							"T500ms_bps", fmt.Sprintf("%.2f", snap.Composite.Values[0]),
							"T1s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[1]),
							"T2s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[2]),
							"T5s_bps", fmt.Sprintf("%.2f", snap.Composite.Values[3]),
							"buy_composite_bps", fmt.Sprintf("%.2f", snap.BuyComposite),
							"sell_composite_bps", fmt.Sprintf("%.2f", snap.SellComposite),
							"trend_fv_shift_fired", tmc.TrendFVShift,
							"alpha_boost_fired", tmc.AlphaBoost,
							"entry_accel_fired", tmc.EntryAccel,
						)
					}
				}
				if prevEquity > 0 {
					pnlNow := time.Now()
					sessionPnL.Update(equity, pnlNow)
					absPnL, sessionPnLBps := sessionPnL.PnL()
					windowPnLBps := sessionPnL.WindowPnLBps(pnlNow)
					slog.Info("  pnl",
						"equity_delta", fmt.Sprintf("%.4f", equityDelta),
						"equity", fmt.Sprintf("%.2f", equity),
						"session_pnl", fmt.Sprintf("%.4f", absPnL),
						"session_pnl_bps", fmt.Sprintf("%.1f", sessionPnLBps),
						"window_pnl_bps", fmt.Sprintf("%.1f", windowPnLBps),
					)
				}

				if liveExec != nil {
					snap := liveExec.Stats()
					cumulativeExchangeReqs += snap.Exchange

					// Feed only exchange (action) calls — info reads don't count
					// against the address-based rate limit (actions only).
					if totalCalls := int(snap.Exchange); totalCalls > 0 {
						if vc := quoteMgr.VirtCycle(); vc != nil {
							vc.RecordAPICalls(totalCalls)
						}
					}

					slog.Info("  api_requests",
						"window_sec", 30,
						"total", snap.Total,
						"exchange", snap.Exchange,
						"info", snap.Info,
						"avg_rps", fmt.Sprintf("%.1f", float64(snap.Total)/30.0),
					)

					fees, builderFees := liveExec.FeeSnapshot()
					slog.Info("  fees",
						"total_exchange_fees", fmt.Sprintf("%.4f", fees),
						"total_builder_fees", fmt.Sprintf("%.4f", builderFees),
						"total_all_fees", fmt.Sprintf("%.4f", fees+builderFees),
					)

					// Rate limit circuit breaker status (read-only, no API call).
					rlTier, rlTTE, rlDepl, rlHalted, rlRemaining, rlEWMA, rlCap, rlSessUsed, rlSessGen, rlSessNet := quoteMgr.RateLimitStatus(cumulativeExchangeReqs)
					rlTTEStr := fmt.Sprintf("%.1f", rlTTE)
					if math.IsInf(rlTTE, 1) {
						rlTTEStr = "inf"
					}
					pctStr := "—"
					if rlCap > 0 {
						pctStr = fmt.Sprintf("%.1f", float64(rlRemaining)/float64(rlCap)*100.0)
					}
					slog.Info("  rate_limit",
						"remaining", rlRemaining,
						"pct", pctStr,
						"tier", rlTier,
						"tte_min", rlTTEStr,
						"depletion", fmt.Sprintf("%.2f", rlDepl),
						"ewma_depletion", fmt.Sprintf("%.4f", rlEWMA),
						"halted", rlHalted,
						"session_used", rlSessUsed,
						"session_generated", rlSessGen,
						"session_net", rlSessNet,
					)
				}

				// Virtuous cycle engine: volume regen vs API burn health.
				if vc := quoteMgr.VirtCycle(); vc != nil {
					slog.Info("  virt_cycle",
						"headroom", fmt.Sprintf("%.3f", vc.Headroom()),
						"volume_rate_usd_min", fmt.Sprintf("%.2f", vc.VolumeRate()),
						"regen_rate_calls_min", fmt.Sprintf("%.2f", vc.RegenRate()),
						"burn_rate_calls_min", fmt.Sprintf("%.2f", vc.BurnRate()),
						"cum_volume_usd", fmt.Sprintf("%.2f", vc.CumVolumeUSD()),
						"cum_regen_reqs", vc.CumRegenReqs(),
						"regen_ratio", fmt.Sprintf("%.3f", vc.RegenRatio()),
					)
				}

				// Rate limit allocator: per-asset ROI scores.
				if rla := quoteMgr.RateLimitAllocatorInstance(); rla != nil {
					scores := rla.AssetROIScores()
					if len(scores) > 0 {
						slog.Info("  rate_limit_allocator", "roi_scores", scores)
					}
				}
			}
		}
	}()

	// Wait for shutdown signal.
	select {
	case sig := <-sigCh:
		slog.Info("received signal, shutting down...", "signal", sig)
	case <-ctx.Done():
	}

	// Begin graceful shutdown.
	slog.Info("initiating graceful shutdown...")

	// P13: dump per-asset post-mortem before tearing down. Done before cancel()
	// so the dumper's snapshots reflect the final live state, and so the dump
	// completes even if the supervisor never reaches OpFlatten on a clean stop.
	quoteMgr.DumpPostMortem("shutdown")
	shutdownStarted := time.Now().UnixMilli()
	cancel()
	if bridgeDone != nil {
		<-bridgeDone
	}

	// Cancel all orders in live mode.
	if cfg.Execution.LiveMode {

		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer shutdownCancel()

		var wg sync.WaitGroup
		for _, asset := range cfg.Strategy.Assets {
			wg.Add(1)
			go func(asset string) {
				defer wg.Done()
				slog.Info("cancelling orders for shutdown", "asset", asset)
				for attempt := 0; attempt < 3; attempt++ {
					if attempt > 0 {
						delay := time.Duration(2<<uint(attempt-1)) * time.Second // 2s, 4s
						slog.Warn("retrying shutdown cancel", "asset", asset, "attempt", attempt+1, "delay", delay)
						select {
						case <-shutdownCtx.Done():
							slog.Error("shutdown timeout during cancel retry", "asset", asset)
							return
						case <-time.After(delay):
						}
					}
					if err := exec.CancelAll(shutdownCtx, asset); err == nil {
						slog.Info("orders cancelled for shutdown", "asset", asset)
						// Verify no orders remain (in-flight refresh may have placed new ones).
						remaining := exec.OpenOrders(asset)
						if len(remaining) == 0 {
							return
						}
						slog.Warn("orders still remain after shutdown cancel, retrying",
							"asset", asset, "remaining", len(remaining))
						continue
					} else {
						slog.Error("failed to cancel orders on shutdown", "asset", asset, "attempt", attempt+1, "error", err)
					}
				}
				slog.Error("exhausted shutdown cancel retries", "asset", asset)
			}(asset)
		}
		wg.Wait()

		// V30 part 6: session flatten on shutdown. After cancelling resting
		// orders, market-close any residual position so the bot doesn't leave
		// inventory carrying across restarts. Opt-in via config. Runs with an
		// independent per-asset timeout so a stuck close on one asset can't
		// block the others.
		if cfg.Risk.SessionFlattenOnShutdown {
			flattenTimeout := 15 * time.Second
			if cfg.Risk.SessionFlattenTimeoutSec > 0 {
				flattenTimeout = time.Duration(cfg.Risk.SessionFlattenTimeoutSec) * time.Second
			}
			var fwg sync.WaitGroup
			for _, asset := range cfg.Strategy.Assets {
				pos := exec.Position(asset)
				if pos.Side == "" || pos.Size == 0 {
					slog.Info("session flatten: already flat, skipping", "asset", asset)
					continue
				}
				fwg.Add(1)
				go func(asset string, pos types.PositionUpdate) {
					defer fwg.Done()
					closeCtx, closeCancel := context.WithTimeout(context.Background(), flattenTimeout)
					defer closeCancel()
					slog.Info("session flatten: closing position",
						"asset", asset,
						"side", pos.Side,
						"size", pos.Size,
						"notional", pos.Notional,
					)
					if err := exec.ClosePosition(closeCtx, asset, "session_flatten_shutdown"); err != nil {
						slog.Error("session flatten: close failed", "asset", asset, "error", err)
						return
					}
					slog.Info("session flatten: close submitted", "asset", asset)
				}(asset, pos)
			}
			fwg.Wait()
		}
	}

	// Stop feeds.
	grpcFeed.Stop()
	if wsFeed != nil {
		wsFeed.Stop()
	}
	tradeFeed.Stop()

	// Log final state.
	equity := exec.Equity()
	slog.Info("final state",
		"equity", equity,
		"mode", mode,
	)

	for _, asset := range cfg.Strategy.Assets {
		pos := exec.Position(asset)
		slog.Info("final position",
			"coin", pos.Coin,
			"side", pos.Side,
			"size", pos.Size,
			"notional", pos.Notional,
			"avg_entry", pos.AvgEntry,
		)
	}

	if bridge != nil {
		ledgerErr := recoverZeusClosingFills(sdk, bridge, shutdownStarted-1000)
		ledgerWarning := ""
		if ledgerErr != nil {
			ledgerWarning = "Comptabilite incomplete: executions finales non confirmees"
		}
		bridge.Update(map[string]any{"accounting_complete": ledgerErr == nil})
		err := verifyZeusFlat(sdk, *zeusSymbol)
		if err != nil {
			bridge.Update(map[string]any{"phase": "failed", "running": false, "position_known": false, "error": "Arret non confirme: " + err.Error()})
		} else {
			bridge.Update(map[string]any{"phase": "stopped", "running": false, "position_known": true, "position": 0.0, "position_usd": 0.0, "error": ledgerWarning, "action": "Arret confirme: ordres et position a zero"})
		}
		bridge.Emit()
		if err != nil {
			os.Exit(2)
		}
	}
	slog.Info("shutdown complete")
}
