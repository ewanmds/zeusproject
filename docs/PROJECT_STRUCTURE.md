# Project structure

The repository has two intentional frontends and two intentional supervisors.
Do not collapse them in a single move.

## Runtime entrypoints

| Entrypoint | Purpose |
| --- | --- |
| `bot_server.py` → `zeus.server` | Private/operator dashboard on port 8080. Loads `.env`, owns the global `BotManager`, and must remain separate from the workspace. |
| `zeus.workspace` (`zeus/workspace/__main__.py`) | Multiuser wallet workspace on port 8081. Sets `ZEUS_WORKSPACE_ONLY=1`, loads only `.env.workspace`, and creates one manager per tenant. |
| `zeus.bots.outcome_worker` | One supervised Outcome market per OS process; spawned by `OutcomeSupervisor`. |
| `zeus.bots.worldcup` | HIP-4 Outcome engine (`BotConfig`, `HyperliquidSpreadBot`). The module name is historical. |

## Shared services

| Area | Files |
| --- | --- |
| Configuration | `zeus/config.py` (side-effect-free), `.env.workspace` |
| Execution safety | `zeus/safety.py`, `process_control.py`, `signing.py`, `ws_execution.py`, `action_worker.py`, `latency.py` |
| Catalogs | `zeus/network.py`, `markets.py`, `outcomes.py`, `catalog_service.py` |
| Outcome discovery | `zeus/outcome_flow.py` |
| Auto rotation | `zeus/outcome_rotation.py`, `zeus/session_targets.py` |
| Process supervision | `zeus/outcome_supervisor.py`, `zeus/outcome_stop.py` |
| Fees / queue estimates | `zeus/outcome_costs.py` |
| Dashboard projections | `zeus/dashboard.py` |
| Workspace tenancy | `zeus/workspace/{app,runtime,storage,accounts}.py` |
| Grid engine | `zeus/bots/order_book.py` |
| Outcome engine | `zeus/bots/worldcup.py`, `zeus/bots/outcome_worker.py` |

## Frontends

- `frontend/` is the legacy private dashboard, served by `zeus.server`. Keep it
  for rollback; do not delete `frontend/app.js`, `index.html`, `index.css`,
  `config.js`, `build.mjs` or `frontend/assets/logos/`.
- `frontend/workspace/` is the Vercel/workspace React app. Keep this path: the
  Vercel configuration, systemd service and browser checks depend on it.

## Safe change rules

1. Run the full suite before and after structural changes:
   `python -m pytest tests -q`, `npm test --prefix frontend/workspace`,
   `python tests/check_workspace_ui.py`.
2. Never delete `.env`, `.env.workspace`, `.zeus-state/`, `.zeus-workspace/`,
   `output/`, journals, backups, tests or deploy docs.
3. Keep `zeus.server.BotManager`, `WorldcupStartRequest` and `GridStartRequest`
   importable: tests and legacy systemd use them.
4. Do not import `zeus.server` from the workspace without
   `ZEUS_WORKSPACE_ONLY=1`. That flag prevents the operator `.env` and global
   `BotManager` side effects.
5. Split large modules only with compatibility re-export shims and one module
   per change. `zeus/bots/worldcup.py` and `zeus/server.py` are active despite
   their size.
6. Keep the five page `h1` labels and `data-layro` attributes stable; the browser
   flow checks them.
