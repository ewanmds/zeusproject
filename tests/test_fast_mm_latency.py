"""Fast MID/BBO feed, scheduling and execution tests. No real account or network."""
import math
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_mid_execution_audit as fixtures
import test_mid_maintenance as maintenance
from zeus.latency import LatencyMetrics


class FastMmTests(unittest.TestCase):
    position = fixtures.MidExecutionTests.position
    request = fixtures.MidExecutionTests.request
    install = fixtures.MidExecutionTests.install

    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        self.bot._feed_generation = 1
        self.bot.last_depth_exchange_ms = 0
        self.bot._urgent_quote = False

    def bbo(self, stamp=100, bid=100, ask=100.1, coin="io:ANTH", generation=1):
        self.bot._on_bbo({"data":{"coin":coin,"time":stamp,
            "bbo":[{"px":str(bid),"sz":"1"},{"px":str(ask),"sz":"1"}]}}, generation=generation)

    def l2(self, stamp=100, bid=100, ask=100.1, generation=1):
        self.bot._on_l2({"data":{"coin":"io:ANTH","time":stamp,
            "levels":[[{"px":str(bid),"sz":"1"}],[{"px":str(ask),"sz":"1"}]]}}, generation=generation)

    def test_bbo_wins_timestamp_tie_and_older_l2_cannot_overwrite_prices(self):
        b=self.bot
        self.l2(100,99,100)
        self.bbo(100,100,100.1)
        revision=b.book_revision
        self.l2(100,98,99)
        self.l2(99,97,98)
        self.assertEqual((b.best_bid,b.best_ask,b.book_source),(100,100.1,"bbo"))
        self.assertEqual(b.book_revision,revision)
        self.l2(101,101,101.1)
        self.assertEqual(b.best_bid,101)

    def test_depth_is_updated_independently_of_newer_top_of_book(self):
        self.bbo(102)
        self.l2(101)
        self.bot.signal.on_l2.assert_called_once()
        self.assertEqual(self.bot.last_book_exchange_ms,102)

    def test_invalid_crossed_other_dex_or_previous_connection_does_not_refresh(self):
        b=self.bot
        self.bbo()
        revision=b.book_revision
        for kwargs in ({"bid":math.nan},{"ask":math.inf},{"bid":101},
                       {"coin":"xyz:ANTH"},{"generation":0},{"stamp":0}):
            self.bbo(101,**kwargs) if "stamp" not in kwargs else self.bbo(**kwargs)
        self.l2(102,generation=0)
        self.assertEqual(b.book_revision,revision)

    def test_quiet_bbo_with_valid_depth_is_not_dead_feed(self):
        b=self.bot
        self.bbo()
        b.last_tick_ts=0
        self.l2()
        self.assertGreater(b.last_tick_ts,0)
        self.assertEqual(b.book_source,"bbo")

    def test_delayed_rest_snapshot_does_not_overwrite_new_ws_prices(self):
        b=self.bot;b.ws_ready=True
        def rest(*args):
            self.bbo(102,102,102.1)
            return {"time":101,"levels":[[{"px":"101"}],[{"px":"101.1"}]]}
        b.info.l2_snapshot.side_effect=rest
        self.assertTrue(b.refresh_bbo_rest())
        self.assertEqual(b.best_bid,102)

    def test_subscriptions_use_fast_l2_and_bbo_and_fence_old_connection(self):
        b=self.bot;b.fixed_spot_meta=None
        info=Mock()
        with patch.object(self.mod,"Info",return_value=info),patch("hyperliquid.websocket_manager.WebsocketManager"),patch.object(self.mod,"MACRO_ENABLE",False):
            b.start_ws()
            subscriptions={call.args[0]["type"]:call.args for call in info.subscribe.call_args_list}
            self.assertTrue(subscriptions["l2Book"][0]["fast"])
            self.assertIn("bbo",subscriptions)
            old=subscriptions["bbo"][1]
            b.start_ws()
            old({"data":{"coin":"io:ANTH","time":200,"bbo":[{"px":"100"},{"px":"100.1"}]}})
        self.assertEqual(b.book_revision,1)
        self.assertTrue(b.position_dirty)
        self.assertFalse(b.safety.account_known)

    def test_old_connection_account_messages_do_not_mutate_inventory_or_orders(self):
        b=self.bot;b.orders={99:{"side":"A","px":100.1,"sz":1,"ro":False}}
        b._on_order_updates({"data":[{"status":"filled","order":{"coin":"io:ANTH","oid":99}}]},generation=0)
        b._on_user_fills({"data":{"fills":[{"coin":"io:ANTH","px":"100","sz":"1","side":"B","tid":1,"fee":"0","closedPnl":"0"}]}},generation=0)
        self.assertEqual(b.position,0)
        self.assertIn(99,b.orders)

    def test_latency_samples_each_book_event_once_and_counts_burst_coalescing(self):
        b=self.bot
        b.compute_intent=Mock(return_value=(None,None,"idle"))
        self.bbo()
        for _ in range(100):b.apply_intent()
        timings=b.telemetry.snapshot()["timings"]
        self.assertEqual(timings["event_queue_delay"]["samples"],1)
        self.assertEqual(timings["event_to_decision"]["samples"],1)
        for stamp in range(101,111):self.bbo(stamp)
        b.apply_intent()
        self.assertEqual(b.telemetry.snapshot()["counts"]["coalesced_book_events"],10)
        # The fixture starts at revision=1; the first delivery superseded it too.

    def test_coalesced_action_wakes_at_deadline_instead_of_250_ms(self):
        b=self.bot;b.position=0
        b.last_action_ts=100
        with patch.object(self.mod,"ACTION_COALESCE_SEC",.08),patch.object(self.mod.time,"time",return_value=100.01),patch.object(self.mod.time,"monotonic",return_value=500):
            b.apply_intent()
            self.assertAlmostEqual(b._next_wait_timeout(),.07)
        self.raw.bulk_orders.assert_not_called()

    def test_event_to_submit_includes_deliberate_coalesce_wait(self):
        b=self.bot;b.position=0;b.book_received_ns=1_000_000_000;b.last_action_ts=100
        self.raw.bulk_orders.return_value=fixtures.ack([{"resting":{"oid":123}},{"resting":{"oid":124}}])
        with patch.object(self.mod,"ACTION_COALESCE_SEC",.08):
            with patch.object(self.mod.time,"time",return_value=100.01),patch.object(self.mod.time,"monotonic",return_value=500),patch.object(self.mod.time,"perf_counter_ns",return_value=1_000_000_000):
                b.apply_intent()
            with patch.object(self.mod.time,"time",return_value=100.09),patch.object(self.mod.time,"monotonic",return_value=500.08),patch.object(self.mod.time,"perf_counter_ns",return_value=1_080_000_000):
                b.apply_intent()
        self.raw.bulk_orders.assert_called_once()
        self.assertEqual(b.telemetry.snapshot()["timings"]["event_to_submit"]["p50"],80)

    def test_fill_conversion_is_one_verified_modify_even_inside_coalesce(self):
        b=self.bot
        b.position=-1;self.position(-.4)
        b.orders={99:{"side":"B","px":100,"sz":1,"ro":False,"ts":100}}
        b.safety.owned.add(99)
        b.info.open_orders.return_value=[{"coin":"io:ANTH","oid":99,"side":"B","sz":"1"}]
        b.last_action_ts=time.time();b._urgent_quote=True
        with patch.object(self.mod,"ACTION_COALESCE_SEC",.08):b.apply_intent()
        sent=self.raw.bulk_modify_orders_new.call_args.args[0][0]
        self.assertEqual(sent["oid"],99)
        self.assertEqual(sent["order"]["sz"],.4)
        self.assertTrue(sent["order"]["reduce_only"])
        self.assertEqual(sent["order"]["order_type"],{"limit":{"tif":"Alo"}})
        self.raw.bulk_cancel.assert_not_called();self.raw.bulk_orders.assert_not_called()
        b.info.user_state.assert_called_once();b.info.open_orders.assert_called_once()

    def test_risk_pull_bypasses_coalesce(self):
        b=self.bot
        b.orders={99:{"side":"B","px":100,"sz":1,"ro":False}}
        b.compute_intent=Mock(return_value=(None,None,"risk"))
        self.raw.bulk_cancel.return_value=fixtures.ack(["success"])
        b.last_action_ts=time.time()
        with patch.object(self.mod,"ACTION_COALESCE_SEC",.08):b.apply_intent()
        self.raw.bulk_cancel.assert_called_once()

    def test_local_reduce_only_skip_does_not_trigger_cancel_place_fallback(self):
        b=self.bot;b.position=1;self.position(0)
        b.orders={99:{"side":"A","px":101,"sz":1,"ro":True}}
        b.apply_intent()
        self.assertNotIn("_modify_block_until",b.__dict__)
        self.assertIn(99,b.orders)
        self.raw.bulk_cancel.assert_not_called();self.raw.bulk_orders.assert_not_called()
        self.raw.bulk_modify_orders_new.assert_not_called()

    def test_stop_while_modify_is_inflight_prevents_any_followup_submission(self):
        b=self.bot;b.position=-1;self.position(-1)
        b.orders={99:{"side":"B","px":100,"sz":1,"ro":False},
                  98:{"side":"A","px":100.1,"sz":1,"ro":False}}
        b.safety.owned.update((98,99))
        b.safety.is_running=lambda:b.running and not Path(b._stop_file).exists()
        entered,release=threading.Event(),threading.Event()
        def delayed(rows):
            entered.set();release.wait(2)
            return fixtures.ack([{"resting":{"oid":124}}])
        self.raw.bulk_modify_orders_new.side_effect=delayed
        thread=threading.Thread(target=b.apply_intent)
        try:
            thread.start();self.assertTrue(entered.wait(1))
            Path(b._stop_file).touch()
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.raw.bulk_modify_orders_new.assert_called_once()
        self.raw.bulk_orders.assert_not_called();self.raw.bulk_cancel.assert_not_called()
        self.assertFalse(b.running)

    def test_feed_burst_during_slow_ack_keeps_only_latest_target(self):
        b=self.bot;b.position=-1;self.position(-1)
        b.orders={99:{"side":"B","px":100,"sz":1,"ro":False}}
        b.safety.owned.add(99)
        entered,release=threading.Event(),threading.Event()
        def delayed(rows):
            entered.set();release.wait(2)
            return fixtures.ack([{"resting":{"oid":124}}])
        self.raw.bulk_modify_orders_new.side_effect=delayed
        thread=threading.Thread(target=b.apply_intent)
        try:
            thread.start();self.assertTrue(entered.wait(1))
            for stamp in range(101,601):self.bbo(stamp,100+stamp/1000,101+stamp/1000)
            self.raw.bulk_modify_orders_new.assert_called_once()
            self.raw.bulk_orders.assert_not_called()
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.raw.bulk_modify_orders_new.side_effect=None
        self.raw.bulk_modify_orders_new.return_value=fixtures.ack([{"resting":{"oid":125}}])
        b.apply_intent()
        self.assertEqual(self.raw.bulk_modify_orders_new.call_count,2)
        sent=self.raw.bulk_modify_orders_new.call_args.args[0][0]["order"]
        self.assertEqual(sent["limit_px"],b.compute_intent()[0][0])
        self.raw.bulk_cancel.assert_not_called()

    def test_bbo_final_submission_uses_latest_touch_after_rest_preflight(self):
        b=self.bot;b.position=1
        with patch.object(self.mod,"STRATEGY","bbo"):
            state=b.info.user_state.return_value
            b.info.user_state.side_effect=lambda *a,**k:(self.bbo(102,102,102.1) or state)
            b.place_quotes([self.request()])
        sent=self.raw.bulk_orders.call_args.args[0][0]
        self.assertEqual(sent["limit_px"],102.1)
        self.assertTrue(sent["reduce_only"])

    def test_p90_keeps_existing_percentiles(self):
        m=LatencyMetrics()
        for i in range(100):m.observe("test",i)
        result=m.snapshot()["timings"]["test"]
        self.assertEqual(result,{"samples":100,"p50":49,"p90":89,"p95":94,"p99":98})

    def test_sdk_signing_and_http_are_separate_and_payload_is_preserved(self):
        coordinator=self.coordinator
        class FakeSDK:
            def post(self,path,payload=None):
                self.payload=payload
                return fixtures.ack([{"resting":{"oid":5}}])
            def bulk_orders(self,rows):
                return self.post("/exchange",{"orders":rows,"signature":"fake"})
        raw=FakeSDK();coordinator.instrument_http(raw)
        rows=[{"coin":"io:ANTH"}]
        coordinator.wrap(raw).bulk_orders(rows)
        self.assertIs(raw.payload["orders"],rows)
        timings=self.bot.telemetry.snapshot()["timings"]
        self.assertEqual(timings["sdk_prepare_sign"]["samples"],1)
        self.assertEqual(timings["exchange_http"]["samples"],1)
        self.assertEqual(timings["sdk_to_response"]["samples"],1)


class BboFastMmTests(FastMmTests):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.mod,"STRATEGY","bbo"))


class BboMaintenanceTests(maintenance.OwnerTests):
    """Run the same delayed read/fill/modify/cancel/stop safety cases for BBO."""
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.mod,"STRATEGY","bbo"))

    def test_periodic_orders_not_postponed_indefinitely_by_frequent_quotes(self):
        b=self.bot
        fake=Mock();fake.state.return_value={"alive":True};fake.poll.return_value=None
        fake.request.return_value=True;b._maintenance=fake
        now=time.monotonic()
        b._maintenance_position_request_at=now
        b._maintenance_orders_request_at=now-11
        b.last_quote_ts=time.time()
        b._maintenance_tick()
        self.assertTrue(fake.request.call_args.args[0].orders)
        self.assertFalse(fake.request.call_args.args[0].position)
        b.info.open_orders.assert_not_called()


if __name__=="__main__":unittest.main()
