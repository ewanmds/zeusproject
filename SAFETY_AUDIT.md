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

## Validation commands

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
