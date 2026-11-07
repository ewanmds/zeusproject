# Dashboard data flow

The dashboard observes trading workers; rendering never submits an order.

1. **Execution**: `zeus/bots/outcome_worker.py` and the trading engines own exchange actions.
2. **Lifecycle**: `OutcomeSupervisor` reconciles processes, journals and snapshots. Immutable launch configuration stays with its session. `OutcomeRotation` links replacement workers into one Auto session.
3. **Presentation**: `dashboard_state.py` projects physical workers into logical session cards and matching active totals. Closed historical sessions do not inflate active clocks. An Auto session retains executed totals across its replacements. Unknown accounting remains unknown.
4. **Transport**: `dashboard_broker.py` shares a short cache between REST and WebSocket readers. Each source has its own lock, revision and server identity. Collection runs outside the asyncio event loop. Start/Stop responses contain a freshly collected state.
5. **Browser**: `frontend/state.js` rejects older revisions and retired server frames, tracks freshness independently for each strategy and advances display clocks locally. `app.js` coalesces updates per animation frame and falls back to status polling when pushes stop.

The legacy `bots` and aggregate fields remain available for existing callers. The current Outcome UI uses `display_bots` and `active_metrics`, both describing the same logical sessions. A Stop targets that logical session's worker chain; it is successful only after the supervisor verifies cleanup.

The Auto scanner describes opportunities for the next launch. Its draft selection never retargets an existing session. Failed scanner requests clear obsolete candidates. Stale dashboard data remains visible with a delay indicator and disables Outcome launches.

## Local verification

```powershell
python -m unittest discover -s tests -p 'test_dashboard*.py' -v
node tests/test_dashboard_state.cjs
python -m unittest discover -s tests -p 'test_outcome*.py' -q
python tests/check_outcome_ui.py
python tests/check_mid_exit_ui.py
python integrations/freqtrade/tests/check_ui.py
npm --prefix frontend run build
```

Browser checks mock all exchange/API traffic. They do not start a live bot. Frontend changes load on page refresh; Python server changes need a controlled server restart. Keep `.zeus-state`, `.env`, session journals and risk controls when cleaning generated files. Do not restart an active trading process as part of a static build.
