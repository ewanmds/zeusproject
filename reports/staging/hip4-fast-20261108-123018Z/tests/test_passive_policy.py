"""Execution-layer maker contract and explicit manual-stop close capability."""
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from zeus.safety import SafetyLatch,SubmissionSkipped,ExecutionStopped


def ack(oid=1):
    return {"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":oid}}]}}}


def order(tif="Alo",reduce_only=False):
    return {"coin":"io:ANTH","is_buy":True,"sz":.1,"limit_px":100,
            "order_type":{"limit":{"tif":tif}},"reduce_only":reduce_only}


class PassivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.gate=SafetyLatch(passive_only=True)
        self.gate.phase="running"
        self.raw=Mock()
        self.raw.bulk_orders.return_value=ack()
        self.raw.order.return_value=ack()
        self.raw.modify_order.return_value=ack()
        self.raw.bulk_modify_orders_new.return_value=ack()
        self.exchange=self.gate.wrap(self.raw)

    def test_default_policy_does_not_change_outcome_taker_compatibility(self):
        gate=SafetyLatch();raw=Mock();raw.bulk_orders.return_value=ack()
        gate.wrap(raw).bulk_orders([order("Ioc")])
        raw.bulk_orders.assert_called_once()
        self.assertFalse(gate.passive_only)

    def test_regular_and_reduce_only_alo_remain_allowed(self):
        for ro in (False,True):self.exchange.bulk_orders([order(reduce_only=ro)])
        self.assertEqual(self.raw.bulk_orders.call_count,2)
        self.assertFalse(self.gate.stopped.is_set())

    def test_non_passive_batch_rejected_before_identity_or_journal(self):
        for tif in ("Ioc","Gtc",None,"alo"):
            req=order(tif)
            with self.subTest(tif=tif),patch.object(self.gate,"publish") as publish:
                with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([req])
                publish.assert_not_called()
            self.assertNotIn("cloid",req)
        self.raw.bulk_orders.assert_not_called()
        self.assertFalse(self.gate.pending_cloids or self.gate.cloids)
        self.assertFalse(self.gate.exposure_started)
        self.assertFalse(self.gate.error)

    def test_mixed_batch_is_rejected_whole_before_any_send(self):
        requests=[order(),order("Gtc",True)]
        with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders(requests)
        self.assertTrue(all("cloid" not in req for req in requests))
        self.raw.bulk_orders.assert_not_called()

    def test_trigger_and_implicit_order_types_are_never_passive(self):
        for kind in ({"trigger":{"isMarket":True}},{"trigger":{"isMarket":False}},
                     {"limit":{"tif":"Alo"},"trigger":{}},None,{}):
            req=order();req["order_type"]=kind
            with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([req])
        self.raw.bulk_orders.assert_not_called()

    def test_single_order_keyword_and_positional_tif_are_guarded(self):
        with self.assertRaises(SubmissionSkipped):
            self.exchange.order(name="io:ANTH",is_buy=True,sz=.1,limit_px=100,
                                order_type={"limit":{"tif":"Ioc"}},reduce_only=True)
        with self.assertRaises(SubmissionSkipped):
            self.exchange.order("io:ANTH",True,.1,100,{"limit":{"tif":"Gtc"}},True)
        self.raw.order.assert_not_called()

    def test_single_and_batch_modification_tif_are_guarded(self):
        with self.assertRaises(SubmissionSkipped):
            self.exchange.bulk_modify_orders_new([{"oid":9,"order":order("Ioc",True)}])
        with self.assertRaises(SubmissionSkipped):
            self.exchange.modify_order(9,"io:ANTH",True,.1,100,{"limit":{"tif":"Gtc"}},True)
        with self.assertRaises(SubmissionSkipped):
            self.exchange.modify_order(oid=9,name="io:ANTH",is_buy=True,sz=.1,limit_px=100,
                                       order_type={"trigger":{"isMarket":True}},reduce_only=True)
        self.raw.modify_order.assert_not_called();self.raw.bulk_modify_orders_new.assert_not_called()

    def test_sdk_market_helpers_cannot_bypass_policy(self):
        for method in (self.exchange.market_open,self.exchange.market_close):
            with self.assertRaises(SubmissionSkipped):method("io:ANTH",.1)
        self.raw.market_open.assert_not_called();self.raw.market_close.assert_not_called()

    def test_cancel_and_leverage_remain_available(self):
        self.raw.bulk_cancel.return_value={"status":"ok","response":{"data":{"statuses":["success"]}}}
        self.raw.update_leverage.return_value={"status":"ok","response":{"type":"default"}}
        self.exchange.bulk_cancel([{"coin":"io:ANTH","oid":9}])
        self.exchange.update_leverage(2,"io:ANTH")
        self.raw.bulk_cancel.assert_called_once();self.raw.update_leverage.assert_called_once()

    def test_failure_and_cleanup_scope_never_grant_market_capability(self):
        self.gate.fail("transport failure")
        self.assertFalse(self.gate.manual_stop_requested)
        with self.gate.cleanup_scope():
            with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([order("Ioc",True)])
        self.raw.bulk_orders.assert_not_called()

    def test_cleanup_scope_cannot_create_even_a_post_only_opening(self):
        self.gate.stop()
        req=order("Alo",False)
        with self.gate.cleanup_scope(),patch.object(self.gate,"publish") as publish:
            with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([req])
            publish.assert_not_called()
        self.assertNotIn("cloid",req);self.raw.bulk_orders.assert_not_called()

    def test_public_final_check_rejects_post_only_opening_after_stop(self):
        self.gate.stop()
        with self.assertRaises(SubmissionSkipped):
            self.gate.assert_allowed_action("bulk_orders",([order()],),{})

    def test_stopped_cleanup_can_still_submit_post_only_reduce_only(self):
        self.gate.stop()
        with self.gate.cleanup_scope():self.exchange.bulk_orders([order("Alo",True)])
        self.raw.bulk_orders.assert_called_once()

    def test_manual_scope_requires_explicit_manual_origin(self):
        with self.assertRaises(ExecutionStopped):
            with self.gate.manual_close_scope():pass
        self.gate.stop()
        with self.assertRaises(ExecutionStopped):
            with self.gate.manual_close_scope():pass

    def test_manual_origin_is_set_before_stop_callback(self):
        seen=[]
        gate=SafetyLatch(on_stop=lambda:seen.append(gate.manual_stop_requested),passive_only=True)
        gate.request_manual_stop()
        self.assertEqual(seen,[True]);self.assertTrue(gate.stopped.is_set())

    def test_manual_capability_is_narrow_and_reentrant(self):
        self.gate.request_manual_stop()
        with self.gate.cleanup_scope(),self.gate.manual_close_scope():
            with self.gate.manual_close_scope():self.exchange.bulk_orders([order("Ioc",True)])
            self.exchange.bulk_orders([order("Gtc",True)])
            for req in (order("Ioc",False),{"order_type":{"trigger":{"isMarket":True}},"reduce_only":True}):
                with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([req])
        self.assertEqual(self.raw.bulk_orders.call_count,2)
        with self.gate.cleanup_scope():
            with self.assertRaises(SubmissionSkipped):self.exchange.bulk_orders([order("Ioc",True)])

    def test_manual_capability_does_not_leak_between_threads(self):
        self.gate.request_manual_stop();errors=[]
        def other_thread():
            with self.gate.cleanup_scope():
                try:self.exchange.bulk_orders([order("Ioc",True)])
                except SubmissionSkipped:errors.append("blocked")
        with self.gate.manual_close_scope():
            thread=threading.Thread(target=other_thread);thread.start();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,["blocked"])
        self.raw.bulk_orders.assert_not_called()

    def test_policy_can_be_rechecked_after_request_was_mutated(self):
        req=order();self.gate.assert_allowed_action("bulk_orders",([req],),{})
        req["order_type"]["limit"]["tif"]="Ioc"
        with self.assertRaises(SubmissionSkipped):self.gate.assert_allowed_action("bulk_orders",([req],),{})

    def test_manual_origin_is_not_restored_from_an_old_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"session.json"
            path.write_text('{"manual_stop_requested":true,"session_id":"previous"}')
            gate=SafetyLatch(path=path,passive_only=True)
            self.assertFalse(gate.manual_stop_requested)


