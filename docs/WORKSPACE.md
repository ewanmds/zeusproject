# Zeus Wallet Workspace

## Local startup

Double-click `START-WORKSPACE.bat` on Windows. It builds the frontend and opens
`http://127.0.0.1:8081`. Backend dependencies must be installed with
`python -m pip install -r requirements.txt`. The existing private dashboard on
8080 remains separate, with its existing workers and credentials.

The workspace backend is `python -m zeus.workspace`. It serves the compiled
React/TypeScript frontend from `frontend/workspace/dist` and its own `/api/v2`
routes. It does not mount legacy control/status endpoints. Vite development:
`npm run dev --prefix frontend/workspace`, proxied to the backend on 8081.

## Wallet setup

Installed EVM wallets use EIP-6963 discovery (with an injected-provider fallback).
The user signs a SIWE login message, then separately approves a Hyperliquid
agent from the Account screen. Neither action starts a bot. A final session
review and explicit launch submit the trading command.

WalletConnect is lazy-loaded through Reown AppKit. Configure the public project
identifier `ZEUS_WALLETCONNECT_PROJECT_ID` in the root `.env` or environment to
enable mobile/QR connections, with the deployment origin allowed in Reown.
Without an identifier the button explains that setup is pending; installed
wallets remain usable. No Reown project was created on the user's behalf.

The initial account is the wallet's own main Hyperliquid account. Subaccount
selection is not enabled. The connected identity, traded account and agent are
separate concepts in the execution context. The account address, not the agent,
is used for balances and fills.

## Identity and execution

- Five-minute, single-use login challenge, exact server-generated SIWE message,
  signature recovery and twelve-hour HttpOnly session cookie.
- Authenticated mutations require the session CSRF value and an allowed origin.
  The WebSocket validates the origin/cookie and sends only that identity's data.
- An identity change clears frontend sessions and balances and rejects late API
  responses and old-owner WebSocket frames. Drafts and pending command IDs are
  keyed by wallet. They contain no private signing credentials.
- PostgreSQL is supported through SQLAlchemy; SQLite is the local default.
  The initial schema is created transactionally from `workspace/storage.py`.
  Future structural migrations must be explicit rather than resetting this DB.
- Agent private keys are encrypted with Fernet before database storage. The
  local key file is outside the DB in `.zeus-workspace`, never in the frontend.
  Production requires a separately managed encryption key and PostgreSQL.
- Agents are checked against Hyperliquid before execution. Approval delivery
  is reserved before relay and uncertain approvals are verified, not blindly
  resent. Expired unsigned preparation messages are refreshed.
- An immutable account context supplies each child's credentials without
  changing the parent environment. Each tenant has its own manager, journals,
  funds, cached reads and execution admission. Grid snapshots are per session;
  its instance lock remains per account/market.
- Start/Stop exchange reads do not hold the dashboard read lock.
- Idempotency keys are consumed before launch. A timed-out command can be read
  by its original ID; a repeated request with that ID never launches again.
  An unknown outcome remains unconfirmed and needs verification.
- The same account cannot start through this app while unresolved sessions
  remain in its old private workspace. Stop/reconcile there before migrating.
  Old sessions are not assigned automatically to a newly connected wallet.

## Session behavior

Auto Outcome uses the existing scanner and maker engine. BBO and MID use the
existing Grid engine. Forms show existing strategy semantics rather than
claiming new throughput presets. Parameters and the traded account are frozen
when the session starts; draft changes do not retarget live workers.

HIP-3 and Outcome children share the existing per-agent signing coordinator.
Its process lock serializes signed requests and advances the nonce clock across
both engines; an uncertain submission is never automatically sent again.

For Auto Outcome, optional duration or executed-volume targets are enforced
both by the worker and its rotation controller. The target stops new purchases
and begins the existing maker drain; the session is complete only after its
orders and inventory are independently confirmed flat. The final exit can take
longer and add executed volume. The target and previous volume survive market
rotations. A realized-loss threshold uses the engine's existing cumulative
realized PnL guard; it is not a guarantee on unrealized inventory losses.

Manual Pause is not exposed: the existing risk/network recovery state is not a
substitute for a separately specified manual pause contract. Stop uses the
existing explicit stop semantics and remains unconfirmed until cleanup passes.
Logging out ends web access; it does not silently stop server-side sessions.

Unknown or stale accounting remains unknown. Session cards and overview use the
same snapshot. Performance separates the period's realized session results from
open inventory; its graph shows per-session volume, not fabricated fill history.

## Production

Use `.env.workspace.example` as a configuration reference. Set:

```text
ZEUS_WORKSPACE_PRODUCTION=1
ZEUS_WORKSPACE_ORIGIN=https://your-domain.example
ZEUS_WORKSPACE_DATABASE_URL=postgresql+psycopg://...
ZEUS_WORKSPACE_KEY=<managed Fernet key>
ZEUS_WALLETCONNECT_PROJECT_ID=<public Reown identifier>
```

Serve the frontend and API under the same HTTPS origin using a reverse proxy
that upgrades `/api/v2/ws`. Run **one workspace API process** per state directory:
it supervises local OS workers. Multiple API replicas require a dedicated shared
job supervisor rather than starting duplicate local managers. Set the listen
host/port with `ZEUS_WORKSPACE_HOST` and `ZEUS_WORKSPACE_PORT`.

Back up the database, encrypted agent records, encryption key and execution
journals together. Keep the managed key outside a database backup; losing it
prevents decryption, so do not regenerate it on an existing installation.
Never publish the private dashboard's administrator token in this frontend.

No public deployment, primary-wallet signature or live trading session was
performed to validate this refactor. WalletConnect/mobile QR and production
PostgreSQL require the deployment's own configuration and an actual user test.

## Validation

```sh
python -m pytest tests -q
npm test --prefix frontend/workspace
npm run build --prefix frontend/workspace
python tests/check_workspace_ui.py
```

Browser tests use disposable EOA wallets with real signatures and fake exchange
execution. They cover login, agent approval, review with duration/loss settings,
launch, continuing WebSocket fills, stable cards, live details, scoped Stop,
wallet changes and 1440/390 px layouts. Process tests patch Popen so no trading
child is run. Backend tests cover replay, origin/CSRF checks, tenant isolation,
encrypted secrets, idempotency, production defaults and maker-only targets.

Screenshots: `output/playwright/workspace/`. The checked-in legacy source is
retained for rollback; the new app has an independent build and launcher.
