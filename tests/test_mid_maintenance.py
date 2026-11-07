"""Offline REST-worker isolation and snapshot-fencing regressions."""
import copy
import json
from pathlib import Path
import tempfile
from dataclasses import FrozenInstanceError, replace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_mid_execution_audit as fixtures
from zeus.mid_maintenance import (MidMaintenance, MaintenanceRequest, MaintenanceResult,
                                  PositionRead, OrderRead, parse_orders, parse_position)

POSITION = {"assetPositions": [{"position": {"coin": "ANTH", "szi": "1",
             "entryPx": "100", "unrealizedPnl": ".2"}}], "marginSummary": {"accountValue": "100"}}


class FakeReads:
    def __init__(self, block=None):
        self.block = block
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.thread_ids = []
        self.closed_thread = None
        self.session = Mock()
        self.session.close.side_effect = self.close
        self.payloads = {"clearinghouseState": POSITION, "openOrders": [],
                         "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": "20", "hold": "2"}]}}
        self.errors = {}

    def post(self, path, payload):
        kind = payload["type"]
        if path != "/info" or kind not in self.payloads:
            raise AssertionError("Worker attempted something other than a public info read")
        self.calls.append(kind)
        self.thread_ids.append(threading.get_ident())
        if kind == self.block:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("fake read deadline")
        if kind in self.errors:
            raise self.errors[kind]
        return copy.deepcopy(self.payloads[kind])

    def close(self):
        self.closed_thread = threading.get_ident()


def poll_result(worker, revision=None):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        result = worker.poll()
        if result is not None and (revision is None or result.request.revision == revision):
            return result
        worker.wake.wait(.02)
        worker.wake.clear()
    raise AssertionError("Worker did not publish a result")


class WorkerTests(unittest.TestCase):
    def worker(self, client, **kwargs):
        worker = MidMaintenance("https://unused.invalid", "dummy-account", "io:ANTH",
                                threading.Event(), client_factory=lambda: client, **kwargs)
        self.addCleanup(lambda: (client.release.set(), worker.close()))
        return worker

    def test_reads_and_close_use_only_dedicated_thread_and_immutable_results(self):
        client = FakeReads()
        worker = self.worker(client)
        request = MaintenanceRequest("session", 1, False, True, True)
        worker.request(request)
        result = poll_result(worker)
        self.assertEqual(result.position.quantity, 1)
        self.assertEqual(result.spot_usdc, 18)
        self.assertEqual(result.orders, ())
        with self.assertRaises(FrozenInstanceError):
            result.position.quantity = 2
        worker.close()
        self.assertEqual(len(set(client.thread_ids)), 1)
        self.assertNotEqual(client.thread_ids[0], threading.get_ident())
        self.assertEqual(client.closed_thread, client.thread_ids[0])

    def test_one_active_read_and_one_coalesced_pending_request(self):
        client = FakeReads("clearinghouseState")
        worker = self.worker(client)
        worker.request(MaintenanceRequest("session", 0, False, True, False))
        self.assertTrue(client.entered.wait(1))
        for revision in range(1, 101):
            worker.request(MaintenanceRequest("session", revision, False, revision % 2 == 0, revision % 2 == 1))
        with worker.condition:
            self.assertEqual(worker.pending.revision, 100)
            self.assertTrue(worker.pending.position and worker.pending.orders)
        client.release.set()
        result = poll_result(worker, revision=100)
        self.assertFalse(result.error)
        self.assertEqual(client.calls.count("clearinghouseState"), 2)
        self.assertEqual(client.calls.count("openOrders"), 1)

    def test_timeout_or_invalid_critical_read_never_becomes_zero_state(self):
        for bad in (TimeoutError("unavailable"), ValueError("invalid JSON")):
            client = FakeReads()
            client.errors["clearinghouseState"] = bad
            worker = self.worker(client)
            worker.request(MaintenanceRequest("session", 0, False, True, True))
            result = poll_result(worker)
            self.assertTrue(result.error)
            self.assertIsNone(result.position)
            self.assertIsNone(result.orders)
            worker.close()

    def test_failed_optional_spot_read_preserves_critical_result(self):
        client = FakeReads()
        client.errors["spotClearinghouseState"] = TimeoutError("display unavailable")
        worker = self.worker(client)
        worker.request(MaintenanceRequest("session", 0, False, True, True))
        result = poll_result(worker)
        self.assertFalse(result.error)
        self.assertEqual(result.position.quantity, 1)
        self.assertIsNone(result.spot_usdc)
        self.assertTrue(result.spot_error)

    def test_stop_during_read_reaps_boundedly_and_prevents_followup_reads(self):
        client = FakeReads("clearinghouseState")
        halt = threading.Event()
        worker = self.worker(client, halt_event=halt)
        worker.request(MaintenanceRequest("session", 0, False, True, True))
        self.assertTrue(client.entered.wait(1))
        halt.set()
        self.assertFalse(worker.request(MaintenanceRequest("session", 1, False, True, True)))
        started = time.monotonic()
        self.assertFalse(worker.close())
        self.assertLess(time.monotonic() - started, 1.5)
        client.release.set()
        worker.thread.join(2)
        self.assertIsNone(worker.poll())
        self.assertEqual(client.calls, ["clearinghouseState"])

    def test_malformed_rows_rejected_and_other_dex_not_merged(self):
        for rows in (None, [{"coin":"io:ANTH","oid":1.5,"side":"A","limitPx":"100","sz":"1"}],
                     [{"coin":"io:ANTH","oid":1,"side":"A","limitPx":"NaN","sz":"1"}]):
            with self.assertRaises((ValueError, TypeError)):
                parse_orders(rows,"io:ANTH")
        self.assertEqual(parse_orders([{"coin":"xyz:ANTH"}],"io:ANTH"), ())
        with self.assertRaises(ValueError):
            parse_position({},"io:ANTH")


