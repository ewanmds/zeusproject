# Getting Started

A step-by-step walkthrough for going from zero to a running single-asset market-making bot — first in **paper mode** (no real money), then in **live mode**. Read every section in order. Don't skip the paper-mode step even if you're confident; it's how you confirm your machine, network, and config are sane before risking capital.

> The shipped config and these docs target a **single asset**. The codebase still supports multi-asset / spread-pair mode under the hood, but it is intentionally not covered here.

> **Reality check.** This is a real trading bot. In live mode it will place real orders against real markets with your funds. Bugs, misconfigurations, network glitches, and adverse market conditions can lose you money. Start small. Keep logs. Read [docs/ALGORITHM.md](./ALGORITHM.md) and [docs/CONFIG.md](./CONFIG.md) before tuning anything.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone the repo](#2-clone-the-repo)
3. [Install dependencies and build](#3-install-dependencies-and-build)
4. [Get a QuickNode endpoint](#4-get-a-quicknode-endpoint)
5. [Set up your Hyperliquid wallet](#5-set-up-your-hyperliquid-wallet)
6. [Fund your Hyperliquid account](#6-fund-your-hyperliquid-account)
7. [Create your config file](#7-create-your-config-file)
8. [Set environment variables](#8-set-environment-variables)
9. [Validate the config](#9-validate-the-config)
10. [Run in paper mode](#10-run-in-paper-mode)
11. [Watch with the monitor TUI](#11-watch-with-the-monitor-tui)
12. [Switch to live mode](#12-switch-to-live-mode)
13. [Shutting down safely](#13-shutting-down-safely)
14. [Troubleshooting](#14-troubleshooting)
15. [Where to go next](#15-where-to-go-next)

---

## 1. Prerequisites

You'll need:

- **Go 1.25+** — check with `go version`. Install from <https://go.dev/dl/>.
- **git** — check with `git --version`.
- **make** — preinstalled on macOS (with Xcode CLT) and most Linux distros.
- A terminal you're comfortable in (`bash` or `zsh` on macOS/Linux).
- Roughly **150–500 USDC** of starting capital if you intend to go live (see [§6](#6-fund-your-hyperliquid-account)). Paper mode requires no funds at all.
- A **QuickNode account** (free tier works to start) — see [§4](#4-get-a-quicknode-endpoint).
- A **Hyperliquid account** with an attached **API wallet** — see [§5](#5-set-up-your-hyperliquid-wallet).

This bot has only been tested on macOS and Linux. Windows users should use WSL2.

---

## 2. Clone the repo

```bash
git clone https://github.com/<your-fork-or-upstream>/hyperliquid-market-maker-bot.git
cd hyperliquid-market-maker-bot
```

Replace the URL with whichever fork you're pulling from. Confirm you're on the `main` branch:

```bash
git status
git pull
```

---

## 3. Install dependencies and build

Go modules pull dependencies on the first build — no separate install step.

```bash
make build
```

This produces a `bot` binary at `bin/bot`. If the build fails:

- `go: command not found` → install Go (see [§1](#1-prerequisites)).
- `package … is not in std` or version errors → run `go mod download` then retry.
- Network errors fetching modules → check your firewall / proxy.

Quick smoke test:

```bash
./bin/bot -h
```

You should see usage text. Optionally, run the test suite to confirm everything compiles end-to-end:

```bash
make test
```

---

## 4. Get a QuickNode endpoint

The bot streams market data and submits orders through QuickNode's Hyperliquid endpoint.

1. Sign up at <https://www.quicknode.com/>.
2. Create an endpoint on the **Hyperliquid Mainnet** chain.
3. Note the two values you'll need:
   - **Subdomain** — the label in your endpoint URL. For an endpoint like `https://my-acct.hype-mainnet.quiknode.pro/abc123token/`, the subdomain is `my-acct` (the bot auto-suffixes `.hype-mainnet.quiknode.pro` for short labels, so either form works).
   - **Auth token** — the path component after the subdomain in the URL (e.g. `abc123token`).

Keep both handy — you'll plug them in at [§8](#8-set-environment-variables).

QuickNode's Hyperliquid docs: <https://www.quicknode.com/docs/hyperliquid>

---

## 5. Set up your Hyperliquid wallet

You have **two options** for the signing key the bot uses. Pick one before continuing.

### Option A (recommended): Hyperliquid API wallet

Hyperliquid lets your main account approve a separate "API wallet" (also called an agent wallet) that can sign trades but **cannot withdraw funds**. This is the standard, safer setup for any bot.

1. Open the Hyperliquid web app, connect your main wallet, and navigate to **API** under settings.
2. Click **Generate** to create a new API wallet. Copy the private key it shows you **once** — you cannot retrieve it later.
3. Click **Authorize** to register that API wallet against your main account. This costs a small gas fee on Arbitrum.
4. The trading positions and balances live under your **main account address**, not the API wallet — the API wallet is only the signer.

### Option B: Use your main wallet's private key directly

Faster to set up, but the key has full withdraw authority. Only do this on an account you're using exclusively for the bot.

### Either way

You will end up with a 64-hex-character private key (with or without the `0x` prefix). **Never commit it to git, never paste it in chat, never put it in a screenshot.** Store it in a password manager or a `.env` file that is gitignored (see [§8](#8-set-environment-variables)).

---

## 6. Fund your Hyperliquid account

Hyperliquid keeps three separate balances per account: **Spot**, **Perps**, and (if you're trading them) **HIP-3 dex** balances. USDC you deposit lands in **Spot** first — you have to transfer it to the venue you want to trade.

1. **Deposit USDC** to your Hyperliquid account from Arbitrum. Use the **Deposit** flow in the Hyperliquid web app.
2. **Transfer to the right venue:**
   - For **standard perps** (BTC, ETH, etc.) — use the **Transfer to Perps** action in the web app.
   - For **HIP-3 assets** (e.g. `xyz:CL`, `xyz:BRENTOIL`) — use the dex transfer flow to move USDC into the dex you'll trade on (e.g. `xyz`). Each HIP-3 dex has its own balance.
3. **Verify it landed.** From the repo, run the diagnostic:
   ```bash
   PRIVATE_KEY=<your-key> QUICKNODE_SUBDOMAIN=<sub> QUICKNODE_AUTH_TOKEN=<token> \
     go run ./cmd/diag/
   ```
   This dumps your perps, dex, and spot balances. If you don't see your USDC where you expect it, the transfer hasn't completed yet.

**Sizing guidance for first run.** Don't over-fund. Start with the smallest amount you'd be comfortable losing entirely — `paper_starting_equity` defaults to `150` (USDC) in the example config, and `quote_notional: 50` means each side of each ladder rung is $50. If your real equity is much smaller than `max_position_notional`, tune those down too (see [§7](#7-create-your-config-file)).

---

## 7. Create your config file

The repo ships `config.example.yaml` — a heavily annotated template. Copy it and edit your copy:

```bash
cp config.example.yaml config.yaml
```

(The `.gitignore` excludes anything matching `.env`; `config.yaml` is currently committed by convention but **never** put real secrets in it — keep credentials in environment variables, see [§8](#8-set-environment-variables).)

Open `config.yaml` and check these fields first. Everything else has a sensible default for a beginner.

### Strategy

```yaml
strategy:
  assets: ["xyz:CL"] # exactly one asset
  lookback_window: 500 # required for parsing; unused in single-asset mode
```

The shipped template is single-asset. `xyz:CL` is the HIP-3 crude futures that this repo was tuned against. To run a different market, just change the symbol — `xyz:` prefix for HIP-3 dex assets, bare `BTC` / `ETH` for standard perps.

### Market making

```yaml
market_making:
  quote_spread_bps: 20 # 20 bps half-spread is conservative for a first run
  quote_notional: 50 # USD per side per ladder rung
  ladder_levels: 4 # number of ladder rungs per side
  refresh_interval_ms: 800 # how often to re-quote
```

Keep these defaults until you've watched the bot run for a session.

### Risk

```yaml
risk:
  max_position_notional: 200 # hard cap on how much inventory the bot can build
  max_spread_exposure: 400 # cap on combined open-order notional
  max_drawdown_pct: 5.0 # halt at this % drawdown from session high-water
```

**Set `max_position_notional` to something you're emotionally fine losing.** If you funded the account with $150, set this to $100 or less, not $200.

### Execution — paper vs live

```yaml
execution:
  live_mode: false # KEEP THIS FALSE for your first run
  paper_starting_equity: 150 # simulated equity for paper mode
  leverage: 5 # leverage to set on the asset (live mode only)
```

`live_mode: false` means **no real orders are placed**. The bot still consumes live market data and simulates fills when the book crosses its quotes. You want at least one full paper-mode session before flipping this to `true`.

### Env section (leave empty in the file)

```yaml
env:
  quicknode_subdomain: "" # leave empty — set via env var
  quicknode_auth_token: "" # leave empty — set via env var
  private_key: "" # leave empty — set via env var
```

It is **strongly recommended** to keep these empty in the YAML file and inject the values from environment variables instead (next section).

For everything else: read [docs/CONFIG.md](./CONFIG.md) before changing it. Many of the toggles (`adaptive_*`, `optimizer_v2.*`, `volume_burst_*`, etc.) interact in non-obvious ways.

---

## 8. Set environment variables

Three env vars are read on startup and override anything in the YAML `env:` block:

| Variable               | Required for     | What it is                                                    |
| ---------------------- | ---------------- | ------------------------------------------------------------- |
| `QUICKNODE_SUBDOMAIN`  | always           | The subdomain from [§4](#4-get-a-quicknode-endpoint).         |
| `QUICKNODE_AUTH_TOKEN` | always           | The auth token from [§4](#4-get-a-quicknode-endpoint).        |
| `PRIVATE_KEY`          | live mode only\* | The signing key from [§5](#5-set-up-your-hyperliquid-wallet). |

\*Paper mode does not need a private key, but if you want the bot to read your real account balances during paper mode (for parity with what live mode would see), set it.

Easiest pattern: a `.env` file in the repo root (already gitignored).

```bash
cat > .env <<'EOF'
QUICKNODE_SUBDOMAIN=my-acct
QUICKNODE_AUTH_TOKEN=abc123youractualtoken
PRIVATE_KEY=0xyourprivatekeyhere
EOF
chmod 600 .env
```

Load it into your shell before running the bot:

```bash
set -a
source .env
set +a
```

(The `set -a` makes every variable assigned in `.env` automatically exported to subprocesses.)

Verify the env is set:

```bash
echo "${QUICKNODE_SUBDOMAIN:?not set}"
echo "${QUICKNODE_AUTH_TOKEN:0:6}…"   # show first 6 chars only
```

---

## 9. Validate the config

Before running for real, parse + validate the config without starting the bot:

```bash
./bin/bot -config config.yaml -validate
```

If everything is valid, the bot prints the resolved config and exits with code 0. If something is wrong (missing required field, out-of-range value, asset not allowed), it exits non-zero with a message describing the problem. Fix and re-run until it validates cleanly.

`-validate` skips credential checks so you can sanity-check a config file even before setting `QUICKNODE_SUBDOMAIN` / `QUICKNODE_AUTH_TOKEN` / `PRIVATE_KEY` — handy for CI or for lint-style runs against `config.example.yaml`.

You can also run `make validate` to validate `config.example.yaml` as a sanity check.

---

## 10. Run in paper mode

Confirm `live_mode: false` is set in your config, then:

```bash
./bin/bot -config config.yaml 2>&1 | tee logs/paper-run-1.txt
```

The `tee` pipe writes everything to a log file — you'll want it for the monitor TUI ([§11](#11-watch-with-the-monitor-tui)) and for post-mortem analysis.

What you should see in the first few seconds:

- Resolved config printed as a structured JSON block.
- `connected to QuickNode` (or similar) once the gRPC stream comes up.
- `book update` events flowing for each configured asset.
- `[PAPER]` log lines as the simulated executor prices and (eventually) "fills" your quotes.

What you should NOT see:

- Repeated reconnect / TLS errors → check your QuickNode credentials.
- `private key required` → either set `PRIVATE_KEY` or confirm `live_mode: false`.
- `validation failed` → re-run [§9](#9-validate-the-config).

Let it run for at least 15–30 minutes so the rolling stats (VWAP, regime, fill rate) have meaningful samples. Watch for halts (`RiskHalt` in the log) — those are the bot protecting you. Read why before disabling them.

---

## 11. Watch with the monitor TUI

In a second terminal, launch the bundled TUI dashboard:

```bash
make run-monitor          # auto-discovers the latest log in logs/
# or
go run ./cmd/monitor/ logs/paper-run-1.txt
```

The monitor parses the structured JSON logs in real time and shows position, PnL, fill rate, optimizer state, and risk events on a single screen. Quit with `q` or `Ctrl+C`. The monitor only reads logs — it cannot affect the bot.

---

## 12. Switch to live mode

Only do this **after** you have:

- Run a clean paper session with no recurring errors.
- Read [docs/ALGORITHM.md](./ALGORITHM.md) and skimmed [docs/CONFIG.md](./CONFIG.md).
- Confirmed your funded balance is in the right venue (perps vs HIP-3 dex) for the asset in `strategy.assets`.
- Set `risk.max_position_notional` to a number you are genuinely okay losing.

Then:

1. Edit `config.yaml`:
   ```yaml
   execution:
     live_mode: true
     leverage: 5 # confirm this is appropriate for the asset
   ```
2. Confirm `PRIVATE_KEY` is exported in the shell.
3. Re-validate:
   ```bash
   ./bin/bot -config config.yaml -validate
   ```
4. Run with logging:
   ```bash
   ./bin/bot -config config.yaml 2>&1 | tee logs/live-run-1.txt
   ```

On startup the bot will:

- Set leverage on the configured asset.
- Cancel any pre-existing orders on that asset (so it starts from a clean slate).
- Start placing maker (post-only) orders.

**The first 60 seconds are warmup** — the bot quotes wider and smaller while stats accumulate. Don't panic if you don't see fills immediately; the strategy is mean-reversion on wicks, not aggressive taking.

---

## 13. Shutting down safely

`Ctrl+C` (SIGINT) or `kill <pid>` (SIGTERM) triggers a graceful shutdown:

- Cancels all open orders on the configured asset.
- Optionally market-closes residual position if `risk.session_flatten_on_shutdown: true` is set in your config.

If you set neither and just kill the bot, **your open position stays open on the exchange** with no one tending it. Either flatten manually in the Hyperliquid web app or set the flatten-on-shutdown flag.

For unrecoverable failures (network outage, hardware crash), you can always cancel and flatten from the Hyperliquid web app.

---

## 14. Troubleshooting

| Symptom                                               | Likely cause / fix                                                                                                      |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `validation failed: …` on startup                     | Re-read the field's doc in [docs/CONFIG.md](./CONFIG.md); fix the value; re-run `-validate`.                            |
| `private key required` in live mode                   | `PRIVATE_KEY` not exported. Check `echo ${PRIVATE_KEY:?}` and re-source `.env`.                                         |
| `Equity()` returns `0` in live mode                   | USDC is in spot or the wrong dex. Run `go run ./cmd/diag/` to see all balances and transfer to the right venue.         |
| No fills in paper mode after 30 min                   | `quote_spread_bps` may be too wide for the asset's natural microstructure. Try lowering to `10`–`15`.                   |
| Repeated rate-limit warnings                          | Your account is near its API budget. Reduce `refresh_interval_ms` or `ladder_levels`.                                   |
| `feed staleness` halts                                | `max_book_staleness_ms` exceeded — your network is slow or QuickNode is throttling. Check your endpoint health.         |
| Bot quoted, got filled, then quotes never returned    | Possible `RiskHalt` event. Search the log for `RiskHalt` / `halt_reason`. Read the reason before disabling.             |
| Build fails after `git pull`                          | Run `go mod download` then `make build`. If still broken, check Go version is `1.25+`.                                  |

When asking for help, include:

- The relevant log section (timestamps + 50 lines around the issue).
- The output of `./bin/bot -config config.yaml -validate` (which prints the resolved config).
- Your bot version (`./bin/bot -version` if available, else `git rev-parse --short HEAD`).
- **Never** include your private key, full QuickNode token, or wallet address in a public bug report.

---

## 15. Where to go next

- **[docs/ALGORITHM.md](./ALGORITHM.md)** — the algorithm spec. Read this before tuning anything in `market_making`.
- **[docs/CONFIG.md](./CONFIG.md)** — the full config reference. Every field, every default, every interaction.
- **[CLAUDE.md](../CLAUDE.md)** — architectural notes and conventions used in the codebase.
- **`config.example.yaml`** — the source-of-truth template. Re-skim it after a few sessions; behaviors that felt mysterious on day one make a lot more sense once you've watched real fills.
- **`make check`** — runs `vet + validate + test-race`. Use it before any code change.

Welcome aboard. Start small, log everything, and resist the urge to tune knobs in the first session.
