# Hyperliquid Market Making Bot

## Local Windows install: public API mode

For API-wallet setup, run `check-account.cmd`: enter the main account address,
then the API wallet key in the masked prompt. This check never starts trading.
`start-live.cmd` uses the separate `config.live.yaml`, checks the account again,
shows its parameters, and requires typing `LIVE` to start actual orders.
The main wallet private key is not accepted as the API signer. Keys are not saved.

This checkout supports `data_source: public` in `config.yaml`, using native
Hyperliquid WebSocket and info endpoints without a QuickNode account or token.
Double-click `start-paper.cmd` for paper mode, and `start-monitor.cmd` for the
dashboard. Paper mode needs no private key. The launcher refuses live configs.
Live execution now signs locally and submits directly to Hyperliquid `/exchange`
without any builder field, builder approval or builder fee. Standard exchange fees
still apply. Live fills have not been validated locally. QuickNode instructions
below describe the original upstream setup.

A Go bot for market-making on Hyperliquid perpetual markets — both HIP-3 dex assets (e.g. `xyz:CL`) and standard perps (e.g. `BTC`, `ETH`). Always quotes both sides, uses a short-window VWAP as the primary fair-value anchor, and earns the bid-ask spread with mean-reversion on VWAP wicks.

**Scope of the shipped template.** `config.example.yaml` and the user-facing docs target **a single asset** (`xyz:CL`) to keep the surface area small for new operators. The codebase still supports **multi-asset / spread-pair mode** — mixed HIP-3 and standard perps in one process, with an optional z-score cross-leg spread engine — and every relevant subsystem is per-asset aware. To run multiple assets, add them to `strategy.assets` (and, for correlated pairs, leave `spread_engine_enabled` at its default auto-on behavior). See `docs/CONFIG.md` for the per-field reference.

