"""Economic exit policy and recovered execution/accounting failures, offline."""
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

import test_mid_execution_audit as fixtures
from zeus.maker_exit import exit_price
from zeus.session_accounting import summarize_fills


class ExitPriceTests(unittest.TestCase):
    def test_short_exit_covers_observed_fees_plus_tick(self):
        px=exit_price(1700.9,True,1700.7,.1,.0000288,.0000288)
        self.assertAlmostEqual(px,1700.5)
        self.assertGreaterEqual(1700.7-px-1700.7*.0000288-px*.0000288,.1)
    def test_long_exit_covers_fees_plus_tick(self):
        px=exit_price(1699.9,False,1700.7,.1,.0000288,.0000288)
        self.assertGreaterEqual(px-1700.7-1700.7*.0000288-px*.0000288,.1)
    def test_already_better_mid_price_is_preserved(self):
        self.assertEqual(exit_price(99,True,100,.1,.001,.001),99)
        self.assertEqual(exit_price(101,False,100,.1,.001,.001),101)
    def test_unknown_or_invalid_costs_never_assume_free_trading(self):
        for bad in (None,math.nan,-.01,1):
            self.assertIsNone(exit_price(100,True,101,.1,bad,.001))


class RuntimePolicyTests(unittest.TestCase):
    position=fixtures.MidExecutionTests.position
    request=fixtures.MidExecutionTests.request
    install=fixtures.MidExecutionTests.install
    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b=self.bot;b.entry_price=100;b.pos_since=time.time();b._hard_exit_reason=""
        b._observed_entry_rate=b._observed_maker_rate=.001
        self.enterContext(patch.multiple(self.mod,MID_EXIT_PROTECTION=True,MID_EXIT_PROTECT_SEC=30))
    def test_protection_only_changes_mid_reduce_only_prices(self):
        b=self.bot
        self.assertLess(b._execution_price(True,True,101,101.1,.1),100)
        self.assertGreater(b._execution_price(True,False,101,101.1,.1),100)
        with patch.object(self.mod,"STRATEGY","bbo"):
            self.assertEqual(b._execution_price(True,True,101,101.1,.1),101)
    def test_post_protection_exit_uses_the_profit_anchor(self):
        b=self.bot;b.pos_since=time.time()-31
        # Short exit (buy) is anchored at entry - PROFIT_TICKS*tick.
        self.assertEqual(b._execution_price(True,True,101,101.1,.1),99.8)
    def test_long_post_protection_anchor_is_entry_plus_profit(self):
        b=self.bot;b.entry_price=100;b.pos_since=time.time()-31
        self.assertEqual(b._execution_price(False,True,100,100.1,.1),100.2)
    def test_aged_exit_holds_the_profit_anchor_in_normal_mode(self):
        b=self.bot;b.pos_since=time.time()-61
        # Normal mode never follows the touch into a realised loss: after any
        # age the short exit stays at the fee-aware profit anchor.
        self.assertEqual(b._execution_price(True,True,101,101.1,.1),99.8)
    def test_legacy_aged_exit_joins_the_touch_when_loss_cuts_are_enabled(self):
        b=self.bot;b.pos_since=time.time()-61
        with patch.object(self.mod,"MID_LOSS_CUTS_ENABLED",True):
            self.assertEqual(b._execution_price(True,True,101,101.1,.1),101.0)
    def test_existing_risk_exit_takes_priority_over_profit_floor(self):
        b=self.bot;b._hard_exit_reason="stop 6bps"
        # A stop is the most aggressive valid post-only price: inside the
        # spread, not the touch and never the mid-offset price.
        self.assertEqual(b._execution_price(True,True,101,101.1,.1),101.0)
        self.assertEqual(b._execution_price(False,True,100,100.1,.1),100.1)
        self.raw.order.assert_not_called()
    def test_real_opening_fill_supplies_fee_estimate(self):
        b=self.bot
        del b._observed_entry_rate,b._observed_maker_rate
        b._on_user_fills({"data":{"fills":[{"coin":"io:ANTH","oid":5,"tid":10,"px":"100","sz":".4","side":"A","fee":".004","closedPnl":"0","crossed":False}]}})
        self.assertAlmostEqual(b._observed_entry_rate,.0001)
        self.assertAlmostEqual(b._observed_maker_rate,.0001)


