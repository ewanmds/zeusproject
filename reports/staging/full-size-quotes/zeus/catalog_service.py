"""Public read-only catalogs in independent, bounded subprocesses.

Trading workers never use this service. One refresh per venue is shared by
all dashboard readers; warm readers receive the last good snapshot immediately.
"""
import asyncio
import copy
import multiprocessing
import time


def catalog_worker(kind, connection):
    """Spawn entrypoint: no server imports, account credentials or signed actions."""
    try:
        if kind == "hip3":
            from zeus import markets
            data = {"token_map": markets.refresh_catalog(), "dexes": list(markets.PERP_DEXS)}
        elif kind == "hip4":
            from zeus import outcomes
            meta, templates, mids = outcomes._fetch_payloads()
            data = outcomes.market_payload(outcomes._build_rows(meta, templates, mids))
        else:
            raise ValueError("Unknown catalog")
        connection.send((True, data))
    except Exception as exc:
        connection.send((False, type(exc).__name__ + ": public catalog refresh failed"))
    finally:
        connection.close()


class PublicCatalogService:
    def __init__(self, *, ttl=60, deadline=60, retry_delay=10, worker=catalog_worker):
        self.ttl, self.deadline, self.retry_delay = ttl, deadline, retry_delay
        self.worker = worker
        self.entries = {}
        self.tasks = {}
        self.processes = {}
        self.closed = False

    async def snapshot(self, kind, *, fresh=False):
        if kind not in ("hip3", "hip4"):
            raise ValueError("Unknown catalog")
        if self.closed:
            raise RuntimeError("Catalog service closed")
        entry = self.entries.setdefault(kind, {"data": None, "updated": 0, "attempt": 0, "error": ""})
        now = time.monotonic()
        task = self.tasks.get(kind)
        if (task is None or task.done()) and (entry["data"] is None or now-entry["updated"] >= self.ttl) and now-entry["attempt"] >= self.retry_delay:
            entry["attempt"] = now
            task = self.tasks[kind] = asyncio.create_task(self._refresh(kind, entry))
        if (entry["data"] is None or fresh) and task is not None:
            # Cancellation of one HTTP request must not kill the shared refresh.
            await asyncio.shield(task)
        if entry["data"] is None:
            raise RuntimeError(entry["error"] or "Public catalog unavailable")
        result = copy.deepcopy(entry["data"])
        result.update(catalog_stale=time.monotonic()-entry["updated"] >= self.ttl,
                      catalog_refreshing=not self.tasks[kind].done(),
                      catalog_error=entry["error"], catalog_updated_at=entry["wall_time"])
        return result

    async def _refresh(self, kind, entry):
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        process = ctx.Process(target=self.worker, args=(kind, writer), name="zeus-catalog-"+kind, daemon=True)
        self.processes[kind] = process
        try:
            startup = asyncio.create_task(asyncio.to_thread(process.start))
            try:
                await asyncio.shield(startup)
            except asyncio.CancelledError:
                # A spawn already running in the thread must finish before reaping.
                await startup
                raise
            writer.close()
            until = time.monotonic()+self.deadline
            while not reader.poll():
                if not process.is_alive():
                    raise RuntimeError("Public catalog worker exited")
                if time.monotonic() >= until:
                    raise TimeoutError("Public catalog refresh deadline exceeded")
                await asyncio.sleep(0.025)
            ok, data = await asyncio.to_thread(reader.recv)
            if not ok or not isinstance(data, dict):
                raise RuntimeError("Public catalog refresh failed")
            entry.update(data=data, updated=time.monotonic(), wall_time=time.time(), error="")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry["error"] = str(exc)
        finally:
            reader.close()
            writer.close()
            if process.pid is not None:
                # Only this read-only catalog child may be terminated here.
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 1)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 1)
                if not process.is_alive():
                    process.close()
            self.processes.pop(kind, None)

    async def close(self):
        self.closed = True
        pending = [task for task in self.tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
