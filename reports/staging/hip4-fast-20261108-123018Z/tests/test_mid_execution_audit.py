"""MID-only execution, event freshness, and bounded publication regressions."""
import math
from collections import deque
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import test_reduce_only_preflight as fixtures
from zeus.latency import LatencyMetrics
from zeus.signing import SigningCoordinator
from zeus.snapshot_writer import LatestSnapshotWriter


def ack(statuses):return {"status":"ok","response":{"data":{"statuses":statuses}}}

class MidExecutionTests(unittest.TestCase):
    position=fixtures.ReduceOnlyPreflightTests.position
    request=fixtures.ReduceOnlyPreflightTests.request
    def setUp(self):
        fixtures.ReduceOnlyPreflightTests.setUp(self)
        self.enterContext(patch.multiple(self.mod,STRATEGY="mid",MID_OFFSET_BPS=2.0,SESSION_WINDOWS_UTC=[],ACTION_COALESCE_SEC=0))
        b=self.bot;b.telemetry=LatencyMetrics();b.size_mult=1;b.hourly_pause_until=b.pause_until=0
        b.last_action_ts=0;b.last_tick_ts=time.time();b.book_received_ns=time.perf_counter_ns();b.book_revision=1
        b.last_book_exchange_ms=0;b._submit_position_revision=None;b.order_cancellations=0
        self.directory=self.enterContext(tempfile.TemporaryDirectory())
        b._stop_file=str(Path(self.directory)/"stop.flag")
        coordinator=SigningCoordinator(Path(self.directory)/"signer","dummy",b.telemetry)
        self.coordinator=coordinator
        self.install()
    def install(self):
        b=self.bot
        b.exchange=b.safety.wrap(self.coordinator.wrap(self.raw,before_send=b._prepare_mid_submission,after_send=b._record_mid_response,
            before_action=b._begin_mid_action,after_action=b._end_mid_action),
            reconcile_cancel=b._reconcile_missing_cancels,reconcile_reduce_only=b._reconcile_reduce_only,
            reconcile_post_only=b._reconcile_post_only,reconcile_modify=b._reconcile_missing_modifies)
    def changed_book(self):
        b=self.bot;b.best_bid+=1;b.best_ask+=1;b.mid=(b.best_bid+b.best_ask)/2
        b.book_revision+=1;b.book_received_ns=time.perf_counter_ns()
    def test_mid_ro_exit_uses_modify_and_tracks_final_price(self):
        b=self.bot;b.position=1;self.position(1)
        b.orders={99:{"side":"A","px":99,"sz":1,"ro":True,"ts":100}}
        self.raw.bulk_modify_orders_new.return_value=ack([{"resting":{"oid":124}}])
        state=b.info.user_state.return_value
        b.info.user_state.side_effect=lambda *a,**k:(self.changed_book() or state)
        b.apply_intent()
        self.raw.bulk_cancel.assert_not_called();self.raw.bulk_orders.assert_not_called()
        sent=self.raw.bulk_modify_orders_new.call_args.args[0][0]["order"]
        self.assertTrue(sent["reduce_only"])
        self.assertEqual(sent["order_type"]["limit"]["tif"],"Alo")
        self.assertEqual(sent["limit_px"],b.compute_mid_intent()[1][0])
        self.assertEqual(b.orders[124]["px"],sent["limit_px"])
        self.assertEqual(b.order_requests,1)
    def test_exit_preflight_skips_spot_display_read_but_keeps_inventory_checks(self):
        b=self.bot
        b.place_quotes([self.request()])
        b.info.user_state.assert_called_once()
        b.info.open_orders.assert_called_once()
        b.info.spot_user_state.assert_not_called()
        b.sync_position()
        b.info.spot_user_state.assert_called_once()

    def test_bank_window_prices_mid_exit_inside_the_spread(self):
        # A bank/stop window must actually move the exit, not only block
        # openings. The most aggressive valid post-only exit is one tick
        # inside the spread (below the ask / above the bid).
        b=self.bot;b.bank_until=time.time()+60;b._hard_exit_at=time.time()+60
        with patch.object(self.mod,"MID_OFFSET_BPS",50):
            tick=self.mod.dyn_tick(100.05)
            self.assertEqual(b._execution_price(False,True,100,100.1,tick),
                             self.mod.px_up(100+tick,tick))
            self.assertEqual(b._execution_price(True,True,100,100.1,tick),
                             self.mod.px_down(100.1-tick,tick))

    def test_partial_fill_reduces_modified_exit_quantity(self):
        b=self.bot;b.position=1;self.position(.4)
        b.orders={99:{"side":"A","px":99,"sz":1,"ro":True,"ts":100}}
        b.apply_intent()
        sent=self.raw.bulk_modify_orders_new.call_args.args[0][0]["order"]
        self.assertEqual(sent["sz"],.4);self.assertTrue(sent["reduce_only"])
    def test_unchanged_quote_preserves_age_and_queue(self):
        b=self.bot;b.position=1
        ask=b.compute_mid_intent()[1]
        b.orders={99:{"side":"A","px":ask[0],"sz":ask[1],"ro":True,"ts":100}}
        b.queue_ahead={99:7}
        b.apply_intent()
        self.raw.bulk_orders.assert_not_called();self.raw.bulk_modify_orders_new.assert_not_called();self.raw.bulk_cancel.assert_not_called()
        self.assertEqual(b.orders[99]["ts"],100);self.assertEqual(b.queue_ahead[99],7)
    def test_stop_during_rest_preflight_sends_nothing(self):
        b=self.bot;state=b.info.user_state.return_value
        b.info.user_state.side_effect=lambda *a,**k:(Path(b._stop_file).touch() or state)
        b.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()
        self.assertFalse(b.safety.pending_cloids)
        self.assertFalse(b.safety.error)
        self.assertTrue(b.safety.stopped.is_set())
    def test_fill_before_signing_defers_without_unknown_cloid(self):
        b=self.bot
        original=b._prepare_mid_submission
        def gate(name,args,kwargs):b.position_revision+=1;original(name,args,kwargs)
        b.exchange=b.safety.wrap(self.coordinator.wrap(self.raw,before_send=gate))
        b.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called()
        self.assertTrue(b.running);self.assertFalse(b.safety.pending_cloids);self.assertFalse(b.safety.error)
    def test_stale_book_defers_without_error_or_order(self):
        b=self.bot;b.book_received_ns=time.perf_counter_ns()-int(20e9)
        b.place_quotes([self.request()])
        self.raw.bulk_orders.assert_not_called();self.assertFalse(b.safety.pending_cloids);self.assertTrue(b.running)
    def test_target_returns_to_resting_price_during_preflight(self):
        b=self.bot;b.position=1
        target=b.compute_mid_intent()[1][0]
        b.orders={99:{"side":"A","px":target,"sz":1,"ro":True,"ts":100}}
        b._submit_position_revision=b.position_revision
        with self.assertRaisesRegex(Exception,"already matches"):
            b.exchange.bulk_modify_orders_new([{"oid":99,"order":self.request(size=1)}])
        self.raw.bulk_modify_orders_new.assert_not_called();self.assertFalse(b.safety.pending_cloids)
        self.assertEqual(b.orders[99]["ts"],100)
    def test_old_or_nonfinite_book_does_not_refresh_feed(self):
        b=self.bot;b.last_book_exchange_ms=100
        old=b.book_revision
        for bid,ask,stamp in [(100,101,99),(math.nan,101,101),(100,math.inf,101)]:
            b._on_l2({"data":{"time":stamp,"levels":[[{"px":str(bid)}],[{"px":str(ask)}]]}})
        self.assertEqual(b.book_revision,old)
        b.signal.on_l2.assert_not_called()
    def test_manual_orders_are_not_adopted_by_mid(self):
        b=self.bot;b.info.open_orders.return_value=[{"coin":"io:ANTH","oid":999,"side":"B","limitPx":"99","sz":"1"}]
        b.reconcile_orders(force=True)
        self.assertFalse(b.orders)
        b._on_order_updates({"data":[{"status":"open","order":{"coin":"io:ANTH","oid":999,"side":"B","limitPx":"99","sz":"1"}}]})
        self.assertFalse(b.orders)
    def test_fill_overtaking_order_snapshot_keeps_newer_tracking(self):
        b=self.bot;b.safety.owned.add(99)
        b.orders={99:{"side":"A","px":100,"sz":1,"ro":True,"ts":100}}
        def snapshot(*a,**k):
            b.position_revision+=1;b.orders.clear()
            return [{"coin":"io:ANTH","oid":99,"side":"A","limitPx":"100","sz":"1"}]
        b.info.open_orders.side_effect=snapshot
        b.reconcile_orders(force=True)
        self.assertFalse(b.orders)
    def test_post_only_mixed_batch_keeps_other_quote_and_session(self):
        b=self.bot;b.position=0;self.position(0)
        b.orders={10:{"side":"B","px":99,"sz":1,"ro":False,"ts":100},20:{"side":"A","px":101,"sz":1,"ro":False,"ts":100}}
        b.safety.owned.update((10,20))
        def modify(rows):
            req=rows[0]["order"]
            b.info.open_orders.return_value=[{"coin":"io:ANTH","oid":124,"side":"B","limitPx":str(req["limit_px"]),"sz":"1"},{"coin":"io:ANTH","oid":20,"side":"A","limitPx":"101","sz":"1"}]
            return ack([{"resting":{"oid":124}},{"error":"Post only order would have immediately matched, bbo was 100@100.1."}])
        self.raw.bulk_modify_orders_new.side_effect=modify
        self.assertTrue(b.modify_quotes([(10,self.request(buy=True,size=1,ro=False)),(20,self.request(size=1,ro=False))]))
        self.assertTrue(b.running);self.assertFalse(b.safety.error)
        self.assertIn(124,b.orders);self.assertIn(20,b.orders)
        self.raw.bulk_cancel.assert_not_called()