class OwnerTests(unittest.TestCase):
    position = fixtures.MidExecutionTests.position
    request = fixtures.MidExecutionTests.request
    install = fixtures.MidExecutionTests.install

    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        self.client = FakeReads("clearinghouseState")
        b = self.bot
        self.worker = b._maintenance = MidMaintenance("https://unused.invalid", "dummy-account", "io:ANTH",
            b.wake, b.telemetry, client_factory=lambda:self.client, halt_event=b.safety.stopped)
        now = time.monotonic()
        b._position_verified_at = b._orders_verified_at = now
        b._maintenance_position_request_at = b._maintenance_orders_request_at = now
        b.last_orderupdate_ts = b.last_quote_ts = time.time()
        b.equity = 100
        self.addCleanup(lambda:(self.client.release.set(),self.worker.close()))

    def blocked_read(self):
        b = self.bot
        with b.lock:
            fingerprint = b._orders_fingerprint_locked()
        self.worker.request(MaintenanceRequest(
            b.safety.session_id, getattr(b, "_execution_revision", 0), False,
            True, True, orders_fingerprint=fingerprint))
        self.assertTrue(self.client.entered.wait(1))

    def release_result(self):
        self.client.release.set()
        return poll_result(self.worker)

    def test_worker_does_not_mutate_trading_state_and_only_owner_applies(self):
        self.blocked_read()
        result = self.release_result()
        self.assertEqual(self.bot.position, 0)
        self.assertTrue(self.bot._apply_maintenance_result(result))
        self.assertEqual(self.bot.position, 1)
        self.assertEqual(self.bot.equity, 118)
        self.raw.bulk_orders.assert_not_called()
        self.raw.bulk_cancel.assert_not_called()

    def test_fill_overtakes_delayed_read(self):
        b = self.bot
        self.blocked_read()
        fill={"coin":"io:ANTH","px":"100","sz":".4","side":"B","tid":123,"fee":".01","closedPnl":"0","crossed":False}
        b._on_user_fills({"data":{"fills":[fill]}})
        b._apply_maintenance_result(self.release_result())
        self.assertEqual(b.position,.4)
        self.assertTrue(b.position_dirty)

    def test_modify_overtakes_delayed_read_and_keeps_new_order(self):
        b = self.bot
        b.orders={99:{"side":"B","px":99,"sz":1,"ro":False,"ts":100}}
        b.safety.owned.add(99)
        self.blocked_read()
        self.assertTrue(b.modify_quotes([(99,self.request(buy=True,size=1,ro=False))]))
        # The order component is stale and must be discarded; the position
        # component (unchanged by a modify) may still be applied.
        b._apply_maintenance_result(self.release_result())
        self.assertIn(124,b.orders)
        self.assertNotIn(99,b.orders)
        self.assertEqual(b.position,1)
        self.assertEqual(b._mid_requests_in_flight,0)

    def test_cancel_overtakes_delayed_read_and_does_not_resurrect_order(self):
        b = self.bot
        b.orders={99:{"side":"B","px":99,"sz":1,"ro":False,"ts":100}}
        b.safety.owned.add(99)
        self.client.payloads["openOrders"]=[{"coin":"io:ANTH","oid":99,"side":"B","limitPx":"99","sz":"1"}]
        self.raw.bulk_cancel.return_value=fixtures.ack(["success"])
        self.blocked_read()
        self.assertTrue(b.cancel_oids([99]))
        # Orders are fenced by the execution revision; the unchanged position
        # component remains valid and is applied independently.
        b._apply_maintenance_result(self.release_result())
        self.assertFalse(b.orders)
        self.assertEqual(b.position,1)

    def test_foreground_authoritative_read_overtakes_delayed_read(self):
        b = self.bot
        self.position(0)
        self.blocked_read()
        self.assertTrue(b.sync_position(include_spot=False))
        b._apply_maintenance_result(self.release_result())
        self.assertEqual(b.position,0)

    def test_stopped_other_session_or_inflight_results_are_discarded(self):
        b = self.bot
        request = MaintenanceRequest(b.safety.session_id,0,False,True,False)
        result = MaintenanceResult(request,position=PositionRead(2,100,0,100),position_started=time.monotonic())
        self.assertFalse(b._apply_maintenance_result(replace(result,request=replace(request,session_id="another"))))
        self.assertFalse(b._apply_maintenance_result(replace(result,request=replace(request,in_flight=True))))
        b._begin_mid_action("bulk_orders")
        self.assertFalse(b._apply_maintenance_result(replace(result,request=replace(request,revision=b._execution_revision))))
        b._end_mid_action("bulk_orders")
        b.safety.stop()
        self.assertFalse(b._apply_maintenance_result(replace(result,request=replace(request,revision=b._execution_revision))))
        self.assertEqual(b.position,0)

    def test_transient_maintenance_failure_retries_without_stopping(self):
        b = self.bot
        b.position=.4
        request = MaintenanceRequest(b.safety.session_id,0,False,True,True)
        b._apply_maintenance_result(MaintenanceResult(request,error="API unavailable"))
        self.assertEqual(b.position,.4)
        self.assertTrue(b.position_dirty)
        self.assertFalse(b.safety.account_known)
        self.assertTrue(b.running)
        self.assertFalse(b.safety.stopped.is_set())
        for _ in range(self.mod.MID_MAINTENANCE_FAIL_LIMIT - 1):
            b._apply_maintenance_result(MaintenanceResult(request,error="API unavailable"))
        self.assertFalse(b.running)
        self.assertTrue(b.safety.stopped.is_set())

    def test_unrelated_orders_excluded_and_resting_queue_preserved(self):
        b = self.bot
        b.orders={99:{"side":"A","px":101,"sz":1,"ro":True,"ts":100}}
        b.safety.owned.add(99)
        b.queue_ahead={99:7};b.queue_init={99:10}
        request = MaintenanceRequest(b.safety.session_id,0,False,False,True,
                                     orders_fingerprint=b._orders_fingerprint_locked())
        result=MaintenanceResult(request,orders=(OrderRead(99,"A",101,1),OrderRead(999,"B",99,1)),orders_started=time.monotonic())
        self.assertTrue(b._apply_maintenance_result(result))
        self.assertEqual(set(b.orders),{99})
        self.assertEqual(b.orders[99]["ts"],100)
        self.assertEqual(b.queue_ahead[99],7)

    def test_expired_verification_keeps_resting_opening_but_blocks_new_send(self):
        b = self.bot
        b._position_verified_at=b._orders_verified_at=time.monotonic()-20
        b.orders={99:{"side":"B","px":99,"sz":1,"ro":False,"ts":100}}
        b.apply_intent()
        self.raw.bulk_orders.assert_not_called();self.raw.bulk_modify_orders_new.assert_not_called()
        self.raw.bulk_cancel.assert_not_called()
        self.assertEqual(b.orders[99]["ts"],100)
        b.place_quotes([self.request(buy=True,size=1,ro=False)])
        self.raw.bulk_orders.assert_not_called()
        self.assertTrue(b._maintenance_status()["opening_blocked"])

    def test_stale_maintenance_does_not_block_verified_reduce_only_exit(self):
        b = self.bot
        b._position_verified_at=b._orders_verified_at=time.monotonic()-20
        b.position=1;self.position(.4)
        b.orders={99:{"side":"A","px":99,"sz":1,"ro":True,"ts":100}}
        b.apply_intent()
        sent=self.raw.bulk_modify_orders_new.call_args.args[0][0]["order"]
        self.assertTrue(sent["reduce_only"]);self.assertEqual(sent["sz"],.4)
        self.raw.bulk_cancel.assert_not_called()

    def test_protective_cancel_is_not_blocked_by_stale_maintenance(self):
        b = self.bot
        b._position_verified_at=b._orders_verified_at=0
        b.orders={99:{"side":"B","px":99,"sz":1,"ro":False,"ts":100}}
        b.compute_intent=Mock(return_value=(None,None,"risk gate"))
        self.raw.bulk_cancel.return_value=fixtures.ack(["success"])
        b.apply_intent()
        self.raw.bulk_cancel.assert_called_once()
        self.assertFalse(b.orders)

    def test_periodic_scheduling_has_no_foreground_rest_and_keeps_cadence(self):
        b = self.bot
        fake=Mock();fake.state.return_value={"alive":True};fake.poll.return_value=None;fake.request.return_value=True
        b._maintenance=fake
        now=time.monotonic()
        b._maintenance_position_request_at=b._maintenance_orders_request_at=now
        with patch("zeus.bots.order_book.time.monotonic",return_value=now+5.1):b._maintenance_tick()
        self.assertTrue(fake.request.call_args.args[0].position)
        self.assertFalse(fake.request.call_args.args[0].orders)
        with patch("zeus.bots.order_book.time.monotonic",return_value=now+5.2):b._maintenance_tick()
        self.assertEqual(fake.request.call_count,1)
        b.last_quote_ts=0
        with patch("zeus.bots.order_book.time.monotonic",return_value=now+10.2):b._maintenance_tick()
        self.assertTrue(fake.request.call_args.args[0].position and fake.request.call_args.args[0].orders)
        b.info.user_state.assert_not_called();b.info.open_orders.assert_not_called()

    def test_failed_maintenance_leaves_loop_before_risk_or_quote_actions(self):
        b = self.bot
        b._check_stop_file = Mock()
        b._refresh_signals = Mock()
        b._maintenance_tick = Mock(side_effect=lambda:b.safety.fail("critical read failed"))
        b.update_markouts = Mock()
        b.risk_checks = Mock()
        b.wake.set()
        b._trading_loop()
        b.risk_checks.assert_not_called()
        b.update_markouts.assert_not_called()
        self.raw.bulk_orders.assert_not_called()

    def test_transport_exception_clears_inflight_but_keeps_unknown_send(self):
        b = self.bot
        self.raw.bulk_orders.side_effect = TimeoutError("unknown execution result")
        b.place_quotes([self.request(buy=True,size=1,ro=False)])
        self.assertEqual(b._mid_requests_in_flight,0)
        self.assertEqual(len(b.safety.pending_cloids),1)
        self.raw.bulk_orders.assert_called_once()

    def test_main_decision_remains_responsive_during_blocked_maintenance(self):
        b = self.bot
        self.blocked_read()
        b.compute_intent=Mock(return_value=(None,None,"no exposure"))
        for _ in range(100):b.apply_intent()
        self.assertEqual(b.compute_intent.call_count,100)
        self.assertTrue(self.worker.state()["active"])
        self.assertFalse(self.client.release.is_set())
        b.info.user_state.assert_not_called()
        self.raw.bulk_orders.assert_not_called()

