# Outcome hardening — 23 November 2026

## Baseline

- Historical reference: `4a05964` (before the 22 November changes).
- Pre-hardening snapshot: tag `pre-outcome-hardening-20261123` = `514a09d`.
- Local audit changes were integrated, never overwritten.
- No bot restart, real order or deployment was performed while preparing this lot.

## Implemented in this lot

### Stops

- **Arrêter et sortir** (`mode=graceful`, default): blocks new buys, requests a
  maker exit at the best ask, cancels competing session orders and keeps the
  session alive until an independent flat verification. Repeated clicks are
  idempotent and never change the policy.
- **Arrêt immédiat** (`mode=cancel_only`): touches the hard-stop sentinel,
  cancels owned orders, performs no taker sale and leaves any residual inventory
  visible and unconfirmed.
- Legacy Stop routes accept the same `mode`; responses separate
  `requested_session_ids` from independently `stopped_session_ids`.
- Stop All cancels rotation admission before requesting exits.
- Auto-recovery distinguishes manual exit, rotation exit and hard stop; a hard
  stop is never resumed.

### Rotation

- Low executed `$/min` is now only a re-evaluation signal.
- **No rotation while inventory is present.** Unknown inventory is treated as
  present.
- Initial observation remains 180 s.
- A better market must remain eligible for 30 s and its estimated cycle must be
  at least 25 % shorter.
- Five-minute per-market cooldown is preserved.
- With no eligible candidate the logical session remains active and waiting,
  never "stopped".
- At flat, bids are cancelled and the next leg starts only after the previous
  worker and account are independently verified.
- If a fill arrives during cancellation, the controller removes the rotation
  drain and the worker clears its in-memory exit gate, then manages the
  inventory normally.

### Bounded exit (maker, then IOC)

- New workspace sessions use `exit_policy=maker_then_ioc`.
- Historical sessions without an explicit policy keep `maker_only`; no existing
  inventory silently receives taker permission.
- Maker exit starts at the best ask. After `maker_exit_timeout_sec` (90 s), the
  worker cancels competing orders, re-reads inventory and book, and may send one
  sell-only IOC.
- IOC limit is at most `exit_slippage_bps` (20 bps) below the fresh best bid.
- The fallback is blocked when entry cost is unknown, the book is stale, or the
  estimated total loss would exceed the remaining session stop-loss budget.
- At most three IOC attempts per minute, at least five seconds apart; after an
  unexecuted IOC the next attempt waits 30 s and a maker exit is maintained.
- A timeout or uncertain submission reconciles by CLOID/account before any new
  attempt. IOC never leaves an unexecuted remainder resting.
- The strict maker-only gate has a narrow `bounded_exit_scope()` that permits
  only sell-side IOC. Buy IOC and all other crossing orders remain forbidden.

### Network, SDK and errors

- `zeus/network.py` now classifies `rate_limited`, `timeout`, `network_error`,
  `exchange_unavailable`, `invalid_metadata` and `invalid_configuration`.
- 429 responses respect `Retry-After`; otherwise exponential backoff with jitter
  is capped at 60 s. Every wait is interruptible by Stop.
- Rate-limit messages are operator-readable, not "check the Internet".
- `safe_error()` classifies HTTP/SDK 429/5xx responses and strips raw SDK
  response headers; the UI no longer displays `Content-Type` dictionaries.
- `hyperliquid-python-sdk==0.24.0` and `eth-account==0.13.7` are pinned.

### Interface

- Stop dialog exposes the two distinct actions.
- Persisted pending/confirmed stop state remains visible in the workspace.
- Session details show volume per minute, orders/cancellations, inventory, fees
  and cost per million.
- Scanner diagnostics are UTF-8 clean, bounded, sanitized and threshold-aware.
- The manual "resume session" button is removed; crash recovery is backend-owned.

## Validation

- `python -m pytest tests -q`: **123 passed**
- `npm test --prefix frontend/workspace`: **44 passed**
- `npm run build --prefix frontend/workspace`: **pass**
- New tests cover network 429/Retry-After/timeout/cancellation, flat-only
  rotation, candidate stability, stop modes, bounded IOC price/loss/rate,
  resume objectives and scanner diagnostics.

## Known limits / next lots

- Shared cross-process REST budget (960 units/min, priority reserve) is not yet
  implemented; classification and backoff are.
- Metadata sharing/caching across `Info`, `Exchange` and WebSocket, and the
  single SDK adapter, remain.
- Shared local market-data service and capacity manager (8 WS / 900 subs) remain.
- REST book polling is still active; WebSocket-only book updates remain.
- Taker fee is a configured conservative estimate (10 bps by default). A real
  per-account taker-fee lookup should replace it before production tuning.
- Crash restart suspension after three crashes in ten minutes is logged by the
  recovery backoff but not yet surfaced as a distinct UI cause.
- Volume comparison over three 30-minute windows with 100+ complete cycles is
  not yet measured. The known local sample (99 orders, 97 cancels, 4 fills in
  19.6 min) is a regression hint, not proof of the cause.

## Rollback

- Source: `git reset --hard pre-outcome-hardening-20261123` (keeps journals).
- Journals, requested stops and uncertain CLOIDs must not be deleted.
- Do not restart an active bot or deploy during preparation.