**This is a real trading bot.** In live mode it places real orders with real funds. Start in paper mode, keep logs, and read the [Disclaimer](#disclaimer) before risking capital.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Documentation](#documentation)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Development](#development)
- [Monitor TUI](#monitor-tui)
- [Diagnostic Binaries](#diagnostic-binaries)
- [Project Structure](#project-structure)
- [License](#license)
- [Disclaimer](#disclaimer)

## Features

- **VWAP-anchored fair value** with configurable blend of raw mid vs. short-window VWAP, plus wick mean-reversion sizing against a long VWAP
- **Ladder quoting** with geometric size graduation and optional vol-scaled inter-level spacing
- **Inventory half-life controller** — adaptive, convex, asymmetric skew that can preserve the exit side (`inventory_skew_exit_preserve`)
- **Per-side toxicity** — independent bid/ask EWMA markout scoring with idle decay, widening, and size scaling (replaces legacy symmetric adverse selection)
- **Unified utility-driven optimizer** — 4D state, policy table, proportional control, counterfactual rollback
- **Adaptive notional** from per-side fill-rate EWMA, plus entry-side size reduction by inventory utilization
- **Trade-flow imbalance** — rolling time-clock or VPIN-lite volume-clock, plus exponentially decaying post-fill FV shifts
- **Guards** — trend/flow/shock, cascading shock chain, dynamic refresh intervals, rapid-fill circuit breaker, feed staleness, fair-value deviation clamp
- **Volume-triggered burst ladders** and **wall-fronting** (snap in front of large resting orders)
- **Markout tracking** at 500ms / 1s / 2s / 5s horizons
- **Rate-limit-aware API budget allocator** — per-asset ROI distribution, virtual-cycle regen tracking
- **Supervisor state machine** — halt / degrade / recovery with consecutive-halt and equity-recovery gates
- **Multi-asset + optional z-score spread engine** for correlated legs (HIP-3 + standard perps in one process)
- **Paper and live modes** sharing the same pipeline; paper simulates fills from book updates

## Architecture

Single Go binary, event-driven pipeline. All components communicate via typed, buffered Go channels. A single `context.Context` governs graceful SIGINT/SIGTERM shutdown.

```
gRPC L2Book ──> fan-out ──> Spread Engine* ──> continuous z-score ──> Quote Manager ──> Executor
                  │                                                         ^              │
                  ├──> Quote Manager (mid prices, book depth)                │              v
                  ├──> Live Executor (book snapshots for slippage close)     │        PositionUpdate
                  └──> Paper Executor (fill sim)                       RiskAction            │
                                                                            │                │
Trade gRPC    ──> Short VWAP ──> Quote Manager                       Risk Manager <──────────┘
              ├──> Long VWAP ──> Quote Manager
              └──> Trade-Flow Imbalance ──> Quote Manager

UserFills WS ──> Live Executor (fill reconciliation)
                  └──> Fill Callback ──> FillDecay, FillRateTracker, MarkoutTracker,
                                         SideToxicityTracker
```

`*` The Spread Engine is only instantiated with 2+ assets (`spread_engine_enabled` defaults to auto: on with 2+ assets). In single-asset mode the book fans out directly to the Quote Manager.

### API routing

- **QuickNode** — gRPC streams (L2Book), WebSocket streams (UserFills, Trades), Info queries
- **api.hyperliquid.xyz** — local signing and native `/exchange` operations, without builder fees (local checkout patch).

## Documentation

- [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — step-by-step operator walkthrough (wallet, funding, env vars, paper → live)
- [`docs/ALGORITHM.md`](docs/ALGORITHM.md) — full algorithm spec (fair value, skew, optimizer, toxicity, gates)
- [`docs/CONFIG.md`](docs/CONFIG.md) — per-field configuration reference
- [`config.example.yaml`](config.example.yaml) — annotated single-asset template; source of truth for the schema
- [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) — repository conventions (symlinked)

## Prerequisites

- **Go 1.25+** — `go version`
- **git** and **make**
- **macOS or Linux** — Windows users should use WSL2
- A **QuickNode** endpoint (gRPC + WebSocket + Info)
- A **Hyperliquid account** with an attached **API wallet** (live mode only)
- **Capital** — none for paper mode; ~150–500 USDC is a reasonable starting range for live

See [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for the full setup walkthrough.

## Quick Start

```bash
# Copy the annotated template to create your runtime config
cp config.example.yaml config.yaml
# Edit config.yaml — leave the env section empty and inject credentials via env vars

# Set credentials in your shell (or a gitignored .env file)
export QUICKNODE_SUBDOMAIN=...          # short name auto-completes to *.hype-mainnet.quiknode.pro
export QUICKNODE_AUTH_TOKEN=...
export PRIVATE_KEY=...                  # API wallet private key; only required for live mode

# Build and run (paper mode by default in the template)
make run

# Or run directly without building
go run ./cmd/bot/ -config config.yaml

# Validate a config without running (offline; credential checks skipped)
./bin/bot -config config.yaml -validate
```

## Configuration

### Config files

- `config.yaml` — your runtime config; created by copying `config.example.yaml`. Not committed.
- `config.example.yaml` — annotated single-asset template; source of truth for the schema.
- `docs/CONFIG.md` — per-field reference.
- Any path can be supplied via `-config <path>`.

### Top-level sections

| Section         | Purpose                                                       |
| --------------- | ------------------------------------------------------------- |
| `strategy`      | Asset selection and (optional) spread-engine knobs            |
| `market_making` | Quoting, sizing, signals, guards, optimizer v2                |
| `risk`          | Position / drawdown / session-loss limits                     |
| `execution`     | Paper vs. live, leverage, polling cadence                     |
| `logging`       | Verbosity and periodic status cadence                         |
| `rate_limit`    | API-budget circuit breaker (optional, advanced)               |
| `postmortem`    | JSON snapshot dumps on halt (optional, advanced)              |
| `env`           | Credentials (prefer env vars over YAML for secrets)           |

### Abbreviated example

See `config.example.yaml` for the full annotated template with every field documented. Commonly-tuned fields:

```yaml
strategy:
  assets: ["xyz:CL"]          # exactly one asset for the single-asset template
  lookback_window: 500        # required; unused in single-asset mode

market_making:
  quote_spread_bps: 20        # base bid-ask half-spread
  quote_notional: 50          # USD per side per ladder rung
  refresh_interval_ms: 800    # min ms between quote refreshes
  inventory_skew_factor: 0.7  # 0-1, inventory skew aggressiveness
  inventory_skew_gamma: 1.5   # convex exponent: 1.0 = linear, >1.0 = aggressive at high util
  ladder_levels: 4            # orders per side
  ladder_spacing_bps: 5       # bps between ladder rungs
  ladder_size_graduation: 1.5 # geometric ratio between levels; 1.0 = uniform
  vwap_alpha: 0.40            # blend: 0 = raw mid, 1 = pure short VWAP

risk:
  max_position_notional: 200  # position cap
  max_spread_exposure: 400    # open-order notional cap
  max_drawdown_pct: 5.0       # halt at this % drawdown
  max_open_orders: 16

execution:
  live_mode: false            # false = paper trading
  paper_starting_equity: 150
  leverage: 5

env:
  quicknode_subdomain: ""
  quicknode_auth_token: ""
  private_key: ""
```

### Multi-asset mode

To run multiple assets in one process, add them to `strategy.assets`. Each of `market_making`, `execution`, and `risk` supports a `per_asset` map that overrides individual fields; unspecified fields inherit from the global config. With 2+ assets the z-score spread engine auto-enables; set `spread_engine_enabled: false` to force it off for independent legs.

```yaml
strategy:
  assets: ["xyz:CL", "xyz:BRENTOIL"]
  # spread_engine_enabled: false   # force off for uncorrelated legs

market_making:
  quote_spread_bps: 20
  per_asset:
    "xyz:BRENTOIL":
      quote_spread_bps: 25          # override for this leg only
```

### Environment variables

Environment variables override the `env:` values in YAML.

| Variable               | Description                                              |
| ---------------------- | -------------------------------------------------------- |
| `QUICKNODE_SUBDOMAIN`  | QuickNode endpoint subdomain (short name auto-completes) |
| `QUICKNODE_AUTH_TOKEN` | QuickNode auth token                                     |
| `PRIVATE_KEY`          | API wallet private key (live mode only)                  |

## Development

Build artifacts land in `bin/` (gitignored). Rebuild after pulling or changing code.

```bash
make build          # build bot binary into bin/bot
make run            # build + run with config.yaml
make run-with-logs  # build + run, tee stdout to logs/logs.txt
make monitor        # build monitor TUI into bin/monitor
make run-monitor    # build + run monitor (auto-discovers latest log)
make test           # run tests
make test-race      # run tests with race detector
make test-v         # run tests verbose
make vet            # go vet
make format         # go fmt
make lint           # go vet + staticcheck (if installed)
make validate       # build + validate config.example.yaml (credential checks skipped)
make check          # vet + validate + test-race
make clean          # remove bin/
```

Run a single test:

```bash
go test ./internal/spread/ -run TestEngineLongSpread -v
```

## Monitor TUI

A separate `bubbletea` dashboard binary that tails the bot's structured JSON logs and renders real-time panels — quotes, positions, fills, markouts, optimizer state, risk posture, rate-limit budget, and session PnL. It is a **pure log consumer**: it does not import from `internal/` and has no trading capability of its own.

```bash
make run-monitor                        # auto-discovers latest log in logs/
go run ./cmd/monitor logs/logs.txt      # open a specific log file
```

## Diagnostic Binaries

Short-lived tools for inspecting exchange state without running the full bot.

```bash
go run ./cmd/diag           # dump raw clearinghouse state (perps, HIP-3 dex, spot)
go run ./cmd/diag-trades    # dump recent public trades
go run ./cmd/diag-book      # stream L2Book depth
```

## Project Structure

```
cmd/
  bot/main.go                      # entrypoint, wiring, shutdown
  diag/main.go                     # diagnostic: dump raw clearinghouse state
  diag-trades/main.go              # diagnostic: dump recent trades
  diag-book/main.go                # diagnostic: stream L2Book depth
  monitor/                         # bubbletea TUI dashboard for tailing logs
internal/
  config/
    config.go                      # YAML + env var config loading, topology validation
    merge.go                       # reflection-based per-asset override merge
  types/types.go                   # shared types (BookUpdate, SpreadSignal, etc.)
  feed/
    grpc.go                        # gRPC L2Book stream
    trades_grpc.go                 # gRPC public trades stream
    ws.go                          # WebSocket UserFills stream
  spread/engine.go                 # rolling stats, z-score (multi-asset mode only)
  quoting/
    manager.go                     # quote computation pipeline
    supervisor.go                  # halt / degrade / recovery state machine
    microstructure.go              # dynamic width from vol, spread floor, imbalance
    regime.go                      # lag-1 autocorrelation regime detector
    fill_rate.go                   # per-side EWMA fill-rate tracker (adaptive notional)
    fill_burst.go                  # rapid-fill circuit breaker
    markout.go                     # post-fill markout at 500ms/1s/2s/5s
    toxicity.go                    # per-side EWMA markout with spread/size multipliers
    inventory_controller.go        # half-life-based adaptive skew factor
    optimizer_v2.go                # unified utility-driven optimizer (4D state, policy, rollback)
    optimizer_overlay.go           # OptimizerOverlay type
    virt_cycle.go                  # rate-limit regeneration tracker (budget pressure signal)
    rate_limit_allocator.go        # per-asset ROI budget distribution
    velocity.go                    # trade velocity dual-EWMA (volume burst trigger)
    wall_quality.go                # side-specific wall-fronting quality EWMA
    funding.go                     # funding-aware bias
    price_shock.go                 # fast mid-move guard
    shock_chain.go                 # cascading shock detector
    perside_stats.go               # per-side win rate / profit factor stats
    side_presence.go               # per-side quote presence tracking
    quantile_normalizer.go         # empirical-quantile sub-dimension normalization
    postmortem.go                  # session postmortem reporting
    session_pnl.go                 # session PnL tracker
  tradeflow/
    calculator.go                  # rolling trade-flow imbalance (buy/sell volume)
    fill_decay.go                  # exponentially decaying post-fill FV shifts
    saturation.go                  # imbalance saturation transform
    volume_clock.go                # VPIN-lite volume-bucketed imbalance
  risk/
    manager.go                     # position limits, drawdown, consecutive halts
    schedule.go                    # time-of-day risk schedule
  vwap/calculator.go               # time-windowed VWAP calculator
  execution/
    executor.go                    # Executor interface
    transport.go                   # Transport interface
    sdk_transport.go               # QuickNode/SDK transport implementation
    paper.go                       # paper trading simulator
    live.go                        # live trading via SDK (includes order modification)
```

## License

MIT License

Copyright (c) 2026 Hyperliquid Market Maker contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Disclaimer

**READ CAREFULLY BEFORE USING THIS SOFTWARE.**

This software is provided for **educational, research, and informational purposes only**. It is **not** financial advice, investment advice, trading advice, or a recommendation to buy, sell, hold, or trade any asset, derivative, or financial instrument of any kind.

### No Warranty

This software is provided "AS IS" and "AS AVAILABLE" without warranty of any kind, either express or implied. The authors, contributors, and copyright holders make no representations or warranties regarding:

- The accuracy, completeness, reliability, or correctness of the code, algorithms, documentation, or outputs
- The suitability or fitness of the software for any particular purpose
- The profitability, performance, or risk characteristics of any strategy implemented by or derived from this software
- Continuous, uninterrupted, timely, secure, or error-free operation
- Compatibility with any exchange, venue, API, SDK, protocol, or third-party service
- Freedom from bugs, defects, vulnerabilities, or security flaws

### Trading Risk

Trading cryptocurrencies, perpetual futures, derivatives, and other leveraged financial instruments carries **substantial and potentially unlimited risk of loss**, including but not limited to:

- **Total loss of capital.** You can and may lose the entire amount you deposit, stake, or expose to the market. Do not trade with funds you cannot afford to lose completely.
- **Losses exceeding deposits.** Leverage, liquidations, funding costs, adverse funding rates, slippage, gap moves, and cascading liquidations can result in losses that exceed your initial margin or deposit.
- **Liquidation risk.** Positions may be forcibly liquidated without warning when equity falls below maintenance margin. Liquidations may occur at prices materially worse than mid-market.
- **Market volatility and flash crashes.** Crypto markets can move violently, thinly, and irrationally. Order books can become one-sided, stale, or disappear entirely in seconds.
- **Execution failures.** Orders may be rejected, delayed, partially filled, double-filled, filled at adverse prices, or lost due to network issues, API outages, exchange downtime, rate limits, or bugs in this or any dependent software.
- **Funding rate exposure.** Perpetual positions held across funding intervals incur funding payments that may be adverse and unpredictable.
- **Counterparty and exchange risk.** Exchanges (including Hyperliquid) and infrastructure providers (including QuickNode) may fail, freeze withdrawals, suffer exploits, be sanctioned, delist assets, change rules, or become unavailable. Funds on any exchange are not FDIC-insured, SIPC-insured, or guaranteed.
- **Smart contract, protocol, and bridging risk.** On-chain and cross-chain operations may be subject to bugs, exploits, reorgs, or loss.
- **Automation risk.** An automated bot can enter, exit, and compound losses faster than any human can react. Bugs in the bot, incorrect configuration, unexpected market conditions, or edge cases can and will cause behavior the author did not foresee.
- **Key compromise.** Your private key provides complete control over funds. If compromised, funds may be irretrievably stolen.

### No Liability

**In no event shall the authors, contributors, maintainers, or copyright holders of this software be liable for any direct, indirect, incidental, special, exemplary, consequential, punitive, or any other damages whatsoever** (including, without limitation, trading losses, liquidations, missed profits, funding costs, slippage, data loss, business interruption, legal or regulatory consequences, tax liabilities, or cost of cover) arising out of or in connection with the use, misuse, inability to use, or performance of this software, even if advised of the possibility of such damages and regardless of the legal theory (contract, tort, strict liability, or otherwise).

By using this software, you acknowledge and agree that:

1. You assume **sole and exclusive responsibility** for any trading, investment, or financial decisions you make, and for any losses or gains resulting from use of this software.
2. You have **independently evaluated** the software, its code, its algorithms, and its risks, and are using it at your **own discretion and risk**.
3. You are **not relying** on the authors or contributors for any advice, guidance, support, or guarantees.
4. You are **solely responsible** for complying with all applicable laws, regulations, tax obligations, licensing requirements, and exchange terms of service in your jurisdiction. Market making, automated trading, and derivatives trading are restricted or prohibited in some jurisdictions.
5. You will **test thoroughly in paper mode** before ever enabling live trading, and you will start with small amounts of capital you can afford to lose.
6. You will **not hold the authors or contributors liable** for any outcome.

**If you do not agree to all of the above, do not use this software.**
