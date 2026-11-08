# Zeus

Hyperliquid market-making dashboard with two strategies and a HIP-4 Outcome bot.

## Wallet workspace

The new multiuser workspace runs independently on **http://127.0.0.1:8081**.
On Windows, double-click [START-WORKSPACE.bat](START-WORKSPACE.bat).

```sh
pip install -r requirements.txt
npm ci --prefix frontend/workspace
npm run build --prefix frontend/workspace
python -m zeus.workspace
```

Each wallet signs its own login message and approves its own Hyperliquid agent.
The four views cover launch, sessions, performance and account authorization.
No trading starts merely by opening the workspace. Installed EVM wallets work
directly; mobile WalletConnect requires `ZEUS_WALLETCONNECT_PROJECT_ID`.
See [workspace operation](docs/WORKSPACE.md) for account isolation, production
configuration, tests and migration. The private dashboard below remains on 8080.

To replace the public EC2 site, use [the workspace deployment guide](deploy/WORKSPACE.md).
It prepares production storage and switches the existing Caddy site to the new service.

## Run

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp deploy/zeus-bot.service /etc/systemd/system/   # optional: systemd deploy
python bot_server.py                              # serves API + frontend on :8080
```

The dashboard is static ([frontend/](frontend/)) and served by the FastAPI app.
Rebuild the deployable copy with `node frontend/build.mjs` (writes `frontend/dist/`).
`config.js` in a build supplies `apiBase`, `apiToken` and an optional `wsUrl`;
locally the page falls back to the current origin and asks for the access code.

## Layout

- [zeus/server.py](zeus/server.py) — HTTP API, WebSocket publisher, session lifecycle.
- [zeus/dashboard.py](zeus/dashboard.py) — snapshot projection, localization and the
  shared read broker (one collection and one JSON encoding per revision).
- [zeus/outcome_supervisor.py](zeus/outcome_supervisor.py) — Outcome sessions and rotation.
- [zeus/bots/order_book.py](zeus/bots/order_book.py) — BBO/MID market maker.
- [zeus/bots/worldcup.py](zeus/bots/worldcup.py) — HIP-4 Outcome execution worker.
- [zeus/safety.py](zeus/safety.py) — atomic journals, signing and submission guards.
- [.zeus-state/](.zeus-state/) — live session journals. Never edit or delete these:
  they are the durable record used to verify stops. `SAFETY_AUDIT.md` documents the
  risk model; read it before changing execution paths.
- [deploy/](deploy/) — systemd unit and Caddy config.
