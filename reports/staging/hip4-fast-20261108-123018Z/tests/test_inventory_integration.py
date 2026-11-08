"""Root integration: fill basis, book marks, manual stop and action worker."""
from collections import deque
import json
import threading
import time
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import test_reduce_only_preflight as fixtures
from zeus.safety import SafetyLatch, SubmissionSkipped
from zeus.mid_maintenance import MidMaintenance, MaintenanceRequest, MaintenanceResult, PositionRead


class BotIntegrationFixture(unittest.TestCase):
    position = fixtures.ReduceOnlyPreflightTests.position

    def setUp(self):
        fixtures.ReduceOnlyPreflightTests.setUp(self)
        b = self.bot
        b.position = 0.0
        b.entry_price = 0.0
        b.unrealized_pnl = 0.0
        b.pos_since = 0.0
        b.position_dirty = False
        b._inventory_mark_known = True
        b._inventory_mark_source = ""
        b.upnl_peak_bps = 0.0
        b.bank_until = b._hard_exit_at = b.pause_until = b.hourly_pause_until = 0.0
        b._hard_exit_reason = ""
        b.session_net_high = 0.0
        b.protect = 0
        b.size_mult = 1.0
        b._last_regime = None
        b._reg_now = (None, 0)
        b._early_now = (None, 0)
        b._sig_vol = 0.0
        b._sig_z = 0.0
        b.session_pnl = 0.0
        b.total_fees = 0.0
        b.total_volume = 0.0
        b.fills_count = 0
        b.maker_fills = 0
        b.taker_fills = 0
        b.unknown_role_fills = 0
        b.accounting_complete = True
        b.processed_fills = set()
        b.pending_markouts = deque()
        b.side_fills = {"B": deque(), "A": deque()}
        b.side_pause_until = {"B": 0.0, "A": 0.0}
        b.last_side_fill_ts = {"B": 0.0, "A": 0.0}
        b.hourly_net_events = deque()
        b.hourly_net_pnl = 0.0
        b.hourly_loss_latched = False
        b.book_received_ns = time.perf_counter_ns()
        b.last_tick_ts = time.time()
        b.ws_ready = False
        b.best_bid, b.best_ask = 100.0, 100.1
        b.mid = 100.05
        b.last_ws_restart = time.time()  # risk-only fixtures never start a live health probe
        b.telemetry = None
        b._quote_worker = None
        b._action_in_progress = False
        b._execution_revision = 0
        b._mid_requests_in_flight = 0
        b._risk_basis_revision = b.position_revision
        b._inventory_basis_known = True
        b._risk_revision = b._submit_risk_revision = 0
        b.book_revision = b.top_revision = b._submit_book_revision = 0
        b._feed_generation = b._submit_feed_generation = 0

    @staticmethod
    def fill(tid=1, side="B", px=100.0, sz=0.1, crossed=False, **extra):
        row = {"coin": "io:ANTH", "oid": 5, "tid": tid, "side": side,
               "px": str(px), "sz": str(sz), "fee": ".01", "closedPnl": "0",
               "crossed": crossed}
        row.update(extra)
        return row

    @staticmethod
    def wait_for(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False


class FillBasisIntegrationTests(BotIntegrationFixture):
    def test_valid_fill_stamps_risk_basis_at_the_new_position_revision(self):
        b=self.bot
        b._on_user_fills({"data":{"fills":[self.fill(tid=101,startPosition="0")]}})
        self.assertEqual((b.position,b.entry_price),(.1,100))
        self.assertEqual(b._risk_basis_revision,b.position_revision)
        self.assertTrue(b._inventory_mark_known)

    def test_opening_add_partial_close_and_flat_reset_follow_fill_prices(self):
        b = self.bot
        b._on_user_fills({"data": {"fills": [self.fill(tid=1, px=100, sz=.1)]}})
        self.assertEqual((b.position, b.entry_price), (.1, 100))
        self.assertAlmostEqual(b.unrealized_pnl, 0)
        self.assertGreater(b.pos_since, 0)

        b._on_user_fills({"data": {"fills": [self.fill(tid=2, px=102, sz=.1)]}})
        self.assertAlmostEqual(b.position, .2)
        self.assertAlmostEqual(b.entry_price, 101)
        self.assertAlmostEqual(b.unrealized_pnl, -.2)

        b._on_user_fills({"data": {"fills": [self.fill(tid=3, side="A", px=101, sz=.05)]}})
        self.assertAlmostEqual(b.position, .15)
        self.assertAlmostEqual(b.entry_price, 101)

        b.upnl_peak_bps = 4.0
        b.bank_until = time.time() + 30
        b._hard_exit_at = time.time() + 30
        b._hard_exit_reason = "old"
        b._on_user_fills({"data": {"fills": [self.fill(tid=4, side="A", px=101, sz=.15)]}})
        self.assertEqual((b.position, b.entry_price, b.pos_since), (0, 0, 0))
        self.assertEqual((b.unrealized_pnl, b.upnl_peak_bps, b.bank_until), (0, 0, 0))
        self.assertEqual((b._hard_exit_at, b._hard_exit_reason), (0, ""))

        b._on_user_fills({"data": {"fills": [self.fill(tid=5, side="A", px=103, sz=.1)]}})
        self.assertEqual((b.position, b.entry_price), (-.1, 103))
        self.assertGreater(b.pos_since, 0)

    def test_mismatched_start_position_requires_sync_without_losing_accounting(self):
        b = self.bot
        b.position, b.entry_price = .1, 100
        b.safety.account_known = True
        b._on_user_fills({"data": {"fills": [self.fill(tid=9, sz=.1, startPosition="0.5")]}})
        self.assertEqual(b.position, .1)
        self.assertTrue(b.position_dirty)
        self.assertFalse(b.safety.account_known)
        self.assertFalse(b._inventory_mark_known)
        self.assertEqual(b.fills_count, 1)
        self.assertIn("9", b.processed_fills)
        self.assertIn("fill basis", b.last_error)

    def test_unknown_local_entry_counts_fill_but_requires_reconciliation(self):
        b = self.bot
        b.position, b.entry_price = .1, 0
        b._on_user_fills({"data": {"fills": [self.fill(tid=10, side="A", sz=.1)]}})
        self.assertEqual(b.position, .1)
        self.assertTrue(b.position_dirty)
        self.assertFalse(b._inventory_mark_known)
        self.assertEqual(b.fills_count, 1)
        self.assertIn("basis", b.last_error)


class BookMarkRiskTests(BotIntegrationFixture):
    def test_risk_revision_changes_for_new_pause_but_not_for_each_noop_poll(self):
        b=self.bot
        b._check_feed_health=Mock()
        b.hourly_net_events.append((time.time(),-3))
        with patch.multiple(self.mod,MAX_SESSION_LOSS_USD=5,HOURLY_LOSS_MULT=.5):
            b.risk_checks()
            self.assertGreater(b.hourly_pause_until,time.time())
            self.assertEqual(b._risk_revision,1)
            b.risk_checks()
            self.assertEqual(b._risk_revision,1)

    def test_fill_reset_cannot_be_overwritten_by_an_older_risk_calculation(self):
        b=self.bot
        b.position,b.entry_price=1,100
        b.position_revision=b._risk_basis_revision=1
        b.best_bid,b.best_ask,b.mid=99,99.1,99.05
        entered,release,fill_done=threading.Event(),threading.Event(),threading.Event()
        original=b._risk_state_checks_locked
        def delayed_risk():
            entered.set();release.wait(2);original()
        b._risk_state_checks_locked=delayed_risk
        def fills():
            b._on_user_fills({"data":{"fills":[self.fill(tid=21,side="A",px=99,sz=1),
                self.fill(tid=22,side="B",px=99.05,sz=1)]}})
            fill_done.set()
        risk_thread=threading.Thread(target=b.risk_checks)
        fill_thread=threading.Thread(target=fills)
        try:
            risk_thread.start();self.assertTrue(entered.wait(1))
            fill_thread.start();self.assertFalse(fill_done.wait(.1))
            release.set();risk_thread.join(2);fill_thread.join(2)
            self.assertFalse(risk_thread.is_alive());self.assertFalse(fill_thread.is_alive())
            self.assertEqual((b.position,b.entry_price),(1,99.05))
            self.assertEqual((b._hard_exit_reason,b._hard_exit_at,b.bank_until,b.upnl_peak_bps),("",0,0,0))
        finally:
            release.set();risk_thread.join(2)
            if fill_thread.ident is not None:fill_thread.join(2)

    def test_late_fill_already_in_authoritative_snapshot_cannot_resurrect_unknown_basis(self):
        b=self.bot
        # REST already includes this opening; the delayed fill still contributes
        # accounting once, but its quantity must never be added for a second time.
        b.position,b.entry_price=.1,100
        b.position_revision=b._risk_basis_revision=3
        b.safety.account_known=True
        b._on_user_fills({"data":{"fills":[self.fill(tid=44,startPosition="0")]}})
        self.assertEqual(b.position,.1)
        self.assertFalse(b._inventory_mark_known)
        self.assertNotEqual(b._risk_basis_revision,b.position_revision)
        b._manage_session_exit=Mock()
        b.risk_checks()
        self.assertFalse(b._inventory_mark_known)
        b._manage_session_exit.assert_not_called()
        self.assertEqual(b.session_net_high,0)
        self.assertEqual(b.fills_count,1)
        self.position(.1)
        self.assertTrue(b.sync_position(include_spot=False))
        self.assertEqual(b._risk_basis_revision,b.position_revision)
        self.assertTrue(b._inventory_mark_known)
        b._on_user_fills({"data":{"fills":[self.fill(tid=44,startPosition="0")]}})
        self.assertEqual(b.fills_count,1)
        self.assertEqual(b.position,.1)

    def test_direct_sync_advances_position_revision_for_quantity_and_entry_changes(self):
        b=self.bot
        b.position,b.entry_price=.1,100
        b.position_revision=b._risk_basis_revision=4
        self.position(.2)
        self.assertTrue(b.sync_position(include_spot=False))
        self.assertEqual((b.position,b.position_revision),(.2,5))
        self.assertEqual(b._risk_basis_revision,5)
        self.assertTrue(b.sync_position(include_spot=False))
        self.assertEqual(b.position_revision,5)
        b.info.user_state.return_value["assetPositions"][0]["position"]["entryPx"]="101"
        self.assertTrue(b.sync_position(include_spot=False))
        self.assertEqual((b.entry_price,b.position_revision),(101,6))
        self.assertEqual(b._risk_basis_revision,6)

    def test_maintenance_snapshot_advances_basis_revision_when_inventory_changes(self):
        b=self.bot
        b.position,b.entry_price=.1,100
        b.position_revision=b._risk_basis_revision=4
        request=MaintenanceRequest(b.safety.session_id,b._execution_revision,False,True,False,
                                   position_revision=b.position_revision)
        result=MaintenanceResult(request,position=PositionRead(.2,101,0,100),position_started=time.monotonic())
        self.assertTrue(b._apply_maintenance_result(result))
        self.assertEqual((b.position,b.entry_price,b.position_revision),(.2,101,5))
        self.assertEqual(b._risk_basis_revision,5)

    def test_risk_uses_current_book_not_stale_rest_upnl(self):
        b = self.bot
        b.position, b.entry_price, b.unrealized_pnl = 1, 100, 5
        b.best_bid, b.best_ask, b.mid = 99, 99.1, 99.05
        b.flatten = Mock()
        b.risk_checks()
        self.assertAlmostEqual(b.unrealized_pnl, -1)
        self.assertTrue(b._inventory_mark_known)
        self.assertGreater(b.bank_until, time.time())
        self.assertIn("runaway", b._hard_exit_reason)
        b.flatten.assert_not_called()

    def test_unknown_mark_is_not_turned_into_zero_or_a_risk_exit(self):
        b = self.bot
        b.position, b.entry_price, b.unrealized_pnl = 1, 100, 5
        b.best_bid = b.best_ask = 0
        b.risk_checks()
        self.assertFalse(b._inventory_mark_known)
        self.assertEqual(b.unrealized_pnl, 5)
        self.assertEqual((b.bank_until, b.session_net_high), (0, 0))

    def test_flat_unknown_state_blocks_new_exposure(self):
        b = self.bot
        b.position = 0
        b._inventory_mark_known = False
        bid, ask, _ = b.compute_bbo_intent()
        self.assertIsNone(bid)
        self.assertIsNone(ask)


class ManualStopIntegrationTests(BotIntegrationFixture):
    def test_stop_file_payload_requires_the_current_session(self):
        b = self.bot
        b.safety.session_id = "session-1"
        with tempfile.TemporaryDirectory() as directory:
            b._stop_file = str(Path(directory) / "stop.flag")
            path = Path(b._stop_file)
            path.write_text("stop", encoding="utf-8")
            self.assertFalse(b._manual_stop_file())
            path.write_text(json.dumps({"manual": True, "session_id": "old"}), encoding="utf-8")
            self.assertFalse(b._manual_stop_file())
            path.write_text(json.dumps({"manual": True, "session_id": "session-1"}), encoding="utf-8")
            self.assertTrue(b._manual_stop_file())

    def test_check_stop_file_distinguishes_manual_and_automatic_stop(self):
        b = self.bot
        b.safety.session_id = "session-1"
        b.safety.request_manual_stop = Mock()
        b._sig_shutdown = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.mod, "supervisor_alive", return_value=True):
            b._stop_file = str(Path(directory) / "stop.flag")
            path = Path(b._stop_file)
            path.write_text(json.dumps({"manual": True, "session_id": "session-1"}), encoding="utf-8")
            b._check_stop_file()
            b.safety.request_manual_stop.assert_called_once()
            b._sig_shutdown.assert_not_called()
            self.assertIn("manual stop", b.last_action)
            path.write_text("stop", encoding="utf-8")
            b._check_stop_file()
            b._sig_shutdown.assert_called_once()

    def test_run_closes_residual_only_under_a_manual_stop_scope(self):
        b = self.bot
        b.running = False
        b.live = True
        b.safety = SafetyLatch(passive_only=True)
        b.safety.exposure_started = True
        b.safety.request_manual_stop()
        b._check_stop_file = Mock()
        b._run_session = Mock()
        b._final_accounting = Mock()
        b.write_stats = Mock()
        b.ws_info = None
        b.exchange = Mock()
        remaining = [True]
        def snapshot():
            return [("io:ANTH", .1 if remaining[0] else 0.0, [])]
        def close(_symbol, _position):
            remaining[0] = False
        b._cleanup_snapshot = Mock(side_effect=snapshot)
        b._cleanup_close = Mock(side_effect=close)
        b.run()
        b._cleanup_close.assert_called_once()
        self.assertTrue(b.safety.cleanup_confirmed)


