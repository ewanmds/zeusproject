# Algorithm Reference

A comprehensive guide to how this market-making bot works, how it makes trading decisions, and what every configuration parameter controls.

---

## Table of Contents

1. [Overview & Thesis](#overview--thesis)
2. [Architecture at a Glance](#architecture-at-a-glance)
3. [Fair Value Computation](#fair-value-computation)
4. [Quoting Pipeline](#quoting-pipeline)
5. [Spread Engine & Z-Score](#spread-engine--z-score)
6. [Ladder Construction](#ladder-construction)
7. [Adaptive Features](#adaptive-features)
8. [Risk Management](#risk-management)
9. [Execution](#execution)
10. [Health System](#health-system)
11. [Operations](#operations)
12. [Configuration Reference](#configuration-reference)

---

## Overview & Thesis

This bot market-makes on Hyperliquid — both HIP-3 perpetual markets (e.g. CL, NATGAS, BRENTOIL, COPPER on an `xyz:` dex) and standard perps (BTC, ETH). It always quotes both sides. Two multi-asset modes are supported:

1. **Independent mode** — multiple assets run with no spread engine, no cross-leg skew, no beta hedge. Each asset has its own per-asset config overrides, including independent optimizer bounds.
2. **Spread-biased mode** — two correlated assets with the spread engine, z-score signals, and optional beta hedge.

Running a single asset is just a degenerate case of independent mode.

**The edge comes from two sources:**

1. **Earning bid-ask spread.** By quoting both sides with a spread wider than exchange fees, the bot earns the difference on each round-trip. On thin HIP-3 markets, even a few basis points of spread represents a meaningful edge when other participants are crossing the spread.

2. **Mean reversion on VWAP wicks.** When the current price deviates sharply from a short-window volume-weighted average price (2 minutes) but the longer-term VWAP (30 minutes) has not moved, the bot interprets this as a temporary wick and leans into the expected reversion — posting larger size on the side that benefits from prices snapping back.

---

## Architecture at a Glance

Single binary, event-driven pipeline. All components communicate via typed Go channels (buffered, size 100) with non-blocking sends. A single `context.Context` governs graceful shutdown.

```
gRPC L2Book ──> fan-out ──> Quote Manager ──> Executor
                  │              ^               │
                  └──> Executor  │               v
                           RiskAction      PositionUpdate
                                 │               │
Trade gRPC ──> Short VWAP (2m) ──> Quote Manager
           └──> Long VWAP (30m) ──> Quote Manager
           └──> Trade Imbalance ──> Quote Manager
           └──> Trade Velocity ──> Quote Manager

Fill Events ──> Fill Decay (adverse shift) ──> Quote Manager
            └──> Adaptive Notional Tracker ──> Quote Manager
            └──> Side Toxicity Tracker ──> Quote Manager
            └──> Markout Tracker ──> Quote Manager
            └──> Wall Quality Tracker ──> Quote Manager
            └──> Risk Manager ───────────────> Quote Manager
```

**Data sources:**

- **gRPC L2Book stream** — real-time order book snapshots (10 levels per side)
- **Trade stream** (gRPC for HIP-3 assets) — public trades for VWAP computation and trade-flow imbalance
- **WebSocket UserFills stream** — the bot's own fills for position tracking and reconciliation, supplemented by a REST fill poller as a safety net

**Refresh cycle:** The quote manager runs an event-driven loop. On every book update or spread signal, it re-evaluates whether quotes need updating. If the price has moved more than `requote_threshold_bps`, size has changed by more than `size_requote_pct`, or quotes are older than `max_quote_age_ms`, it cancels and replaces all orders for that asset. A forced-refresh mechanism (reasons such as `price_shock`, `optimizer_emergency`, `markout_emergency`) bypasses the normal dynamic interval while still respecting critical rate-limit and risk halts.

---

## Fair Value Computation

Fair value is the bot's best estimate of the "true" price at any moment. Every quoting decision is anchored to this value.

### Step 1: Raw Microprice

The starting point is the **microprice** — a size-weighted midpoint that accounts for order book imbalance at the best bid/offer. Unlike a simple mid = (bid + ask) / 2, microprice leans toward the side with more resting liquidity:

```
microprice = (bestBid * askSize + bestAsk * bidSize) / (bidSize + askSize)
```

When `microprice_levels > 1`, a **multi-level microprice** weights deeper book levels by a geometric decay factor (`microprice_decay`, default 0.7), capturing information from the full visible order book rather than just the BBO.

### Step 2: VWAP Blending

The raw microprice is blended with the **short-term VWAP** (volume-weighted average price over recent trades):

```
fairValue = vwap_alpha × shortVWAP + (1 - vwap_alpha) × microprice
```

- `vwap_alpha = 0` → pure microprice (tracks the book instantly)
- `vwap_alpha = 1` → pure VWAP (anchored to recent trade history, ignores book)

**Why dual VWAPs?** The bot maintains two VWAP windows:

- **Short VWAP** (2 minutes) — anchors fair value, smooths out momentary book noise
- **Long VWAP** (30 minutes) — used as a trend filter for wick detection. A price spike that deviates from short VWAP _but not_ from long VWAP is likely a temporary wick; one that deviates from both is a genuine trend move.

**Effective alpha is adjusted by two optional layers applied before the blend:**

**Drift-aware alpha fade.** When `vwap_alpha_drift_scaling_enabled` is true, the effective alpha fades down as `|shortVWAP − longVWAP|` grows (measured in price-stddev units). The fade factor is `max(0, 1 − divergence / (vwap_alpha_drift_k × sigma))`, applied before the blend with the result floored at `vwap_alpha_floor`. In quiet chop, effective alpha equals the configured base; in sustained trends it shrinks so FV anchors more to the microprice rather than chasing a lagging short VWAP.

**Trend-aware alpha boost.** When `vwap_alpha_trend_boost_enabled` is true and lag-1 autocorrelation of mid returns exceeds `trend_fv_drift_autocorr_min` (positive only; negative autocorr is mean-reverting and does not trigger the boost), the effective alpha is boosted toward `vwap_alpha_trend_boost_ceiling`. Boost fraction equals `autocorr`, so at autocorr=0.5 with base alpha 0.40 and ceiling 0.70, effective alpha becomes 0.40 + 0.5×(0.70-0.40) = 0.55. This makes FV anchor more heavily to the lagging VWAP during trends, which naturally widens entry-side quotes (in an uptrend, VWAP lags below mid, so higher alpha → lower FV → wider asks).

The optimizer's `alpha_delta` knob (see [Adaptive Features](#adaptive-features)) is added to the base `vwap_alpha` before these two layers apply.

### Step 3: Trend-Momentum FV Bias

When `trend_fv_drift_bps > 0` and `autocorr > trend_fv_drift_autocorr_min`, fair value is shifted in the VWAP-inferred trend direction:

```
FV *= (1 + dir × autocorr × trend_fv_drift_bps / 10000)
where dir = sign(shortVWAP − longVWAP)    // +1 up-trend, −1 down-trend
```

The autocorr magnitude supplies regime confirmation (trending vs mean-reverting); the VWAP divergence supplies direction. In an up-trend FV shifts up, widening asks and tightening bids; in a down-trend FV shifts down. Applied after the VWAP blend but before the sanity clamp. Logged in quote attribution as `trend_fv_shift_bps`.

### Step 4: Fair Value Sanity Clamp

After the VWAP blend and trend-momentum shift, a safety clamp checks whether the computed fair value has diverged more than `max_fv_deviation_pct` from the book mid. If so, FV is clamped back to mid. This prevents corrupted or stale VWAP data (e.g., during exchange downtime) from producing nonsensical order prices. Set to 0 to disable.

### Step 5: Trade-Flow Imbalance Shift

The bot tracks the buy/sell volume imbalance using one of two modes selected per-asset via `trade_imbalance_mode`. Only **taker (aggressor) trades** are counted — the gRPC trade stream delivers both sides of every execution (maker + taker), and only the taker's side represents directional market pressure. Trades are identified as taker via the `crossed` field in the gRPC data.

**Time mode** (`trade_imbalance_mode: "time"`, default): Tracks buy/sell volume over a rolling time window (`trade_imbalance_window_sec`). Raw trade sizes are ingested.

**Volume mode** (`trade_imbalance_mode: "volume"`): VPIN-lite volume-clock approach. Trades are grouped into equal-notional buckets (`trade_imbalance_bucket_size`, default $5000). Notional (`price × size`) is ingested instead of raw size. A circular buffer of `trade_imbalance_bucket_count` completed buckets is maintained; the in-progress bucket is excluded. This naturally adapts to activity: during high-volume periods buckets fill faster, providing more responsive signals.

Both modes share the same downstream formula:

```
weightedSize = size ^ trade_imbalance_size_exponent                // power-law weighting; 1.0 = raw
imbalance = (buyVolume - sellVolume) / (buyVolume + sellVolume)    // range [-1, +1]
shift = imbalance × trade_imbalance_max_shift_bps
```

Sub-linear exponents (e.g. 0.5) dampen whale impact; super-linear exponents (e.g. 1.2) amplify large trades. Weighting is applied at ingestion time, so eviction, recompute, and imbalance ratio all operate on pre-weighted values.

Heavy buying shifts fair value up; heavy selling shifts it down. This is a fast-reacting signal that front-runs order flow information. VWAP calculators continue to receive all trades (both sides), since volume-weighted average price benefits from total volume.

### Step 6: Post-Fill Adverse Decay

After the bot's own orders get filled, fair value is temporarily shifted _away_ from the filled side. The logic: if someone crossed the spread to hit our bid, the market may be heading lower (adverse selection). This shift decays exponentially:

```
shift = fill_adverse_shift_bps × 2^(-elapsed / fill_decay_half_life_ms)
```

- Buy fill → negative shift (fair value moves down)
- Sell fill → positive shift (fair value moves up)

Multiple fills stack additively. With `fill_adverse_shift_bps = 5.0` and `half_life = 8000ms`, a single fill shifts fair value by 5.0 bps immediately, 2.5 bps after 8 seconds, and effectively disappears after ~40 seconds.

### Step 7: Markout-to-FV Feedback

When `markout_fv_scale > 0`, the bot uses per-side markout EWMAs from the toxicity tracker to shift fair value. The idea: if buy fills consistently produce adverse mid-movement (negative buy-side markout) but sell fills do not, the market is likely trending down and fair value should be nudged down to protect bids.

```
delta = (buyEWMA - sellEWMA) / 2.0
shift = clamp(delta × markout_fv_scale, -markout_fv_max_shift_bps, +markout_fv_max_shift_bps)
fairValue *= 1.0 + shift / 10000.0
```

- When buy-side markout is worse than sell-side (e.g., buys at −3 bps, sells at +1 bps): `delta = (−3 − 1) / 2 = −2 bps`, shift is negative → FV moves **down**. This widens bids (protective) and tightens asks (captures more fills on the benign side).
- When sell-side markout is worse: delta is positive → FV moves **up**, widening asks and tightening bids.
- **Attenuation:** When the toxicity spread multiplier is already high (>1.5×), the FV shift is attenuated to avoid double-counting the same adverse signal via both spread widening and FV adjustment.

---

## Quoting Pipeline

After fair value is computed, the quote manager runs a multi-step pipeline to determine bid and ask prices and sizes. Each step adjusts the quotes from the previous step.

### Step 1: Base Quotes

Starting from fair value, apply the base half-spread. When per-side spreads are configured (`quote_spread_bps_buy` / `quote_spread_bps_sell`), each side uses its own base; otherwise falls back to the symmetric `quote_spread_bps`:

```
bidBaseBps = quote_spread_bps_buy  (or quote_spread_bps if 0)
askBaseBps = quote_spread_bps_sell (or quote_spread_bps if 0)
bid = fairValue × (1 - bidBaseBps / 20000)
ask = fairValue × (1 + askBaseBps / 20000)
```

Example: with `quote_spread_bps_buy = 6` and `quote_spread_bps_sell = 9`, a fair value of $70.000 gives bid = $69.979 (6 bps) and ask = $70.032 (9 bps) — a 15 bps asymmetric total spread.

**Trade arrival rate adjustment:** When trade arrival rate drops (velocity tracker ratio < 1, thin market), both sides widen by up to 1.25×. When arrival is high (ratio > 1, active market), spreads tighten down to 0.85×. This amortizes adverse selection risk across more fills in active markets.

**Time-of-day adjustment:** When configured (`tod_spread_start_hour_utc` / `tod_spread_end_hour_utc`), extra spread bps are added to both sides during the specified UTC hour window. Supports midnight-wrapping.

**Microstructure adjustments** (when `dynamic_width` is true): Three multiplicative per-side adjustments are applied:

- **Volatility multiplier** — realized vol vs. EWMA baseline. Higher recent vol widens quotes.
- **Venue spread floor** — if the exchange's own best bid/ask spread exceeds our quoting spread, we widen to at least match it. No point quoting tighter than the venue.
- **Order book imbalance** — if one side of the book is significantly thinner, the quote on that side widens (less liquidity = more risk).

These are combined multiplicatively and capped at `width_max_multiplier`.

### Step 2: Fee Floor

When `fee_floor_enabled` is true, the bot ensures the half-spread is wide enough to cover round-trip exchange fees:

```
minHalfSpread = maker_fee_bps + builder_fee_bps
```

If the current half-spread is narrower than this, both bid and ask are pushed symmetrically outward.

### Step 3: Spread Bias (Z-Score)

**Status:** Only active when running 2+ correlated assets with the spread engine enabled. In independent or single-asset mode this step is a no-op.

When active, the spread engine continuously computes a z-score measuring how far the inter-asset spread is from its rolling mean. The bias uses a tanh-shaped continuous shift: `rawBias = tanh(z / spread_bias_scale) × spread_bias_max_bps`. The `spread_bias_asymmetry` distributes the shift unevenly between convergence and divergence sides.

### Step 4: Inventory Skew

When the bot accumulates a position, quotes are shifted to encourage mean-reversion back to flat:

```
utilization = abs(positionNotional) / max_position_notional
skewBps = utilization^gamma × skew_factor × quote_spread_bps
shift = fairValue × skewBps / 10000
```

- **Long position** → both quotes shift down (more aggressive on sells, less aggressive on buys)
- **Short position** → both quotes shift up

The `inventory_skew_gamma` controls convexity:

- `gamma = 1.0` → linear (skew kicks in immediately)
- `gamma = 1.5` → convex (gentle at low utilization, aggressive at high)

The `inventory_skew_asymmetry` distributes the shift unevenly — e.g. 0.70 sends 70% of the shift to the _aggressive_ side (the one that reduces position) and 30% to the _passive_ side.

**Concrete example:** Long $200, `max_position = 400`, `gamma = 1.5`, `skew_factor = 2.5`, `spread = 7 bps`:

- `utilization = 200/400 = 0.50`
- `skewBps = 0.50^1.5 × 2.5 × 7 = 0.354 × 17.5 = 6.2 bps`
- With asymmetry 0.70: ask (aggressive exit) shifts down by `6.2 × 0.70 × 2 = 8.7 bps`; bid shifts down by `6.2 × 0.30 × 2 = 3.7 bps`

**Exit-preserving mode** (`inventory_skew_exit_preserve: true`): When enabled, only the _exposure-increasing_ side is shifted; the exit side stays at its bias-adjusted price. This produces effective one-sided quoting during inventory stress without fully withdrawing from the exit side:

- **Long position** → only the bid shifts down (discourages further buys). The ask remains at its spread-biased price, preserving exit fill quality.
- **Short position** → only the ask shifts up (discourages further sells). The bid remains unchanged.

The benefit is sharper inventory control: the entry side becomes progressively less attractive while the exit side continues to attract counter-flow at the best possible price. With symmetric skew, the exit side also shifts, making it _harder_ to reduce position — the opposite of what's desired.

**Funding rate session-end urgency** (`funding_urgency_enabled`): When enabled, the skew factor is further multiplied by an urgency multiplier that ramps near funding resets. A funding tracker polls the exchange for per-asset predicted funding rates and computes:

```
fundingCost = predictedRate × sideSign     // +1 for long, -1 for short
timeDecay = max(0, 1 - minutesToNextReset / funding_urgency_horizon_minutes)
urgencyMultiplier = 1 + funding_urgency_scale × fundingCost × timeDecay
```

- **Paying funding near reset** (long + positive rate, or short + negative rate) → multiplier > 1.0 → faster exit
- **Receiving funding near reset** → multiplier < 1.0 → hold position longer
- **Far from reset** → timeDecay ≈ 0 → multiplier ≈ 1.0 (no effect)

HIP-3 assets (CL, NATGAS, etc.) use 1-hour funding intervals by default; standard perps (BTC, ETH) use 8-hour intervals. Per-asset `funding_interval_hours` override controls this.

### Step 4.5: Cross-Leg Inventory Skew

**Status:** Only active when running 2+ correlated assets (`cross_leg_skew_enabled: true`). When enabled, it considers the combined position across both legs (beta-normalized) and applies additional skew to discourage same-side accumulation.

### Step 5: VWAP Wick Sizing

This step adjusts **per-side order sizes** (not prices) based on VWAP deviation. The logic detects temporary price wicks where the bot should lean into mean reversion.

A **wick** is detected when:

1. Short VWAP deviation exceeds `wick_dev_threshold_pct` (price has spiked away from recent VWAP)
2. Long VWAP deviation is below `wick_long_dev_threshold_pct` (it's not a sustained trend)
3. Inventory utilization is below `wick_max_inventory_util` (if set; 0 disables the gate)

When a wick is detected:

- The side that benefits from reversion (e.g., the bid side during a downward wick) gets a **size multiplier** up to `wick_size_multiplier`
- Ladder spacing is **compressed** on that side (levels cluster closer to the best price)
- The opposite side may get reduced sizing

The scaling is linear from 1.0× at threshold to `wick_size_multiplier` at 3× threshold.

**Shock/trend suppression:** When `wick_trend_suppress_enabled` is true, wick amplification is removed from the exposure-increasing side while any fast adverse guard is active:

- Price-shock guard is active on that side
- Trend-guard suppression is at or above `wick_trend_suppress_threshold`
- Flow-gate suppression is at or above `wick_trend_suppress_threshold`

The side still quotes if other layers allow it, but the wick multiplier is divided back out so the bot does not double down on a move that currently looks directional rather than mean-reverting.

### Step 5.5: Price-Shock Guard

A book-mid shock detector runs independently of trade-count burst detection. A `PriceShockTracker` keeps a short rolling mid-price window (`price_shock_window_ms`). If the oldest-to-current mid return exceeds `price_shock_threshold_bps`, the guard activates for `price_shock_cooldown_ms`.

Activation effects:

- **Identify the adverse entry side** from shock direction: a down shock protects bids, an up shock protects asks.
- **Cancel the entry side immediately** if it increases the current position. The quote manager calls `Executor.CancelSide()` with reason `price_shock_*`.
- **Freeze the entry side**: if `price_shock_freeze_entry` is true, zero the entry side's desired notional while the guard is active.
- **Attenuate the adverse exit**: if `price_shock_exit_size_factor ∈ (0, 1)`, multiply the exit side's notional by that factor for the cooldown window — a down shock that fires while long cancels the bid but halves the ask, so mean-reversion exits continue at reduced size instead of bleeding through at worsening prices.
- **Boost the favorable exit**: if `price_shock_favorable_exit_factor > 1.0` and the shock direction _favors_ current inventory (i.e. the shock's `EntrySide` would reduce the position), boost that side's notional by the factor. This leans into profit-taking instead of attenuating it.
- **Tighten the favorable exit spread**: if `price_shock_favorable_exit_tighten_bps > 0` and the shock is favorable, subtract that bps amount from the profit-taking side's spread. Applied after all other spread multipliers, so it composes additively; `min_crossing_spread_bps` is the safety backstop.
- **Favorable IOC**: if `price_shock_favorable_ioc_frac > 0` and the shock is favorable, fire a single limit-IOC order sized at `frac × position_size` on the position-reducing side, priced at best-opposite ± `favorable_ioc_slippage_bps`. Gated by `min_inv_util` and `min_capture_bps` so the IOC does not fire on trivial moves or near-flat positions. One fire per shock activation; if it misses, the passive layers continue as before.
- **Scoped refresh + out-of-cadence optimizer eval** so protective overlay changes are not delayed by the normal timer.
- **Emit `price shock guard activated`** and include `price_shock`, `price_shock_side`, `price_shock_bps`, `wick_supp_bid`, and `wick_supp_ask` in STATUS/monitor output.

**Shock-chain cascade.** The per-shock guard treats each activation independently, but a rapid sequence of shocks indicates a structurally broken regime rather than a single gappy print. The `ShockChainTracker` counts activations in `shock_chain_window_sec` (rolling) and when the count crosses `shock_chain_threshold` escalates to a hard flatten:

- Cancel all orders and close the position on every managed asset
- Assert `SignalRiskHalt` (force-flatten) for `shock_chain_pause_sec`
- Transition to `SignalRecovery(shock_chain_recovery_sec)` (reduced notional / wider spreads)
- Reset the chain counter

This catches cascade patterns (e.g. 3+ shocks in a few minutes) that would otherwise whipsaw the bot into a session stop-loss.

### Step 6: Z-Score Widening

**Status:** Only active when running 2+ assets with the spread engine. In independent or single-asset mode there is no z-score, so this step is a no-op.

When active, at extreme z-scores the bot progressively widens quotes: `multiplier = 1 + (abs(z) - threshold) × z_widening_rate`, capped at `z_max_width_multiplier`. When `adaptive_z_enabled` is true, thresholds are dynamically calibrated by the rolling standard deviation of z-scores.

### Step 6.5: Per-Side Toxicity

When `toxicity_per_side: true`, buy-side and sell-side markout EWMAs are tracked **independently**. Each side maintains its own EWMA of post-fill mid-price movement (using the same markout tracker horizons), and each side gets its own spread multiplier in `[1.0, toxicity_max_widen]`.

**Warmup gate:** Until a side has accumulated `toxicity_warmup_fills` markout samples, `SpreadMultiplier()` and `SizeFactor()` return neutral values (1.0). The EWMA still accumulates internally — only the output is suppressed. This prevents a single early adversely-selected fill from driving minutes of widening before the EWMA has enough data to be meaningful.

**Optimizer attenuation:** When the optimizer has already _widened_ a side (positive `spread_delta_buy` / `spread_delta_sell`), both the toxicity spread multiplier and size factor for that side are attenuated to avoid double-counting the same per-side markout signal. Attenuation only applies to positive deltas — when the optimizer has _tightened_ (negative delta), toxicity is the only defense and is not dampened. The formula for spread: `effective_mult = 1 + (mult - 1) * max(0, 1 - delta / base_spread)`. For size: `effective_size = 1 - (1 - size_factor) * max(0, 1 - delta / base_spread)`. At full base-spread widening, toxicity effects are completely neutralized.

```
side_multiplier = 1.0 + max(0, baseline - side_ewma) / baseline × (max_widen - 1.0)
```

A side whose fills are consistently followed by adverse mid-movement (negative markout) gets wider spreads, while the other side may remain tight.

**Idle decay:** After `toxicity_idle_decay_half_lives × half_life` with no fills on a given side, the EWMA decays exponentially toward baseline. This prevents the frozen-EWMA stickiness problem observed on sparse assets (fractional fills/min), where a single marginally-negative fill could permanently peg the multiplier at its cap. Set `toxicity_idle_decay_half_lives` to a negative value to disable idle decay. The grace period preserves memory — toxic fills are remembered for several half-lives before decaying.

**Size reduction on toxic sides:** In addition to spread widening, per-side toxicity scales down order **size** on the toxic side using a **convex power-law** curve:

```
if side_ewma < toxicity_size_reduce_threshold:              // e.g. -0.5 bps
    rawReduction = min(1.0, (threshold - side_ewma) / abs(threshold))
    reduction = rawReduction ^ toxicity_size_exponent × (1.0 - toxicity_size_reduce_max)
    size_factor = 1.0 - reduction                           // floors at toxicity_size_reduce_max
```

The `toxicity_size_exponent` controls the convexity of the size reduction curve:

- **1.0** (linear): reduction is proportional to markout distance from threshold
- **1.5** (convex): gentle at mild toxicity, aggressive at extreme
- **2.0** (quadratic): even gentler near threshold, steeper at the extremes

Example with `threshold = -0.5 bps`, `exponent = 1.5`, `size_reduce_max = 0.5`: a side with −1.0 bps markout EWMA gets `rawReduction = 1.0`, raised to 1.5 → 1.0, so `size_factor = 0.5×`. A side at −0.75 bps gets `rawReduction = 0.5`, raised to 1.5 → 0.35, so `size_factor = 0.82×`. This avoids over-penalizing moderate adverse selection while still aggressively cutting size at extreme toxicity.

### Step 6.9: Spread Stack Cap

When `max_total_spread_multiplier > 0`, the bot prevents runaway widening caused by multiple pipeline layers each widening independently and stacking multiplicatively. A per-side absolute cap is enforced based on the configured base spread:

```
maxBidSpread = quote_spread_bps_buy  × max_total_spread_multiplier / 10000 × fairValue
maxAskSpread = quote_spread_bps_sell × max_total_spread_multiplier / 10000 × fairValue
if (fairValue - bid) > maxBidSpread: bid = fairValue - maxBidSpread
if (ask - fairValue) > maxAskSpread: ask = fairValue + maxAskSpread
```

Example: with `quote_spread_bps_buy = 6`, `max_total_spread_multiplier = 3.0`, `fairValue = $70`: the bid cannot be more than 18 bps ($0.126) below fair value regardless of how many widening layers have applied (z-widening, toxicity, microstructure). This preserves profitability by keeping quotes close enough to mid to attract fills — an infinitely wide quote is equivalent to no quote.

### Step 7: Crossing Protection

After all adjustments, the bot ensures `bid < ask` using **position-aware** logic:

**When a position exists:** The exit-side order (the side that reduces inventory) keeps its computed price. The entry-side order is pulled inward to maintain at least `min_crossing_spread_bps` of spread from the exit side. This preserves exit fill quality — the exit side's price reflects the full pipeline (bias, skew, toxicity), and the entry side absorbs the clamping.

- **Long position** → ask (exit) price is kept; bid (entry) is clamped to `ask × (1 - min/10000)`
- **Short position** → bid (exit) price is kept; ask (entry) is clamped to `bid × (1 + min/10000)`

**When flat:** Falls back to midpoint clamping — both bid and ask are pushed symmetrically to maintain the minimum spread around their midpoint.

Additionally, bids are still clamped to not exceed the venue's best ask, and asks to not go below the venue's best bid — avoiding self-crossing the book.

### Post-Pipeline: Sizing

The base notional per side is `quote_notional`. This is adjusted by (applied in order):

1. **Fill rate multiplier** (adaptive notional) — scales up/down to hit a target fill rate
2. **Optimizer overlay** (`notional_mult`) — auto-adjusted within configurable bounds
3. **Warmup reduction** — during the first `warmup_duration_sec` after startup, notional is reduced to `warmup_notional_pct`
4. **Recovery reduction** — after a risk-triggered halt, notional is reduced to `recovery_notional_pct` for `recovery_duration_sec`
5. **Degraded health reduction** — if data feeds are stale, notional drops to 50%
6. **Wick sizing multipliers** — per-side size adjustments from Step 5
7. **Toxicity size factors** — per-side size reduction from Step 6.5 when `toxicity_per_side` is enabled
8. **Entry-side size reduction** — linearly reduces entry-side notional as inventory utilization grows (see below)
9. **Trading schedule multiplier** — when `trading_schedule_enabled` is true, a global notional multiplier is read from the current time-of-day schedule (list of `start/end/tz/multiplier/label` windows). On overlap the smallest multiplier wins. Applied symmetrically to buy and sell size _before_ per-side fill-rate, toxicity, and burst factors. Unmatched time → 1.0. `schedule window entered` / `schedule window exited` log once per label transition.

### Post-Pipeline: Exposure Limits

Before orders are submitted, the bot caps each side's total notional by:

- `max_position_notional` minus existing long/short exposure
- `max_spread_exposure` minus exposure from the other leg minus existing long/short exposure

This ensures no single refresh can push the bot past its risk limits.

### Post-Pipeline: Entry-Side Size Reduction

When `entry_size_reduction_rate > 0`, the entry-side notional is linearly reduced as inventory utilization grows:

```
util = abs(positionNotional) / max_position_notional
effectiveRate = entry_size_reduction_rate
if autocorr < -0.1:
    effectiveRate *= 0.5   // halve reduction during mean-reverting regimes
sizeFactor = max(entry_size_reduction_floor, 1.0 - util × effectiveRate)
```

Only the entry side (buy when long, sell when short) is reduced; the exit side is unchanged. This complements inventory skew (which adjusts prices) by also adjusting size — slowing accumulation at high inventory without affecting exit fills. During mean-reverting regimes (autocorrelation < −0.1), the reduction rate is halved to allow larger entry positions that benefit from expected reversion.

| Inventory Util | Entry-Side Notional (rate=0.8, floor=0.2) |
| -------------- | ----------------------------------------- |
| 0%             | 100%                                      |
| 25%            | 80%                                       |
| 50%            | 60%                                       |
| 75%            | 40%                                       |
| 100%           | 20% (floor)                               |

**Acceleration cliff.** When `entry_accel_threshold` and `entry_accel_exponent` are both > 0, a power-law cliff is applied above the threshold utilization:

```
if util > entry_accel_threshold:
    exponent = 1.0 + (util - entry_accel_threshold) × entry_accel_exponent
    entrySizeFactor = entrySizeFactor ^ exponent
```

This compounds the linear reduction into a convex curve that aggressively suppresses entry size at high utilization without affecting behavior at low utilization.

| Inventory Util | Linear Factor (rate=2.2, floor=0.05) | With Cliff (thresh=0.40, exp=3.0) |
| -------------- | ------------------------------------ | --------------------------------- |
| 30%            | 0.34                                 | 0.34 (below threshold, no change) |
| 40%            | 0.12                                 | 0.12 (at threshold, exponent=1.0) |
| 50%            | 0.05                                 | 0.05^1.3 = 0.027                  |
| 60%            | 0.05                                 | 0.05^1.6 = 0.014                  |
| 70%            | 0.05                                 | 0.05^1.9 = 0.008                  |

**Trend acceleration.** When `entry_reduction_trend_k > 0` and `autocorr > 0` (trending regime), the effective reduction rate is scaled by `(1 + autocorr × k)` _if the VWAP-inferred trend direction is adverse to the current position._ "Adverse" means `sign(shortVWAP − longVWAP)` is negative while long, or positive while short. Direction is derived from the VWAPs, not autocorr's sign (which is direction-invariant); autocorr supplies regime confirmation only. At k=2.0 and autocorr=0.5, the effective rate doubles, making entry suppression 2× more aggressive during strong adverse trends.

### Post-Pipeline: Trend Guard (VWAP Momentum Entry Gate)

When `trend_guard_enabled` is true, the bot monitors the deviation of book mid from the long VWAP to detect sustained directional trends. When the price has moved against the current position (drop when long, rally when short), entry-side notional is suppressed to prevent adding more inventory on the losing side.

**Dual-VWAP confirmation**: Both short (2m) and long (30m) VWAPs must agree on direction to confirm a trend. This distinguishes genuine trends (mid above both VWAPs) from wicks that have already reverted (short VWAP caught up but long VWAP still lagging). Falls back to long-VWAP-only if short VWAP is unavailable.

**Hysteresis**: Deactivation uses 80% of the activation threshold to prevent noisy on/off cycling when price oscillates near the boundary. Tracked per-coin.

```
devPct = (mid - longVWAP) / longVWAP × 100
shortDevPct = (mid - shortVWAP) / shortVWAP × 100
trendConfirmed = (devPct > 0 && shortDevPct > 0) || (devPct < 0 && shortDevPct < 0)

if trendConfirmed:
    adverseDev = -devPct if long, devPct if short
    threshold = trend_guard_threshold_pct × (0.80 if was_active_prev else 1.0)

    if adverseDev > threshold:
        t = clamp((adverseDev - trend_guard_threshold_pct) /
                  (saturation - trend_guard_threshold_pct), 0, 1)
        suppress = t × trend_guard_max_suppress
        entryNotional *= (1 - suppress)
```

The suppression ramps linearly from 0 at `threshold_pct` to `max_suppress` at `saturation_pct`. At `max_suppress = 0.90`, entry notional is reduced to 10% of its base at saturation.

**Asymmetric spread widening:** when `trend_guard_spread_widen_bps > 0` and `suppress >= 0.5`, the entry-side quote price is also shifted by `fairValue × trend_guard_spread_widen_bps × asymmetry / 10000` away from the mid — lowering the bid when long (entry = buy) or raising the ask when short (entry = sell). Notional suppression alone does not always prevent wick fills because the level-0 quote may still be at market; spread widening lifts the entry quote entirely out of the taker's path. The shift is applied after the main spread pipeline and crossing protection.

**Entry-asymmetry multiplier:** `trend_guard_spread_widen_asymmetry` (default 1.0) scales the entry-side widening. At 2.0, the entry side gets 2× the configured bps.

**Full entry freeze:** when `trend_guard_freeze_threshold > 0` and `suppress >= trend_guard_freeze_threshold`, entry-side notional is zeroed entirely — no orders placed on the entry side this cycle. Stacks on top of spread widening for extreme adverse trends.

Signal design: uses long VWAP (30m default) as the primary anchor with short VWAP (2m) as confirmation. The long VWAP captures sustained momentum; the short VWAP prevents false positives on wicks that have already reverted.

| Long VWAP Dev (%) | Suppression (thresh=0.20, sat=1.00, max=0.90) | Entry Factor         |
| ----------------- | --------------------------------------------- | -------------------- |
| 0.15              | 0%                                            | 1.00                 |
| 0.20              | 0%                                            | 1.00 (at threshold)  |
| 0.40              | 22.5%                                         | 0.78                 |
| 0.60              | 45.0%                                         | 0.55                 |
| 0.80              | 67.5%                                         | 0.33                 |
| 1.00              | 90.0%                                         | 0.10 (at saturation) |
| 1.50              | 90.0%                                         | 0.10 (capped)        |

### Post-Pipeline: Directional Flow Gate

When `flow_gate_enabled` is true, the bot monitors the rolling trade imbalance and suppresses entry-side notional when trade flow is moving against the current position. When long, sell flow (negative imbalance) is adverse; when short, buy flow (positive imbalance) is adverse. Unlike the FV shift in fair-value step 5, which adjusts the price, the flow gate adjusts the size.

```
adverseImb = -imbalance if long, imbalance if short

if adverseImb > flow_gate_threshold:
    t = clamp((adverseImb - threshold) / (1.0 - threshold), 0, 1)
    suppress = t × flow_gate_max_suppress
    if flow_gate_min_util > 0:
        utilScale = clamp(inv_util / flow_gate_min_util, 0, 1)
        suppress *= utilScale
    entryNotional *= (1 - suppress)
```

The flow gate uses the calculator's `Imbalance()` (read-only) rather than `ImbalanceAndRecord()` to avoid double-recording saturation, which is already performed during the FV shift step.

| Trade Imbalance | inv_util | Suppression (thresh=0.60, max=0.50, min_util=0.25) | Entry Factor           |
| --------------- | -------- | -------------------------------------------------- | ---------------------- |
| 0.50            | any      | 0%                                                 | 1.00 (below threshold) |
| 0.80            | 0.05     | 5.0% × (0.05/0.25) = 1.0%                          | 0.99                   |
| 0.80            | 0.25     | 5.0% × 1.0 = 5.0%                                  | 0.95                   |
| 0.80            | 0.50     | 5.0% × 1.0 = 5.0%                                  | 0.95                   |
| 1.00            | 0.05     | 50.0% × (0.05/0.25) = 10.0%                        | 0.90                   |
| 1.00            | 0.25     | 50.0% × 1.0 = 50.0%                                | 0.50                   |
| 1.00            | 0.50     | 50.0% × 1.0 = 50.0%                                | 0.50                   |

When `flow_gate_min_util` is set, suppression is proportionally scaled down at low inventory utilization — at 5% util with min_util=0.25, only 20% of the computed suppression is applied, preventing the gate from over-penalizing nearly-flat positions on thin books.

All three entry-side mechanisms (acceleration cliff, trend guard, flow gate) stack multiplicatively on the entry side. At extreme conditions (high inventory + strong trend + adverse flow), entry notional can be reduced to near-zero, effectively making the bot exit-only until conditions normalize.

### Post-Pipeline: Regime-Gated Quoting

When `regime_gate_enabled` is true, the bot monitors lag-1 autocorrelation of mid returns together with short-vs-long VWAP divergence to detect sustained trends. Activation requires BOTH `|autocorrelation| >= regime_autocorr_threshold` and `|shortV − longV| / sigma >= regime_vwap_divergence_sigma` to hold for at least `regime_gate_min_duration_sec` seconds. The trend direction is `sign(shortV − longV)` (+1 up, −1 down).

While active, the regime gate suppresses the trend-aligned side — buys in an uptrend, sells in a downtrend — by `regime_gate_max_suppression` (fraction of entry-side notional removed).

**Interaction with trend guard**: when both guards would act on the same side, they compose via `min(factor)` — the larger suppression wins instead of multiplying them. The regime gate inverts the trend guard's prior multiplication on that side before applying the chosen factor so the two don't double-count.

Mode is restricted to `"exit_only"`; the reserved `"directional"` mode is rejected by validation and deferred to future work. Logs emit `regime gate activated`, `regime gate deactivated`, and a once-per-5-minute `regime gate heartbeat` per coin.

### Post-Pipeline: Position-Age Exit Tightening

When a position has been held for longer than `position_age_tighten_min_sec` without a fill on the exit side, the exit-side spread is progressively tightened:

```
holdSeconds = time since last exit-side fill (or position open)
if holdSeconds > position_age_tighten_min_sec:
    extraMinutes = (holdSeconds - position_age_tighten_min_sec) / 60
    exitTighten = extraMinutes × position_age_tighten_bps / 10000 × fairValue
    exitPrice -= exitTighten   // for longs: ask moves down
    exitPrice += exitTighten   // for shorts: bid moves up
```

This prevents the bot from holding stale positions indefinitely. The tightening is additive to inventory skew — as hold time increases, exit fills become progressively more likely. Combined with the inventory half-life controller, this provides two complementary mechanisms for position aging: skew increases and exit price tightens.

### Post-Pipeline: Idle-Fill Circuit Breaker

A complementary mechanism to position-age tightening that fires as a one-shot exit-side override when adverse inventory has sat with no fills for a long period. Unlike the linear ramp above, this is binary: it fires when all conditions are met, applies a fixed tighten + size boost for the next refresh cycle, then enters a per-asset cooldown.

Trigger conditions (all required):

- `time.Since(lastFillTime[coin]) > idle_fill_threshold_sec`
- `|inv_util| > idle_fill_min_inv_util`
- Supervisor allows quoting (not Flatten / CancelOnly) and the coin is not individually halted
- `time.Since(lastFiredAt) > idle_fill_retry_interval_sec`

On fire (exit side only):

```
exitSide = "sell" if pos.Side == "long" else "buy"
exitPrice tightened toward fairValue by idle_exit_tighten_bps
exitNotional × idle_exit_size_multiplier
forceRefresh marked so the change goes out immediately
```

The tighten lands BEFORE crossing protection, so even an aggressive value cannot cross — `min_crossing_spread_bps` clamps the result. The active flag is cleared on the next fill for the asset (the bleed scenario is resolved) or implicitly when the retry interval elapses on the next check.

---

## Spread Engine & Z-Score

**Status:** Only active when `spread_engine_enabled: true` AND 2 correlated assets are configured. Disabled in independent or single-asset mode; z-score, beta, spread-bias, z-widening, and cross-leg skew all become no-ops.

The spread engine tracks the price relationship between two correlated assets and produces a continuous z-score signal.

### How the Spread is Computed

```
spread = asset1_mid - beta × asset2_mid
```

Where `beta` is the **OLS (ordinary least squares) regression coefficient** computed over a rolling window. The beta is computed incrementally:

```
beta = Cov(asset1, asset2) / Var(asset1)
```

Beta is clamped to `[beta_min, beta_max]` to prevent extreme values from distorting the spread.

When `use_beta_hedge` is false, beta = 1.0 (simple difference).

### Z-Score

The z-score measures how many standard deviations the current spread is from its rolling mean:

```
z = (current_spread - mean) / stddev
```

**Crucially, the z-score is computed BEFORE adding the new value to the rolling window.** This means the incoming observation is scored against the established distribution, not a distribution that includes itself. This prevents lookahead bias.

The `z_min_stddev` provides a floor on the standard deviation, preventing z-score explosion when the spread is very stable (near-zero stddev).

### Circular Buffer & Drift Correction

Both the spread statistics and OLS beta use **circular buffers** of size `lookback_window`. Running sums and sum-of-squares are maintained incrementally (O(1) per update). Every 1000 full rotations of the buffer, a brute-force recomputation eliminates accumulated floating-point error.

### Sampling Logic

The engine only takes a sample when **both** legs have fresh data:

1. Leg A updates are marked as "dirty"
2. Leg B updates are also marked as "dirty"
3. A sample is taken only when both dirty flags are set
4. After sampling, both flags are cleared

This prevents overweighting one leg when it updates more frequently.

Additionally, a **cross-leg skew check** rejects samples where the two legs' timestamps differ by more than `max_cross_leg_skew_ms`. This prevents stale-vs-fresh comparisons from distorting the spread signal.

### Warm-Up

No signals are emitted until the circular buffer is completely full (`lookback_window` samples). During warm-up, the quote manager uses wider spreads and smaller sizes.

### Adaptive Z-Score

When `adaptive_z_enabled` is true, the engine maintains a second rolling window (`z_calibration_window`) that tracks the standard deviation _of the z-scores themselves_. This `ZStdDev` is passed through to the quote manager, which uses it to dynamically scale:

- The `spread_bias_scale` (how aggressively the bias responds to z-score)
- The `z_widening_threshold` (when widening kicks in)

If z-scores are naturally noisy (high ZStdDev), thresholds are relaxed. If z-scores are stable, thresholds tighten. This prevents the system from overreacting in volatile regimes or underreacting in calm ones.

---

## Ladder Construction

Instead of quoting a single bid and ask, the bot posts **multiple price levels** (a "ladder") on each side.

### Level Count and Spacing

- `ladder_levels` — number of price levels per side
- Level 0 is closest to the mid price; higher indices are farther
- Spacing increases geometrically: `offset = mid × i^ladder_spacing_exponent × ladder_spacing_bps / 10000`
- When `ladder_spacing_dynamic` is true, spacing scales with realized volatility

### Size Graduation

`ladder_size_graduation` controls how size is distributed across levels:

- `1.0` → uniform (each level gets equal notional)
- `1.05` → each level gets 5% more notional than the previous
- `1.10` → each level gets 10% more notional than the previous

The innermost level (closest to mid) gets the smallest allocation. The idea: the nearest level fills most often, so keep it small. Outer levels are larger — they only fill during moves, and when they do, the bot wants to lean in with size.

If the geometric distribution would push the innermost level below the exchange minimum ($10), the graduation is automatically softened.

### Wall-Fronting

When `wall_front_enabled` is true, the bot detects large resting orders ("walls") in the order book:

```
wall detected when: level_size > avg_level_size × wall_min_size_multiplier
```

If a wall is found within the ladder's price range, one ladder level is "snapped" to sit one tick in front of the wall. The reasoning: a large resting order provides a support/resistance level. By sitting in front of it, the bot gets priority fills when the wall absorbs flow.

**Wall quality.** Snapped orders are tagged with `WallFront` provenance all the way through live/paper execution and markout tracking. When a wall-fronted fill receives its T5s markout, the `WallQualityTracker` updates a side-specific EWMA score:

- Benign or positive T5s markout raises the score toward 1.0.
- Adverse T5s markout lowers the score toward 0.0.
- If score falls below `wall_quality_min_score`, wall-fronting is disabled on that side for `wall_quality_disable_sec`.

This keeps wall-fronting available when walls hold, but prevents repeated snapping in front of walls that are being pulled or swept.

**Hysteresis:** when both `wall_quality_hysteresis_sec > 0` and `wall_quality_reenable_threshold_bps > 0`, the tracker requires a minimum continuous hold time before any state flip. Disable→enable additionally requires `score >= wall_quality_reenable_threshold_bps` (which should be set higher than `wall_quality_min_score` to form an asymmetric hysteresis band); enable→disable requires score below `wall_quality_min_score` for `hysteresis_sec` continuous time. Transient oscillations reset the pending timer. An INFO `wall_quality state` log fires on actual flips. Zero defaults preserve immediate-flip behavior.

### Price Rounding

All prices are rounded to Hyperliquid's tick size (5 significant figures) via `RoundToTickSize()`. For example, $70.123456 becomes $70.123.

### Order Type

All ladder orders use **ALO (Add Liquidity Only)** time-in-force, which guarantees maker execution (the order is rejected if it would cross the spread and take liquidity). This ensures the bot always earns maker fees, never pays taker fees.

### Volume Burst Ladders

When market trade velocity spikes, the bot temporarily **multiplies** the number of ladder levels per side while **keeping total notional the same**. Each order gets smaller, but the bot is present at more price points — catching fills across a wider range during volatile periods.

**Detection — dual-EWMA trade velocity:**

A `TradeVelocityTracker` (one per asset) processes every full public trade update from the gRPC/WS feed. It maintains two exponential moving averages of both trade rate and notional rate:

- **Fast EWMA** (half-life `burst_velocity_fast_sec`) — responds quickly to velocity spikes
- **Slow EWMA** (half-life `burst_velocity_slow_sec`) — establishes a rolling baseline

The **rate ratio** = fast / max(slow, `burst_velocity_min_floor`). The **notional ratio** uses the same fast/slow structure on dollar notional. The tracker also keeps signed notional fast EWMA and short-window price impact bps for shock/attribution logs. The `min_floor` prevents false triggers on very thin books where the slow baseline could be near zero.

When `max(rate_ratio, notional_ratio) >= burst_velocity_threshold`, burst mode activates. This lets a few large prints trigger burst handling even when raw trade count is low.

**Level scaling:**

```
effectiveLevels = baseLevels × burst_level_multiplier    // e.g. 2 × 2 = 4
perLevelNotional = totalNotional / effectiveLevels       // budget stays the same
```

Total notional stays the same — only the distribution changes. Spacing between levels uses the same vol-scaled formula as normal mode. The geometric graduation still applies across the expanded levels.

**Gradual decay:**

When trade velocity drops below the threshold:

1. The deactivation timestamp is recorded
2. Over `burst_decay_sec`, effective levels linearly interpolate from burst count back to base count
3. Level count is `floor()` of the interpolated value
4. Notional redistributes as levels reduce
5. If velocity spikes again during decay, burst reactivates immediately at full count

**Safety:**

- Burst mode is suppressed during warmup, recovery, and degraded health states
- The `$10 minimum order` constraint in `buildSideOrders` automatically caps actual levels to what the notional can support: `sideLevels = min(requestedLevels, floor(notional / $10))`
- `max_open_orders` still caps total orders across both sides

### Lazy Ladders

When `lazy_ladder_enabled` is true, the bot starts with 1 level per side (BBO only) and only expands to the full `ladder_levels` count when a fill occurs within the last `lazy_ladder_decay_sec` seconds. After the decay window elapses without a fill, levels collapse back to 1. This is the primary rate-limit conservation mechanism: idle refreshes drop from 6-12 orders (3 levels × 2 sides) to 2 orders (1 level × 2 sides), saving ~67% of per-refresh API credits during quiet periods.

**Notional scales with active levels:** when fewer levels are active than configured, per-side notional is scaled by `effectiveLevels / ladderLevels`. With 3 configured levels and 1 active (lazy contracted), each side gets 1/3 of the full budget — preventing a single lazy-level order from concentrating the entire `quote_notional` into one fill.

Volume burst only amplifies levels when lazy ladder is already expanded (post-fill); during idle periods, burst is suppressed regardless of trade velocity. This prevents burst from bypassing lazy ladder's idle savings. When `lazy_ladder_enabled` is false, burst applies directly to base ladder levels.

### Quiet Period Ladder Reduction

When no fills have occurred for `quiet_ladder_reduce_sec`, ladder levels are reduced by 1 (minimum 2) to save rate-limit tokens. Each order costs ~5 rate-limit tokens, so reducing from 3 to 2 levels per side saves ~33% of per-refresh token cost during idle periods. This reduction is suppressed when volume burst is active. Effectively subsumed when lazy ladders are enabled (lazy already reduces to 1).

---

## Adaptive Features

The bot has several adaptive features that dynamically adjust parameters based on market conditions.

### Adaptive Notional

Tracks the bot's fill rate (EWMA-smoothed $/minute) and adjusts quote notional to hit a target rate:

```
multiplier = target_per_min / actual_rate_per_min
```

- Clamped to `[adaptive_notional_min / quote_notional, adaptive_notional_max / quote_notional]`
- Enabled via `adaptive_notional_enabled`

When fills are slow, the multiplier increases (post more size to attract fills). When fills are fast, it decreases (reduce exposure from rapid fills). The EWMA decays toward the target rate during idle periods, so the multiplier returns to 1.0 rather than growing unbounded.

### Per-Side Fill Rate Tracking

Always active (built into `FillRateTracker`). The tracker maintains independent buy/sell EWMAs in addition to the global tracker. Each side has its own EWMA fill rate with target = `adaptive_notional_target_per_min / 2`. The quoting pipeline applies per-side multipliers via `NotionalMultiplierSide("buy")` and `NotionalMultiplierSide("sell")` in the sizing step, enabling asymmetric notional when one side overfills and the other underfills.

- Returns 1.0 (neutral) for sides with no data yet
- `RecordFillSide(side, notional)` updates both per-side and global trackers
- Backward-compatible: `RecordFill()` and `NotionalMultiplier()` still work

### Inventory Half-Life Controller

Measures the actual inventory decay rate and compares it against a target half-life (`inventory_half_life_minutes`). If inventory is decaying slower than the target, the controller multiplicatively increases the skew factor; if faster, it relaxes the skew. The skew factor is clamped to `[inventory_controller_skew_min, inventory_controller_skew_max]`.

```
observed_half_life = -ln(2) / decay_rate_estimate
ratio = observed_half_life / target_half_life
skew_adjustment = ratio^0.5                   // square root dampening
skew_factor = base_skew × skew_adjustment     // clamped to [skew_min, skew_max]
```

The controller evaluates every position update. If the position has been flat for longer than the target half-life, the skew factor relaxes toward the base value. This is a direct feedback loop: instead of inferring what the skew _should_ be from autocorrelation, it measures what the skew _is achieving_ and corrects.

**Anti-windup:** the PID integrator is frozen whenever the output is already saturated at `skew_max` (or `skew_min`) AND the current error would push further in the same direction. Without this, a long run pegged at max accumulates integrator charge that takes minutes to unwind once inventory finally reverses. The anti-windup freeze stops the accumulation at the rail, so when the error sign flips the integrator responds immediately. Hard `[−2.0, +2.0]` integrator clamps are kept as a belt-and-suspenders bound.

### Configurable Parameters

A number of values that were previously hardcoded are now configurable:

| Parameter                 | Config Field                    | Description                                                  |
| ------------------------- | ------------------------------- | ------------------------------------------------------------ |
| Composite markout weights | `markout_composite_weights`     | Per-horizon weights for T500ms/T1s/T2s/T5s markout composite |
| Microprice decay          | `microprice_decay`              | Geometric decay factor for multi-level microprice            |
| Emergency widen threshold | `emergency_widen_threshold_bps` | T1s markout below this triggers emergency widen              |
| Emergency widen amount    | `emergency_widen_amount_bps`    | Spread bps added on emergency widen                          |
| Quiet ladder reduce       | `quiet_ladder_reduce_sec`       | Seconds without fills before reducing ladder levels          |
| Toxicity size weight cap  | `toxicity_size_weight_cap`      | Max size-weighting factor in toxicity EWMA                   |
| Velocity initial seed     | `velocity_initial_seed`         | Initial trade velocity EWMA seed (trades/sec)                |

### Dynamic Refresh Interval

Context-aware refresh interval replaces the static `refresh_interval_ms` as the sole timer:

| Condition                                             | Interval               | Purpose                     |
| ----------------------------------------------------- | ---------------------- | --------------------------- |
| Recent fill (within 30s)                              | `refresh_post_fill_ms` | Post-fill quote freshness   |
| Adverse markout (worst EWMA < −1.5 bps)               | `refresh_adverse_ms`   | Slow down during toxic flow |
| Quiet book (no update > `refresh_quiet_threshold_ms`) | `refresh_quiet_ms`     | Conserve API budget         |
| Default                                               | `refresh_interval_ms`  | Normal operation            |

Post-fill takes priority over adverse/quiet. Adverse only applies when it slows refresh (value > base). Quiet only applies when the book is idle past the threshold.

### Runtime Optimizer

The optimizer is a unified, utility-driven controller that coordinates all adjustable knobs through a single evaluation loop. It compresses market signals into a 4-dimensional state vector and uses a policy table to compute coordinated target values.

**8 Knobs** (each tracked as a single value in `o.current`):

| Knob                 | Neutral | What it controls                                 |
| -------------------- | ------- | ------------------------------------------------ |
| `spreadDeltaBuy`     | 0 delta | Additive bps widening/tightening on the bid side |
| `spreadDeltaSell`    | 0 delta | Additive bps widening/tightening on the ask side |
| `notionalMult`       | 1.0     | Multiplicative scale on quote size               |
| `skewMult`           | 1.0     | Multiplicative scale on inventory skew           |
| `alphaDelta`         | 0.0     | Added to base `vwap_alpha`, clamped to [0, 1]    |
| `imbalanceShiftMult` | 1.0     | Multiplier on trade imbalance FV shift           |
| `fillDecayMult`      | 1.0     | Multiplier on post-fill adverse decay magnitude  |
| `refreshMult`        | 1.0     | Refresh interval scaling (<1 = faster)           |

Bounds for each knob are configured under `optimizer_v2.*` (per-knob min/max pairs, e.g. `optimizer_v2.spread_bps: [2.0, 10.0]`). The single `spread_bps` bound is applied to both `spreadDeltaBuy` and `spreadDeltaSell`.

**Fill sample gate:** The optimizer only activates after `min_fill_samples` fills have been observed. The `min_fill_sample_window_sec` config controls the rolling window over which fills are counted — fills older than this window are not considered, preventing stale fill history from gating the optimizer.

**4D State Model:**

| Dimension        | Range  | Signals                                                                                                                                                                                                                                        |
| ---------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Toxicity         | [0, 1] | Worse of (T1s WorstMarkout EWMA, composite T500ms/T1s/T2s/T5s markout) vs baseline (symmetric scaling for negative markouts), fill burst active, emergency widen count                                                                         |
| Opportunity      | [0, 1] | Fill rate ratio, autocorrelation (regime), VWAP deviation, spread/BBO ratio                                                                                                                                                                    |
| Inventory Stress | [0, 1] | Position utilization, inventory controller adjustment (normalized linearly across `[inventory_controller_skew_min, inventory_controller_skew_max]`), same-side duration, peer inventory (cross-asset), same-direction check, pair-reduce state |
| Budget Pressure  | [0, 1] | VirtCycle headroom, overridden by rate-limit degraded/critical flags                                                                                                                                                                           |

Each dimension is a weighted sum of its constituent signals (weights configurable via `toxicity_weights`, `opportunity_weights`, `inventory_stress_weights`). Thresholds for policy transitions are all configurable.

**Toxicity markout scaling:** The `tox_markout` sub-component uses asymmetric formulas depending on markout sign. For positive markouts (benign fills): `clamp(1.0 − worst_markout/baseline, 0, 1)` — markout equal to baseline → 0 (not toxic), markout at 0 → 1.0 (maximally toxic). For negative markouts (adverse selection): `clamp(−worst_markout/baseline, 0, 1)` — gives a linear gradient (−1 bps → 0.2, −2.5 bps → 0.5, −5 bps → 1.0 with baseline=5). This prevents saturation at 1.0 for all negative markouts, allowing the optimizer to distinguish mild from severe adverse selection.

`worst_markout` is computed as `min(WorstMarkoutBps, CompositeMarkoutBps)` — the worse of the T1s EWMA and the multi-horizon composite (T500ms/T1s/T2s/T5s blend). The T5s horizon often reveals toxicity invisible at T1s when fills are picked off by directional flow within seconds.

**Raw vs decayed per-side EWMA:** The optimizer reads a raw per-side EWMA (without idle decay) for `buyMarkout`, `sellMarkout`, and `worstMarkout1s`. The decayed EWMA is retained for (a) the Markout-to-FV feedback shift (where fading to baseline during idle periods is desirable so stale markouts don't keep pushing FV) and (b) adverse-refresh timing. Motivation: during long no-fill windows both sides' decayed EWMAs relax toward the baseline, and the toxicity spread multiplier reads "baseline" as "no widening needed." For the optimizer that inverted the signal at the worst possible moment. The raw/decayed split keeps both behaviors correct for their respective callers.

**Emergency sub-dim gate:** The emergency bypass fires when the combined `Toxicity > EmergencyToxicityThreshold`, OR `ToxEmergency > 0.5`, OR `ToxMarkout > 0.95` (deep markout saturation always fires regardless of combined value).

**`opp_tighten` gates.** The tighten branch of the opportunity policy is additionally gated by:

- `markoutHealth = clamp(1 + CompositeMarkoutBps/baseline, 0, 1)` — skips entirely when health < 0.25
- `OppTightenInvUtilGate > 0 AND |InventoryUtil| > gate AND PositionUnrealizedBps < 0` — block (labelled `opp_tighten_blocked_inv`)
- `OppTightenSessionPnLFloorBps < 0 AND SessionPnLBps < floor` — block (labelled `opp_tighten_blocked_pnl`)

Asymmetric when firing into a losing session: if a gate would block AND position is non-flat (`|InventoryUtil| > 0.05`), the tighten target is deferred past the per-side split and applied to the exit side only. This prevents the self-reinforcing loop where red PnL → gate blocks tightening → wide quotes don't fill → PnL stays red. When flat, the original full block is preserved.

**`skew_relax_on_adverse`.** The skew target logic turns `skewMult` into adverse-markout feedback: when `CompositeMarkoutBps < SkewRelaxAdverseMarkoutBps` AND `|InventoryUtil| > SkewRelaxInvUtilMin`, scale `skewMult` linearly down toward `SkewRelaxFloor`. Adversity = `(threshold − markout) / divisor`, clamped to `[0, 1 − floor]`. Composes multiplicatively with the `skew_inv_stress` rule when both fire. Aggressive inventory skew that gets picked off by informed flow is worse than neutral skew; the rule backs off skew intensity when recent fills have been adverse.

**Per-side winrate / profit factor.** Optional optimizer input. When `per_side_winrate_enabled` and both sides have `>= per_side_winrate_window_fills` samples, a side whose `WinRate < per_side_winrate_threshold` OR `ProfitFactor < per_side_profit_factor_threshold` has its `spreadDeltaBuy` / `spreadDeltaSell` target widened by `per_side_winrate_widen_bps`. Captures distribution shape (fat tails) where mean markout looks fine but one side is structurally losing. Logs `per-side winrate gate` on fire.

**Empirical-quantile sub-dim normalization.** When `subdim_normalization: "empirical_quantile"`, each of the five normally-hardcoded linear clips in `computeState` (`fillRate/2.0`, `|autocorr|×5.0`, `vwapDeviation/wickThreshold`, `(spreadBBORatio−1)/9.0`, `sameSideDuration/600.0`) is replaced by a `quantileNormalizer` that observes raw values and maps p5..p95 to [0, 1]. Falls back to the linear clip until `Ready()` (count ≥ window/5). Emits one-shot `quantile normalizer warm` and periodic 5-min `quantile normalizer snapshot` logs per coin per sub-dim. Also applied to `tox_markout`. `"linear"` (default) preserves the hardcoded clip behavior exactly.

**Fourth-dimension swap.** `fourth_dimension: "session_pnl"` replaces the `BudgetPressure` slot with `mapSessionPnLToStress()` — buckets are `< b1 → 1.0`, `[b1,b2) → 0.75`, `[b2,b3] → 0.50`, `(b3,b4] → 0.25`, `> b4 → 0.0` (edges configured by `session_pnl_bucket_edges_bps`). Pro-cyclical mitigation: when `state.BudgetPressure >= fourth_dim_defensive_floor` and the composed policy would net-tighten (`spreadBase < 0`), `spreadBase` is clamped to 0 and label `fourth_dim_defensive_clamp` is fired. Default `""` / `"budget_pressure"` preserves the budget-pressure pipeline.

**Cross-asset signals:** The inventory stress dimension incorporates peer inventory (other assets' position utilization, beta-normalized), same-direction detection (multiple assets accumulating the same side), and pair-reduce state. These prevent the optimizer from tightening one asset while another correlated asset is under inventory pressure.

**Trade flow inputs:** The opportunity and toxicity dimensions incorporate trade imbalance magnitude and imbalance saturation (fraction of recent reads at cap). High saturation indicates the imbalance signal is binary noise, reducing its influence on the state vector.

**Proportional control with momentum:**

Rather than discrete step changes, the optimizer uses proportional control: each knob moves smoothly toward its policy target at a rate governed by `learning_rate * confidence * velocity`. Momentum (`momentum`) smooths oscillations. Confidence ramps from 0 to 1 as fills accumulate toward `min_fill_samples`.

**Per-knob deadband.** Computes `lr × conf × newVelocity` before committing; if `|delta|` is below `spread_delta_deadband_bps` / `skew_delta_deadband` / `notional_delta_deadband`, the knob write AND the velocity update are skipped so noise does not accumulate momentum. Skipped knobs log one rate-limited `optimizer deadband skip` INFO per coin per 30s. Zero deadband (default) preserves per-eval behavior.

**Policy table (target computation):**

- **Spread (per-side):** Widens when toxic + low opportunity, tightens when benign + high opportunity. Budget pressure adds further widening. Mean-reversion regime tightens, trending widens. Per-side toxicity asymmetry shifts the more toxic side. The `regime_widen` rule applies asymmetrically when `regime_widen_entry_mult != 1.0` — entry side gets `entryMult×` the widening, exit side gets `(2 − entryMult)×`, preserving the total widening budget while biasing protection to the entry side.
- **Notional:** Reduces under high toxicity, increases under high opportunity + low inventory.
- **Skew:** Increases above the inventory-stress threshold, returns to neutral below the relax threshold.
- **Alpha:** Reduces VWAP weight when trending + toxic, increases when mean-reverting + benign.
- **FillDecay:** Scales up with toxicity to increase post-fill adverse shift.
- **Refresh:** Speeds up under high opportunity + low budget pressure. Slows under high budget pressure or toxicity.
- **ImbalanceShift:** Neutral (1.0) by default.

**Rate limiting:** Each eval ranks knobs by distance from target and applies only the top `max_knob_changes_per_eval`. Emergency bypass: when combined `state.Toxicity` exceeds `emergency_toxicity_threshold`, OR when any toxicity sub-dimension saturates (`tox_emergency > 0.5` OR `tox_markout > 0.95`), all knobs move simultaneously.

**Emergency force refresh.** When `optimizer_v2.force_refresh_on_emergency` is true, a material overlay change during optimizer emergency mode marks the affected coin for an immediate scoped refresh. Materiality is controlled by `force_refresh_min_spread_delta_bps` for spread knobs plus small fixed thresholds for notional, skew, and refresh multipliers. `force_refresh_min_interval_ms` throttles repeated forced refreshes. Forced orders carry `OrderRequest.Force`, which makes the live executor bypass normal price/size skip thresholds for that refresh.

**Burst-triggered evaluation.** When `optimizer_v2.burst_eval_enabled` is true, a new volume-burst activation from the `TradeVelocityTracker` fires an out-of-cadence optimizer eval. `burst_eval_min_interval_sec` throttles repeated burst-triggered evals.

**Per-knob selective rollback:** Tracks utility = `compositeMarkout × fillRateRatio`. If utility is negative for `rollback_window_evals` consecutive evals, graduated rollback is triggered. Only knobs whose per-knob markout has worsened are rolled back by `rollback_rate × (current − shadow)` toward session-start values. Knobs that are performing well are preserved.

**Directional cooldown after rollback:** After a knob is rolled back, it enters a directional cooldown that prevents it from being pushed in the same direction again for the cooldown period. This breaks apply-then-rollback-then-reapply oscillation cycles. The knob can still be moved in the opposite direction during cooldown.

**Shadow mode:** When `shadow_mode: true`, the optimizer computes all state, policy targets, and knob adjustments but does not apply them. All decisions are logged for offline analysis. Useful for validating optimizer behavior on a new asset before going live.

**Emergency widen:** `EmergencyWiden(side, deltaBps)` immediately widens a specific side, bypassing the normal eval cycle.

**Logging:** The optimizer emits structured JSON logs at each evaluation cycle for full observability:

- `[OptimizerV2] inputs` — all input signals (markouts, fill rates, inventory, autocorr, VWAP deviation, budget pressure, imbalance saturation)
- `[OptimizerV2] state` — 4D state vector with per-dimension component breakdown (e.g., `tox_markout`, `tox_burst`, `tox_emergency`)
- `[OptimizerV2] policy` — comma-separated list of which policy conditions fired (e.g., `tox_widen,fdecay_tox`) and per-knob target values
- `[OptimizerV2] evaluate` — per-knob approach detail (`prev->new(tgt=X,vel=Y)`), cooldown blocks, or `(no-op)` when no knobs changed
- `[OptimizerV2] utility` — counterfactual self-evaluation with utility value, fill rate, composite markout, rolling window status, and rollback readiness
- `[OptimizerV2] state crossing` — threshold transition events when any state dimension crosses a policy threshold
- `[OptimizerV2] emergency ENTER/EXIT` — transitions in/out of emergency mode
- `[OptimizerV2] graduated rollback` — per-knob detail showing which knobs were rolled back vs kept (with markout comparison), all post-rollback values, and cooldown duration

All log lines include `coin` for multi-asset attribution.

**Quote attribution logs:** When `quote_attribution_enabled` is true, the quote manager emits sampled `quote attribution` logs with the main per-layer contributions: base bid/ask spread, optimizer deltas, skew, spread bias, wick multipliers, toxicity multipliers, trend guard, flow gate, price shock, and final allowed buy/sell notional. Sampling uses `quote_attribution_min_interval_sec`, but logs always fire while trend/flow/shock guards are active.

### Rate Limit Allocator

**Status:** Active when `rate_limit_allocator_enabled: true` AND 2+ assets are configured. No-op for single-asset mode.

Hyperliquid API rate limits regenerate at 1 request per 1 USD traded cumulatively. With multiple assets each requiring periodic refresh calls, the bot needs to allocate API budget intelligently: spending more on profitable assets and less on loss-making ones.

The allocator tracks per-asset ROI via EWMA-smoothed metrics:

```
roiScore = captureEWMA × volumeEWMA / max(apiCalls, 1)
```

- `captureEWMA` — EWMA of spread capture (in bps) per fill for this asset
- `volumeEWMA` — EWMA of fill volume (USD) per fill
- `apiCalls` — cumulative API calls since last evaluation

`Evaluate()` is called every 30 seconds (after each status tick). It normalizes scores across all assets and maps the range [minROI, maxROI] to a refresh multiplier range [maxMult, minMult]:

- Highest-ROI asset → `rate_limit_min_refresh_multiplier` (e.g. 0.7 = 30% faster refresh)
- Lowest-ROI asset → `rate_limit_max_refresh_multiplier` (e.g. 3.0 = 3× slower refresh)
- All-equal scores (including before first evaluation) → 1.0 for all assets

This multiplier is applied inside `computeRefreshIntervalMs()` as the final scaling factor, after the optimizer refresh overlay. Assets that are filling profitably get more API budget; stale or unprofitable assets are deliberately slowed.

### Virtuous Cycle Engine

Always active (initialized unconditionally in `Manager.New()`).

Hyperliquid's rate limit regenerates at 1 request per 1 USD traded — meaning trading _volume itself_ replenishes the API budget. The virtuous cycle engine tracks whether fills are regenerating budget faster than API calls are consuming it.

**Dual-EWMA model:**

```
volumeRate = EWMA($/min of fill volume)
burnRate   = EWMA(API calls/min)
headroom   = clamp(-netBurn / burnRate, -1, 1)
           where netBurn = burnRate - volumeRate
```

- `headroom = +1.0` — fills completely cover API usage (virtuous cycle in effect)
- `headroom = 0.0` — balanced (volumes regen exactly as much as API costs)
- `headroom = -1.0` — API calls far exceed fill volume (degenerate: burning budget without fills)

`BudgetPressure() = (1 - headroom) / 2` maps headroom to [0, 1] for optimizer consumption. The `BudgetPressure` dimension in the optimizer state model incorporates this value alongside rate-limit tier flags (degraded/critical override to 0.75/1.0 respectively).

Status logs include `virt_cycle` fields: `headroom`, `volume_rate_usd_min`, `burn_rate_calls_min`, `cum_volume_usd`.

---

## Risk Management

The risk manager monitors positions and equity and can halt quoting to prevent catastrophic losses.

### Position Limits

Two per-asset limits and one aggregate limit are checked on every position update:

- `max_position_notional` — halts if any single asset's position notional reaches this value
- `max_spread_exposure` — halts if any single asset's notional reaches the per-asset spread exposure limit
- `max_aggregate_exposure` — halts if the sum of all asset position notionals exceeds this portfolio-wide cap (0 = falls back to global `max_spread_exposure`)

The per-asset limits prevent any single asset from dominating the book. The aggregate limit is a portfolio-wide safety net that prevents the combined exposure across all assets from growing too large, even when no individual asset has breached its own limit.

When halted from a position limit, the bot automatically resumes once all positions are flat (closed by the executor).

### Pair-Reduce Mode

**Status:** Only active when running 2+ assets. In single-asset mode this is a no-op.

When both legs are long (or both short) and combined notional exceeds `pair_reduce_entry_fraction` of `max_spread_exposure`, the bot enters pair-reduce mode: only the exit side is quoted, pricing is tightened by `pair_reduce_tighten_factor`, and it deactivates with hysteresis (`pair_reduce_exit_fraction`).

### Equity Polling

Equity is polled every 5 seconds. The number of API calls depends on the asset mix:

- **Pure HIP-3 dex**: 2 calls — dex `marginSummary.accountValue` + spot USDC (`total − hold`).
- **Pure standard perps**: 2 calls — perps `marginSummary.accountValue` + spot USDC.
- **Mixed (HIP-3 + perps)**: 3 calls — dex equity + perps equity + spot USDC. This is needed because HIP-3 dex and perps are separate margin pools on Hyperliquid.

All sources are queried in parallel (goroutines) to minimize the settlement race window. Despite this, non-atomic reads can still produce transient spikes or drops. A **median-of-5 filter** rejects outliers: only values confirmed by at least 3 of the 5 most recent readings are accepted. This requires 3+ consecutive bad readings to defeat, and the ring buffer is seeded at init so the filter is active from the first poll.

### Equity Drawdown

The risk manager tracks a **smoothed equity** using an EMA (`drawdown_smoothing_samples`) and maintains a **high-water mark**:

```
drawdown_pct = (highWater - smoothedEquity) / highWater × 100
```

If `drawdown_pct > max_drawdown_pct`, quoting is halted and all positions are closed.

**Recovery sequence:**

1. Wait for `drawdown_cooldown_sec`
2. Check equity has recovered to `min_resume_equity_pct` of high-water
3. If not recovered, increment `haltCount` and extend cooldown
4. If `haltCount >= max_consecutive_halts`, **permanent halt** — bot stops entirely
5. On resume, enter recovery mode: reduced notional (`recovery_notional_pct`) and wider spreads (`recovery_spread_multiplier`) for `recovery_duration_sec`

A **successful run** (time since last resume exceeds the cooldown period) resets the halt counter. Reaching a new equity high-water also resets it.

### Session Stop-Loss

Distinct from the equity-drawdown path (which compares smoothed equity to the long-running high-water mark), the session stop-loss monitors **session-relative PnL** via `SessionPnLTracker`. When `risk.session_stop_loss_bps < 0` and `SessionPnLTracker.PnL() <= session_stop_loss_bps`, the risk manager emits a `RiskSessionStopLoss` action. On trigger the quote manager:

1. Signals the supervisor into `SignalRiskHalt` (forces flatten).
2. Fires a post-mortem dump with reason `"session_stop_loss"` (if enabled).
3. Cancels all orders and closes all positions.

The risk manager stays in forced-HALT for `session_stop_loss_pause_sec` seconds, then emits `RiskResumeFromSessionStop`. The quote manager enters a distinct recovery state (`sessionStopRecovery = true`) that uses `session_stop_loss_recovery_notional_pct` (instead of the drawdown path's `recovery_notional_pct`) for `session_stop_loss_recovery_duration_sec`. The session PnL tracker is reset on resume so a failed recovery can re-trigger the stop. Default `session_stop_loss_bps: 0` is a no-op.

**Early-warn widen.** A softer rung sits above the hard stop: when `session_stop_loss_warn_bps < 0` (less negative than the hard floor) and session PnL crosses it, the quote manager multiplies both bid and ask spreads by `session_stop_loss_warn_spread_mult` and scales notional by `session_stop_loss_warn_notional_pct`. Widen-only — no flatten, no pause. The check is inline in the spread-composition pipeline and runs on every quote refresh, so response latency is bounded by the refresh cadence rather than the risk manager's ticker. A warn rung gives spreads a chance to pre-widen before the hard floor fires, compressing overshoot.

### Session Flatten on Shutdown

When `risk.session_flatten_on_shutdown` is `true`, graceful shutdown (SIGINT/SIGTERM) does not just cancel resting orders — after the cancel loop completes, `cmd/bot/main.go` spawns a per-asset goroutine that calls `ClosePosition(ctx, asset, "session_flatten_shutdown")` on any non-flat position, with an independent `session_flatten_timeout_sec` per asset so one stuck close does not block the others. Runs only in live mode. Fires only on operator-initiated shutdown — there is no scheduled trigger — so the timing is always under user control. Default `false` preserves cancel-only behavior.

### Absolute Equity Floor

If smoothed equity drops below `absolute_equity_floor` for 3 consecutive readings, the bot permanently halts. This is a catastrophic stop-loss that cannot be resumed without restarting.

### Fail-Closed

If the executor fails to refresh orders (API error, timeout), the bot enters **fail-closed mode**: cancels all outstanding orders and halts. It will attempt automatic recovery after `fail_closed_cooldown_sec`, up to `max_fail_closed_retries` times.

### Position Close Strategy

When the risk manager triggers a halt, the executor attempts to close positions in three tiers:

1. **Passive close** — post an aggressive ALO at the best bid/ask, wait up to 5 seconds
2. **Limit IOC with slippage** — send an IOC order at 1× then 2× `max_close_slippage_bps`
3. **Market close** — SDK market close with 3 retries

---

## Execution

### Execution Modes

**Paper mode** (`live_mode: false`): Simulates fill logic using order book data. A paper order fills when the best bid crosses below a sell limit or the best ask crosses above a buy limit. Paper equity = starting equity + realized PnL + unrealized PnL. Useful for testing strategies without risking capital.

**Live mode** (`live_mode: true`): Sends locally signed native actions directly to Hyperliquid `/exchange`. This local checkout includes no builder field and adds no builder fee. Standard exchange fees still apply.

### Order Refresh

The executor uses a **diff-based refresh** rather than cancel-all-then-replace:

1. Compare desired orders against tracked live orders
2. Partition matches by side, sort by price
3. For each positional match:
   - If price and size are within thresholds → **skip** (no action needed)
   - If price or size changed → **modify** in place (single API call)
   - If no match → **cancel** stale + **place** new
4. Cancels and modifies execute concurrently in Phase 1
5. New placements execute in Phase 2 (after cancels complete)

This minimizes API calls and avoids the brief unquoted period that cancel-all-then-replace creates.

### Side-Specific Protective Cancels

`Executor.CancelSide(ctx, coin, side, reason)` is implemented by both live and paper executors. It cancels only tracked open orders on one side of one coin, preserving the opposite side. The quote manager uses this for:

- Price-shock entry protection
- Trend-guard full freeze when the current entry side has live orders but desired orders have been zeroed
- Emergency markout streaks, after 3 consecutive adverse markouts on the same side

This is lower latency and lower request cost than a full cancel-and-replace when the immediate goal is to remove stale toxic entry exposure while leaving exit-side orders live.

### Position Reconciliation

Every 30 seconds (15 seconds when halted) the live executor queries the exchange for actual positions and compares against local tracking:

- **Dust** differences (< $10 notional) are ignored
- **Hard** mismatches (side flip or > $50) trigger coin-level quoting halt
- **Side flip within exposure:** Side flips where the notional delta is within `max_spread_exposure` are treated as **soft corrections** (not hard drift). This handles the common case where rapid fills on resting orders flip the on-chain position, but the WS/poller hasn't delivered all fills yet. The position is corrected from chain state and a synthetic fill is generated, but quoting is not halted.
- **Post-fill grace period:** When any fill was recently processed (not just sign-flip fills), hard-mismatch classification is suppressed for `recon_grace_period_ms`. The position is still corrected from chain state, but the halt is suppressed. This prevents false-positive halts when fills arrive rapidly and the reconciliation ticker fires before all fills are reflected in local state.
- **Refresh-in-progress guard:** Order-count reconciliation is skipped while any `Refresh` / `RefreshMulti` is executing. During a refresh, the exchange order state is transitional (cancels/modifies in flight), so count comparisons are unreliable. Real drift is caught on the next reconciliation tick when no refresh is active.
- **Order generation grace:** An order-generation timestamp is recorded whenever refresh, cancel-all, side-cancel, fill reconciliation, or tracking-clear mutates tracked order state. If order-count reconciliation sees a mismatch inside `recon_grace_period_ms` after that generation, it defers the mismatch instead of clearing tracking. This separates expected in-flight exchange skew from real drift.
- **Empty-exchange fast path:** When the mismatch is specifically `tracked>0 && exchange=0` (all tracked orders silently auto-cancelled by the exchange or ALO-rejected on place), the reconciler uses the shorter `recon_empty_exchange_grace_ms` instead of `recon_grace_period_ms`. Partial-drift mismatches (e.g. `tracked:8, exchange:6`) still use the full grace since they're typically transient mid-refresh skew; a fully empty exchange side is unambiguous and waiting 30s wastes quoting time. The deferral log includes `grace_ms` and `grace_reason` (`empty_exchange` vs `normal`). ALO rejections are logged at INFO (both single-order and batch paths) so the underlying cause is visible without DEBUG logging.
- **Recovery** requires 2 consecutive healthy reconciliations
- **Mismatch diagnostic:** when `execution.recon_mismatch_diagnostic_enabled` is true, the order-count reconciliation emits a structured `reconciliation mismatch diagnostic` INFO line (before the WARN clear) containing tracked OIDs + ages, exchange OIDs, symmetric difference (`only_local` / `only_exchange`), `only_local_sides` / `only_local_prices` enrichment, `generation` + `generation_age_ms`, and `exchange_fetch_ok` (false when the `OpenOrders` call itself errored, removing the zero-count ambiguity). Rate-limited per-coin via a sliding-hour counter (`recon_mismatch_diagnostic_max_per_hour`). Diagnostic-only — does not change recon behavior.

### Fill Detection

Fills are detected from two sources (dual-source for reliability):

- **WebSocket UserFills** — real-time, ~50-200ms latency
- **REST fill poller** — every `poll_fills_sec` seconds (default 10s), queries recent fills

Content-based deduplication (`oid:coin:price:size:side`) prevents double-counting.

### Retry Logic

All exchange operations use **3-attempt exponential backoff** (100ms → 200ms → 400ms). Non-retryable errors (invalid parameters, insufficient margin) short-circuit immediately. ALO rejections ("would cross") are treated as benign — they mean the order would have been a taker, which is expected during fast markets.

---

## Health System

The quote manager monitors three data freshness signals per asset:

### Trade Freshness

Trades feed into VWAP calculators. If trades stop arriving:

- After `trade_stale_degraded_sec` → **degraded mode**: spread widened 1.5×, notional halved
- After `trade_stale_halt_sec` → quoting halted for that asset

These thresholds are generous for HIP-3 markets where natural trade gaps of 15-60 seconds are common.

### Book Freshness

If no book update arrives within `max_book_staleness_ms`, all orders are cancelled. If no update arrives within `max_no_book_update_ms`, all orders are cancelled AND positions are closed (feed death).

### Fill Poll Freshness

If the fill poller hasn't succeeded in 6 seconds or has 2+ consecutive errors → degraded. After 15 seconds or 5+ errors → halt.

---

## Operations

### Post-Mortem Dump

When enabled via the top-level `postmortem:` block, the `PostMortemDumper` continuously buffers three ring-buffers per process: the last `include_last_n_evals` optimizer eval snapshots, the last `include_last_n_refreshes` refresh attributions, and (when `include_all_fills: true`) every fill of the session. On transition into `OpFlatten` (edge-detected; 5s identical-reason throttle), graceful shutdown, `RiskHalt`, `RiskSessionStopLoss`, RL critical, or feed-death, the dumper writes one per-asset structured JSON file to `output_dir` with sections: `coin`, `reason`, `timestamp`, `session` (start/current equity, elapsed, window PnL), `final_state` (optimizer `Snapshot()` + knobs + overlay + confidence), `last_evals`, `last_refreshes`, `fills`, `per_side_stats`.

Filename templating supports `{coin}`, `{YYYYMMDD}`, `{HHMMSS}`, `{reason}` with path-component sanitization (colons in coin names become underscores). After a successful write, the directory is rotated by mtime to keep `rotate_keep_count` files. All recorder methods are nil-safe and concurrent-safe; JSON I/O happens outside the dumper mutex so producers are not blocked. A one-line `post-mortem written` INFO log with `path`, `reason`, `size_bytes` surfaces the file to the monitor TUI.

Default OFF (`postmortem.enabled: false` or block absent). Purpose: convert post-incident analysis from log archaeology into `jq` over a single file.

### Markout EWMA Snapshot Log

The periodic status loop emits a `markout_ewma_snapshot` INFO line per asset per status tick (once per `logging.status_interval_sec`, default 60s). The line exposes the running EWMA values that would otherwise only be visible by re-deriving them from hundreds of raw `markout` fill events or by parsing the fused composite out of `[OptimizerV2] inputs`:

- `horizon_500ms_bps` / `horizon_1s_bps` / `horizon_2s_bps` / `horizon_5s_bps` — fused (both sides) EWMA at each horizon, from `MarkoutTracker`
- `horizon_fills` — total completed fills feeding the fused EWMA
- `buy_ewma_bps` / `sell_ewma_bps` — per-side composite EWMA from `SideToxicityTracker` (includes idle decay)
- `buy_fills` / `sell_fills` — per-side markout counts

Per-side × per-horizon (2×4) decomposition is not yet exposed — the trackers don't bucket that way today.

---

## Configuration Reference

The canonical schema is `config.example.yaml` in the repo root — every field is documented inline there. Copy it to `config.yaml` (or any filename) and pass via `-config <path>`.

### Per-Asset Overrides

Each of `market_making`, `execution`, and `risk` sections supports a `per_asset` map that overrides specific fields for individual assets. Unspecified fields inherit from the global (top-level) config. This allows running multiple assets with different parameters in a single bot process.

- `strategy.spread_engine_enabled` — explicit bool to control the spread engine. `nil` = auto (enabled with 2+ assets), `false` = disabled (for independent multi-asset setups)
- `market_making.per_asset` — override any market-making field (spread, notional, ladder, VWAP, etc.) per asset
- `execution.per_asset` — override `leverage` per asset
- `risk.per_asset` — override `max_position_notional`, `max_spread_exposure`, `max_open_orders` per asset

The `Config.ForAsset(coin)` method returns the fully resolved config for a given asset. All runtime code uses `ForAsset()` instead of reading the global config directly.

### Non-Obvious Cross-Cutting Behaviors

- **Optimizer per-asset overrides**: `optimizer_v2.per_asset` is struct-level replace, not field merge — the override MUST include `enabled: true` or the asset silently falls off the optimizer.
- **`inventory_skew_exit_preserve`**: when true, skew only shifts the entry side; the exit side quotes at its natural (bias-adjusted) price.
- **Toxicity attenuation**: per-side toxicity widening is attenuated when the optimizer has already widened the same side, to avoid double-counting. Output is also suppressed until `toxicity_warmup_fills` is reached.
- **Spread stack cap** (`max_total_spread_multiplier`): caps compounded per-side spread so toxicity + burst + optimizer can't produce unfillable quotes.
- **Crossing protection**: position-aware — keeps the exit side at its computed price and pulls only the entry side to maintain `min_crossing_spread_bps`. Midpoint clamp is the flat fallback.
- **Rate-limit allocator**: when enabled, API budget is distributed per-asset by ROI, not uniformly. `VirtCycleEngine` feeds `BudgetPressure` to the optimizer.
- **Flow Gate `min_util`**: scales adverse-flow suppression by inventory utilization, so the gate is a no-op when nearly flat.

### Strategy

| Field                   | Description                                                                   |
| ----------------------- | ----------------------------------------------------------------------------- |
| `assets`                | List of coin symbols to quote (e.g. `["xyz:CL"]`, `["BTC", "ETH"]`, or mixed) |
| `spread_type`           | Spread computation type (used with 2+ assets): `"difference"`                 |
| `lookback_window`       | Rolling window size for spread stats and OLS beta                             |
| `use_beta_hedge`        | OLS beta-adjusted spread (2+ assets only)                                     |
| `spread_engine_enabled` | Explicit toggle; `nil` = auto (on with 2+ assets)                             |
| `z_min_stddev`          | Floor on spread stddev before z-score computation                             |
| `beta_min` / `beta_max` | Clamps for OLS beta                                                           |

### Fair Value (market_making)

| Field                              | Description                                                       |
| ---------------------------------- | ----------------------------------------------------------------- | -------- | ------------------- |
| `vwap_short_window_minutes`        | Short VWAP window (fair value anchor)                             |
| `vwap_long_window_minutes`         | Long VWAP window (trend filter for wick detection)                |
| `vwap_alpha`                       | Blend weight: 0 = pure microprice, 1 = pure VWAP                  |
| `vwap_alpha_drift_scaling_enabled` | Fade alpha toward floor during sustained trends                   |
| `vwap_alpha_drift_k`               | Sigma multiplier for divergence fade                              |
| `vwap_alpha_floor`                 | Minimum effective alpha after fade                                |
| `vwap_alpha_trend_boost_enabled`   | Boost alpha toward ceiling on positive autocorrelation            |
| `vwap_alpha_trend_boost_ceiling`   | Boost target ceiling                                              |
| `trend_fv_drift_bps`               | Max FV shift magnitude (bps) from trend-momentum bias; 0 disables |
| `trend_fv_drift_autocorr_min`      | Min                                                               | autocorr | to trigger FV shift |
| `microprice_levels`                | Number of book levels for multi-level microprice                  |
| `microprice_decay`                 | Geometric decay factor for multi-level microprice weighting       |
| `trade_imbalance_mode`             | `"time"` (rolling window) or `"volume"` (VPIN-lite volume-clock)  |
| `trade_imbalance_window_sec`       | Window for buy/sell volume imbalance (time mode)                  |
| `trade_imbalance_bucket_size`      | $ per volume bucket (volume mode)                                 |
| `trade_imbalance_bucket_count`     | Completed buckets to retain (volume mode)                         |
| `trade_imbalance_size_exponent`    | Power-law size weight; 1.0 = raw                                  |
| `trade_imbalance_max_shift_bps`    | Max FV shift from trade flow imbalance                            |
| `fill_adverse_shift_bps`           | Post-fill adverse selection shift                                 |
| `fill_decay_half_life_ms`          | Decay half-life for post-fill shift                               |
| `markout_fv_scale`                 | Fraction of markout EWMA asymmetry applied as FV shift            |
| `markout_fv_max_shift_bps`         | Max FV shift magnitude from markout feedback                      |
| `max_fv_deviation_pct`             | Clamp FV if it deviates more than this from book mid; 0 disables  |

### Quoting

| Field                               | Description                                                              |
| ----------------------------------- | ------------------------------------------------------------------------ |
| `quote_spread_bps`                  | Base half-spread in basis points (float, supports fractional)            |
| `quote_spread_bps_buy`              | Per-side override: buy half-spread (0 = fallback to `quote_spread_bps`)  |
| `quote_spread_bps_sell`             | Per-side override: sell half-spread (0 = fallback to `quote_spread_bps`) |
| `quote_notional`                    | Base notional per side before adjustments                                |
| `refresh_interval_ms`               | Default time between requotes (ms)                                       |
| `refresh_post_fill_ms`              | Faster refresh after fills                                               |
| `refresh_adverse_ms`                | Slower refresh during adverse markout                                    |
| `refresh_quiet_ms`                  | Slower refresh during quiet book periods                                 |
| `refresh_quiet_threshold_ms`        | Book-silence threshold for quiet mode                                    |
| `requote_threshold_bps`             | Price change threshold to trigger requote                                |
| `size_requote_pct`                  | Size change threshold to trigger requote                                 |
| `max_quote_age_ms`                  | Force requote after this time regardless                                 |
| `fee_floor_enabled`                 | Widen quotes to cover round-trip fees                                    |
| `maker_fee_bps` / `builder_fee_bps` | Exchange fee components                                                  |

### Microstructure

| Field                    | Description                                         |
| ------------------------ | --------------------------------------------------- |
| `dynamic_width`          | Enable volatility/imbalance-based width adjustments |
| `width_vol_window`       | Number of samples for realized vol calculation      |
| `width_max_multiplier`   | Maximum per-side width multiplier                   |
| `width_imbalance_levels` | Book levels to consider for imbalance signal        |

### Spread Bias

| Field                      | Description                                                   |
| -------------------------- | ------------------------------------------------------------- |
| `spread_bias_scale`        | tanh denominator (higher = gentler); 0 disables               |
| `spread_bias_max_bps`      | Max bias magnitude in bps                                     |
| `spread_bias_max_fraction` | Cap bias at this fraction of quote spread                     |
| `spread_bias_asymmetry`    | Convergence/divergence split (0.70 = 70% on convergence side) |

### Inventory Skew

| Field                                            | Description                                                                 |
| ------------------------------------------------ | --------------------------------------------------------------------------- |
| `inventory_skew_factor`                          | Base skew multiplier (scaled by spread)                                     |
| `inventory_skew_gamma`                           | Position utilization exponent (1.0 = linear, >1 = convex)                   |
| `inventory_skew_asymmetry`                       | Aggressive/passive skew split                                               |
| `inventory_skew_exit_preserve`                   | Only shift entry side; exit side keeps bias-adjusted price                  |
| `cross_leg_skew_enabled`                         | Beta-normalized cross-leg skew (2+ assets only)                             |
| `entry_size_reduction_rate`                      | Linear entry-side notional reduction rate                                   |
| `entry_size_reduction_floor`                     | Minimum entry-side fraction                                                 |
| `entry_accel_threshold` / `entry_accel_exponent` | Non-linear acceleration cliff                                               |
| `entry_reduction_trend_k`                        | Trend-adverse acceleration multiplier                                       |
| `inventory_half_life_minutes`                    | Target inventory half-life for controller                                   |
| `inventory_controller_skew_min` / `skew_max`     | Min/max skew factor; also bounds optimizer `inv_ctrl` sub-dim normalization |
| `inventory_controller_gain_p/_i/_d`              | PID gains                                                                   |
| `position_age_tighten_min_sec`                   | Start tightening exit after this hold time (0 = disabled)                   |
| `position_age_tighten_bps`                       | bps/min of exit-side tightening                                             |
| `funding_urgency_enabled`                        | Enable funding-rate-aware skew urgency                                      |
| `funding_urgency_scale`                          | Urgency multiplier scale factor                                             |
| `funding_urgency_horizon_minutes`                | Minutes before reset where urgency ramps                                    |
| `funding_poll_interval_sec`                      | Seconds between funding rate API polls                                      |
| `funding_interval_hours`                         | Per-asset funding interval (0 = auto: 1h HIP-3, 8h standard)                |

### VWAP Wick Sizing

| Field                           | Description                                                                   |
| ------------------------------- | ----------------------------------------------------------------------------- |
| `wick_size_multiplier`          | Maximum size boost during a wick                                              |
| `wick_dev_threshold_pct`        | Short VWAP deviation to trigger wick detection                                |
| `wick_long_dev_threshold_pct`   | Long VWAP deviation ceiling (above = trend, not wick)                         |
| `wick_max_inventory_util`       | Suppress wick amplification above this inventory utilization (0 disables)     |
| `wick_trend_suppress_enabled`   | Remove entry-side wick amplification while trend/flow/shock guards are active |
| `wick_trend_suppress_threshold` | Guard suppression threshold used to disable wick amplification                |

### Price Shock Guard

| Field                                       | Description                                                          |
| ------------------------------------------- | -------------------------------------------------------------------- |
| `price_shock_enabled`                       | Fast book-mid shock guard                                            |
| `price_shock_window_ms`                     | Rolling mid-price window                                             |
| `price_shock_threshold_bps`                 | Absolute move threshold over the window                              |
| `price_shock_cooldown_ms`                   | Guard active duration after trigger                                  |
| `price_shock_freeze_entry`                  | Zero/cancel the inventory-increasing side while active               |
| `price_shock_exit_size_factor`              | Exit-side size factor during adverse shock (1.0 disables)            |
| `price_shock_favorable_exit_factor`         | Exit-side size boost when shock favors inventory (1.0 disables)      |
| `price_shock_favorable_exit_tighten_bps`    | Absolute bps tightening on profit-taking side during favorable shock |
| `price_shock_favorable_ioc_frac`            | Fraction of position to IOC-close on favorable shock (0 disables)    |
| `price_shock_favorable_ioc_min_inv_util`    | Min inv utilization gate for favorable IOC                           |
| `price_shock_favorable_ioc_min_capture_bps` | Min shock magnitude to fire favorable IOC                            |
| `price_shock_favorable_ioc_slippage_bps`    | Slippage past best-opposite for favorable IOC                        |
| `shock_chain_enabled`                       | Cascade detection across multiple shocks                             |
| `shock_chain_window_sec`                    | Rolling window for shock counting                                    |
| `shock_chain_threshold`                     | Shocks to trigger flatten + pause                                    |
| `shock_chain_pause_sec`                     | Pause duration after escalation                                      |
| `shock_chain_recovery_sec`                  | Recovery duration after pause                                        |

### Trend Guard / Flow Gate / Regime Gate

| Field                                                                                                                                         | Description                                             |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `trend_guard_enabled`                                                                                                                         | VWAP-momentum entry gate                                |
| `trend_guard_threshold_pct`                                                                                                                   | Activation threshold for entry suppression              |
| `trend_guard_saturation_pct`                                                                                                                  | Saturation threshold at `max_suppress`                  |
| `trend_guard_max_suppress`                                                                                                                    | Max entry-side notional reduction                       |
| `trend_guard_spread_widen_bps`                                                                                                                | Entry-side price widening when suppression ≥ 0.5        |
| `trend_guard_spread_widen_asymmetry`                                                                                                          | Multiplier on the entry-side widening                   |
| `trend_guard_freeze_threshold`                                                                                                                | Suppression threshold above which entry is fully frozen |
| `flow_gate_enabled`                                                                                                                           | Trade-imbalance entry gate                              |
| `flow_gate_threshold` / `flow_gate_max_suppress` / `flow_gate_min_util`                                                                       | Same ramp structure, scaled by inventory utilization    |
| `regime_gate_enabled`                                                                                                                         | Autocorrelation + VWAP-divergence regime gate           |
| `regime_autocorr_threshold` / `regime_vwap_divergence_sigma`                                                                                  | Activation thresholds (both must hold)                  |
| `regime_gate_min_duration_sec`                                                                                                                | Minimum duration before activation                      |
| `regime_gate_max_suppression`                                                                                                                 | Entry-side notional reduction on the trend-aligned side |
| `idle_fill_detector_enabled`                                                                                                                  | One-shot exit tighten + size boost after long idle      |
| `idle_fill_threshold_sec` / `idle_fill_min_inv_util` / `idle_exit_tighten_bps` / `idle_exit_size_multiplier` / `idle_fill_retry_interval_sec` | Idle circuit breaker parameters                         |

### Ladder

| Field                                                                                                         | Description                                            |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `ladder_levels`                                                                                               | Number of price levels per side                        |
| `ladder_spacing_bps`                                                                                          | Base spacing between levels                            |
| `ladder_size_graduation`                                                                                      | Geometric size ratio between levels                    |
| `ladder_spacing_exponent`                                                                                     | Non-linear spacing curve (>1 = wider outer levels)     |
| `ladder_spacing_dynamic`                                                                                      | Scale spacing with realized vol                        |
| `lazy_ladder_enabled` / `lazy_ladder_decay_sec`                                                               | BBO-only by default, expand after recent fills         |
| `volume_burst_enabled`                                                                                        | Expand levels during trade velocity spikes             |
| `burst_velocity_threshold`                                                                                    | Fast/slow EWMA ratio to trigger burst                  |
| `burst_velocity_fast_sec` / `burst_velocity_slow_sec`                                                         | EWMA half-lives                                        |
| `burst_velocity_min_floor`                                                                                    | Minimum slow-rate floor (trades/sec)                   |
| `burst_level_multiplier`                                                                                      | Level count multiplier during burst                    |
| `burst_decay_sec`                                                                                             | Linear decay time after burst ends                     |
| `wall_front_enabled` / `wall_min_size_multiplier`                                                             | Snap level in front of large walls                     |
| `wall_quality_enabled` / `wall_quality_half_life_sec` / `wall_quality_min_score` / `wall_quality_disable_sec` | Track wall-fronted fill quality and disable poor sides |
| `wall_quality_hysteresis_sec` / `wall_quality_reenable_threshold_bps`                                         | Hysteresis band parameters                             |

### Adaptive Features

| Field                                                              | Description                                                    |
| ------------------------------------------------------------------ | -------------------------------------------------------------- |
| `adaptive_notional_enabled`                                        | EWMA fill-rate targeting                                       |
| `adaptive_notional_target_per_min`                                 | Target fill rate ($/min)                                       |
| `adaptive_notional_min` / `adaptive_notional_max`                  | Floor/ceiling notional                                         |
| `adaptive_notional_half_life_sec`                                  | EWMA half-life                                                 |
| `regime_window`                                                    | Rolling window for autocorrelation                             |
| `adaptive_z_enabled` / `z_calibration_window`                      | Z-score self-calibration (2+ assets only)                      |
| `toxicity_per_side`                                                | Independent buy/sell markout EWMAs and spread multipliers      |
| `toxicity_half_life_sec`                                           | EWMA decay for toxicity tracker                                |
| `toxicity_max_widen`                                               | Maximum widening multiplier from toxicity                      |
| `toxicity_baseline_bps`                                            | Baseline markout used by toxicity-multiplier formula           |
| `toxicity_size_reduce_threshold`                                   | Markout EWMA (bps) below which size is reduced                 |
| `toxicity_size_reduce_max`                                         | Minimum size factor on toxic side                              |
| `toxicity_size_exponent`                                           | Power-law exponent for size reduction                          |
| `toxicity_idle_decay_half_lives`                                   | Grace period before idle decay (half-lives); negative disables |
| `toxicity_warmup_fills`                                            | Min fills per side before toxicity output is trusted           |
| `toxicity_t5s_weight`                                              | Blend of T5s into toxicity input                               |
| `markout_enabled` / `markout_ewma_half_life_sec`                   | Post-fill mid-price tracking                                   |
| `quote_attribution_enabled` / `quote_attribution_min_interval_sec` | Sampled per-layer quote attribution logs                       |

### Risk

| Field                                                                                 | Description                                                                          |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `max_position_notional`                                                               | Per-asset position limit                                                             |
| `max_spread_exposure`                                                                 | Per-asset spread exposure limit                                                      |
| `max_aggregate_exposure`                                                              | Total portfolio exposure cap across all assets (0 = fallback to max_spread_exposure) |
| `max_drawdown_pct`                                                                    | Equity drawdown halt trigger (percent)                                               |
| `absolute_equity_floor`                                                               | Permanent halt below this equity                                                     |
| `max_open_orders`                                                                     | Exchange-side order count limit                                                      |
| `drawdown_cooldown_sec`                                                               | Seconds before resume attempt after drawdown halt                                    |
| `max_consecutive_halts`                                                               | Permanent halt after this many consecutive halts                                     |
| `min_resume_equity_pct`                                                               | Required equity (% of high-water) to resume                                          |
| `max_close_slippage_bps`                                                              | Max slippage for limit-IOC close                                                     |
| `drawdown_smoothing_samples`                                                          | EMA smoothing for equity tracking                                                    |
| `recovery_duration_sec` / `recovery_notional_pct` / `recovery_spread_multiplier`      | Drawdown-recovery parameters                                                         |
| `fail_closed_cooldown_sec` / `max_fail_closed_retries`                                | Executor fail-closed parameters                                                      |
| `session_stop_loss_bps`                                                               | Hard session stop trigger (negative bps)                                             |
| `session_stop_loss_pause_sec`                                                         | Forced-halt duration after session stop                                              |
| `session_stop_loss_recovery_notional_pct` / `session_stop_loss_recovery_duration_sec` | Session-stop recovery parameters                                                     |
| `session_stop_loss_warn_bps`                                                          | Warn rung (less negative than the hard stop)                                         |
| `session_stop_loss_warn_spread_mult` / `session_stop_loss_warn_notional_pct`          | Warn-rung widen/size                                                                 |
| `session_flatten_on_shutdown` / `session_flatten_timeout_sec`                         | Close positions on graceful shutdown                                                 |

### Execution

| Field                                                                          | Description                                              |
| ------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `live_mode`                                                                    | Real trading vs. paper simulation                        |
| `leverage`                                                                     | Exchange-side leverage setting                           |
| `recon_grace_period_ms`                                                        | Post-fill grace period for reconciliation                |
| `recon_empty_exchange_grace_ms`                                                | Shorter grace for the "all tracked orders vanished" case |
| `recon_mismatch_diagnostic_enabled` / `recon_mismatch_diagnostic_max_per_hour` | Structured mismatch diagnostic logging                   |
| `poll_fills_sec`                                                               | REST fill-poll interval (default 10)                     |

### Warmup

| Field                      | Description                            |
| -------------------------- | -------------------------------------- |
| `warmup_duration_sec`      | Seconds of reduced activity at startup |
| `warmup_notional_pct`      | Notional fraction during warmup        |
| `warmup_spread_multiplier` | Spread multiplier during warmup        |

### Feed Staleness

| Field                      | Description                                  |
| -------------------------- | -------------------------------------------- |
| `trade_stale_degraded_sec` | Trades stale → degraded mode                 |
| `trade_stale_halt_sec`     | Trades stale → halt                          |
| `max_book_staleness_ms`    | Book stale → cancel orders                   |
| `max_no_book_update_ms`    | Book dead → cancel + close                   |
| `max_cross_leg_skew_ms`    | Max timestamp skew between legs for sampling |
| `fill_refresh_cooldown_ms` | Min interval between fill-triggered requotes |

### Optimizer

| Field                                             | Description                                                           |
| ------------------------------------------------- | --------------------------------------------------------------------- |
| `optimizer_v2.enabled`                            | Master switch                                                         |
| `optimizer_v2.eval_interval_sec`                  | Seconds between evaluation cycles                                     |
| `optimizer_v2.warmup_evals`                       | Evals before the optimizer starts acting                              |
| `optimizer_v2.min_fill_samples`                   | Minimum fills before the optimizer acts                               |
| `optimizer_v2.min_fill_sample_window_sec`         | Rolling window for fill counting                                      |
| `optimizer_v2.momentum`                           | Momentum for knob update smoothing                                    |
| `optimizer_v2.learning_rate`                      | Step size for proportional control                                    |
| `optimizer_v2.max_knob_changes_per_eval`          | Max knobs adjusted per eval cycle                                     |
| `optimizer_v2.emergency_toxicity_threshold`       | Toxicity score above which all knobs move simultaneously              |
| `optimizer_v2.rollback_window_evals`              | Consecutive negative-utility evals before selective rollback          |
| `optimizer_v2.rollback_rate`                      | Fraction to revert worsened knobs toward session-start values         |
| `optimizer_v2.rollback_cooldown_sec`              | Directional cooldown after rollback                                   |
| `optimizer_v2.burst_eval_enabled`                 | Fire an out-of-cadence eval on new volume-burst activation            |
| `optimizer_v2.burst_eval_min_interval_sec`        | Min seconds between burst-triggered evals                             |
| `optimizer_v2.force_refresh_on_emergency`         | Force a scoped quote refresh after material emergency overlay changes |
| `optimizer_v2.force_refresh_min_interval_ms`      | Minimum time between emergency forced refreshes                       |
| `optimizer_v2.force_refresh_min_spread_delta_bps` | Min spread delta change considered material                           |
| `optimizer_v2.shadow_mode`                        | Log all decisions without applying them                               |
| `optimizer_v2.policy_thresholds`                  | Configurable thresholds for policy decisions                          |
| `optimizer_v2.spread_bps`                         | Spread delta bounds `[min, max]` (applied to both buy and sell)       |
| `optimizer_v2.notional_mult`                      | Notional multiplier bounds                                            |
| `optimizer_v2.skew_mult`                          | Inventory skew multiplier bounds                                      |
| `optimizer_v2.vwap_alpha`                         | VWAP alpha bounds (applied to `alphaDelta` relative to base)          |
| `optimizer_v2.imbalance_shift_mult`               | Imbalance shift multiplier bounds                                     |
| `optimizer_v2.fill_decay_mult`                    | Fill decay multiplier bounds                                          |
| `optimizer_v2.refresh_mult`                       | Refresh interval multiplier bounds                                    |
| `optimizer_v2.subdim_normalization`               | `"linear"` or `"empirical_quantile"`                                  |
| `optimizer_v2.fourth_dimension`                   | `""`/`"budget_pressure"` or `"session_pnl"`                           |
| `optimizer_v2.fourth_dim_defensive_floor`         | Budget-pressure floor above which pro-cyclical tightening is clamped  |

### Smart-Layer Helpers

| Field                               | Description                                                  |
| ----------------------------------- | ------------------------------------------------------------ |
| `max_total_spread_multiplier`       | Caps per-side spread at Nx base half-spread                  |
| `rate_limit_allocator_enabled`      | Per-asset ROI-based API budget allocation (multi-asset only) |
| `rate_limit_roi_ewma_half_life_sec` | ROI estimate EWMA half-life                                  |
| `rate_limit_min_refresh_multiplier` | Fastest refresh scaling (high-ROI assets)                    |
| `rate_limit_max_refresh_multiplier` | Slowest refresh scaling (low-ROI assets)                     |
| `markout_composite_weights`         | Per-horizon weights for T500ms/T1s/T2s/T5s markout composite |
| `emergency_widen_threshold_bps`     | Markout below this triggers emergency spread widening        |
| `emergency_widen_amount_bps`        | Extra spread added on emergency widen                        |
| `quiet_ladder_reduce_sec`           | Reduce ladder levels after this many quiet seconds           |
| `toxicity_size_weight_cap`          | Max allowed toxicity-based size scaling factor               |
| `velocity_initial_seed`             | Initial value for trade velocity EWMA at startup             |
# Local public API mode

Fixed-size correction: per-side fill-rate multipliers apply only when
`adaptive_notional_enabled` is true. With adaptation disabled, zero default
targets/bounds must not zero buy/sell sizing after the first fills. Fill-rate
statistics remain recorded, but fixed quote sizes stay independent of them.

`rate_limit.disabled` bypasses local quota tier selection and quota-driven
flattening/throttling while preserving observed balances of the API allowance.
It does not disable feed, inventory, drawdown or execution-failure protections.

Live account selection: the explicit trading account is used for account data,
fill subscriptions, allowance queries and SDK close/cancel helpers. Signing stays
with the API wallet. A read-only authorization check precedes every live startup.
Automatic builder approval is disabled. The standalone `-check-account` path
returns before leverage changes, executor initialization or any exchange action.

Set `data_source: public` to use the native Hyperliquid info API and WebSocket
subscriptions for books and trades, without QuickNode credentials. Book updates
retain the existing crossed-book and outlier guards; public snapshots older than
5 seconds are rejected, using the WebSocket handshake server clock to compensate
for local clock drift. Downstream timestamps stay in the local clock domain.
Trade subscriptions use exact DEX-qualified coin names,
deduplicate by coin/time/tid, ignore historical reconnect snapshots, and mark the
public aggressor side as taker flow. Connections send native pings and reconnect
with bounded backoff. Existing paper execution and risk controls are unchanged.
Live execution uses the local SDK's native action path instead of the upstream
builder service. It computes HIP-3 numeric IDs from `perpDexs` and DEX metadata,
uses compact ordered MessagePack plus EIP-712 Agent signing, and submits only to
Hyperliquid `/exchange`. Nonces are monotonic within the process. Native actions
exclude builders, and unsupported actions fail without a remote fallback.
Signing is verified against official Python SDK vectors; no live test orders
were submitted. Restart is required to use the new executable.
# Zeus integration profile

The standalone algorithm remains the default. `signal_pauses_enabled` defaults
to true. The Zeus profile sets it false: trend/flow/regime/price-shock and
optimizer size suppression cannot remove a side solely on a market signal.
Shock-chain external halts and markout emergency cancellations are disabled in
that profile. Fair-value, volatility and spread calculations remain active.
Executor mismatch, missing/stale feeds, rate budgets, margin, drawdown and
session-loss guards retain their authority to halt or close.

Zeus runs one asset and one rung per side. Opening quantities are floored at
the actual quote price against one configured order's inventory budget, then
rechecked against current position and pending same-side exposure at submission.
Inventory exits carry reduce-only on both placement and modification.

The `-zeus-session`, `-zeus-symbol` and `-zeus-stop-file` flags enable a dedicated
stdout JSON snapshot protocol. It excludes configuration and credentials;
ordinary logs are suppressed. Confirmed exchange fill IDs and exchange
`closedPnl`/fees drive accounting; inferred reconciliation fills are excluded.
Shutdown explicitly verifies positions and orders by fresh REST reads and
reports unknown state on any failure. No post-mortem files are written by Zeus.