class InvalidModifyTests(unittest.TestCase):
    position=fixtures.MidExecutionTests.position
    request=fixtures.MidExecutionTests.request
    install=fixtures.MidExecutionTests.install
    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b=self.bot;self.position(0)
        b.orders={99:{"side":"B","px":98,"sz":1,"ro":False}}
        b.safety.owned.add(99)
        b.exchange=b.safety.wrap(self.coordinator.wrap(self.raw,before_send=b._prepare_mid_submission,
            before_action=b._begin_mid_action,after_action=b._end_mid_action),
            reconcile_invalid_modify=b._reconcile_invalid_modifies)
        self.raw.bulk_modify_orders_new.return_value=fixtures.ack([{"error":"Attempted to modify to invalid new order"}])
        b.info.frontend_open_orders.return_value=[{"coin":"io:ANTH","oid":99,"side":"B","limitPx":"98","sz":"1"}]
    def test_exact_reject_reconciles_without_stop_or_liquidation(self):
        b=self.bot
        self.assertTrue(b.modify_quotes([(99,self.request(buy=True,size=1,ro=False))]))
        self.assertTrue(b.running);self.assertFalse(b.safety.error)
        self.assertIn(99,b.orders);self.assertEqual(b._invalid_modify_oids,{99})
        self.assertFalse(b.safety.pending_cloids)
        self.raw.order.assert_not_called();self.raw.bulk_cancel.assert_not_called()
    def test_next_decision_cancels_verified_old_order_then_places_fresh_maker(self):
        b=self.bot
        b.modify_quotes([(99,self.request(buy=True,size=1,ro=False))])
        self.raw.bulk_cancel.return_value=fixtures.ack(["success"])
        b.compute_intent=Mock(return_value=((99,1,False),None,"new quote"))
        b.apply_intent()
        self.raw.bulk_modify_orders_new.assert_called_once()
        self.raw.bulk_cancel.assert_called_once_with([{"coin":"io:ANTH","oid":99}])
        self.raw.bulk_orders.assert_called_once()
        sent=self.raw.bulk_orders.call_args.args[0][0]
        self.assertEqual(sent["order_type"],{"limit":{"tif":"Alo"}})
        self.raw.order.assert_not_called()
    def test_unavailable_verification_remains_blocked(self):
        b=self.bot;b.info.user_state.side_effect=TimeoutError("offline")
        self.assertFalse(b.modify_quotes([(99,self.request(buy=True,size=1,ro=False))]))
        self.assertTrue(b.safety.stopped.is_set())
        self.raw.bulk_orders.assert_not_called()
    def test_other_invalid_error_is_not_silently_swallowed(self):
        self.raw.bulk_modify_orders_new.return_value=fixtures.ack([{"error":"invalid signature"}])
        self.assertFalse(self.bot.modify_quotes([(99,self.request(buy=True,size=1,ro=False))]))
        self.assertTrue(self.bot.safety.stopped.is_set())
    def test_mixed_response_preserves_accepted_other_side(self):
        b=self.bot;b.orders[98]={"side":"A","px":102,"sz":1,"ro":False};b.safety.owned.add(98)
        b.info.frontend_open_orders.return_value=[{"coin":"io:ANTH","oid":124,"side":"B","limitPx":"100","sz":"1"},
            {"coin":"io:ANTH","oid":98,"side":"A","limitPx":"102","sz":"1"}]
        self.raw.bulk_modify_orders_new.return_value=fixtures.ack([{"resting":{"oid":124}},
            {"error":"Attempted to modify to invalid new order"}])
        self.assertTrue(b.modify_quotes([(99,self.request(buy=True,size=1,ro=False)),(98,self.request(size=1,ro=False))]))
        self.assertIn(124,b.orders);self.assertIn(98,b.orders)
        self.assertEqual(b._invalid_modify_oids,{98})
        self.raw.bulk_cancel.assert_not_called()


