"""Offline regressions for the adaptive MID quoting and fast exit path."""
import math
import time
import unittest
from collections import deque
from unittest.mock import Mock, patch

import test_mid_execution_audit as fixtures
from zeus.bots.order_book import SignalEngine


class AdaptiveFixture(unittest.TestCase):
    """Flat, synchronized MID bot with deterministic cached signals."""

    position = fixtures.MidExecutionTests.position
    request = fixtures.MidExecutionTests.request
    install = fixtures.MidExecutionTests.install

    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b = self.bot
        b.position = 0.0
        b.entry_price = 0.0
        b.pos_since = 0.0
        b.position_dirty = False
        b._inventory_basis_known = True
        b._inventory_mark_known = True
        b._position_recheck_due = False
        b._orders_recheck_due = False
        b._position_read_generation = 0
        b._position_verified_at = 0.0
        b._orders_verified_at = 0.0
        b._sig_z = 0.0
        b._sig_alpha = 0.0
        b._sig_vol = 0.0
        b._sig_touch = 0.0
        b._sig_micro = 0.0
        b._sig_micro_tilt = 0.0
        b._sig_micro_ts = 0.0
        b._sig_adj_bid = 0.0
        b._sig_adj_ask = 0.0
        b._sig_adj_ts = 0.0
        b._sig_mom_fast = 0.0
        b._sig_mom_slow = 0.0
        b._reg_now = (None, 0)
        b._early_now = (None, 0)
        b._macro = self.mod.MACRO_NEUTRAL
        b._qf_b = None
        b._qf_a = None
        b.markout_bps = {"B": 0.0, "A": 0.0}
        b.side_pause_until = {"B": 0.0, "A": 0.0}
        b.last_side_fill_ts = {"B": 0.0, "A": 0.0}
        b.best_bid, b.best_ask, b.mid = 100.0, 100.1, 100.05
        b.book_received_ns = time.perf_counter_ns()
        b.book_revision = 1
        b._urgent_quote = False
        b.bank_until = b.pause_until = b.hourly_pause_until = 0.0
        b._hard_exit_reason = ""
        b._hard_exit_at = 0.0
        b._last_regime = None
        b.size_mult = 1.0
        b.protect = 0
        b.ws_ready = True

    def set_signals(self, **values):
        for name, value in values.items():
            setattr(self.bot, name, value)


class AdaptiveQuoteTests(AdaptiveFixture):
    def test_strong_positive_z_pulls_the_ask_and_keeps_the_bid(self):
        self.set_signals(_sig_z=self.mod.ZSCORE_PULL + 0.1)
        bid, ask, tag = self.bot.compute_mid_intent()
        self.assertIsNotNone(bid)
        self.assertIsNone(ask)
        self.assertIn("ask z+", tag)

    def test_strong_negative_z_pulls_the_bid(self):
        self.set_signals(_sig_z=-(self.mod.ZSCORE_PULL + 0.1))
        bid, ask, _ = self.bot.compute_mid_intent()
        self.assertIsNone(bid)
        self.assertIsNotNone(ask)

    def test_negative_markout_removes_the_side_that_keeps_getting_run_over(self):
        # Derived hard threshold at this 10 bps book is well below -1 bps.
        self.set_signals(markout_bps={"B": -6.0, "A": 0.0})
        bid, ask, tag = self.bot.compute_mid_intent()
        self.assertIsNone(bid)
        self.assertIsNotNone(ask)
        self.assertIn("moB", tag)

    def test_short_term_momentum_removes_the_dangerous_opening_side(self):
        self.set_signals(_sig_mom_fast=-self.mod.MOM_GATE_BPS - 0.1,
                         _sig_mom_slow=-self.mod.MOM_GATE_BPS - 0.1)
        bid, ask, _ = self.bot.compute_mid_intent()
        self.assertIsNone(bid)
        self.assertIsNotNone(ask)

    def test_burst_pause_removes_only_the_paused_opening_side(self):
        self.set_signals(side_pause_until={"B": time.time() + 5, "A": 0.0})
        bid, ask, _ = self.bot.compute_mid_intent()
        self.assertIsNone(bid)
        self.assertIsNotNone(ask)

    def test_quotes_never_improve_or_cross_the_bbotouch(self):
        # A micro-spread forces the EV gate to push both sides behind the touch.
        with patch.object(self.mod, "PRICE_DECIMALS", 4), \
                patch.object(self.mod, "MID_OFFSET_BPS", 0.0), \
                patch.object(self.mod, "SPREAD_CAPTURE_FRAC", 0.0):
            self.bot.best_bid, self.bot.best_ask = 100.0, 100.0001
            self.bot.mid = (self.bot.best_bid + self.bot.best_ask) / 2
            bid, ask, _ = self.bot.compute_mid_intent()
            self.assertIsNotNone(bid)
            self.assertIsNotNone(ask)
            tick = self.mod.dyn_tick(self.bot.mid)
            self.assertLessEqual(bid[0], self.bot.best_bid)
            self.assertGreaterEqual(ask[0], self.bot.best_ask)
            self.assertLess(bid[0], self.bot.best_bid)
            self.assertGreater(ask[0], self.bot.best_ask)
            self.assertAlmostEqual(bid[0] / tick, round(bid[0] / tick), places=6)
            self.assertAlmostEqual(ask[0] / tick, round(ask[0] / tick), places=6)

    def test_fee_floor_is_never_below_the_configured_offset(self):
        with patch.object(self.mod, "PRICE_DECIMALS", 4), \
                patch.object(self.mod, "MID_OFFSET_BPS", 0.0), \
                patch.object(self.mod, "SPREAD_CAPTURE_FRAC", 0.0):
            self.bot.best_bid, self.bot.best_ask = 100.0, 100.0001
            self.bot.mid = 100.00005
            target = self.bot._mid_targets({
                "bb": 100.0, "ba": 100.0001, "mid": 100.00005,
                "tick": self.mod.dyn_tick(100.00005), "pos": 0.0,
                "bid_ro": False, "ask_ro": False,
                "bid_ok": True, "ask_ok": True,
                "bid_size": 1.0, "ask_size": 1.0}, time.time())
            self.assertLess(target["bid_px"], 100.0)
            self.assertGreater(target["ask_px"], 100.0001)


