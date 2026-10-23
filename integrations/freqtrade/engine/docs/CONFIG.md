# Configuration Reference

Canonical reference for the YAML config fields that apply in **single-asset mode**. The source of truth is `internal/config/config.go` (loader + validation) and `config.example.yaml` (annotated template). This document organizes those fields by section, calls out defaults and validation rules, and links related algorithmic behavior in [ALGORITHM.md](./ALGORITHM.md).

> The codebase still supports multi-asset / spread-pair mode under the hood, but it is intentionally not documented here. Fields that only apply with two or more assets — `strategy.spread_type`, `strategy.use_beta_hedge`, `strategy.spread_engine_enabled`, `strategy.z_min_stddev`, `strategy.beta_*`, `market_making.spread_bias_*`, `market_making.z_widening_*`, `market_making.cross_leg_*`, `market_making.pair_reduce_*`, `market_making.adaptive_z_*`, `market_making.z_calibration_window`, `market_making.max_cross_leg_skew_ms`, `market_making.rate_limit_allocator_*`, `risk.max_aggregate_exposure`, and the `per_asset:` override blocks — are no-ops in single-asset mode. To run multi-asset, read the loader source directly.

> **Convention.** Throughout this doc:
>
> - "Default = `0` (disabled)" means a sentinel value that turns the feature off.
> - "Default = `0` (auto)" means the loader substitutes a real value at startup.
> - "Required" means the loader rejects the config if the field is missing or zero.
> - Bracket notation `[lo, hi]` is inclusive; `(lo, hi)` is exclusive.

---

## Table of Contents

