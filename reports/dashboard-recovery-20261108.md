# Dashboard recovery - 2026-11-08

The API reported real Outcome fills while the browser counters remained empty/zero. Reproduced the same partial render by removing quoteText from a cached state.js: the session header/state rendered, then the undefined helper stopped all financial rendering. The old /frontend/state.js URL had no Cache-Control header while app.js required revalidation. This is a demonstrated mixed-version failure; direct inspection of the user's existing browser was unavailable.

Fixes:
- Load state.js through its canonical revalidating route, with the same explicit version as app.js. Static builds derive a shared content hash from both scripts.
- Fall back safely when cached optional quote/error display helpers are missing.
- Repair missing quote nodes. Isolate each dashboard render so a failed Grid frame cannot discard Outcome.
- Distinguish a render failure from successful freshness; do not report Updated just now for a failed display.
- Avoid a misleading Status unavailable toast for a failed REST refresh while current streamed data is fresh.

No CSS, trading settings, orders or session lifecycle changed by this fix. No server or worker restart. An already loaded browser tab requires one reload to load the updated JavaScript.

Validation:
- Before fix: the cached-helper browser regression failed with counters still unrendered.
- After fix: cached helpers, missing quote-node repair, REST outage with continuing WebSocket data, and isolated Grid render failure passed at 1440px and 390px.
- Real live server in a GET-only browser: 2 WebSocket frames, 101.90 USD and 5 fills matching between overview and session card, no JavaScript/render errors. This is a timed observation; subsequent fills change the numbers.
- Outcome controls (independent sessions, Stop, recovery, reload) and MID UI desktop/mobile passed with APIs mocked. No live trading control clicked.
- 7 Python dashboard tests, Node state contracts and syntax passed. Frontend build passed, both script URLs carry one content hash.

Artifacts: ../artifacts/dashboard-recovery/live-readonly.png and live-readonly.json.
Backup manifest: backups/dashboard-recovery-20261108-141907Z/manifest.json.