class ExitPolicyTests(AdaptiveFixture):
    def test_long_exit_joins_the_ask_after_the_profit_anchor(self):
        b = self.bot
        b.position, b.entry_price, b.pos_since = 1.0, 99.0, time.time() - 61.0
        bid, ask, tag = b.compute_mid_intent()
        tick = self.mod.dyn_tick(b.mid)
        self.assertIsNone(bid)
        self.assertTrue(ask[2])
        self.assertEqual(ask[0], self.mod.px_up(b.best_ask, tick))
        self.assertIn("exit", tag)

    def test_bank_window_moves_a_long_exit_inside_the_spread(self):
        b = self.bot
        b.position, b.entry_price, b.pos_since = 1.0, 99.0, time.time() - 5.0
        b.bank_until = time.time() + 10
        b.best_bid, b.best_ask, b.mid = 100.0, 100.5, 100.25
        bid, ask, _ = b.compute_mid_intent()
        tick = self.mod.dyn_tick(b.mid)
        self.assertIsNone(bid)
        self.assertEqual(ask[0], self.mod.px_up(b.best_bid + tick, tick))
        self.assertLess(ask[0], b.best_ask)

    def test_bank_window_moves_a_short_exit_inside_the_spread(self):
        b = self.bot
        b.position, b.entry_price, b.pos_since = -1.0, 101.0, time.time() - 5.0
        b.bank_until = time.time() + 10
        b.best_bid, b.best_ask, b.mid = 100.0, 100.5, 100.25
        bid, ask, _ = b.compute_mid_intent()
        tick = self.mod.dyn_tick(b.mid)
        self.assertIsNone(ask)
        self.assertEqual(bid[0], self.mod.px_down(b.best_ask - tick, tick))
        self.assertGreater(bid[0], b.best_bid)

    def test_exit_hysteresis_keeps_favourable_moves_and_small_young_drifts(self):
        b = self.bot
        b.mid = 5000.0
        now = time.time()
        old = {"side": "A", "px": 5000.0, "sz": 1.0, "ro": True, "ts": now - 0.5}
        # Target moved up: our resting ask is now the best price -> keep.
        self.assertTrue(b._keep_mid_exit_locked("A", old, (5000.1, 1.0, True), 0.1, False, now))
        # Adverse move is small and the order is young -> keep its queue slot.
        self.assertTrue(b._keep_mid_exit_locked("A", old, (4999.9, 1.0, True), 0.1, False, now))
        old["ts"] = now - 2.0
        # A favourable move is still kept when the order is old.
        self.assertTrue(b._keep_mid_exit_locked("A", old, (5000.1, 1.0, True), 0.1, False, now))
        # Old order + adverse drift -> reprice; urgency always reprices.
        self.assertFalse(b._keep_mid_exit_locked("A", old, (4999.9, 1.0, True), 0.1, False, now))
        self.assertFalse(b._keep_mid_exit_locked("A", old, (5000.1, 1.0, True), 0.1, True, now))


