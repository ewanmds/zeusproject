# Outcome throughput repair — 23 November 2026

This delivery fixes observed execution and accounting defects. It does not
guarantee $4,000/hour or a positive PnL. No active worker, exchange order or EC2
service was changed during preparation. The OpenCode notes were reviewed;
their earlier unfinished maker-only work was compared with the current code.
The newer approved maker-then-bounded-IOC policy is retained for new sessions.

## Findings and repairs

- At $70/order the former 90% full-size ask threshold could leave the screenshot's
  $32.90 and $48.57 partial inventories without a normal ask. Sellable partial
  inventories now get post-only asks without waiting for another complete order.
- Normal unsold inventory now uses the 90-second bounded-exit timer too, not only
  explicit Stop/risk drains. Partial fills, requotes and restarts retain the timer.
  After a normal inventory exit the logical session can continue trading.
- IOC respects the 20-bps bound after rounding. The final wire gate checks the
  account revision, inventory, book, policy, price and remaining loss budget.
- The cancel-to-IOC interval suppresses maker reposting. A blocked IOC preserves
  the existing ask queue; it does not cancel it repeatedly while the spread,
  fees or loss budget prevent a crossing sale.
- Account taker fees and Outcome fee scales are verified before IOC, with the
  configured fee estimate as a conservative floor. Unknown fees keep maker exit.
- Locally skipped orders release their USDC reservation. Uncertain sends retain
  their CLOID and reservation until independent reconciliation.
- Reconnect fill snapshots are processed instead of discarded. Composite fill
  identities include market, block time and trade ID. Volume and realized PnL
  are durably recorded and carried into resumed workers and rotations.
- Inventory risk and the displayed latent PnL use the executable bid. The session
  loss trigger includes known latent losses; a stop-loss remains a decision
  threshold, not a guaranteed final realized loss.
- The old `api-budget.json` is excluded from session-journal scanning. Malformed
  actual session journals still block admission.
- Importing a manager or running an audit cannot start crash recovery. Recovery
  is enabled by the backend lifespan; shutdown closes replacement admission first.

## WebSocket first

### Follow-up: launch admission and interrupted updates

- Recent flow is now **informational only**: no minimum trade count, two-way
  activity, cadence, 20-second last-trade threshold or 60-second observation
  period blocks a market. A connected scanner with a fresh catalog can admit
  valid fresh books immediately. Spread, depth, schedule and inventory/funds
  controls still apply.
- No observed trades means zero observed volume and an unknown queue estimate,
  represented as JSON null. It never becomes fictitious flow or Infinity.
- Valid candidates are kept stable while spare bounded book slots explore the
  rest of the catalog, including markets without recent trades.
- Maintenance now uses a live OS lock. An abandoned `maintenance` marker no
  longer disables launch indefinitely; a running update still blocks entries.
  The updater releases its lock even after abrupt process termination. Legacy
  markers are honored only while their exact updater process remains alive.
- Deployment unit tests use an isolated maintenance root, so the real deployment
  guard cannot reject their simulated launches.

- The existing signed WebSocket transport now also supports documented read-only
  info RPC envelopes. SDK signatures and nonce/account locks are preserved.
- Outcome orders/cancels and live account verification use the warm WebSocket
  transport. There is no HTTP fallback or automatic resend for signed actions.
- BBO, L2, fills and order updates keep their streaming subscriptions. A fresh
  streamed book causes zero periodic REST book reads. Recovery book requests
  use info RPC when connected and retain exchange timestamps.
- The shared scanner subscribes to L2 for its 24 shortlisted markets. HTTP is
  limited to initial/missing snapshots, at most once per market per five seconds.
- Idle account sync changes from every 2 seconds to every 10 seconds. Fills,
  order changes and pre-send checks still request independent synchronization.
- Native/spot SDK metadata is cached across processes for one hour and shared
  across SDK constructors. Stop verification uses a read-only client requiring
  no SDK metadata initialization.
- HTTP fallback readers share a SQLite budget: 720 ordinary units/minute, up to
  960 including critical reads. The remaining venue headroom is for signed
  traffic and other clients. The budget is deployment-local, not shared across
  unrelated machines behind the same public IP. A quota refusal never sleeps
  under an execution lock. Both numeric and HTTP-date Retry-After are respected.

Protocol references: [WebSocket post requests](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/post-requests),
[subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

## Measured account history

`python tools/audit_outcome_volume.py --output output/outcome-throughput.json`
reads fills through WebSocket, splits full API pages, deduplicates them and
separates full versus partial hours. It never signs or sends exchange actions.
It accepts `--account` for a master address; otherwise it reads the latest local
Outcome journal identity. The report excludes addresses and credentials.

Personal trading results are kept in the ignored local `output/` report, not
published in this source document. These are account-wide Outcome fills, not
proof of session ownership or a controlled before/after comparison. Interpret
gross price PnL, actual fees and maker/taker fills separately. Live improvement
of the new execution policy has not been measured.

## Updating the existing EC2 workspace

After publishing the source commit, use the EC2 terminal:

```bash
cd /home/ec2-user/zeusproject
git pull --ff-only origin main && bash deploy/refresh-workspace.sh
```

First use **Arrêter et sortir** and verify flatness. **Arrêt immédiat** cancels
orders and can retain inventory; it does not satisfy the update preflight.
The updater refuses unknown/unconfirmed journals. A confirmed same-account,
same-market resume successor can resolve its older linked journal.

The updater preserves local source changes, installs/builds/tests before restart,
temporarily blocks new session admission, saves private state with restrictive
permissions, then checks `/api/v2/config.build_revision` against Git. It neither
changes Caddy/database configuration nor launches trading. It verifies all
tenants twice, including immediately before restarting `zeus-workspace`.
If readiness fails, inspect `sudo journalctl -u zeus-workspace -n 80 --no-pager`.
Do not delete journals or reset risk state to bypass a blocked update.

## Validation

Run `python -m unittest discover -s tests -p 'test_*.py'`, the frontend tests and
production build. New regressions cover the screenshot inventories, normal
bounded exits, cancellation/repost ordering, preserved maker queues, actual fees,
local versus uncertain fund reservations, reconnect fills, shared quotas/cache,
read-only WS envelopes, startup/recovery ownership and deployment preflight.
A public BTC info-RPC smoke check returned a valid two-sided book without signing.

FastAPI's TestClient needs HTTPX, which is not installed by the plain `fastapi`
dependency. The validated FastAPI/HTTPX versions are now declared explicitly.
Without HTTPX, discovery reproduces exactly `136 tests / errors=1`: the 17
workspace tests are replaced by one failed import. The updater now probes the
HTTP test stack before building and preserves the complete Python test report
under its backup directory. Failed checks never restart the running backend.

The EC2 updater has been syntax-checked, not executed on the remote machine.
Existing frontend build warnings about the wallet dependency bundle remain.