class PassiveCleanupTests(unittest.TestCase):
    def test_automatic_cleanup_cancels_own_orders_without_closing_residual(self):
        gate=SafetyLatch(passive_only=True);gate.owned.add(1);gate.exposure_started=True
        orders=[{"oid":1}];cancel=Mock(side_effect=lambda _:orders.clear());close=Mock()
        result=gate.cleanup(lambda:[("io:ANTH",.1,list(orders))],cancel,close,
                            allow_close=False,sleep=lambda _:None)
        self.assertFalse(result);cancel.assert_called_once();close.assert_not_called()
        self.assertIn("manual Stop",gate.cleanup_error)

    def test_passive_cleanup_default_also_requires_manual_capability(self):
        gate=SafetyLatch(passive_only=True);gate.exposure_started=True;close=Mock()
        self.assertFalse(gate.cleanup(lambda:[("io:ANTH",.1,[])],Mock(),close,sleep=lambda _:None))
        close.assert_not_called()


    def test_pending_ids_block_any_new_close_even_in_legacy_mode(self):
        gate=SafetyLatch();gate.exposure_started=True;gate.pending_cloids.add("uncertain-action")
        close=Mock()
        self.assertFalse(gate.cleanup(lambda:[("io:ANTH",.1,[])],Mock(),close,sleep=lambda _:None))
        close.assert_not_called();self.assertIn("unconfirmed",gate.cleanup_error)

    def test_pending_ids_still_allow_protective_cancellation(self):
        gate=SafetyLatch();gate.exposure_started=True;gate.owned.add(9);gate.pending_cloids.add("unknown")
        orders=[{"oid":9}];cancel=Mock(side_effect=lambda _:orders.clear());close=Mock()
        self.assertFalse(gate.cleanup(lambda:[("io:ANTH",.1,list(orders))],cancel,close,sleep=lambda _:None))
        cancel.assert_called_once();close.assert_not_called()

    def test_explicit_manual_stop_can_close_and_confirm_flat(self):
        gate=SafetyLatch(passive_only=True);gate.exposure_started=True;gate.request_manual_stop()
        raw=Mock();raw.order.return_value=ack()
        exchange=gate.wrap(raw);position=[.1]
        def close(symbol,qty):
            exchange.order(name=symbol,is_buy=False,sz=qty,limit_px=99,
                           order_type={"limit":{"tif":"Ioc"}},reduce_only=True)
            position[0]=0
        with gate.manual_close_scope():
            self.assertTrue(gate.cleanup(lambda:[("io:ANTH",position[0],[])],Mock(),close,
                                         allow_close=True,sleep=lambda _:None))
        raw.order.assert_called_once();self.assertTrue(gate.cleanup_confirmed)

    def test_flat_automatic_cleanup_needs_no_manual_authorization(self):
        gate=SafetyLatch(passive_only=True);close=Mock()
        self.assertTrue(gate.cleanup(lambda:[("io:ANTH",0,[])],Mock(),close,
                                    allow_close=False,sleep=lambda _:None))
        close.assert_not_called()

    def test_known_cleanup_rejection_retries_only_after_fresh_residual_snapshot(self):
        gate=SafetyLatch(passive_only=True);gate.exposure_started=True;gate.request_manual_stop()
        raw=Mock();raw.order.side_effect=[
            {"status":"ok","response":{"data":{"statuses":[{"error":
                "Order could not immediately match against any resting orders."}]}}},
            {"status":"ok","response":{"data":{"statuses":[{"filled":{
                "oid":9,"totalSz":".04","avgPx":"100"}}]}}}]
        exchange=gate.wrap(raw);position=[.1];reads=[];quantities=[]
        def snapshot():
            reads.append(1)
            if len(reads)==2:position[0]=.04  # independently observed residual
            return [("io:ANTH",position[0],[])]
        def close(symbol,quantity):
            quantities.append(quantity)
            exchange.order(name=symbol,is_buy=False,sz=quantity,limit_px=99,
                           order_type={"limit":{"tif":"Ioc"}},reduce_only=True)
            position[0]=0
        with gate.manual_close_scope():
            self.assertTrue(gate.cleanup(snapshot,Mock(),close,sleep=lambda _:None))
        self.assertEqual(quantities,[.1,.04])
        self.assertFalse(gate.uncertain_close or gate.pending_cloids)
        self.assertGreaterEqual(len(reads),3)

    def test_unknown_cleanup_result_still_never_retries(self):
        gate=SafetyLatch();gate.exposure_started=True;raw=Mock()
        raw.order.side_effect=TimeoutError("response lost")
        exchange=gate.wrap(raw)
        result=gate.cleanup(lambda:[("io:ANTH",.1,[])],Mock(),
            lambda symbol,qty:exchange.order(name=symbol,sz=qty),sleep=lambda _:None)
        self.assertFalse(result);self.assertTrue(gate.uncertain_close)
        self.assertTrue(gate.pending_cloids);raw.order.assert_called_once()


