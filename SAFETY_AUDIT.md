# Zeus execution/stop audit

Implemented for Outcome, BBO/MID, Freqtrade and their dashboard supervisor.

| Defect | Protection |
| --- | --- |
| Outcome could submit with `running=False` | Engine check plus serialized exchange gate |
| Stop during initialization could be lost | Persistent stop event/sentinel; recheck before launch |
| Exception skipped cleanup | Session `try/finally`; idempotent cleanup owner |
| API error was cleared by the next successful request | BBO/MID/Freqtrade latch; Outcome verifies orders and inventory before automatic recovery |
| Failed cancellation erased order tracking | Confirm acknowledgements; independent exchange reconciliation |
| Timeout/process exit was reported as a clean stop | Separate cleanup confirmation and process termination |
| Late order response could survive cancellation | Drain requests first; record session OIDs/client IDs |
| Uncertain close was retried with stale size | Stop escalation; read fresh residual before known-result retry |
| Restart forgot interrupted sessions | Atomic `.zeus-state` journals; Start remains blocked |
| Orphan child kept quoting after its supervisor died | Python and Go parent-process monitors close the execution gate and trigger cleanup |
| Cached state displayed as current | Explicit stale/error states; preserve final statistics |
| Outcome cost/million displayed the PnL sign | Costs are negative net PnL, normalized by executed volume |

## Operator behavior

- Stop disables quoting, cancels session orders and attempts to close inventory.
- BBO/MID/Freqtrade execution errors follow the same safety sequence and require manual Start.
- Outcome runtime errors keep the session alive in `recovering`, without automatic
  cancellation or liquidation. New execution is blocked until independent order
  and inventory reads succeed. Checks back off from 2 seconds up to 30 seconds;
  recovery preserves a further 2-second quote cooldown. Manual Stop always wins.
  Fatal startup errors, journal failures and process shutdown still stop safely.
- An uncertain placement must be found by its unique client ID via `orderStatus`.
  `unknownOid` or an ambiguous response keeps recovery pending; no blind resend.
  Pending client IDs are persisted and also block false flat-stop confirmations.
- Outcome's exact `Order was never placed, already canceled, or filled.` cancel
  response is reconciled under the execution lock. It is treated as resolved
  only after independent open-order verification and a fresh inventory/order
  synchronization. No cancellation is resent and opening rearm is preserved.
  Still-open orders, failed/ambiguous reads and other rejections enter recovery.
  This exception never revives a session that was already stopped.
- New Outcome submissions refresh balances and open orders under the execution
  gate. Buys are bounded by available USDC at the actual limit price; sells by
  whole available tokens (`total - hold`). Total inventory still includes held
  tokens. Existing orders and changed trade direction prevent a stale submission.
  Insufficient available funds wait with a cooldown, without sending a rejected
  order. An actual exchange rejection triggers read-only recovery before quoting.
- `Vérifier l’arrêt` / `POST /api/sessions/reconcile` performs read-only checks.
  It never cancels someone else's orders or restarts a session.
- An interrupted child that is still alive, residual orders/positions, a corrupt
  journal, or an unreachable exchange keeps Start blocked. Resolve the live state
  manually, then reconcile again. Journals must not be deleted to bypass this.
- New sessions refuse pre-existing orders/inventory on their selected markets.
- No deployment, service restart, or live exchange mutation is part of validation.

## Outcome processes

- The dashboard starts one `zeus.bots.outcome_worker` OS process per canonical
  market. `ALL` and an empty market are rejected. Admission and worker-lifetime
  locks prevent two simultaneous owners of the same account/market.
- The form exposes native Yes/No radio controls (default No). Start accepts
  `outcome_side: "yes" | "no"`, resolves the corresponding exact token coin,
  and persists that side in the worker configuration. A missing field retains
  the side encoded by an exact coin, or the catalog default for an alias.
  Yes and No can run separately; changing the draft never retargets a live bot.
  Worker validation rejects a mismatched side/coin. Cards retain explicit
  Yes/No labels after reload, and targeted Stop touches only that exact token.
- Each UUID has its own runtime configuration, stop signal, atomic stats file
  and durable session journal under `.zeus-state`. Wallet credentials are passed
  only through the worker environment, never CLI arguments or runtime JSON.