class ApiCompatibilityTests(unittest.TestCase):
    def test_grid_status_keeps_existing_fields_and_exposes_maintenance(self):
        from fastapi.testclient import TestClient
        from zeus import server
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"stats.json"
            now = time.time()
            latency = {"unit":"ms", "timings":{}, "counts":{},
                       "maintenance":{"enabled":True,"active":True,"opening_blocked":False}}
            path.write_text(json.dumps({"symbol":"io:ANTH","start_time":now-10,"ts":now,
                "phase":"running","position_known":True,"position_usd":0,"volume":100,
                "fees":.1,"rpnl":.05,"fills":4,"maker_fills":2,"taker_fills":1,
                "unknown_role_fills":1,"latency":latency,"accounting_complete":True}),encoding="utf-8")
            manager = server.BotManager.__new__(server.BotManager)
            manager.lock = threading.Lock()
            manager.grid_config = {"strategy":"mid"}
            manager._grid_stats_cache = None
            manager._orphans = Mock(return_value=[])
            proc = Mock();proc.poll.return_value=None
            manager.obi_procs = {"IO_ANTH":{"proc":proc,"symbol":"io:ANTH","started":now-10,
                "stats_file":str(path),"safety_file":None,"config":{"strategy":"mid","mode":"live"}}}
            with patch.object(server,"manager",manager),patch.object(server,"BOT_API_TOKEN",""):
                response=TestClient(server.app).get("/api/grid/status")
            self.assertEqual(response.status_code,200)
            data=response.json()
            self.assertTrue(data["running"])
            self.assertAlmostEqual(data["cost_per_1m"],500)
            self.assertEqual(data["bots"][0]["latency"],latency)
            self.assertEqual(data["bots"][0]["unknown_role_fills"],1)


if __name__=="__main__":unittest.main()
