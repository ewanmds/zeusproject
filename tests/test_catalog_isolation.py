"""Offline subprocess proof: HIP-4 retries cannot occupy the API event loop."""
import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from http.client import IncompleteRead
import requests
from zeus.catalog_service import PublicCatalogService
from zeus.network import retry_read


def fake_catalog_worker(kind, connection):
    # No HTTP or exchange SDK exists in this fake child.
    if kind == "hip4":
        time.sleep(2)
    connection.send((True, {"kind": kind, "pid": os.getpid()}))
    connection.close()


def hung_catalog_worker(kind, connection):
    time.sleep(60)


class CatalogIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = PublicCatalogService(worker=fake_catalog_worker, deadline=10, retry_delay=0)

    async def asyncTearDown(self):
        await self.service.close()
        self.assertFalse(self.service.processes)

    async def test_hip3_and_health_continue_during_slow_hip4(self):
        import httpx
        from zeus import server
        slow = asyncio.create_task(self.service.snapshot("hip4"))
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
            response = await asyncio.wait_for(client.get("/health"), 0.5)
        self.assertEqual(response.status_code,200)
        fast = await self.service.snapshot("hip3")
        self.assertFalse(slow.done(), "HIP-3 waited for HIP-4")
        other = await slow
        self.assertNotEqual(fast["pid"],other["pid"])
        self.assertNotEqual(fast["pid"],os.getpid())

    async def test_routes_use_isolated_service(self):
        import httpx
        from zeus import server
        payloads={"hip3":{"token_map":{"IO_ANTH":{"symbol":"io:ANTH","sz_decimals":3}},"dexes":["io"],"catalog_stale":True},
                  "hip4":{"venue":"out","outcomes":[],"count":0,"categories":[],"catalog_stale":True}}
        snapshot=AsyncMock(side_effect=lambda kind:payloads[kind])
        with patch.object(server,"BOT_API_TOKEN",""),patch.object(server.public_catalogs,"snapshot",snapshot):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
                hip3=await client.get("/api/grid/tokens")
                hip4=await client.get("/api/outcomes/markets")
        self.assertEqual(hip3.status_code,200)
        self.assertEqual(hip3.json()["tokens"][0]["symbol"],"io:ANTH")
        self.assertTrue(hip3.json()["catalog_stale"])
        self.assertTrue(hip4.json()["catalog_stale"])
        self.assertEqual(snapshot.await_count,2)

    async def test_concurrent_readers_share_one_refresh_and_cache(self):
        a,b=await asyncio.gather(self.service.snapshot("hip3"),self.service.snapshot("hip3"))
        self.assertEqual(a["pid"],b["pid"])
        again=await self.service.snapshot("hip3")
        self.assertEqual(a["pid"],again["pid"])
        self.assertFalse(again["catalog_stale"])
        a["kind"]="corrupted"
        self.assertEqual(again["kind"],"hip3")

    async def test_fresh_reader_waits_for_existing_refresh_without_blocking_cached_readers(self):
        original = await self.service.snapshot("hip4")
        self.service.entries["hip4"]["updated"] -= 61
        cached = await self.service.snapshot("hip4")
        self.assertTrue(cached["catalog_stale"])
        fresh = asyncio.create_task(self.service.snapshot("hip4", fresh=True))
        await asyncio.sleep(.05)
        self.assertFalse(fresh.done())
        concurrent = await asyncio.wait_for(self.service.snapshot("hip4"), .2)
        self.assertEqual(concurrent["pid"], original["pid"])
        refreshed = await fresh
        self.assertFalse(refreshed["catalog_stale"])
        self.assertNotEqual(refreshed["pid"], original["pid"])

    async def test_stale_cache_served_during_refresh_failure(self):
        original=await self.service.snapshot("hip3")
        self.service.ttl=0
        self.service.deadline=0.15
        self.service.worker=hung_catalog_worker
        cached=await asyncio.wait_for(self.service.snapshot("hip3"),0.1)
        self.assertEqual(cached["pid"],original["pid"])
        self.assertTrue(cached["catalog_stale"])
        await self.service.tasks["hip3"]
        self.assertIn("deadline",self.service.entries["hip3"]["error"])
        self.assertEqual(self.service.entries["hip3"]["data"]["pid"],original["pid"])
        self.assertFalse(self.service.processes)

    async def test_cold_timeout_is_explicit_and_child_is_reaped(self):
        self.service.worker=hung_catalog_worker
        self.service.deadline=0.15
        with self.assertRaisesRegex(RuntimeError,"deadline"):
            await self.service.snapshot("hip4")
        self.assertFalse(self.service.processes)

    async def test_shutdown_reaps_only_public_workers(self):
        self.service.worker=hung_catalog_worker
        read=asyncio.create_task(self.service.snapshot("hip4"))
        await asyncio.sleep(0.1)
        await self.service.close()
        with self.assertRaises(asyncio.CancelledError): await read
        self.assertFalse(self.service.processes)

    async def test_cancelled_reader_does_not_cancel_shared_refresh(self):
        one=asyncio.create_task(self.service.snapshot("hip4"))
        await asyncio.sleep(0.05)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError): await one
        result=await self.service.snapshot("hip4")
        self.assertEqual(result["kind"],"hip4")


class CatalogRetryTests(unittest.TestCase):
    def test_truncated_reads_retry_without_touching_execution(self):
        for failure in [IncompleteRead(b"short",100),requests.exceptions.ChunkedEncodingError("short body")]:
            call=Mock(side_effect=[failure,{"ok":True}]);sleep=Mock()
            self.assertEqual(retry_read("public catalog",call,sleep=sleep),{"ok":True})
            sleep.assert_called_once_with(1)
            self.assertEqual(call.call_count,2)

if __name__=="__main__": unittest.main()