class FillAccountingTests(unittest.TestCase):
    position=MidExecutionTests.position
    install=MidExecutionTests.install
    def setUp(self):
        MidExecutionTests.setUp(self)
        b=self.bot;b.position=0;b.processed_fills=set();b.pending_markouts=deque()
        b.last_side_fill_ts={"B":0,"A":0};b.side_fills={"B":deque(),"A":deque()}
        b.side_pause_until={"B":0,"A":0};b.total_volume=b.total_fees=b.session_pnl=0
        b.hourly_net_events=deque();b.fills_count=b.maker_fills=b.taker_fills=b.unknown_role_fills=0
        b.accounting_complete=True
    def test_unknown_role_does_not_count_as_maker(self):
        b=self.bot
        base={"coin":"io:ANTH","px":"100","sz":".1","side":"B","fee":".01","closedPnl":"0"}
        b._on_user_fills({"data":{"fills":[{**base,"tid":1},{**base,"tid":2,"crossed":False},{**base,"tid":3,"crossed":True}]}})
        self.assertEqual((b.fills_count,b.maker_fills,b.taker_fills,b.unknown_role_fills),(3,1,1,1))
        self.assertAlmostEqual(b.total_fees,.03)
    def test_malformed_fee_does_not_partially_apply_or_deduplicate_fill(self):
        b=self.bot
        fill={"coin":"io:ANTH","px":"100","sz":".1","side":"B","tid":10,"fee":"bad","closedPnl":"0"}
        b._on_user_fills({"data":{"fills":[fill]}})
        self.assertEqual(b.position,0);self.assertNotIn("10",b.processed_fills)
        self.assertTrue(b.position_dirty);self.assertFalse(b.accounting_complete)
        b._on_user_fills({"data":{"fills":[{**fill,"fee":".01","crossed":False}]}})
        self.assertEqual(b.position,.1);self.assertEqual(b.fills_count,1)
    def test_duplicate_fill_is_applied_once(self):
        b=self.bot
        fill={"coin":"io:ANTH","px":"100","sz":".1","side":"B","tid":10,"fee":".01","closedPnl":"0","crossed":False}
        b._on_user_fills({"data":{"fills":[fill,fill]}})
        self.assertEqual(b.fills_count,1);self.assertEqual(b.position,.1)
    def test_missing_fee_marks_accounting_incomplete(self):
        self.bot._on_user_fills({"data":{"fills":[{"coin":"io:ANTH","px":"100","sz":".1","side":"B","tid":10}]}})
        self.assertFalse(self.bot.accounting_complete)


class PublicationTests(unittest.TestCase):
    def test_metrics_are_bounded_and_ignore_invalid_samples(self):
        m=LatencyMetrics(capacity=4)
        for i in range(100):m.observe("decision",i)
        m.observe("decision",math.nan)
        self.assertEqual(m.snapshot()["timings"]["decision"]["samples"],4)
    def test_slow_stats_writer_coalesces_and_preserves_final(self):
        started=threading.Event();release=threading.Event();written=[]
        def publish(path,data):
            started.set();release.wait(2);written.append(data)
        writer=LatestSnapshotWriter("unused",publish=publish)
        try:
            writer.submit({"n":0});self.assertTrue(started.wait(1))
            for i in range(1,1000):writer.submit({"n":i})
            self.assertEqual(writer.pending,{"n":999})
            release.set();self.assertTrue(writer.close({"running":False}))
            self.assertEqual(written[-1],{"running":False})
            self.assertLessEqual(len(written),3)
        finally:release.set()

if __name__=="__main__":unittest.main()