class AckIdentityTests(unittest.TestCase):
    def test_mixed_malformed_ack_preserves_valid_owned_order_and_uncertainty(self):
        gate=SafetyLatch();raw=Mock()
        raw.bulk_orders.return_value={"status":"ok","response":{"data":{"statuses":[
            {"resting":{"oid":5}},{"filled":{"totalSz":".1","avgPx":"100"}}]}}}
        with self.assertRaisesRegex(RuntimeError,"malformed"):
            gate.wrap(raw).bulk_orders([order(),order()])
        self.assertEqual(gate.owned,{5});self.assertEqual(len(gate.pending_cloids),2)
        self.assertTrue(gate.stopped.is_set())

    def test_conflicting_error_and_fill_status_is_not_a_settled_action(self):
        gate=SafetyLatch();raw=Mock()
        raw.order.return_value={"status":"ok","response":{"data":{"statuses":[{
            "error":"rejected","filled":{"oid":7,"totalSz":".1","avgPx":"100"}}]}}}
        with self.assertRaisesRegex(RuntimeError,"malformed"):
            gate.wrap(raw).order(name="io:ANTH")
        self.assertTrue(gate.pending_cloids)

    def test_missing_fractional_boolean_or_invalid_order_ids_remain_uncertain(self):
        for kind in ("resting","filled"):
            for oid in (None,0,-1,True,1.5,"1.5","bad"):
                with self.subTest(kind=kind,oid=oid):
                    gate=SafetyLatch();raw=Mock()
                    detail={"oid":oid}
                    if kind=="filled":detail.update(totalSz=".1",avgPx="100")
                    raw.bulk_orders.return_value={"status":"ok","response":{"data":{"statuses":[{kind:detail}]}}}
                    with self.assertRaises((RuntimeError,ValueError,TypeError)):
                        gate.wrap(raw).bulk_orders([order()])
                    self.assertTrue(gate.stopped.is_set())
                    self.assertTrue(gate.pending_cloids)

    def test_missing_filled_oid_and_zero_size_are_not_successful_fills(self):
        for detail in ({"totalSz":".1","avgPx":"100"},
                       {"oid":1,"totalSz":"0","avgPx":"100"}):
            gate=SafetyLatch();raw=Mock()
            raw.order.return_value={"status":"ok","response":{"data":{"statuses":[{"filled":detail}]}}}
            with self.assertRaises(RuntimeError):gate.wrap(raw).order(name="io:ANTH")
            self.assertTrue(gate.pending_cloids)

    def test_valid_integer_and_integer_string_fill_ids_are_owned(self):
        for oid in (7,"7"):
            gate=SafetyLatch();raw=Mock()
            raw.order.return_value={"status":"ok","response":{"data":{"statuses":[{"filled":{
                "oid":oid,"totalSz":".1","avgPx":"100"}}]}}}
            gate.wrap(raw).order(name="io:ANTH")
            self.assertEqual(gate.owned,{7});self.assertFalse(gate.pending_cloids)


if __name__=="__main__":unittest.main()