def fill(oid,tid,side="A",size=".5",price="100",fee=".01",pnl="0",crossed=False):
    return {"coin":"io:ANTH","oid":oid,"tid":tid,"side":side,"px":price,"sz":size,
            "fee":fee,"closedPnl":pnl,"crossed":crossed,"feeToken":"USDC"}


class FinalAccountingTests(unittest.TestCase):
    position=fixtures.MidExecutionTests.position
    request=fixtures.MidExecutionTests.request
    install=fixtures.MidExecutionTests.install
    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b=self.bot;b.start_time=time.time()-20;b.safety.exposure_started=True
        b.safety.cleanup_confirmed=True;b.safety.owned.update((101,102))
        b._expected_cleanup_fills={102:.5}
    def test_cleanup_taker_fills_and_fees_are_in_final_totals_once(self):
        b=self.bot;b.position=-.5;b.entry_price=100;b.unrealized_pnl=1
        rows=[fill(101,1),fill(102,2,side="B",price="101",fee=".03",pnl="-.5",crossed=True),fill(999,3,fee="10")]
        api=Mock();api.post.return_value=rows
        with patch("hyperliquid.api.API",return_value=api):b._final_accounting(3)
        self.assertEqual(b.position,0);self.assertEqual(b.unrealized_pnl,0)
        self.assertAlmostEqual(b.total_fees,.04);self.assertAlmostEqual(b.session_pnl,-.5)
        self.assertEqual((b.fills_count,b.maker_fills,b.taker_fills),(2,1,1))
        b._on_user_fills({"data":{"fills":[rows[1]]}})
        self.assertEqual(b.fills_count,2);self.assertEqual(b.position,0)
        api.session.close.assert_called_once()
    def test_missing_closing_history_is_unknown_not_a_clean_pnl(self):
        b=self.bot;api=Mock();api.post.return_value=[fill(101,1)]
        with patch("hyperliquid.api.API",return_value=api):b._final_accounting(3)
        self.assertFalse(b.accounting_complete)
        self.assertTrue(b._accounting_error)
        self.assertEqual(b.position,0)
    def test_missing_previously_received_fill_also_blocks_complete_accounting(self):
        b=self.bot;b._expected_cleanup_fills={};b._received_fills={"1":(101,.5)}
        api=Mock();api.post.return_value=[]
        with patch("hyperliquid.api.API",return_value=api):b._final_accounting(3)
        self.assertFalse(b.accounting_complete)
    def test_current_connection_delivers_cleanup_fills_after_stop(self):
        b=self.bot;b.fixed_spot_meta=None;info=Mock()
        with patch.object(self.mod,"Info",return_value=info),patch("hyperliquid.websocket_manager.WebsocketManager"),patch.object(self.mod,"MACRO_ENABLE",False):
            b.start_ws()
        callback=next(call.args[1] for call in info.subscribe.call_args_list if call.args[0]["type"]=="userFills")
        b.running=False;b.position=-.5;b.entry_price=100
        callback({"data":{"fills":[fill(102,1,side="B",crossed=True)]}})
        self.assertEqual(b.fills_count,1);self.assertEqual(b.position,0)
    def test_api_failure_preserves_prior_financial_values_but_marks_incomplete(self):
        b=self.bot;b.total_fees=.12;api=Mock();api.post.side_effect=TimeoutError("offline")
        with patch("hyperliquid.api.API",return_value=api):b._final_accounting(3)
        self.assertEqual(b.total_fees,.12);self.assertFalse(b.accounting_complete)
    def test_no_budget_does_not_start_a_read(self):
        with patch("hyperliquid.api.API") as api:self.bot._final_accounting(0)
        api.assert_not_called();self.assertFalse(self.bot.accounting_complete)
    def test_filled_ack_records_ownership_and_expected_fill_before_final_read(self):
        b=self.bot;b.info.l2_snapshot.return_value={"levels":[[{"px":"100"}],[{"px":"100.1"}]]}
        self.raw.order.return_value=fixtures.ack([{"filled":{"oid":103,"totalSz":".4","avgPx":"100.1"}}])
        with b.safety.cleanup_scope():b._cleanup_close("io:ANTH",-.4)
        self.assertIn(103,b.safety.owned);self.assertEqual(b._expected_cleanup_fills[103],.4)
    def test_conflicting_or_truncated_fills_are_rejected(self):
        for rows in ([fill(101,1),fill(101,1,fee=".1")],[fill(101,1)]*2000):
            with self.assertRaises(ValueError):summarize_fills(rows,"io:ANTH",{101})


