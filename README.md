# Zeus

Hyperliquid market-making dashboard with two strategies and a HIP-4 Outcome bot.

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