1. [How Config Loading Works](#how-config-loading-works)
2. [Section: `strategy`](#section-strategy)
3. [Section: `market_making`](#section-market_making)
   - [Core quoting](#core-quoting)
   - [VWAP & fair value](#vwap--fair-value)
   - [Wick mean-reversion sizing](#wick-mean-reversion-sizing)
   - [Ladder construction](#ladder-construction)
   - [Wall-fronting](#wall-fronting)
   - [Fees](#fees)
   - [Trade flow (imbalance & post-fill decay)](#trade-flow-imbalance--post-fill-decay)
   - [Crossing protection](#crossing-protection)
   - [Adaptive notional](#adaptive-notional)
   - [Markout tracking & FV feedback](#markout-tracking--fv-feedback)
   - [Refresh interval (dynamic + post-fill)](#refresh-interval-dynamic--post-fill)
   - [Toxicity (per-side adverse selection)](#toxicity-per-side-adverse-selection)
   - [Inventory controller (B3)](#inventory-controller-b3)
   - [Inventory exit-preserve & entry-side reduction](#inventory-exit-preserve--entry-side-reduction)
   - [Warmup](#warmup)
   - [Fill burst circuit breaker](#fill-burst-circuit-breaker)
   - [Volume burst ladders](#volume-burst-ladders)
   - [Trend Guard](#trend-guard)
   - [Regime Gate (P1)](#regime-gate-p1)
   - [Price Shock Guard](#price-shock-guard)
   - [Shock Chain Detector](#shock-chain-detector)
   - [Directional Flow Gate](#directional-flow-gate)
   - [Position-age exit tightening](#position-age-exit-tightening)
   - [Per-side win rate gate (P6)](#per-side-win-rate-gate-p6)
   - [Idle-fill circuit breaker (P9)](#idle-fill-circuit-breaker-p9)
   - [Time-of-day adjustments](#time-of-day-adjustments)
   - [Funding-rate session-end urgency](#funding-rate-session-end-urgency)
   - [Smart layers (spread cap)](#smart-layers-spread-cap)
   - [Hardcoded constants now configurable](#hardcoded-constants-now-configurable)
   - [Optimizer V2](#optimizer-v2)
4. [Section: `risk`](#section-risk)
5. [Section: `execution`](#section-execution)
6. [Section: `rate_limit`](#section-rate_limit)
7. [Section: `logging`](#section-logging)
8. [Section: `postmortem`](#section-postmortem)
9. [Section: `env` and Environment Variables](#section-env-and-environment-variables)
10. [Cross-Field Validation Rules](#cross-field-validation-rules)
11. [Removed / Deprecated Fields](#removed--deprecated-fields)
12. [Minimal Working Config](#minimal-working-config)

---

## How Config Loading Works

```
go run ./cmd/bot/ -config <path>
go run ./cmd/bot/ -config <path> -validate    # parse + validate, then exit
```

There is no implicit default config path — `-config` is required.

**Resolution order** (highest precedence wins):

1. **Environment variables** override matching `env:` YAML fields (`QUICKNODE_SUBDOMAIN`, `QUICKNODE_AUTH_TOKEN`, `PRIVATE_KEY`).
2. **Top-level YAML values** are the baseline.
3. **In-code defaults** apply to any field left at its zero value, when a default exists.

Validation runs once at load time. Failures are fatal — the bot does not start with an invalid config. All resolved values are logged on startup so a complete picture lives in the run logs.

**Subdomain shorthand.** `env.quicknode_subdomain` may be a bare label (e.g. `myacct`); the loader auto-suffixes `.hype-mainnet.quiknode.pro`. The full QuickNode endpoint is `https://{subdomain}/{token}`.

---

## Section: `strategy`

Defines the single asset the bot quotes.

| Field             | Type       | Default  | Description                                                                                                                                                  |
| ----------------- | ---------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `assets`          | `[]string` | required | A list with exactly one symbol for the single-asset config. Use `xyz:CL` for HIP-3 dex assets, bare `BTC` for standard perps.                                |
| `lookback_window` | int        | required | Required for parsing in all modes. Unused in single-asset mode (the spread engine that consumes it only initializes when ≥2 assets are configured).          |

---

## Section: `market_making`

The largest section. Field groupings below mirror the inline structure of `config.example.yaml`.

### Core quoting

| Field                    | Type    | Default             | Description                                                                                                 |
| ------------------------ | ------- | ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `quote_spread_bps`       | float64 | required (`>0`)     | Half-spread in bps. The bot quotes ±this around fair value.                                                 |
| `quote_spread_bps_buy`   | float64 | `0` (use base)      | Per-side override for the bid half-spread.                                                                  |
| `quote_spread_bps_sell`  | float64 | `0` (use base)      | Per-side override for the ask half-spread.                                                                  |
| `quote_notional`         | float64 | required (`≥10`)    | Per-side per-level notional in USD. Hyperliquid's order minimum is $10.                                     |
| `refresh_interval_ms`    | int     | required (`>0`)     | Base interval between cancel-and-replace cycles.                                                            |
| `inventory_skew_factor`  | float64 | required, `[0, 10]` | Strength of inventory-reducing price shift. `0` = symmetric quoting.                                        |
| `inventory_skew_gamma`   | float64 | `1.0`, `[0.1, 5.0]` | Convex exponent on the skew curve. `>1.0` = gentle at low inventory, aggressive near full.                  |
| `requote_threshold_bps`  | int     | `2`, `≥0`           | Minimum price delta in bps before replacing quotes.                                                         |
| `size_requote_pct`       | float64 | `0.10`, `[0, 1]`    | Minimum fractional size delta before replacing quotes.                                                      |
| `max_quote_age_ms`       | int     | `3000`              | Force-refresh quotes older than this. Must be `≥ refresh_interval_ms`.                                      |
| `max_book_staleness_ms`  | int     | `5000`              | Stop quoting if no book update for this many ms. `-1` disables. When `>0`, must be `> refresh_interval_ms`. |
| `max_no_book_update_ms`  | int     | `0` (use staleness) | Cancel + close positions if no book update for this long. When set, must be `≥ max_book_staleness_ms`.      |
| `dynamic_width`          | bool    | `false`             | Microstructure-aware quote-width scaling using realized vol + book imbalance.                               |
| `width_vol_window`       | int     | `60`                | Samples for realized vol rolling window.                                                                    |
| `width_max_multiplier`   | float64 | `3.0`, `≥1.0`       | Hard cap on per-side width scaling from `dynamic_width`.                                                    |
| `width_imbalance_levels` | int     | `5`                 | Book depth levels used in imbalance computation.                                                            |

**See:** [Quoting Pipeline](./ALGORITHM.md#quoting-pipeline) for the full per-cycle pipeline.

### VWAP & fair value

| Field                              | Type    | Default                                           | Description                                                                                                  |
| ---------------------------------- | ------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------- | -------------------------------- |
| `vwap_short_window_minutes`        | int     | `5`, `≥1`                                         | Short VWAP window. Used as the FV anchor.                                                                    |
| `vwap_long_window_minutes`         | int     | `30`, `≥short`                                    | Long VWAP window. Used to detect mean-reversion wicks.                                                       |
| `vwap_alpha`                       | float64 | `0.40`, `[0, 1]`                                  | Blend coefficient: `0` = pure microprice, `1` = pure short VWAP.                                             |
| `vwap_alpha_drift_scaling_enabled` | bool    | `false`                                           | **P2**: Fade `vwap_alpha` toward `vwap_alpha_floor` when short/long VWAPs diverge sharply (sustained trend). |
| `vwap_alpha_drift_k`               | float64 | required if drift enabled                         | Sigma multiplier for the divergence trigger. Higher = less aggressive fade.                                  |
| `vwap_alpha_floor`                 | float64 | required if drift enabled, `≤ vwap_alpha`         | Minimum effective alpha after fading.                                                                        |
| `vwap_alpha_trend_boost_enabled`   | bool    | `false`                                           | **V31**: Boost `vwap_alpha` toward a ceiling during confirmed trends.                                        |
| `vwap_alpha_trend_boost_ceiling`   | float64 | required if boost enabled, `≥ vwap_alpha`, `≤1.0` | Max alpha during a trend boost.                                                                              |
| `trend_fv_drift_bps`               | float64 | `0` (disabled), `[0, 20]`                         | **V31**: Shift FV in the trend direction by `autocorr × this`.                                               |
| `trend_fv_drift_autocorr_min`      | float64 | `0.10`, `[0, 1]`                                  | Minimum `                                                                                                    | autocorr | ` to trigger the FV drift shift. |
| `microprice_levels`                | int     | `1`, `≥1`                                         | Book levels in the multi-level microprice. `1` = BBO only.                                                   |
| `max_fv_deviation_pct`             | float64 | `10.0`                                            | Clamp FV when it deviates more than this from the book mid (stale-VWAP protection). `0` disables the clamp.  |

**See:** [Fair Value Computation](./ALGORITHM.md#fair-value-computation).

### Wick mean-reversion sizing

When mid deviates from both short and long VWAPs in the same direction, lean into the expected reversion with larger entry size and tighter spacing on the favored side.

| Field                           | Type    | Default                      | Description                                                             |
| ------------------------------- | ------- | ---------------------------- | ----------------------------------------------------------------------- |
| `wick_size_multiplier`          | float64 | `2.0`, `≥1.0`                | Max sizing increase on fresh wicks.                                     |
| `wick_dev_threshold_pct`        | float64 | `0.10`, `≥0`                 | Min % deviation from short VWAP to engage wick logic.                   |
| `wick_long_dev_threshold_pct`   | float64 | `0.50`                       | Min % deviation from long VWAP (second condition).                      |
| `wick_max_inventory_util`       | float64 | `0` (always allow), `[0, 1]` | Disable wick amplification once inventory util exceeds this.            |
| `wick_trend_suppress_enabled`   | bool    | `false`                      | Suppress entry-side wick boost when trend/flow/shock guards are active. |
| `wick_trend_suppress_threshold` | float64 | `0.50`, `[0, 1]`             | Suppression threshold for the wick-trend gate.                          |

### Ladder construction

| Field                     | Type    | Default             | Description                                                                               |
| ------------------------- | ------- | ------------------- | ----------------------------------------------------------------------------------------- |
| `ladder_levels`           | int     | `1`, `≥1`           | Orders per side.                                                                          |
| `ladder_spacing_bps`      | int     | required, `≥0`      | Bps between rungs (linear baseline).                                                      |
| `ladder_size_graduation`  | float64 | `1.0`, `[1, 10]`    | Geometric weight ratio per level (`1.0` = uniform, `>1.0` = larger size on outer levels). |
| `ladder_spacing_dynamic`  | bool    | `false`             | Vol-scale inter-level spacing (widens when realized vol > baseline).                      |
| `ladder_spacing_exponent` | float64 | `1.0`, `[0.5, 3.0]` | Power-law spacing curve. `1.0` = linear, `>1.0` = dense inner / sparse outer.             |
| `lazy_ladder_enabled`     | bool    | `false`             | Start with 1 level/side; expand to `ladder_levels` only after a fill.                     |
| `lazy_ladder_decay_sec`   | int     | `30`                | Seconds after the last fill before collapsing back to 1 level.                            |
| `quiet_ladder_reduce_sec` | int     | `30`                | Reduce ladder levels after this many quiet (no-fill) seconds.                             |

### Wall-fronting

Snap the nearest ladder level in front of large resting orders (walls) detected by `wall_min_size_multiplier × avg book level size`.

| Field                                 | Type    | Default                  | Description                                                                                                    |
| ------------------------------------- | ------- | ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `wall_front_enabled`                  | bool    | `false`                  | Master toggle.                                                                                                 |
| `wall_min_size_multiplier`            | float64 | `3.0`, `≥1.0` if enabled | Wall must be Nx the average book level size.                                                                   |
| `wall_quality_enabled`                | bool    | `false`                  | Disable side-specific wall-fronting when snapped fills produce adverse markouts.                               |
| `wall_quality_half_life_sec`          | int     | `300`, `>0` if enabled   | EWMA half-life for the wall-front quality score.                                                               |
| `wall_quality_min_score`              | float64 | `0.35`, `[0, 1]`         | Score below which the side is disabled temporarily.                                                            |
| `wall_quality_disable_sec`            | int     | `120`, `>0` if enabled   | Disable duration after a poor wall-front score.                                                                |
| `wall_quality_hysteresis_sec`         | int     | `0` (immediate flip)     | **P7**: Min continuous hold time before allowing a state flip. Prevents thrash.                                |
| `wall_quality_reenable_threshold_bps` | float64 | `0` (disabled)           | Re-enable threshold for asymmetric hysteresis. Mapped score `0.5 + bps/10` must be `≥ wall_quality_min_score`. |

### Fees

| Field               | Type    | Default   | Description                                             |
| ------------------- | ------- | --------- | ------------------------------------------------------- |
| `maker_fee_bps`     | float64 | `0`, `≥0` | Maker rebate/fee (bps).                                 |
| `builder_fee_bps`   | float64 | `0`, `≥0` | Paper-model builder fee only; keep at zero for the local direct execution path. |
| `fee_floor_enabled` | bool    | `false`   | Widen quotes to cover round-trip maker + builder fees.  |

### Edge gate

The edge gate floors the **exposure-increasing** side at measured fees + measured
adverse selection + a target net edge. Exit-side quotes keep the normal skew /
position-age / idle-breaker pipeline so inventory can always be reduced. It is
off by default; the Zeus Freqtrade preset enables it.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `edge_gate_enabled` | bool | `false` | Enable the per-side cost floor. |
| `edge_target_bps` | float64 | `1.0`, `[0, 50]` | Target net edge per round trip. The per-side requirement adds half of it. |
| `edge_adverse_floor_bps` | float64 | `0.75`, `[0, 50]` | Adverse-selection buffer used before the markout EWMA is warm. |
| `edge_max_width_bps` | float64 | `10.0`, `[0, 200]` | Maximum half-spread the gate may impose; above it the side is not quoted. |
| `edge_fee_floor_bps` | float64 | `0.5`, `[0, 50]` | Floor applied to the measured fee so one cheap fill cannot collapse the gate. |

Required half-spread formula: `fee_bps + adverse_bps + edge_target_bps / 2`.
The fee is the EWMA of `(fill.Fee + fill.BuilderFee) / notional` once a fill has
been seen, otherwise `maker_fee_bps + builder_fee_bps`.

### Trade flow (imbalance & post-fill decay)

| Field                           | Type    | Default                       | Description                                                                      |
| ------------------------------- | ------- | ----------------------------- | -------------------------------------------------------------------------------- |
| `trade_imbalance_window_sec`    | int     | `3`, `≥1`                     | Rolling window for buy/sell trade imbalance.                                     |
| `trade_imbalance_max_shift_bps` | float64 | `0` (disabled), `≥0`          | Max FV shift in bps from trade imbalance.                                        |
| `trade_imbalance_size_exponent` | float64 | `1.0`, `(0, 5]`               | Power-law on trade size weighting. `<1.0` dampens whales, `>1.0` amplifies them. |
| `trade_imbalance_mode`          | string  | `"time"`                      | `"time"` (rolling window) or `"volume"` (VPIN-lite volume clock).                |
| `trade_imbalance_bucket_size`   | float64 | `5000.0`, `>0` if volume mode | $ per volume bucket.                                                             |
| `trade_imbalance_bucket_count`  | int     | `20`, `≥1` if volume mode     | Completed buckets to keep.                                                       |
| `fill_adverse_shift_bps`        | float64 | `0`, `≥0`                     | Post-fill FV shift in bps to lean against post-fill adverse moves.               |
| `fill_decay_half_life_ms`       | int     | `2000`, `≥1`                  | Half-life for the post-fill FV shift to decay.                                   |
| `trade_stale_degraded_sec`      | int     | `0` (=10s)                    | Degrade quoting if no trades for this long.                                      |
| `trade_stale_halt_sec`          | int     | `0` (=30s)                    | Halt quoting if no trades for this long.                                         |

### Crossing protection

| Field                      | Type    | Default          | Description                                                                                                                             |
| -------------------------- | ------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `inventory_skew_asymmetry` | float64 | `0.5`, `[0, 1]`  | `>0.5` = more shift on the inventory-reducing side.                                                                                     |
| `min_crossing_spread_bps`  | float64 | `2.0`, `[0, 20]` | Minimum spread maintained by crossing protection. Position-aware: keeps the exit side at its computed price; pulls only the entry side. |

### Adaptive notional

| Field                              | Type    | Default                     | Description                                                   |
| ---------------------------------- | ------- | --------------------------- | ------------------------------------------------------------- |
| `adaptive_notional_enabled`        | bool    | `false`                     | **Feature 2**: Adjust quote notional based on EWMA fill rate. |
| `adaptive_notional_target_per_min` | float64 | required if enabled         | Target filled $/min.                                          |
| `adaptive_notional_min`            | float64 | required if enabled         | Floor notional.                                               |
| `adaptive_notional_max`            | float64 | required if enabled, `≥min` | Ceiling notional.                                             |
| `adaptive_notional_half_life_sec`  | int     | `60`, `≥1`                  | EWMA smoothing half-life.                                     |
| `regime_window`                    | int     | `120`, `≥10`                | Rolling window for autocorrelation (used by optimizer).       |

### Markout tracking & FV feedback

| Field                        | Type         | Default                    | Description                                                                                |
| ---------------------------- | ------------ | -------------------------- | ------------------------------------------------------------------------------------------ |
| `markout_enabled`            | bool         | `false`                    | Track post-fill mid-price drift to measure adverse selection.                              |
| `markout_ewma_half_life_sec` | int          | `300`                      | EWMA half-life for the per-fill markout score.                                             |
| `markout_fv_scale`           | float64      | `0.5`, `≥0`                | **Smart Layer 1**: Fraction of markout EWMA applied as FV shift. `0` disables FV feedback. |
| `markout_fv_max_shift_bps`   | float64      | `3.0`, `≥0`                | Max FV shift magnitude from markout feedback.                                              |
| `markout_composite_weights`  | `[4]float64` | `[0.20, 0.40, 0.25, 0.15]` | Weights for the 500ms / 1s / 2s / 5s markout horizons. Each ≥0.                            |

### Refresh interval (dynamic + post-fill)

**A7 dynamic refresh** — context-aware refresh timing that overrides `refresh_interval_ms` per situation.

| Field                        | Type | Default              | Description                                                 |
| ---------------------------- | ---- | -------------------- | ----------------------------------------------------------- |
| `fill_refresh_cooldown_ms`   | int  | `2000`, `≥0`         | Min ms between fill-triggered refreshes.                    |
| `refresh_post_fill_ms`       | int  | `0` (disabled), `≥0` | Faster refresh after fills.                                 |
| `refresh_adverse_ms`         | int  | `0` (disabled), `≥0` | Slower refresh when toxicity tracker shows adverse markout. |
| `refresh_quiet_ms`           | int  | `0` (disabled), `≥0` | Slower refresh during quiet book periods.                   |
| `refresh_quiet_threshold_ms` | int  | `0`, `≥0`            | Book-silence threshold for switching to quiet refresh.      |

### Toxicity (per-side adverse selection)

**B2 per-side toxicity.** When enabled, buy and sell EWMAs are tracked separately for asymmetric spread + size adjustment.

| Field                            | Type    | Default                                 | Description                                                     |
| -------------------------------- | ------- | --------------------------------------- | --------------------------------------------------------------- |
| `toxicity_half_life_sec`         | int     | `120`, `≥1`                             | EWMA half-life.                                                 |
| `toxicity_max_widen`             | float64 | `2.0`, `≥1.0`                           | Max spread multiplier from toxicity.                            |
| `toxicity_baseline_bps`          | float64 | `0` (auto = half of `quote_spread_bps`) | Expected capture bps; markout below this counts as adverse.     |
| `toxicity_per_side`              | bool    | `false`                                 | Track buy/sell EWMAs separately.                                |
| `toxicity_size_reduce_threshold` | float64 | `-2.0`                                  | Markout (bps) below which the toxic side is shrunk.             |
| `toxicity_size_reduce_max`       | float64 | `0.5`                                   | Min size factor on the toxic side (0.5 = halve).                |
| `toxicity_size_exponent`         | float64 | `1.0`, `(0, 5]`                         | Power-law on the size reduction curve.                          |
| `toxicity_t5s_weight`            | float64 | `0`, `[0, 1)`                           | Weight of the 5s-horizon markout in the per-side composite.     |
| `toxicity_idle_decay_half_lives` | float64 | `0` (=4.0), `<0` disabled               | Grace period (in half-lives) before idle decay kicks in.        |
| `toxicity_warmup_fills`          | int     | `0` (=5), `<0` disabled                 | Suppress per-side multipliers until this many fills accumulate. |
| `toxicity_size_weight_cap`       | float64 | `3.0`, `≥1.0`                           | Max allowed toxicity-based size scaling factor.                 |

> Per-side toxicity widening is automatically attenuated when the optimizer has already widened the same side, to avoid double-counting.

### Inventory controller (B3)

Half-life-based adaptive skew; turns inventory drift into a PID-controlled skew multiplier.

| Field                           | Type    | Default        | Description                                      |
| ------------------------------- | ------- | -------------- | ------------------------------------------------ |
| `inventory_half_life_minutes`   | float64 | `0` (disabled) | Target half-life for inventory decay in minutes. |
| `inventory_controller_skew_min` | float64 | `1.0`          | Min skew multiplier from the controller.         |
| `inventory_controller_skew_max` | float64 | `5.0`          | Max skew multiplier from the controller.         |
| `inventory_controller_gain_p`   | float64 | `0.10`         | Proportional gain.                               |
| `inventory_controller_gain_i`   | float64 | `0.02`         | Integral gain.                                   |
| `inventory_controller_gain_d`   | float64 | `0.05`         | Derivative gain.                                 |

### Inventory exit-preserve & entry-side reduction

| Field                          | Type    | Default                   | Description                                                                                               |
| ------------------------------ | ------- | ------------------------- | --------------------------------------------------------------------------------------------------------- | ---------------- | ------ |
| `inventory_skew_exit_preserve` | bool    | `false`                   | **A1**. When true, never skew AWAY from exit; only widen the entry side, leave exit at its natural price. |
| `entry_size_reduction_rate`    | float64 | `0` (disabled), `[0, 5]`  | **A6**. Linear entry-size reduction rate. At rate=0.8 and 50% util → 60% entry size.                      |
| `entry_size_reduction_floor`   | float64 | `0.2` if rate>0, `[0, 1]` | Minimum entry-side fraction (never zero).                                                                 |
| `entry_accel_threshold`        | float64 | `0` (disabled), `[0, 1]`  | **A6b**. Inventory util above which acceleration kicks in.                                                |
| `entry_accel_exponent`         | float64 | `0` (disabled), `[0, 10]` | Power-law exponent applied to entry size factor above threshold.                                          |
| `entry_reduction_trend_k`      | float64 | `0` (disabled), `[0, 10]` | **V31**. Scale reduction rate by `(1 +                                                                    | adverse_autocorr | × k)`. |

### Warmup

Reduced size and wider spread during the first N seconds after startup.

| Field                      | Type    | Default                                  | Description                              |
| -------------------------- | ------- | ---------------------------------------- | ---------------------------------------- |
| `warmup_notional_pct`      | float64 | `1.0` (no warmup); inline default `0.10` | Fraction of base notional during warmup. |
| `warmup_spread_multiplier` | float64 | `1.0` (no warmup); inline default `1.8`  | Spread multiplier during warmup.         |
| `warmup_duration_sec`      | int     | `0` (disabled); inline default `60`      | Warmup length in seconds.                |

### Fill burst circuit breaker

Widen + reduce size on rapid consecutive fills (likely informed flow).

| Field                    | Type    | Default                  | Description                            |
| ------------------------ | ------- | ------------------------ | -------------------------------------- |
| `fill_burst_window_sec`  | float64 | `0` (disabled)           | Rolling window to count fills.         |
| `fill_burst_threshold`   | int     | `3`, `≥1` if window>0    | Fills in window to trigger burst mode. |
| `fill_burst_spread_mult` | float64 | `2.0`, `≥1.0`            | Spread multiplier during burst.        |
| `fill_burst_size_factor` | float64 | `0.3`, `(0, 1]`          | Size factor during burst.              |
| `fill_burst_decay_sec`   | float64 | `15.0`, `>0` if window>0 | Burst decay time.                      |

### Volume burst ladders

Double ladder levels during trade-velocity spikes (capture transient liquidity demand).

| Field                      | Type    | Default     | Description                                                |
| -------------------------- | ------- | ----------- | ---------------------------------------------------------- |
| `volume_burst_enabled`     | bool    | `false`     | Master toggle.                                             |
| `burst_velocity_threshold` | float64 | `2.0`       | Fast/slow EWMA ratio that triggers burst.                  |
| `burst_velocity_fast_sec`  | float64 | `5.0`       | Fast EWMA window.                                          |
| `burst_velocity_slow_sec`  | float64 | `60.0`      | Slow EWMA window.                                          |
| `burst_velocity_min_floor` | float64 | `0.5`       | Minimum slow-EWMA floor (avoids division by tiny numbers). |
| `burst_level_multiplier`   | int     | `2`         | Ladder level multiplier during burst.                      |
| `burst_decay_sec`          | float64 | `30.0`      | Burst decay.                                               |
| `velocity_initial_seed`    | float64 | `1.0`, `>0` | Initial trade-velocity EWMA at startup.                    |

### Trend Guard

VWAP momentum entry gate — suppress entry-side notional when mid deviates persistently from the long VWAP (sustained directional move).

| Field                                | Type    | Default                   | Description                                                           |
| ------------------------------------ | ------- | ------------------------- | --------------------------------------------------------------------- |
| `trend_guard_enabled`                | bool    | `false`                   | Master toggle.                                                        |
| `trend_guard_threshold_pct`          | float64 | `0.15`, `(0, 5]`          | Long-VWAP deviation % to start suppression.                           |
| `trend_guard_saturation_pct`         | float64 | `0.80`, `> threshold`     | Deviation % for max suppression.                                      |
| `trend_guard_max_suppress`           | float64 | `0.90`, `(0, 1]`          | Max entry notional reduction (`0.90` = reduce to 10%).                |
| `trend_guard_spread_widen_bps`       | float64 | `0` (disabled), `[0, 20]` | Widen entry side by this many bps when suppress ≥ 0.5.                |
| `trend_guard_spread_widen_asymmetry` | float64 | `0` (=1.0), `[0, 5]`      | **V31**. Entry-side widening multiplier. `2.0` = entry gets 2× widen. |
| `trend_guard_freeze_threshold`       | float64 | `0` (disabled), `[0, 1]`  | Zero entry side entirely once suppress reaches this threshold.        |

### Regime Gate (P1)

Suppress trend-aligned entry side during sustained autocorrelated trends.

| Field                          | Type    | Default          | Description                                               |
| ------------------------------ | ------- | ---------------- | --------------------------------------------------------- | -------------- | ------------------------- |
| `regime_gate_enabled`          | bool    | `false`          | Master toggle.                                            |
| `regime_gate_mode`             | string  | `"exit_only"`    | Only accepted value. `"directional"` reserved for future. |
| `regime_autocorr_threshold`    | float64 | `0.35`, `(0, 1)` | `                                                         | autocorr       | ` required to qualify.    |
| `regime_vwap_divergence_sigma` | float64 | `2.0`, `>0`      | `                                                         | shortV − longV | / σ` required to qualify. |
| `regime_gate_min_duration_sec` | int     | `30`, `≥0`       | Min seconds qualified before activation.                  |
| `regime_gate_max_suppression`  | float64 | `0.80`, `[0, 1]` | Fraction of entry-side notional removed at max.           |

### Price Shock Guard

Fast mid-price-move protection independent of volume burst.

| Field                                       | Type    | Default                         | Description                                                                            |
| ------------------------------------------- | ------- | ------------------------------- | -------------------------------------------------------------------------------------- | -------- | --------------------------------------- |
| `price_shock_enabled`                       | bool    | `false`                         | Master toggle.                                                                         |
| `price_shock_window_ms`                     | int     | `5000`, `>0`                    | Rolling mid-price window.                                                              |
| `price_shock_threshold_bps`                 | float64 | `35.0`, `>0`                    | Absolute move threshold over the window.                                               |
| `price_shock_cooldown_ms`                   | int     | `15000`, `>0`                   | Guard active duration after trigger.                                                   |
| `price_shock_freeze_entry`                  | bool    | `false`                         | Zero/cancel the inventory-increasing side while active.                                |
| `price_shock_exit_size_factor`              | float64 | `0` (disabled), `[0, 1]`        | **V24**. Exit-side size factor during adverse shock. `1.0` = no change, `<1` = shrink. |
| `price_shock_favorable_exit_factor`         | float64 | `0` (disabled), `[0, 5]`        | **V30**. Exit-side boost when shock direction favors inventory (profit-taking).        |
| `price_shock_favorable_exit_tighten_bps`    | float64 | `0` (disabled), `[0, 50]`       | **V30**. Absolute bps tightening on the profit-taking side during favorable shock.     |
| `price_shock_favorable_ioc_frac`            | float64 | `0` (disabled), `[0, 1]`        | **V30 part 4**. Fraction of position to IOC-close on favorable shock.                  |
| `price_shock_favorable_ioc_min_inv_util`    | float64 | `0`, `[0, 1]` if `ioc_frac>0`   | Min `                                                                                  | inv_util | ` to fire the IOC.                      |
| `price_shock_favorable_ioc_min_capture_bps` | float64 | `0`, `≥0` if `ioc_frac>0`       | Min shock `                                                                            | move_bps | ` to fire (should exceed 2× taker fee). |
| `price_shock_favorable_ioc_slippage_bps`    | float64 | `0`, `[0, 100]` if `ioc_frac>0` | Slippage past best-opposite when pricing the IOC.                                      |

### Shock Chain Detector

**V30 part 3.** Count shocks in a rolling window; escalate cascades to flatten + pause + recovery mode.

| Field                      | Type | Default          | Description                                                            |
| -------------------------- | ---- | ---------------- | ---------------------------------------------------------------------- |
| `shock_chain_enabled`      | bool | `false`          | Master toggle.                                                         |
| `shock_chain_window_sec`   | int  | required (`>0`)  | Rolling cascade window.                                                |
| `shock_chain_threshold`    | int  | required (`≥2`)  | Shocks in window to trigger escalation.                                |
| `shock_chain_pause_sec`    | int  | required (`≥60`) | Pause duration after cascade.                                          |
| `shock_chain_recovery_sec` | int  | required (`≥0`)  | Recovery mode duration after pause (reduced notional / wider spreads). |

### Directional Flow Gate

Suppress entry-side notional on adverse trade-flow imbalance.

| Field                    | Type    | Default                           | Description                                          |
| ------------------------ | ------- | --------------------------------- | ---------------------------------------------------- | -------- | -------------------------------------------------- |
| `flow_gate_enabled`      | bool    | `false`                           | Master toggle.                                       |
| `flow_gate_threshold`    | float64 | `0.50` (when enabled), `(0, 1)`   | Imbalance magnitude to start gating.                 |
| `flow_gate_max_suppress` | float64 | `0.70` (when enabled), `(0, 1]`   | Max entry notional reduction (`0.70` = down to 30%). |
| `flow_gate_min_util`     | float64 | `0` (default `0.25` in algorithm) | Scale suppression by `                               | inv_util | `below this floor.`0` = full suppress at any util. |

### Position-age exit tightening

| Field                          | Type    | Default        | Description                                  |
| ------------------------------ | ------- | -------------- | -------------------------------------------- |
| `position_age_tighten_min_sec` | int     | `0` (disabled) | Seconds before tightening starts.            |
| `position_age_tighten_bps`     | float64 | `0.5`          | Bps/min of exit-side tightening once active. |

### Per-side win rate gate (P6)

Widen the offending side's spread if its win rate OR profit factor falls below threshold.

| Field                              | Type    | Default           | Description                                                         |
| ---------------------------------- | ------- | ----------------- | ------------------------------------------------------------------- |
| `per_side_winrate_enabled`         | bool    | `false`           | Master toggle.                                                      |
| `per_side_winrate_window_fills`    | int     | `50`, `[10, 500]` | Rolling fill window per side.                                       |
| `per_side_winrate_threshold`       | float64 | `0.35`, `(0, 1)`  | Win rate below this widens that side.                               |
| `per_side_profit_factor_threshold` | float64 | `0.9`, `>0`       | Profit factor below this widens that side.                          |
| `per_side_winrate_widen_bps`       | float64 | `0.5`, `≥0`       | Bps added to the offending side's spread delta when the gate fires. |

### Idle-fill circuit breaker (P9)

One-shot exit-side tighten when position sits adverse with no fills.

| Field                          | Type    | Default            | Description                                      |
| ------------------------------ | ------- | ------------------ | ------------------------------------------------ | -------- | --------------------------- |
| `idle_fill_detector_enabled`   | bool    | `false`            | Master toggle.                                   |
| `idle_fill_threshold_sec`      | int     | required (`>0`)    | Seconds since last fill before breaker can fire. |
| `idle_fill_min_inv_util`       | float64 | required, `(0, 1]` | `                                                | inv_util | ` threshold for activation. |
| `idle_exit_tighten_bps`        | float64 | required, `≥0`     | Exit-side tighten amount in bps.                 |
| `idle_exit_size_multiplier`    | float64 | required, `≥1.0`   | Exit-side size multiplier.                       |
| `idle_fill_retry_interval_sec` | int     | required (`>0`)    | Cooldown between consecutive triggers per asset. |

> Tighten runs BEFORE crossing protection, so it cannot cross the book.

### Time-of-day adjustments

**A9 spread widening** (overnight illiquid hours).

| Field                       | Type    | Default | Description                              |
| --------------------------- | ------- | ------- | ---------------------------------------- |
| `tod_spread_start_hour_utc` | int     | `0`     | Start hour (UTC) for overnight widening. |
| `tod_spread_end_hour_utc`   | int     | `0`     | End hour (UTC).                          |
| `tod_spread_extra_bps`      | float64 | `0`     | Extra bps during the overnight window.   |

**P12 trading schedule** — global only, scales notional inside configured windows. Most-conservative wins on overlap.

| Field                      | Type            | Default | Description    |
| -------------------------- | --------------- | ------- | -------------- |
| `trading_schedule_enabled` | bool            | `false` | Master toggle. |
| `trading_schedule`         | list of windows | `[]`    | See below.     |

Each `trading_schedule` entry:

| Subfield              | Type    | Notes                                                                       |
| --------------------- | ------- | --------------------------------------------------------------------------- |
| `label`               | string  | Free-form label (logged).                                                   |
| `start`               | string  | `HH:MM` 24h, `00:00`–`23:59`.                                               |
| `end`                 | string  | `HH:MM`. Must be `> start` same calendar day.                               |
| `tz`                  | string  | IANA timezone (e.g. `America/New_York`). Validated via `time.LoadLocation`. |
| `notional_multiplier` | float64 | `[0, 1]`. Multiplier applied to global notional inside the window.          |

### Funding-rate session-end urgency

**Improvement 5.** Amplifies inventory skew near funding resets when paying funding (accelerates exit). Reduces skew when receiving funding (holds position).

| Field                             | Type    | Default                        | Description                                        |
| --------------------------------- | ------- | ------------------------------ | -------------------------------------------------- |
| `funding_urgency_enabled`         | bool    | `false`                        | Master toggle.                                     |
| `funding_urgency_scale`           | float64 | `1.5`, `(0, 10]`               | Max skew multiplier boost.                         |
| `funding_urgency_horizon_minutes` | float64 | `30.0`, `(0, 120]`             | Minutes before reset where urgency ramps linearly. |
| `funding_poll_interval_sec`       | int     | `60`, `≥10` if set             | Seconds between funding-rate API polls.            |
| `funding_interval_hours`          | float64 | `0` (auto: 1h HIP-3, 8h perps) | Per-asset funding interval override.               |

### Smart layers (spread cap)

| Field                         | Type    | Default       | Description                                                                                                                                                                               |
| ----------------------------- | ------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `max_total_spread_multiplier` | float64 | `3.0`, `≥1.0` | **Smart Layer 2.** Hard cap on cumulative per-side spread multiplier across all widening sources. Prevents toxicity + burst + optimizer compounding into unfillable quotes. `0` disables. |

### Hardcoded constants now configurable

| Field                                | Type    | Default         | Description                                            |
| ------------------------------------ | ------- | --------------- | ------------------------------------------------------ |
| `microprice_decay`                   | float64 | `0.7`, `(0, 1)` | Geometric decay for multi-level microprice weighting.  |
| `emergency_widen_threshold_bps`      | float64 | `-3.0`          | Markout below this triggers emergency spread widening. |
| `emergency_widen_amount_bps`         | float64 | `2.0`, `≥0`     | Extra spread added on emergency widen.                 |
| `quote_attribution_enabled`          | bool    | `false`         | Sample per-layer quote attribution logs.               |
| `quote_attribution_min_interval_sec` | int     | `30`, `≥0`      | Min seconds between sampled attribution logs.          |

### Optimizer V2

A unified utility-driven controller that uses a 4D state model (toxicity, opportunity, inventory stress, budget pressure) and proportional-with-momentum control to adjust 8 quoting knobs each eval cycle. Block: `market_making.optimizer_v2`.

#### Core control loop

| Field                          | Type    | Default               | Description                                                            |
| ------------------------------ | ------- | --------------------- | ---------------------------------------------------------------------- |
| `enabled`                      | bool    | `false`               | Master toggle.                                                         |
| `shadow_mode`                  | bool    | `false`               | Evaluate but apply a neutral overlay (log-only).                       |
| `eval_interval_sec`            | int     | `30`                  | Seconds between evaluations.                                           |
| `warmup_evals`                 | int     | `5`                   | Eval cycles before v2 acts.                                            |
| `min_fill_samples`             | int     | `5`                   | Min fills required in window before v2 acts.                           |
| `min_fill_sample_window_sec`   | int     | `0` (per-eval legacy) | Rolling window for fill counting. `0` = per-eval mode.                 |
| `momentum`                     | float64 | `0.8`, `[0, 1)`       | Knob update smoothing momentum. `0` = no momentum, `1` = full inertia. |
| `learning_rate`                | float64 | `0.1`, `(0, 1]`       | Step size for proportional control.                                    |
| `max_knob_changes_per_eval`    | int     | `3`                   | Cap on knobs adjusted per cycle.                                       |
| `emergency_toxicity_threshold` | float64 | `0.9`, `(0, 1]`       | Composite toxicity score that triggers emergency widening.             |

#### Rollback

| Field                         | Type    | Default              | Description                                                                                                                                            |
| ----------------------------- | ------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `rollback_window_evals`       | int     | `3`                  | Evals to look back when detecting utility decline.                                                                                                     |
| `rollback_rate`               | float64 | `0.5`, `(0, 1]`      | Fraction to revert each rolled-back knob by.                                                                                                           |
| `rollback_cooldown_sec`       | int     | `120`                | Directional cooldown after rollback to prevent cycles.                                                                                                 |
| `rollback_absolute_floor_bps` | float64 | `0` (disabled), `≤0` | **V30 part 5.** Force rollback when composite markout stays below this floor across the rollback window, regardless of Δ-from-snapshot. Must be `≤ 0`. |

#### Burst-triggered eval

| Field                                  | Type | Default              | Description                                                                                                                                     |
| -------------------------------------- | ---- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `burst_eval_enabled`                   | bool | `false`              | Run an out-of-cadence eval when volume burst activates.                                                                                         |
| `burst_eval_min_interval_sec`          | int  | `0`                  | Throttle for burst-triggered evals.                                                                                                             |
| `burst_eval_all_negative_cooldown_sec` | int  | `0` (disabled), `≥0` | **V30 part 5.** Extended cooldown while utility is all-negative across the rollback window. Replaces the regular burst cooldown in that regime. |

#### Force-refresh on emergency

| Field                                | Type    | Default      | Description                                                   |
| ------------------------------------ | ------- | ------------ | ------------------------------------------------------------- |
| `force_refresh_on_emergency`         | bool    | `false`      | Force quote refresh after material emergency overlay changes. |
| `force_refresh_min_interval_ms`      | int     | `3000`, `≥0` | Throttle for forced emergency refreshes.                      |
| `force_refresh_min_spread_delta_bps` | float64 | `0.5`, `≥0`  | Minimum overlay change (bps) to consider material.            |

#### Composite weight blocks

Each weight block must sum to ≈1.0 (validated in `[0.95, 1.05]`).

`toxicity_weights`:

| Field             | Default |
| ----------------- | ------- |
| `markout_ewma`    | `0.5`   |
| `fill_burst`      | `0.3`   |
| `emergency_count` | `0.2`   |

`opportunity_weights`:

| Field              | Default |
| ------------------ | ------- |
| `fill_rate_ratio`  | `0.4`   |
| `regime_signal`    | `0.2`   |
| `vwap_deviation`   | `0.2`   |
| `spread_bbo_ratio` | `0.2`   |

`inventory_stress_weights`:

| Field                | Default |
| -------------------- | ------- |
| `position_util`      | `0.5`   |
| `controller_adj`     | `0.3`   |
| `same_side_duration` | `0.2`   |

#### Policy thresholds (`policy_thresholds`)

| Field                               | Default                  | Notes                                                                           |
| ----------------------------------- | ------------------------ | ------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| `toxicity_high`                     | `0.6`                    |                                                                                 |
| `toxicity_medium`                   | `0.5`                    |                                                                                 |
| `toxicity_low`                      | `0.3`                    |                                                                                 |
| `opportunity_high`                  | `0.5`                    |                                                                                 |
| `opportunity_low`                   | `0.3`                    |                                                                                 |
| `inventory_high`                    | `0.4`                    |                                                                                 |
| `inventory_low`                     | `0.15`                   |                                                                                 |
| `budget_high`                       | `0.7`                    |                                                                                 |
| `budget_medium`                     | `0.5`                    |                                                                                 |
| `autocorr_threshold`                | `0.1`                    |                                                                                 |
| `fill_rate_delta`                   | `0.5`                    |                                                                                 |
| `imbalance_sat_high`                | `0.6`                    |                                                                                 |
| `imbalance_sat_low`                 | `0.2`                    |                                                                                 |
| `peer_inventory_min`                | `0.2`                    |                                                                                 |
| `opp_tighten_inv_util_gate`         | `0` (disabled), `≥0`     | **P8 / V25.** Block opp_tighten when `                                          | inv_util | > gate` AND unrealized < 0. Asymmetric: when blocked AND carrying inventory, exit-side tighten still applies. |
| `opp_tighten_session_pnl_floor_bps` | `0` (disabled), `≤0`     | Block opp_tighten when session PnL below this floor. Must be `<0` to be active. |
| `skew_relax_adverse_markout_bps`    | `0` (disabled), `≤0`     | **V25.** Markout threshold for adverse-markout feedback into skewMult.          |
| `skew_relax_inv_util_min`           | `0`, `[0, 1]`            | `                                                                               | inv_util | ` floor for the rule to fire.                                                                                 |
| `skew_relax_floor`                  | `0` (disabled), `[0, 1]` | Minimum skewMult when floored.                                                  |
| `skew_relax_divisor`                | `0` (=2.0), `≥0`         | Bps-of-adversity per full range of skew relaxation.                             |
| `regime_widen_entry_mult`           | `0` (=1.0), `[0, 3]`     | Entry-side widening multiplier in regime-widen states.                          |

#### Knob bounds

Each is `{min, max}`. The optimizer never moves a knob outside these bounds. Only `spread_bps.max > 0` is strictly required; the others use sensible defaults if omitted.

| Knob                   | Affects                     | Typical defaults         |
| ---------------------- | --------------------------- | ------------------------ |
| `spread_bps`           | `quote_spread_bps`          | `min: 4.0`, `max: 12.0`  |
| `notional_mult`        | `quote_notional` multiplier | `min: 0.70`, `max: 1.30` |
| `skew_mult`            | inventory skew multiplier   | `min: 0.80`, `max: 1.50` |
| `vwap_alpha`           | `vwap_alpha` knob           | `min: 0.28`, `max: 0.48` |
| `imbalance_shift_mult` | trade imbalance sensitivity | `min: 0.5`, `max: 1.5`   |
| `fill_decay_mult`      | post-fill FV decay          | `min: 0.5`, `max: 1.5`   |
| `refresh_mult`         | refresh interval scaling    | `min: 0.6`, `max: 1.5`   |

#### Update deadbands (P4)

Skip a knob update when the proposed `|delta|` is below the deadband — reduces noise-driven thrash.

| Field                       | Default        | Validation |
| --------------------------- | -------------- | ---------- |
| `spread_delta_deadband_bps` | `0` (disabled) | `[0, 1.0]` |
| `skew_delta_deadband`       | `0` (disabled) | `[0, 1.0]` |
| `notional_delta_deadband`   | `0` (disabled) | `[0, 0.5]` |

#### Sub-dimension normalization (P14)

Empirical-quantile normalization for state sub-dimensions.

| Field                          | Default    | Validation                                                              |
| ------------------------------ | ---------- | ----------------------------------------------------------------------- |
| `subdim_normalization`         | `""`       | `""` \| `"linear"` (default, preserves v22.1) \| `"empirical_quantile"` |
| `subdim_quantile_window_evals` | `0` (=500) | `≥100` if normalization=`empirical_quantile` and explicitly set         |
| `subdim_quantile_low_pct`      | `0` (=5)   | `[1, 25]`                                                               |
| `subdim_quantile_high_pct`     | `0` (=95)  | `[75, 99]`; must be `> low`                                             |

#### Fourth-dimension swap (P5)

Swap the optimizer's 4th state dimension between budget pressure and session PnL.

| Field                          | Default                         | Validation                                                                                                                |
| ------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `fourth_dimension`             | `""`                            | `""` \| `"budget_pressure"` (default) \| `"session_pnl"`. `"session_pnl"` requires `risk.session_pnl_window_minutes > 0`. |
| `session_pnl_bucket_edges_bps` | `[-20, -10, 10, 20]`            | Length-4, strictly ascending. Active when `fourth_dimension="session_pnl"`.                                               |
| `fourth_dim_defensive_floor`   | `0.75`, `(0, 1]` if session_pnl | Clamp to 0 when 4th dim ≥ this AND policy would net-tighten spread (pro-cyclical mitigation).                             |

---

## Section: `risk`

| Field                        | Type    | Default                                            | Description                                             |
| ---------------------------- | ------- | -------------------------------------------------- | ------------------------------------------------------- |
| `max_position_notional`      | float64 | required (`>0`)                                    | Position cap.                                           |
| `max_position_halt_tolerance_pct` | float64 | `0`, `[0, 50]`                                | Allow the position to reach `cap × (1 + tol/100)` before a forced flatten. The quote manager already stops entries at the cap; use `10` in Zeus to avoid liquidating normal max inventory. |
| `max_spread_exposure`        | float64 | required (`>0`); must be `≥ max_position_notional` | Combined long+short open-order notional cap.            |
| `max_drawdown_pct`           | float64 | required, `(0, 100]`                               | Hard equity-drawdown stop.                              |
| `max_open_orders`            | int     | required (`>0`)                                    | Open-order cap.                                         |
| `drawdown_cooldown_sec`      | int     | `0` (stay halted)                                  | Seconds before auto-resume after a halt.                |
| `max_consecutive_halts`      | int     | `0` (infinite)                                     | Permanent halt after N consecutive drawdown halts.      |
| `min_resume_equity_pct`      | float64 | `0` (disabled), `[0, 100]`                         | Must recover to N% of high-water equity to resume.      |
| `max_close_slippage_bps`     | int     | `200` (`-1`=market close)                          | IOC slippage limit before falling back to market close. |
| `fail_closed_cooldown_sec`   | int     | `0` (permanent halt)                               | Seconds before auto-recovery from fail-closed halt.     |
| `max_fail_closed_retries`    | int     | `0` (unlimited)                                    | Max recovery attempts before permanent halt.            |
| `absolute_equity_floor`      | float64 | `0` (disabled)                                     | Permanent halt if equity drops below this.              |
| `drawdown_smoothing_samples` | int     | `1` (no smoothing); inline default `10`            | EMA samples for drawdown equity smoothing.              |
| `recovery_duration_sec`      | int     | `0` (disabled); inline default `300`               | Recovery-mode duration after a drawdown halt.           |
| `recovery_notional_pct`      | float64 | `0`; inline default `0.50`                         | Notional fraction during recovery.                      |
| `recovery_spread_multiplier` | float64 | `0`; inline default `1.50`                         | Spread widening during recovery.                        |
| `session_pnl_window_minutes` | int     | `0` (=60)                                          | **P0.** Rolling window for `window_pnl_bps`.            |

### Session stop-loss (P3)

Session-relative PnL floor, distinct from equity drawdown. Triggers flatten-and-pause; resumes in reduced-size recovery mode.

| Field                                     | Type    | Default                         | Description                                                                                                |
| ----------------------------------------- | ------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `session_stop_loss_bps`                   | float64 | `0` (disabled), `≤0`            | Session PnL floor in bps. Suggested `-40` when active.                                                     |
| `session_stop_loss_pause_sec`             | int     | required if active, `≥60`       | Pause duration after the stop triggers.                                                                    |
| `session_stop_loss_recovery_notional_pct` | float64 | required if active, `(0, 1]`    | Notional fraction during recovery.                                                                         |
| `session_stop_loss_recovery_duration_sec` | int     | required if active, `≥60`       | Recovery mode duration.                                                                                    |
| `session_stop_loss_warn_bps`              | float64 | `0` (disabled), `≤0`            | Early-warn level. Must satisfy `0 > warn > stop_loss_bps`. Widens spreads without flatten/pause.           |
| `session_stop_loss_warn_spread_mult`      | float64 | required if warn active, `≥1.0` | Spread multiplier while in warn zone.                                                                      |
| `session_stop_loss_warn_notional_pct`     | float64 | `0` or `1` = disabled, `[0, 1]` | **V30 part 5.** Notional multiplier in warn zone. Pair with widen for both narrower size and wider quotes. |

### Shutdown flatten (V30 part 6)

| Field                         | Type | Default   | Description                                                                                                               |
| ----------------------------- | ---- | --------- | ------------------------------------------------------------------------------------------------------------------------- |
| `session_flatten_on_shutdown` | bool | `false`   | On SIGINT/SIGTERM, market-close any residual position after cancelling orders. Prevents inventory carry across shutdowns. |
| `session_flatten_timeout_sec` | int  | `0` (=15) | Per-asset close timeout when flatten is enabled.                                                                          |

---

## Section: `execution`

| Field                                    | Type    | Default             | Description                                                                                                                                           |
| ---------------------------------------- | ------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live_mode`                              | bool    | `false`             | `false` = paper executor (simulated fills). `true` = LiveExecutor (requires `PRIVATE_KEY`, sets leverage at startup, cancels all orders on shutdown). |
| `paper_starting_equity`                  | float64 | `0`                 | Starting equity for the paper executor.                                                                                                               |
| `leverage`                               | int     | required, `[1, 50]` | Leverage for the configured asset.                                                                                                                    |
| `recon_grace_period_ms`                  | int     | `3000`              | Post-fill sign-flip grace period for fill reconciliation.                                                                                             |
| `recon_empty_exchange_grace_ms`          | int     | `5000`              | Shorter grace for `tracked>0 && exchange=0` desync (silent cancel / ALO reject). `0` falls back to `recon_grace_period_ms`.                           |
| `poll_equity_sec`                        | int     | `15`                | Equity polling interval.                                                                                                                              |
| `poll_open_orders_sec`                   | int     | `15`                | Open-orders polling interval.                                                                                                                         |
| `poll_fills_sec`                         | int     | `10`                | REST fill poller interval (safety net under the WS fill stream).                                                                                      |
| `poll_rate_limit_sec`                    | int     | `30`                | Rate-limit polling interval.                                                                                                                          |
| `recon_mismatch_diagnostic_enabled`      | bool    | `false`             | **P10.** Dump tracked/exchange OID symmetric difference on mismatch.                                                                                  |
| `recon_mismatch_diagnostic_max_per_hour` | int     | `0`, `≥0`           | Per-coin rate limit on diagnostic emission.                                                                                                           |

---

## Section: `rate_limit`

Rate-limit circuit breaker — proactively shuts down quoting before the lifetime API budget is exhausted. Uses time-to-exhaustion (TTE) computed from depletion rate, not cumulative utilization.

> **Important.** Hyperliquid rate-limit `remaining`, `cap`, and `pct` are cumulative since address inception, not per-session. A session opening at "4.5% remaining" just means prior trading consumed most of the lifetime budget. `VirtCycle` regenerates budget from cumulative USDC volume, also since inception.

| Field                       | Type    | Default                                                      | Description                                                                                                                                                                                                                                                                   |
| --------------------------- | ------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `absolute_floor`            | int     | `300`, `≥0`                                                  | Remaining below this = immediate critical (cancel all + close all).                                                                                                                                                                                                           |
| `tte_caution_minutes`       | int     | `60`                                                         | Caution: suppress volume burst, 1.5× refresh throttle.                                                                                                                                                                                                                        |
| `tte_degraded_minutes`      | int     | `30`                                                         | Degraded: collapse to 1 ladder level, 2× throttle, 0.5× notional.                                                                                                                                                                                                             |
| `tte_critical_minutes`      | int     | `15`                                                         | Critical: cancel all orders, close all positions, enter dormant.                                                                                                                                                                                                              |
| `tte_recovery_minutes`      | int     | `45`                                                         | Exit dormant when TTE above this AND remaining `> 2× absolute_floor`.                                                                                                                                                                                                         |
| `ewma_half_life_minutes`    | int     | `10`, `>0`                                                   | Half-life for the depletion-rate EWMA.                                                                                                                                                                                                                                        |
| `remaining_pct_warn`        | float64 | `10`, `(0, 100]`                                             | Warn when remaining < N% of cap.                                                                                                                                                                                                                                              |
| `remaining_pct_critical`    | float64 | `3`, `(0, warn)`                                             | Critical when remaining < N% of cap.                                                                                                                                                                                                                                          |
| `pct_critical_grace_sec`    | int     | `120`, `≥0`                                                  | Skip pct-based critical check for this many seconds after startup (avoid false trips during rate-limit data warmup).                                                                                                                                                          |
| `budget_pressure_pct_start` | float64 | `20`, `[0, 100]`; if `>0` must be `> remaining_pct_critical` | Ramp absolute-remaining budget pressure to optimizer starts at this remaining/cap %; reaches 1.0 at `remaining_pct_critical`. Blended via `max` with virt-cycle headroom pressure so the optimizer proactively throttles before the cancel-only trip. `0` disables the blend. |

**TTE ordering** (validated): `tte_critical < tte_degraded < tte_caution`, and `tte_recovery > tte_critical`.

---

## Section: `logging`

| Field                 | Type | Default   | Description                     |
| --------------------- | ---- | --------- | ------------------------------- |
| `verbose`             | bool | `false`   | Enable DEBUG-level slog output. |
| `status_interval_sec` | int  | `0` (=15) | Periodic status-line interval.  |

---

## Section: `postmortem`

**P13.** Auto post-mortem dump on halt/stop — writes a structured JSON snapshot per-asset on `RiskHalt`, rate-limit critical, feed-death, or graceful shutdown.

| Field                      | Type   | Default                                           | Description                                                   |
| -------------------------- | ------ | ------------------------------------------------- | ------------------------------------------------------------- |
| `enabled`                  | bool   | `false`                                           | Master toggle. No-op when disabled.                           |
| `output_dir`               | string | `""` (defaulted to `postmortem/` at construction) | Output directory (auto-created).                              |
| `filename_format`          | string | `"{coin}-{YYYYMMDD}-{HHMMSS}-{reason}.json"`      | Placeholders: `{coin}`, `{YYYYMMDD}`, `{HHMMSS}`, `{reason}`. |
| `include_last_n_evals`     | int    | `0`, `≥0`                                         | Most-recent optimizer eval snapshots.                         |
| `include_last_n_refreshes` | int    | `0`, `≥0`                                         | Most-recent refresh attributions.                             |
| `include_all_fills`        | bool   | `false`                                           | Include all session fills (`false` = none).                   |
| `rotate_keep_count`        | int    | `0`, `≥0`                                         | Keep N most-recent files in `output_dir`, rotate older.       |

---

## Section: `env` and Environment Variables

| YAML field             | Env var                | Required                 | Notes                                                            |
| ---------------------- | ---------------------- | ------------------------ | ---------------------------------------------------------------- |
| `quicknode_subdomain`  | `QUICKNODE_SUBDOMAIN`  | yes                      | Auto-suffixed with `.hype-mainnet.quiknode.pro` if a bare label. |
| `quicknode_auth_token` | `QUICKNODE_AUTH_TOKEN` | yes                      | Combines into `https://{subdomain}/{token}` for the SDK.         |
| `private_key`          | `PRIVATE_KEY`          | yes if `live_mode: true` | EVM private key (hex). Never commit.                             |

Environment variables override YAML values when both are present.

---

## Cross-Field Validation Rules

The loader enforces these relationships at startup. A failure is fatal.

- `market_making.max_quote_age_ms ≥ refresh_interval_ms`
- `max_book_staleness_ms > refresh_interval_ms` (when `>0`)
- `max_no_book_update_ms ≥ max_book_staleness_ms` (when both set)
- `vwap_long_window_minutes ≥ vwap_short_window_minutes`
- `vwap_alpha_floor ≤ vwap_alpha` (when drift scaling enabled)
- `vwap_alpha_trend_boost_ceiling ≥ vwap_alpha` and `≤ 1.0` (when boost enabled)
- `trend_guard_saturation_pct > trend_guard_threshold_pct`
- `wall_quality_reenable_threshold_bps` mapped score (`0.5 + bps/10`) must be `≥ wall_quality_min_score`
- `risk.max_spread_exposure ≥ max_position_notional`
- Rate-limit ordering: `tte_critical < tte_degraded < tte_caution`; `tte_recovery > tte_critical`
- `remaining_pct_critical < remaining_pct_warn`
- `budget_pressure_pct_start > remaining_pct_critical` (when `>0`)
- `session_stop_loss_warn_bps > session_stop_loss_bps` (less negative; warn fires earlier)
- `optimizer_v2`: each composite weight block sums to ≈1.0 (in `[0.95, 1.05]`) when any value is non-zero
- `optimizer_v2.subdim_quantile_high_pct > subdim_quantile_low_pct`
- `optimizer_v2.session_pnl_bucket_edges_bps`: exactly 4 entries, strictly ascending
- `optimizer_v2.fourth_dimension == "session_pnl"` requires `risk.session_pnl_window_minutes > 0`
- `toxicity_t5s_weight ∈ [0, 1)`

---

## Removed / Deprecated Fields

The following fields existed in earlier versions and are now rejected or no longer read. Consult `git log` for migration paths if you have an old config.

**Removed entirely:** `entry_z_score`, `exit_z_score`, `stop_z_score`, `max_z_score`, `ExitConfig`, `hedge_timeout_ms`, `max_hedge_slippage_bps`, `spread_circuit_breaker_bps`, `adaptive_spread_enabled`, `adaptive_spread_k`, `adaptive_skew_enabled`, `inventory_skew_min`, `inventory_skew_max`, `adaptive_alpha_enabled`, `vwap_alpha_min`, `vwap_alpha_max`, `adverse_selection_enabled`.

**Renamed:**

| Old                               | New                      |
| --------------------------------- | ------------------------ |
| `adverse_selection_half_life_sec` | `toxicity_half_life_sec` |
| `adverse_selection_max_widen`     | `toxicity_max_widen`     |
| `adverse_selection_baseline_bps`  | `toxicity_baseline_bps`  |

**Optimizer v1 consolidated into v2:** `RuntimeOptimizerConfig`, `OptimizerThresholdsConfig`, `OptimizerInputs`, `RuntimeOptimizer`, `KnobBoundsConfig.Step`, `KnobBoundsConfig.CooldownSec` (current `KnobBoundsConfig` only has `Min` / `Max`).

---

## Minimal Working Config

Single-asset paper-mode (CL on the `xyz` HIP-3 dex):

```yaml
strategy:
  assets: ["xyz:CL"]
  lookback_window: 500 # required for parsing; unused in single-asset mode

market_making:
  quote_spread_bps: 20
  quote_notional: 50
  refresh_interval_ms: 800
  inventory_skew_factor: 0.7
  ladder_levels: 4
  ladder_spacing_bps: 5

risk:
  max_position_notional: 200
  max_spread_exposure: 400
  max_drawdown_pct: 5.0
  max_open_orders: 16

execution:
  live_mode: false
  paper_starting_equity: 150
  leverage: 5

env:
  quicknode_subdomain: "" # set via QUICKNODE_SUBDOMAIN env var
  quicknode_auth_token: "" # set via QUICKNODE_AUTH_TOKEN env var
```

Validate with:

```bash
go run ./cmd/bot/ -config <path> -validate
```

To go live, flip `execution.live_mode: true` and export `PRIVATE_KEY`. To target a different asset, change `strategy.assets` (use `xyz:` prefix for HIP-3 dex assets, bare `BTC` / `ETH` for standard perps).

---

## Where to Look Next

- **Why a feature exists / how the algorithm uses a config:** [ALGORITHM.md](./ALGORITHM.md)
- **Annotated full template:** [`config.example.yaml`](../config.example.yaml)
- **Loader + validation source of truth:** [`internal/config/config.go`](../internal/config/config.go)
- **Project-wide context and conventions:** [CLAUDE.md](../CLAUDE.md)
# Local public API option

## Local quota guard override

`rate_limit.disabled: true` keeps quota telemetry but disables local quota tier
blocking, shutdown/flatten, allocator throttling and optimizer budget pressure,
including the startup check at zero remaining allowance. Hyperliquid's own limits
and exchange rejection handling still apply. Other risk/feed protections remain.
The live config selects this override at the operator's request. Restart the bot
to apply it; changing the file does not alter an already running process.

## API wallet and live launcher

`WALLET_ADDRESS` (or top-level `wallet_address`) is the **main trading account**.
`PRIVATE_KEY` is the **authorized API wallet signing key**, never the main key.
The local SDK patch keeps these identities separate, including internal close
and cancel helpers. Live mode requires an account address. Startup verifies the
signer's `userRole` belongs to that account before any exchange action.

`check-account.cmd` prompts locally for the address and masked API key, then
checks authorization, DEX balances and open orders without trading.
`start-live.cmd` runs the same check, shows the live parameters and starts trading
only when the operator types `LIVE`. The API key stays in the child process
environment, is not written to config files, and is cleared when the launcher
exits. Do not send keys through chat.

`config.live.yaml` is a separate local copy with live mode enabled; `config.yaml`
remains paper. Orders, modifications, cancellations, leverage and reduce-only
closes use native Hyperliquid actions, signed locally with no builder field.
No builder approval or builder fee is used. Standard exchange fees remain;
live fills and account-specific trading fees have not been validated here.

Top-level `data_source: public` selects `https://api.hyperliquid.xyz/info` and
`wss://api.hyperliquid.xyz/ws` for metadata and market data. No QuickNode account
or token is required. Omitted or `quicknode` retains the upstream gRPC setup.
The local `start-paper.cmd` launcher uses `config.yaml`, which selects public
data and `execution.live_mode: false`. No private key is needed for paper mode.
# Zeus profile additions

- Top-level `signal_pauses_enabled`: optional boolean, default true. Zeus sets
  false to disable signal-only quote suppression while retaining hard guards.
- Zeus generates a single-asset runtime config from `../profile.json`, using the
  UI notional as quote and inventory cap, one rung, fixed sizing, bounded leverage,
  public data, rate-limit protection and `session_flatten_on_shutdown: true`.
- Credentials are inherited from the Zeus process environment. They are never
  included in the generated JSON/YAML configuration.
- The snapshot protocol is enabled only with the `-zeus-*` flags; standalone
  monitor logging and original config behavior remain unchanged outside Zeus.
- In Zeus, percentage rate-limit thresholds refer to the session's initial
  available request credit, not the lifetime cumulative exchange cap. The
  configured absolute reserve and time-to-exhaustion protections still apply.
- Zeus session configs set `fail_closed_cooldown_sec: 15` and
  `max_fail_closed_retries: 5` so a transient API or order-state race cannot
  end the session permanently. Superseded refreshes (fill/reconciliation or an
  already-gone order) are retried without entering fail-closed at all.