class FinalApiTests(unittest.TestCase):
    def test_mid_exit_configuration_is_passed_to_child_and_persisted(self):
        from zeus import server
        for enabled in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                manager=server.BotManager.__new__(server.BotManager);manager.lock=threading.Lock()
                manager.obi_procs={};manager._reserve_session=Mock(return_value=None)
                module=Mock();module.resolve_token.return_value="io:ANTH"
                module.stop_file_path.return_value=str(Path(directory)/"stop.flag")
                module.stats_file_path.return_value=str(Path(directory)/"stats.json")
                proc=Mock();proc.poll.return_value=None
                config=server.GridStartRequest(token="IO_ANTH",strategy="mid",mid_protect_exits=enabled)
                with patch.object(server,"_safe_import",return_value=module),patch.object(server.subprocess,"Popen",return_value=proc) as spawn,patch.object(server.time,"sleep"):
                    manager.start_grid(config)
                command=spawn.call_args.args[0]
                self.assertIn("--protect-mid-exits" if enabled else "--strict-mid-exits",command)
                self.assertIn("--mid-exit-protect-sec",command)
                config.mid_protect_exits=not enabled
                self.assertEqual(manager.obi_procs["IO_ANTH"]["config"]["mid_protect_exits"],enabled)

    def test_invalid_exit_horizon_rejected_before_any_spawn(self):
        from zeus import server
        manager=server.BotManager.__new__(server.BotManager)
        for value in (0,121,math.nan,math.inf):
            with self.assertRaises(ValueError):
                manager.start_grid(server.GridStartRequest(strategy="mid",mid_exit_protect_sec=value))

    def test_closed_session_is_flat_duration_frozen_and_incomplete_money_hidden(self):
        from fastapi.testclient import TestClient
        from zeus import server
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"stats.json";now=time.time()
            path.write_text(json.dumps({"symbol":"io:ANTH","start_time":now-100,"end_time":now-90,
                "ts":now-90,"phase":"stopped","cleanup_confirmed":True,"position_known":True,
                "position_usd":-100,"upnl":1,"volume":100,"fees":.1,"rpnl":0,"accounting_complete":False}))
            manager=server.BotManager.__new__(server.BotManager);manager.lock=threading.Lock()
            manager.grid_config={};manager._grid_stats_cache=None;manager._orphans=Mock(return_value=[])
            proc=Mock();proc.poll.return_value=0
            manager.obi_procs={"IO_ANTH":{"proc":proc,"symbol":"io:ANTH","started":now-100,
                "stats_file":str(path),"safety_file":None,"config":{"strategy":"mid","mode":"live"}}}
            with patch.object(server,"manager",manager),patch.object(server,"BOT_API_TOKEN",""):
                data=TestClient(server.app).get("/api/grid/status").json()
            bot=data["bots"][0]
            self.assertEqual(bot["duration"],"00:00:10");self.assertEqual(data["duration"],"00:00:10")
            self.assertEqual(bot["position_usd"],0);self.assertEqual(bot["upnl"],0)
            self.assertIsNone(bot["net_pnl"]);self.assertIsNone(data["net_pnl"]);self.assertIsNone(data["cost_per_1m"])


if __name__=="__main__":unittest.main()