class FinalWsGateTests(BotIntegrationFixture):
    @staticmethod
    def payload(*orders):
        return {"action": {"type": "order", "orders": list(orders)}, "nonce": 1}

    @staticmethod
    def wire(*, reduce_only=False, tif="Alo"):
        return {"a": 1, "b": True, "p": "100", "s": ".9", "r": reduce_only,
                "t": {"limit": {"tif": tif}}}

    def stamp(self):
        b=self.bot
        b._submit_position_revision=b.position_revision
        b._submit_book_revision=getattr(b,"top_revision",0)
        b._submit_feed_generation=b._feed_generation
        b._submit_risk_revision=b._risk_revision

    def test_new_risk_hourly_or_bank_window_blocks_previously_signed_opening(self):
        b=self.bot
        for field in ("pause_until","hourly_pause_until","bank_until"):
            with self.subTest(field=field):
                self.stamp()
                setattr(b,field,time.time()+30)
                with self.assertRaises(SubmissionSkipped):
                    b._final_ws_gate(self.payload(self.wire()))
                # A freshly prepared risk-reducing quote remains possible.
                self.stamp()
                b._final_ws_gate(self.payload(self.wire(reduce_only=True)))
                setattr(b,field,0)

    def test_changed_risk_policy_revision_invalidates_old_opening_signature(self):
        b=self.bot;self.stamp();b._risk_revision+=1
        with self.assertRaises(SubmissionSkipped):
            b._final_ws_gate(self.payload(self.wire()))
        self.stamp()
        b._final_ws_gate(self.payload(self.wire(reduce_only=True)))

    def test_nonflat_inventory_never_accepts_an_opening_even_if_revision_was_not_updated(self):
        b=self.bot;self.stamp()
        b.position,b.entry_price=.1,100
        with self.assertRaises(SubmissionSkipped):
            b._final_ws_gate(self.payload(self.wire()))

    def test_current_notional_cap_is_rechecked_at_the_wire_boundary(self):
        b=self.bot;self.stamp()
        wire=self.wire();wire["s"]="1.1"
        with self.assertRaises(SubmissionSkipped):
            b._final_ws_gate(self.payload(wire))

    def test_new_size_multiplier_cannot_be_bypassed_by_an_old_signature(self):
        b=self.bot;self.stamp();b.size_mult=.7
        with self.assertRaises(SubmissionSkipped):
            b._final_ws_gate(self.payload(self.wire()))

    def test_reduce_only_fill_after_signing_invalidates_the_signed_quote(self):
        b=self.bot;self.stamp()
        payload=self.payload(self.wire(reduce_only=True))
        b._final_ws_gate(payload)
        b.position_revision+=1
        with self.assertRaisesRegex(SubmissionSkipped,"Inventory changed"):
            b._final_ws_gate(payload)

    def test_old_book_blocks_signed_reduce_only_as_well_as_opening(self):
        b=self.bot;self.stamp()
        b.book_received_ns=time.perf_counter_ns()-int(20e9)
        for ro in (True,False):
            with self.assertRaisesRegex(SubmissionSkipped,"Book stale"):
                b._final_ws_gate(self.payload(self.wire(reduce_only=ro)))

    def test_new_touch_or_feed_generation_supersedes_signed_quote(self):
        b=self.bot
        for attribute in ("top_revision","_feed_generation"):
            self.stamp();setattr(b,attribute,getattr(b,attribute)+1)
            with self.assertRaisesRegex(SubmissionSkipped,"Newer book"):
                b._final_ws_gate(self.payload(self.wire(reduce_only=True)))
        # A depth-only revision must not abort an otherwise valid signed price.
        b.top_revision=5;b._feed_generation=0;self.stamp()
        b.book_revision+=1
        b._final_ws_gate(self.payload(self.wire(reduce_only=True)))

    def test_periodic_verification_expiry_blocks_opening_before_send(self):
        b=self.bot;self.stamp()
        b._maintenance=Mock()
        b._position_verified_at=b._orders_verified_at=time.monotonic()-20
        with self.assertRaisesRegex(SubmissionSkipped,"verification expired"):
            b._final_ws_gate(self.payload(self.wire()))
        b._final_ws_gate(self.payload(self.wire(reduce_only=True)))

    def test_limit_plus_trigger_wire_shape_is_not_accepted_as_post_only(self):
        b=self.bot;self.stamp()
        wire=self.wire(reduce_only=True)
        wire["t"]["trigger"]={"isMarket":True,"triggerPx":"99","tpsl":"sl"}
        with self.assertRaisesRegex(SubmissionSkipped,"explicit limit"):
            b._final_ws_gate(self.payload(wire))

    def test_manual_cleanup_scope_never_authorizes_a_new_opening(self):
        b=self.bot;self.stamp()
        b.safety.request_manual_stop()
        with b.safety.cleanup_scope(),b.safety.manual_close_scope():
            with self.assertRaisesRegex(SubmissionSkipped,"opening exposure"):
                b._final_ws_gate(self.payload(self.wire()))

    def test_signed_opening_is_rejected_when_inventory_changes_or_mark_unknown(self):
        b = self.bot
        b._submit_position_revision = b.position_revision = 0
        b.position_dirty, b._inventory_mark_known = False, True
        b._final_ws_gate(self.payload(self.wire()))
        b.position_dirty = True
        with self.assertRaisesRegex(SubmissionSkipped, "Inventory state unknown"):
            b._final_ws_gate(self.payload(self.wire()))
        b.position_dirty = False
        b._inventory_mark_known = False
        with self.assertRaisesRegex(SubmissionSkipped, "Inventory state unknown"):
            b._final_ws_gate(self.payload(self.wire()))
        b._inventory_mark_known = True
        b.position_revision += 1
        with self.assertRaisesRegex(SubmissionSkipped, "Inventory changed"):
            b._final_ws_gate(self.payload(self.wire()))

    def test_signed_taker_is_rejected_without_a_manual_close_scope(self):
        b = self.bot
        b.safety.stopped.clear()
        b.running = True
        with self.assertRaisesRegex(SubmissionSkipped, "non-post-only"):
            b._final_ws_gate(self.payload(self.wire(reduce_only=True, tif="Ioc")))
        b.safety.request_manual_stop()
        with b.safety.cleanup_scope(), b.safety.manual_close_scope():
            b._final_ws_gate(self.payload(self.wire(reduce_only=True, tif="Ioc")))

    def test_signed_cancel_is_allowed_during_stop_cleanup(self):
        b = self.bot
        b.safety.stop()
        with self.assertRaisesRegex(SubmissionSkipped, "Stop/supervisor"):
            b._final_ws_gate({"action": {"type": "cancel", "cancels": []}})
        with b.safety.cleanup_scope():
            b._final_ws_gate({"action": {"type": "cancel", "cancels": []}})