- `POST /api/outcomes/start` (and `/api/worldcup/start`) returns `session_id`,
  `market`, `pid`, and the initial `connecting` phase. This is not a quote-ready
  acknowledgement; the child publishes its independently observed lifecycle.
- `POST /api/outcomes/stop` accepts optional `{"session_id":"..."}`. An empty body
  means Stop all. All targets are signaled before concurrent 30-second waits.
  A failed/blocked stop retains the process and blocks only that market's Start.
- Status/WebSocket preserve aggregate fields and add `bots`, per-session configs,
  phases, recovery errors, freshness and final accounting. Aggregates summarize
  the latest session of each market, including its retained final results. Cost/M
  is calculated from total net PnL and volume, never by summing per-bot ratios.
- On server restart, journals/stats restore the last market cards. Interrupted
  sessions are not relaunched or auto-attached. Only exchange verification and
  process termination can clear an unconfirmed session. Legacy multi-market
  journals continue blocking every market they covered until reconciled.
- New Outcome submissions hold an account-wide process lock across fresh balance
  reads and signing/submission. The same lock serializes signer nonces. Uncertain
  buys retain durable CLOID/USD reservations, conservatively deducted from free
  USDC until the exact client order and account are independently reconciled.
  Orders belonging to another market/process are not canceled by targeted Stop.
- Parent death or the per-session stop file closes the execution gate even if
  initialization or an API response was in flight. Runtime API errors continue
  the existing per-session read-only recovery; they do not stop other markets.
- The offline process suite launches actual children with fake bots and blocks
  all HTTP requests in those children. A separate concurrent fake-wallet test
  verifies that three 50 USD requests cannot overspend a 105 USD balance.

## Validation commands

BBO/MID startup retains `onlyIsolated` from live market metadata. Isolated-only
markets use `is_cross=False`, and requested leverage stays bounded by that
market's published maximum. Cross-compatible markets keep their cross default.
`update_leverage` validates the documented `response.type=default` acknowledgement;
exchange rejections retain their sanitized reason rather than a generic message.

BBO/MID reduce-only placement and replacement refresh positions before sending,
check the closing direction, round quantity down to the fresh residual, and
subtract existing orders on that closing side (excluding replaced order IDs).
Fills/order updates invalidate computed requests until account synchronization.
The exact `Reduce only order would increase position.` rejection can be resolved
only through fresh position and owned-order reconciliation. The rejected status
remains a rejection: it is never presented as a fill and RO is never removed.
Unknown account/order reads and unrelated rejection reasons retain the interlock.

BBO/MID ALO rejections (`Post only order would have immediately matched...`)
are normal quote races, including mixed batches with a resting order accepted
on the other side. Complete acknowledgements preserve those accepted OIDs and
do not trigger global cancellation, liquidation, or a failed lifecycle. Only
the rejected opening side backs off (1, 2, 4, then at most 8 seconds) and obtains
fresh pricing/inventory before its next attempt. Accepted-side queue priority
is retained while correct; genuine risk pulls and closing orders remain urgent.
Rejected modifications reconcile actual resting orders before continuing.
Expected quote warnings are separate from fatal errors, and the dashboard's
red failure border follows the lifecycle rather than any warning string.

BBO/MID also reconcile the exact already-canceled/filled cancel response, including
mixed `success`/missing-OID batches. Fresh position and order snapshots must show
the requested missing OIDs absent before the guard normalizes those statuses to
resolved cancellation. Other owned quotes survive reconciliation. No cancellation
is resent, no fill is invented, and successful reconciliation creates no fatal
error or failed lifecycle. Unavailable snapshots and actually open OIDs are not
silently classified as absent.

```powershell
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/check_outcome_ui.py
npm.cmd --prefix frontend run build
```

From `integrations/freqtrade/engine`, using its bundled Go installation:

```powershell
& ../.build/go/bin/go.exe test ./...
& ../.build/go/bin/go.exe vet ./...
& ../.build/go/bin/go.exe test -race ./internal/execution ./cmd/bot ./internal/zeusbridge
```

The race detector additionally needs a supported C compiler/CGO environment.
Source changes require rebuilding the platform binaries and their manifest via
`python -m zeus.bots.freqtrade --build` before deploying the Go runtime.
