# Inventory and autonomous taker audit

Read-only audit of the Python MID/BBO paths before this pass's core changes.
The Go Freqtrade engine is a different execution path. No order, service action
or live strategy launch was performed for this audit.

## Confirmed code causes

- `_manage_session_exit` used a runaway IOC at twice a dynamic stop. The dynamic
  stop was `min(20, max(6, 3*vol))` bps, reduced another 25% in protection mode.
  An autonomous taker threshold could therefore be 12 bps, or 9 bps in that mode.
- A long from 1709.9 sold at 1706.6 has a price return of about -19.30 bps. At
  0.058 units its gross price loss is 0.1914 USD, before fees. This is compatible
  with the runaway branch; the screenshot alone does not prove attribution.
- The dead-feed branch could call `flatten` after its REST book refresh failed.
  Session-loss halt, execution failures, supervisor loss and process signals
  could enter `finally` cleanup, whose existing close callback used IOC too.
- Fill callbacks advanced quantity but did not update entry or unrealized PnL.
  Risk read the latest quantity with REST PnL up to five seconds old. A partial
  close could inflate the apparent loss bps; flat inventory could retain an old
  unrealized loss and double-count it with realized PnL; a new position could
  inherit the old position's loss, entry, peak, risk reason or holding age.

## State corrections supplied

`zeus/inventory.py` supplies pure, immutable fill transitions and explicit marks:
weighted additions; unchanged entry for partial reductions; entry zero at flat;
fill-price entry for a residual sign flip. The caller receives flags to reset
per-position timers, bank window, risk reason and peak. A mismatched exchange
start position is a reconciliation requirement, never a blind second addition.

Marks use current best bid for longs and best ask for shorts by default; a mid
mark is available explicitly. Neither includes fees or depth beyond level one.
An invalid or stale book and an unknown non-flat basis raise instead of turning
an unknown loss into zero. Flat has zero inventory PnL even before a book exists.

## Latency and queue observations

The decision loop previously called the whole SDK request synchronously. A fast
book feed alone did not stop fills/risk decisions waiting behind an ACK. The
separate `LatestActionWorker` now supplied is bounded to one running callback
plus one pending wake-up, not a queue of obsolete price payloads. Root owns its
integration and execution fences. The worker itself has no exchange or network.

Opening or exit modifications still lose price-time priority. Exact unchanged
prices/sizes are retained. Making more requests is not proof of better PnL.
Any extra MID rest/size debounce requires its own useful-change/risk validation;
strict BBO prices should remain exact, with no silent adverse-selection gates.

## Limits

Maker-only exits can remain unfilled during a one-way move, and a pause does not
cap the loss of existing inventory. An unavailable book cannot support a safe
fresh maker exit. New opening exposure must remain blocked while state/book is
unknown; manual closure remains an explicit independent action. No backtest of
matching, queue position or profit was fabricated from the screenshot.

Standalone tests supplied: 17 inventory/basis/mark cases and 10 action-worker
cases. They exercise partial fills, closes/reopens, flips, late/mismatched fills,
precision and finite validation, stale books, one-thousand coalesced updates,
bounded stop, late callback completion and terminal callback failure.
