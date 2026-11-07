# Public catalog isolation

HIP-3 trading and Outcome trading already run in independent child processes.
The server's former async catalog routes nevertheless performed synchronous
HTTP calls/retry sleeps on the FastAPI event loop. This could delay dashboard
WebSocket frames, health, and start/stop requests; it does not prove a measured
slowdown of an already-running strategy's quoting loop.

The dashboard now uses zeus.catalog_service.PublicCatalogService:

- Separate read-only refresh subprocess for HIP-3 and HIP-4, at most one per venue.
- Process spawn and result transfer are off the API event loop.
- A 60-second cache returns last-good data immediately while a refresh runs.
- Concurrent readers share one refresh; disconnecting a reader cannot cancel it.
- Each worker has a 60-second wall-clock refresh deadline, bounded retries,
  and a ten-second minimum interval between failed refreshes.
- Failed refreshes retain cached data. Response metadata includes catalog_stale,
  catalog_refreshing, catalog_error and catalog_updated_at. Cold failure stays
  explicitly unavailable; no exchange state is inferred from missing data.
- Shutdown cancels/reaps only owned catalog children. It never targets trading
  processes, positions, or orders. Workers make public info reads only.
- Truncated HTTP bodies are treated as retryable read failures, never applied
  to signed trading actions.

The public workers are created lazily on catalog requests and exit after refresh.
Cache lifetime is the API server's lifetime. A restart requires a new cold read;
no cached snapshot is treated as live account/order state. HIP-3 and HIP-4 may
still share exchange rate limits, network bandwidth and host resources.

Offline tests use actual spawned fake workers: slow HIP-4 vs HIP-3 + health,
single-flight refresh, cache reuse, stale data, unavailable cold catalog, worker
timeout/shutdown, reader cancellation, and truncated-read retry handling.