class LatencyPathTests(AdaptiveFixture):
    def test_fresh_verified_state_skips_synchronous_rest_before_an_exit(self):
        b = self.bot
        b.position = 1.0
        b.entry_price = 100.0
        b.pos_since = time.time() - 61.0
        b._position_verified_at = time.monotonic()
        b._orders_verified_at = time.monotonic()
        b.orders = {99: {"side": "A", "px": 100.0, "sz": 1.0, "ro": True,
                         "ts": time.time()}}
        b.safety.owned.add(99)
        b._urgent_quote = True
        b.apply_intent()
        self.raw.bulk_modify_orders_new.assert_called_once()
        b.info.user_state.assert_not_called()
        b.info.open_orders.assert_not_called()

    def test_exit_modify_is_not_blocked_by_a_pending_fill_reconciliation(self):
        b = self.bot
        b.position = 1.0
        b.entry_price = 100.0
        b.pos_since = time.time() - 61.0
        b.position_dirty = True          # a fill was just applied locally
        b._inventory_basis_known = True
        b._inventory_mark_known = True
        b._position_verified_at = time.monotonic()
        b._orders_verified_at = time.monotonic()
        b.orders = {99: {"side": "A", "px": 100.0, "sz": 1.0, "ro": True,
                         "ts": time.time()}}
        b.safety.owned.add(99)
        b._urgent_quote = True
        b.apply_intent()
        self.raw.bulk_modify_orders_new.assert_called_once()
        b.info.user_state.assert_not_called()
        b.info.open_orders.assert_not_called()

    def test_opening_stays_blocked_until_the_background_recheck(self):
        b = self.bot
        b.position = 0.0
        b.position_dirty = True
        b._inventory_basis_known = True
        b._inventory_mark_known = True
        bid, ask, _ = b.compute_mid_intent()
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_stale_state_keeps_the_authoritative_rest_preflight(self):
        b = self.bot
        b.position = 1.0
        b.entry_price = 100.0
        b.pos_since = time.time() - 61.0
        b._position_verified_at = 0.0
        b._orders_verified_at = 0.0
        b.orders = {99: {"side": "A", "px": 100.0, "sz": 1.0, "ro": True,
                         "ts": time.time()}}
        b.safety.owned.add(99)
        b._urgent_quote = True
        b.apply_intent()
        b.info.user_state.assert_called()
        b.info.open_orders.assert_called_once()

    def test_mid_post_only_backoff_is_sub_second(self):
        now = time.time()
        self.bot._defer_quote_requests([{"is_buy": True}], "book moved")
        self.assertLessEqual(self.bot.quote_retry_after["B"] - now, 0.2)

    def test_fill_schedules_an_immediate_maintenance_recheck(self):
        b = self.bot
        worker = Mock()
        worker.state.return_value = {"alive": True}
        worker.poll.return_value = None
        worker.request.return_value = True
        b._maintenance = worker
        now = time.monotonic()
        b._maintenance_position_request_at = now
        b._maintenance_orders_request_at = now
        b._position_recheck_due = True
        b.last_orderupdate_ts = b.last_quote_ts = time.time()
        b._maintenance_tick()
        request = worker.request.call_args.args[0]
        self.assertTrue(request.position)
        self.assertFalse(b._position_recheck_due)


class SnapshotAccountingTests(unittest.TestCase):
    position = fixtures.MidExecutionTests.position
    request = fixtures.MidExecutionTests.request
    install = fixtures.MidExecutionTests.install

    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b = self.bot
        b.start_time = time.time() - 100.0
        b.position = b.entry_price = 0.0
        b.position_dirty = False
        b._inventory_basis_known = True
        b._inventory_mark_known = True
        b._risk_basis_revision = b.position_revision
        b.safety.account_known = True
        b._scratch_ts = 0.0

    @staticmethod
    def fill(tid, when_ms):
        return {"coin": "io:ANTH", "oid": 5, "tid": tid, "side": "B",
                "px": "100", "sz": ".1", "fee": ".01", "closedPnl": "0",
                "crossed": False, "time": when_ms, "startPosition": "0"}

    def test_snapshot_counts_a_fill_from_the_current_session(self):
        b = self.bot
        b._on_user_fills({"data": {"isSnapshot": True, "fills": [
            self.fill(1, int(time.time() * 1000))]}})
        self.assertAlmostEqual(b.position, .1)
        self.assertEqual(b.fills_count, 1)

    def test_snapshot_only_deduplicates_a_previous_session_fill(self):
        b = self.bot
        b._on_user_fills({"data": {"isSnapshot": True, "fills": [
            self.fill(2, int((b.start_time - 50) * 1000))]}})
        self.assertEqual(b.position, 0)
        self.assertEqual(b.fills_count, 0)
        self.assertIn("2", b.processed_fills)