class DashboardManualStopTests(unittest.TestCase):
    def test_stop_grid_writes_manual_payload_with_session_id(self):
        from zeus import server
        manager = server.BotManager.__new__(server.BotManager)
        manager.lock = threading.Lock()
        manager._session_status = Mock(return_value={"session_id": "session-9",
                                                     "cleanup_confirmed": True})
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop.flag"
            proc = Mock()
            proc.poll.return_value = 0
            manager.obi_procs = {"IO_ANTH": {
                "stop_file": str(stop_file), "proc": proc, "safety_file": "journal.json",
                "config": {"strategy": "bbo"}}}
            manager.stop_grid("IO_ANTH")
            self.assertEqual(json.loads(stop_file.read_text(encoding="utf-8")),
                             {"manual": True, "session_id": "session-9"})


class ActionWorkerIntegrationTests(BotIntegrationFixture):
    def test_noop_callback_is_busy_but_does_not_invalidate_a_read_only_result(self):
        b=self.bot
        observed=[]
        before=b._execution_revision
        b.apply_intent=Mock(side_effect=lambda:observed.append((b._execution_revision,b._action_in_progress)))
        b._run_quote_action()
        self.assertEqual(observed,[(before,True)])
        self.assertEqual(b._execution_revision,before)
        self.assertFalse(b._action_in_progress)

    def test_maintenance_cannot_apply_while_actor_preflight_runs_before_signing(self):
        b=self.bot
        entered,release=threading.Event(),threading.Event()
        def action():
            entered.set();release.wait(2)
            b._begin_mid_action("bulk_orders")
            b._end_mid_action("bulk_orders")
        b.apply_intent=Mock(side_effect=action)
        old_request=MaintenanceRequest(b.safety.session_id,b._execution_revision,False,True,False)
        worker=b._start_quote_worker()
        self.addCleanup(worker.close,2);self.addCleanup(release.set)
        b._request_quote_action();self.assertTrue(entered.wait(1))
        self.assertTrue(b._action_in_progress)
        self.assertEqual(b._mid_requests_in_flight,0)
        during=MaintenanceRequest(b.safety.session_id,b._execution_revision,False,True,False)
        def result(request):return MaintenanceResult(request,position=PositionRead(.2,101,0,100),position_started=time.monotonic())
        self.assertFalse(b._apply_maintenance_result(result(old_request)))
        # Even a request stamped with the current generation must not overwrite
        # a quote decision's preflight-to-signing window.
        self.assertFalse(b._apply_maintenance_result(result(during)))
        self.assertEqual((b.position,b.entry_price),(0,0))
        release.set();self.assertTrue(self.wait_for(lambda:not b._action_in_progress))
        # Once the actor is idle, a position read captured during the window is
        # still valid if no fill/sync advanced the position generation. Order
        # components remain fenced by the execution revision.
        self.assertTrue(b._apply_maintenance_result(result(during)))
        self.assertEqual((b.position,b.entry_price),(.2,101))

    def test_slow_maintenance_survives_repeated_noop_quote_wakes(self):
        b=self.bot
        class SlowReads:
            session=Mock()
            def post(self,path,payload):
                time.sleep(.02)
                if payload["type"]=="clearinghouseState":
                    return {"assetPositions":[],"marginSummary":{"accountValue":"100"}}
                if payload["type"]=="spotClearinghouseState":
                    return {"balances":[]}
                if payload["type"]=="openOrders":
                    return []
                raise AssertionError("Unexpected endpoint")
        client=SlowReads()
        maintenance=MidMaintenance("https://unused.invalid","dummy-account","io:ANTH",b.wake,
            client_factory=lambda:client,halt_event=b.safety.stopped)
        self.addCleanup(maintenance.close,1)
        b._maintenance=maintenance
        now=time.monotonic()
        b._position_verified_at=b._orders_verified_at=now-20
        b._maintenance_position_request_at=b._maintenance_orders_request_at=now
        b.last_orderupdate_ts=b.last_quote_ts=time.time()
        b.apply_intent=Mock()
        actions=b._start_quote_worker();self.addCleanup(actions.close,1)
        maintenance.request(MaintenanceRequest(b.safety.session_id,b._execution_revision,False,True,True))
        deadline=time.monotonic()+1
        while time.monotonic()<deadline and b._position_verified_at<now:
            b._request_quote_action()
            b._maintenance_tick()
            time.sleep(.001)
        self.assertGreater(b.apply_intent.call_count,1)
        self.assertGreaterEqual(b._position_verified_at,now)
        self.assertGreaterEqual(b._orders_verified_at,now)
        self.assertTrue(b.safety.account_known)
        self.raw.bulk_orders.assert_not_called()

    def test_main_risk_and_stop_remain_responsive_while_actor_waits_for_ack(self):
        b=self.bot
        entered,release=threading.Event(),threading.Event()
        b.apply_intent=Mock(side_effect=lambda:(entered.set(),release.wait(3)))
        worker=b._start_quote_worker()
        self.addCleanup(worker.close,2);self.addCleanup(release.set)
        b._check_stop_file=Mock();b._refresh_signals=Mock();b._maintenance_tick=Mock()
        b.update_markouts=Mock();b.risk_checks=Mock();b.write_stats=Mock()
        b._last_stats_write=0;b.ws_ready=True;b.wake.set()
        main=threading.Thread(target=b._trading_loop,name="test-risk-loop")
        try:
            main.start();self.assertTrue(entered.wait(1))
            b.wake.set()
            self.assertTrue(self.wait_for(lambda:b.risk_checks.call_count>=2))
            self.assertTrue(worker.state()["active"])
            b.safety.request_manual_stop();b.wake.set()
            main.join(.5)
            self.assertFalse(main.is_alive())
            self.assertFalse(release.is_set())
        finally:
            b.running=False;b.wake.set();release.set();main.join(1)

    def test_worker_runs_apply_intent_off_the_loop_thread(self):
        b = self.bot
        seen = []
        b.apply_intent = Mock(side_effect=lambda: seen.append(threading.current_thread().name))
        worker = b._start_quote_worker()
        self.addCleanup(worker.close, 2)
        self.assertTrue(b._request_quote_action())
        self.assertTrue(self.wait_for(lambda: b.apply_intent.call_count == 1))
        self.assertEqual(seen, ["mm-actions"])
        self.assertFalse(b._action_in_progress)

    def test_thousand_requests_coalesce_to_one_pending_action(self):
        b = self.bot
        entered, release = threading.Event(), threading.Event()
        calls = []
        def apply():
            calls.append(1)
            entered.set()
            release.wait(2)
        b.apply_intent = apply
        worker = b._start_quote_worker()
        self.addCleanup(worker.close, 2)
        self.addCleanup(release.set)
        self.assertTrue(b._request_quote_action())
        self.assertTrue(entered.wait(1))
        for _ in range(1000):
            b._request_quote_action()
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertTrue(self.wait_for(lambda: len(calls) == 2))
        self.assertEqual(len(calls), 2)

    def test_callback_failure_halts_the_session_and_worker(self):
        b = self.bot
        b.apply_intent = Mock(side_effect=RuntimeError("boom"))
        worker = b._start_quote_worker()
        self.addCleanup(worker.close, 2)
        b._request_quote_action()
        self.assertTrue(self.wait_for(lambda: b.safety.stopped.is_set()))
        self.assertFalse(b.running)
        self.assertIn("action worker", b.last_error)
        self.assertTrue(worker.state()["failed"])
        self.assertFalse(b._request_quote_action())

    def test_no_action_runs_after_stop(self):
        b = self.bot
        b.apply_intent = Mock()
        b.safety.request_manual_stop()
        worker = b._start_quote_worker()
        self.addCleanup(worker.close, 2)
        b._request_quote_action()
        time.sleep(.05)
        b.apply_intent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
