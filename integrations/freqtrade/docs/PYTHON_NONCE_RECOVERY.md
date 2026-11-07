# Python HIP-3 signing and rejected nonce recovery

BBO/MID signed actions are serialized across local Python market processes by
public signing-agent identity. An OS file lock covers the complete SDK request;
a durable millisecond high-water mark prevents reuse after another local send.
A stalled/backward clock fails without sending. Agent private keys are never
stored in the coordination files. This does not coordinate external clients,
remote hosts or the separate Go SDK.

An exact whole-action exchange reply `status=err` with
`Invalid nonce: duplicate nonce <digits>` definitively rejects that action.
Only its own client IDs are removed from pending uncertainty. Older pending
requests remain unresolved. Timeouts, incomplete responses and arbitrary errors
keep their pending client IDs and are never replayed automatically.

Legacy stopped sessions with that exact first error, no recovery error, and
unknown client IDs can be reconciled only after fresh orderStatus queries and
an independent open-order/position snapshot confirm no managed exposure. API
failures, open orders and residual positions continue to block Start. This
migration never starts a bot, closes a position or cancels an order. Back up a
live session journal before running a manual legacy migration.

Offline tests cover action-scoped uncertainty, ambiguous errors, strict legacy
classification, non-flat/unavailable account reads, and actual fake signing
processes producing unique millisecond nonces.