class SafetyUpgradeTests(AdaptiveFixture):
    def test_fill_removes_the_consumed_order_from_tracking(self):
        b = self.bot
        b.position = 0.0
        b.entry_price = 0.0
        b.orders = {5: {"side": "B", "px": 100.0, "sz": 1.0, "ro": False,
                        "ts": time.time()}}
        b.safety.owned.add(5)
        b._scratch_ts = 0.0
        b._on_user_fills({"data": {"fills": [{
            "coin": "io:ANTH", "oid": 5, "tid": 1, "side": "B", "px": "100",
            "sz": ".4", "fee": ".01", "closedPnl": "0", "crossed": False,
            "startPosition": "0"}]}})
        self.assertAlmostEqual(b.orders[5]["sz"], .6)
        b._on_user_fills({"data": {"fills": [{
            "coin": "io:ANTH", "oid": 5, "tid": 2, "side": "B", "px": "100",
            "sz": ".6", "fee": ".01", "closedPnl": "0", "crossed": False,
            "startPosition": ".4"}]}})
        self.assertNotIn(5, b.orders)

    def test_only_entry_fills_feed_the_markout_ewma(self):
        b = self.bot
        b.position = 0.0
        b.entry_price = 0.0
        b.pending_markouts = deque()
        b._scratch_ts = 0.0
        b._on_user_fills({"data": {"fills": [{
            "coin": "io:ANTH", "oid": 5, "tid": 1, "side": "B", "px": "100",
            "sz": ".4", "fee": ".01", "closedPnl": "0", "crossed": False,
            "startPosition": "0"}]}})
        self.assertEqual(len(b.pending_markouts), 1)
        b._on_user_fills({"data": {"fills": [{
            "coin": "io:ANTH", "oid": 6, "tid": 2, "side": "A", "px": "100.1",
            "sz": ".4", "fee": ".01", "closedPnl": ".01", "crossed": False,
            "startPosition": ".4"}]}})
        self.assertEqual(len(b.pending_markouts), 1)

    def test_exit_is_not_cancelled_when_the_book_is_unavailable(self):
        b = self.bot
        b.position = 1.0
        b.entry_price = 100.0
        b.best_bid = b.best_ask = 0.0
        b.orders = {99: {"side": "A", "px": 100.1, "sz": 1.0, "ro": True,
                         "ts": time.time()}}
        b.safety.owned.add(99)
        b.apply_intent()
        self.raw.bulk_cancel.assert_not_called()
        self.assertIn(99, b.orders)

    def test_automatic_cleanup_keeps_the_closing_order_resting(self):
        b = self.bot
        b.safety.manual_stop_requested = False
        b._cleanup_snapshot = Mock(return_value=[("io:ANTH", 1.0, [
            {"oid": 1, "side": "A", "sz": "1"},
            {"oid": 2, "side": "B", "sz": "1"}])])
        rows = b._cleanup_snapshot_keep_exits()
        self.assertEqual([order["oid"] for order in rows[0][2]], [2])

    def test_post_only_rejection_does_not_dirty_inventory(self):
        b = self.bot
        b.position_dirty = False
        b.position_revision = 4
        b._reconcile_post_only([{"is_buy": True}], "bulk_orders")
        self.assertFalse(b.position_dirty)
        self.assertEqual(b.position_revision, 4)
        self.assertTrue(b._orders_recheck_due)

    def test_levels_without_own_subtract_our_resting_size(self):
        b = self.bot
        b.orders = {1: {"side": "B", "px": 100.0, "sz": 2.0, "ro": False}}
        bids, asks = b._levels_without_own(
            [{"px": "100.0", "sz": "5"}, {"px": "99.9", "sz": "1"}],
            [{"px": "100.1", "sz": "3"}])
        self.assertEqual(bids, [{"px": "100.0", "sz": "3.0"},
                                {"px": "99.9", "sz": "1.0"}])
        self.assertEqual(asks, [{"px": "100.1", "sz": "3.0"}])
        b.orders = {1: {"side": "B", "px": 100.0, "sz": 5.0, "ro": False}}
        bids, _ = b._levels_without_own(
            [{"px": "100.0", "sz": "5"}, {"px": "99.9", "sz": "1"}],
            [{"px": "100.1", "sz": "3"}])
        self.assertEqual(bids, [{"px": "99.9", "sz": "1.0"}])

    def test_stale_mark_still_arms_the_stop(self):
        b = self.bot
        b.position, b.entry_price = 1.0, 100.0
        b.mid = 99.05
        b.best_bid = b.best_ask = 0.0
        b._inventory_mark_known = False
        b._last_mark_upnl = -1.0
        b._last_mark_bps = -100.9
        b._last_mark_at = time.time() - 1.0
        b._sig_vol = 0.0
        b._sig_z = 0.0
        b._reg_now = (None, 0)
        b._early_now = (None, 0)
        b.session_pnl = 0.0
        b.total_fees = 0.0
        b.session_net_high = b.upnl_peak_bps = 0.0
        b.hourly_net_events = deque()
        b._hard_exit_at = 0.0
        b._hard_exit_reason = ""
        b.pause_until = b.hourly_pause_until = b.bank_until = 0.0
        b._check_feed_health = Mock()
        b.risk_checks()
        self.assertGreater(b.bank_until, time.time())
        self.assertIn("runaway", b._hard_exit_reason)

    def test_session_loss_winds_down_exits_only_before_stopping(self):
        b = self.bot
        b.position, b.entry_price = 1.0, 100.0
        b.best_bid, b.best_ask, b.mid = 99.0, 99.1, 99.05
        b.session_pnl, b.total_fees = -6.0, 0.0
        b._sig_vol = b._sig_z = 0.0
        b._reg_now = b._early_now = (None, 0)
        b.session_net_high = b.upnl_peak_bps = 0.0
        b.hourly_net_events = deque()
        b._hard_exit_at = 0.0
        b._hard_exit_reason = ""
        b.pause_until = b.hourly_pause_until = b.bank_until = 0.0
        b._check_feed_health = Mock()
        b.risk_checks()
        self.assertTrue(b.running)
        self.assertTrue(b._session_halt)
        self.assertGreater(b.bank_until, time.time())
        b.position = 0.0
        b.risk_checks()
        self.assertFalse(b.running)

    def test_discarded_position_read_rearms_the_recheck(self):
        b = self.bot
        request = self.mod_maintenance().MaintenanceRequest(
            b.safety.session_id, getattr(b, "_execution_revision", 0), False,
            position=True, orders=False, position_revision=b.position_revision,
            position_generation=b._position_read_generation)
        b._position_recheck_due = False
        b._position_read_generation += 1   # a foreground sync won the race
        result = self.mod_maintenance().MaintenanceResult(
            request, position=self.mod_maintenance().PositionRead(1, 100, 0, 100),
            position_started=time.monotonic())
        self.assertFalse(b._apply_maintenance_result(result))
        self.assertTrue(b._position_recheck_due)

    @staticmethod
    def mod_maintenance():
        from zeus import mid_maintenance
        return mid_maintenance


