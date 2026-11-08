# Outcome Auto execution

The target remains one funded bid and one inventory-backed ask, post-only, using the order size frozen at launch. Existing partial orders retain their queue. Normal new asks wait for the full configured value; safety exits and passive rotation drains can be smaller. Fed rate markets remain excluded.

## Execution and freshness

`outcome_reads.py` reads spot balances and open orders concurrently with two persistent HTTP workers. Both responses must succeed and pass validation. The engine discards the whole pair if a WebSocket account event overtakes it. Each submission still obtains a fresh account snapshot under the execution/funds locks, then rechecks Stop, inventory, cap, book freshness and the quote plan. Unknown acknowledgements are not resent.

Account events request reconciliation on the next trading-loop pass, rather than waiting for the periodic two-second timer. Ordinary, positively acknowledged Auto repricings use a 50 ms rearm interval instead of two seconds; fresh reconciliation remains compulsory. Risk/limit/drain pulls and manual modes retain their previous protection.

Book updates have timestamps and local revision counters. Old WS messages are discarded; a REST response cannot overwrite a book received while its request was in flight, including messages sharing the same clock tick. A new crossed book invalidates execution. HTTP book workers and connections are reused.

The SDK clients share the native and spot metadata fetched at startup. Exact Outcome coins are checked against fresh outcome metadata without downloading the global catalog/templates/mids again.

## Selection and rotations

The public scanner retains its shortest-queue ranking and two-way-flow gates. Identical concurrent scans share a 400 ms result; cached results never extend book/trade freshness or survive reconnection/catalog changes. Catalog refresh runs outside the lifecycle loop.

Without any fill, evaluation can occur after a queue-aware grace period: `min(configured_window, max(60s, 1.25 × initial_queue_estimate))`. Filled workers retain the configured observation window, and a very recent fill preserves their queue. Repeated zero-fill visits receive longer cooldowns and a decaying queue penalty within the Auto session. These are heuristics to test against executed volume and cost, not fill guarantees.

Replacement metadata resolution does not hold the controller/UI lock. Spawn and registration are atomic against Stop. A new worker is admitted only after the previous worker is dead and its cleanup verified; PnL carry and frozen budgets survive automatic replacements.

## Dashboard and measurement

Cards expose the actual bid/ask quantities and prices, execution state, and account-sync timing. Candidate rows are updated in place. Existing colors/layout, ordered snapshots, local clocks and frame-coalesced rendering remain.

`outcome_telemetry.py` retries temporary Windows permission failures for dashboard snapshots over a bounded five-second grace period. Failed writes leave the last valid file and its timestamp intact; successful recovery resets that period. Other disk errors and persistent locks remain fatal. Terminal telemetry retries for at most two seconds. Durable risk/order journals retain strict atomic writes and never use this telemetry grace. The worker exports `telemetry_write_retries` for diagnosis.

`tests/benchmark_outcome_fast.py` is an offline benchmark: two injected 80 ms reads measured about 161 ms serial versus 81 ms parallel, and eight concurrent previews shared one computation. This does not establish a live latency reduction, higher fill rate or profitability. A funded 25 USD bid plus 25 USD inventory cannot coexist with only about 29 USD total capital; the engine does not silently resize the user's orders.

Validation commands:

```powershell
python -m unittest discover -s tests -p 'test_outcome*.py' -q
python -m unittest discover -s tests -p 'test_dashboard*.py' -q
node tests/test_dashboard_state.cjs
python tests/check_outcome_fast_ui.py
python tests/check_outcome_ui.py
python tests/check_mid_exit_ui.py
python -c "import runpy; runpy.run_path('tests/benchmark_outcome_fast.py', run_name='__main__')"
```

Backend changes require a controlled server restart; never restart an active worker merely to rebuild static assets. A newly spawned worker loads engine updates naturally, and the browser loads UI updates on refresh.