class FinalAccountingPagingTests(unittest.TestCase):
    position = fixtures.MidExecutionTests.position
    request = fixtures.MidExecutionTests.request
    install = fixtures.MidExecutionTests.install

    def setUp(self):
        fixtures.FillAccountingTests.setUp(self)
        b = self.bot
        b.start_time = time.time() - 100
        b.safety.exposure_started = True
        b.safety.owned = {1}
        b._expected_cleanup_fills = {}
        b._received_fills = {}
        b.safety.pending_cloids = set()

    def test_more_than_2000_fills_are_paged_and_counted(self):
        def fill(tid, stamp):
            return {"coin": "io:ANTH", "oid": 1, "tid": tid, "px": "100",
                    "sz": "0.1", "fee": "0.01", "closedPnl": "0",
                    "crossed": False, "time": stamp}
        first = [fill(i, 1000 + i) for i in range(2000)]
        second = [fill(2000, 5000)]

        class API:
            def __init__(self, *args, **kwargs):
                self.session = Mock()
                self.calls = 0

            def post(self, path, payload):
                self.calls += 1
                return first if self.calls == 1 else second

        api = API()
        with patch("hyperliquid.api.API", return_value=api):
            self.bot._final_accounting(10)
        self.assertTrue(self.bot.accounting_complete)
        self.assertEqual(self.bot.fills_count, 2001)
        self.assertAlmostEqual(self.bot.total_volume, 20010.0)


class SignalMemoryTests(unittest.TestCase):
    def test_trade_history_is_bounded_to_the_flow_window(self):
        engine = SignalEngine()
        now = time.time()
        engine.on_trade("B", 100.0, 1.0, now - 60.0)
        engine.on_trade("B", 100.0, 1.0, now)
        self.assertTrue(engine.trades)
        self.assertGreaterEqual(engine.trades[0][0], now - 10.0)


if __name__ == "__main__":
    unittest.main()
