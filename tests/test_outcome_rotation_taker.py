"""Bounded taker assist for a stalled automatic rotation. No exchange I/O."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import MarketSpec, MarketState, round_outcome_sell_px
from zeus.safety import SafetyLatch, SubmissionSkipped


class RotationTakerExitTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.drain = Path(self.folder.name) / "drain.json"
        self.rotation_intent()
        self.exchange = Mock()
        self.exchange.order = Mock(return_value={
            "status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": "40.0", "avgPx": "0.5", "oid": 99}}]}}})
        self.bot = make_bot()
        self.bot.cfg.rotation_file = str(self.drain)
        self.bot.running = True
        self.bot.safety = SafetyLatch(strict_maker_spot=True)
        self.bot.safety.phase = "running"
        self.bot.exchange = self.bot.safety.wrap(self.exchange)
        self.bot.sync_account = Mock(return_value=True)

    def rotation_intent(self):
        self.drain.write_text(json.dumps({
            "requested_at": time.time(), "reason": "low executed $/min",
            "next_market": "#202"}), encoding="utf-8")

    def state(self, position=40, bid=.5, ask=.5001, drain_age=20.0):
        st = MarketState(MarketSpec("#101", 0, balance_coin="+101"),
                         position=position, available_position=position)
        st.update_bbo(bid, ask)
        st.drain_started_at = time.time() - drain_age
        return st

    def test_maker_exit_keeps_priority_before_the_delay(self):
        st = self.state(drain_age=5.0)
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.exchange.order.assert_not_called()

    def test_wide_spread_never_crosses(self):
        st = self.state(bid=.50, ask=.52, drain_age=20.0)
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.exchange.order.assert_not_called()
        self.assertIn("spread", st.last_action)

    def test_tight_spread_after_the_delay_sells_the_whole_inventory_at_the_bid(self):
        st = self.state()
        self.assertTrue(self.bot.maybe_rotation_taker_exit(st))
        order = self.exchange.order.call_args.kwargs
        self.assertFalse(order["is_buy"])
        self.assertEqual(order["sz"], 40.0)
        self.assertEqual(order["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertEqual(order["limit_px"], round_outcome_sell_px(.5 * (1 - 5 / 10_000)))
        self.assertTrue(self.bot.safety.rotation_taker_close_requested)
        self.assertEqual(st.position, 0)
        self.bot.sync_account.assert_called_once_with(force=True)

    def test_resting_quotes_are_pulled_before_the_ioc(self):
        st = self.state()
        st.ask_oid = 7
        self.bot.cancel_all = Mock()
        self.assertTrue(self.bot.maybe_rotation_taker_exit(st))
        self.bot.cancel_all.assert_called_once_with("rotation taker", ["#101"])
        self.exchange.order.assert_not_called()

    def test_session_target_exit_stays_maker_only(self):
        self.drain.write_text(json.dumps({
            "requested_at": time.time(), "reason": "Session duration reached",
            "session_finished": True}), encoding="utf-8")
        st = self.state()
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.exchange.order.assert_not_called()

    def test_unsellable_residual_is_never_sent(self):
        st = self.state(position=5)
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.exchange.order.assert_not_called()
        self.assertIn("minimum", st.last_action)

    def test_stale_book_blocks_a_taker_decision(self):
        st = self.state()
        st.book_received_at = time.time() - 10
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.exchange.order.assert_not_called()
        self.assertIn("fresh book", st.last_action)

    def test_flat_fill_is_reconciled_immediately(self):
        st = self.state(position=0)
        st.reconcile_required = True
        self.assertFalse(self.bot.maybe_rotation_taker_exit(st))
        self.bot.sync_account.assert_called_once_with(force=True)


class RotationTakerSafetyGateTests(unittest.TestCase):
    def test_ioc_sell_requires_both_the_rotation_flag_and_scope(self):
        gate = SafetyLatch(strict_maker_spot=True)
        gate.phase = "running"
        sell = dict(is_buy=False, order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=False)
        with self.assertRaises(SubmissionSkipped):
            gate.assert_allowed_action("order", kwargs=sell)
        gate.rotation_taker_close_requested = True
        with self.assertRaises(SubmissionSkipped):
            gate.assert_allowed_action("order", kwargs=sell)
        with gate.rotation_close_scope():
            gate.assert_allowed_action("order", kwargs=sell)
            for blocked in ({**sell, "is_buy": True},
                            {**sell, "order_type": {"limit": {"tif": "Gtc"}}}):
                with self.assertRaises(SubmissionSkipped):
                    gate.assert_allowed_action("order", kwargs=blocked)

    def test_ioc_miss_is_a_no_fill_not_a_paused_session(self):
        gate = SafetyLatch(strict_maker_spot=True)
        gate.rotation_taker_close_requested = True
        exchange = SimpleNamespace(order=Mock(return_value={
            "status": "ok", "response": {"data": {"statuses": [
                {"error": "Order could not immediately match against any resting orders. asset=1"}
            ]}}}))
        missed = []
        wrapped = gate.wrap(exchange, reconcile_ioc_miss=lambda reqs: missed.append(reqs))
        with gate.rotation_close_scope():
            wrapped.order(coin="#101", is_buy=False, sz=40, limit_px=.49975,
                          order_type={"limit": {"tif": "Ioc"}}, reduce_only=False)
        self.assertEqual(exchange.order.call_count, 1)
        self.assertEqual(len(missed), 1)
        self.assertFalse(gate.pending_cloids)


class RotationTakerConfigTests(unittest.TestCase):
    def test_defaults_and_bounded_overrides(self):
        cfg = build_config({"market": "#101", "order_size_usd": 15,
                            "max_position_usd": 200}, None)
        self.assertEqual(cfg.rotation_taker_after_sec, 12.0)
        self.assertEqual(cfg.rotation_taker_max_spread_bps, 10.0)
        self.assertEqual(cfg.rotation_taker_slippage_bps, 5.0)
        cfg = build_config({"market": "#101", "order_size_usd": 15,
                            "max_position_usd": 200,
                            "rotation_taker_after_sec": 1,
                            "rotation_taker_max_spread_bps": 500}, None)
        self.assertEqual(cfg.rotation_taker_after_sec, 3.0)
        self.assertEqual(cfg.rotation_taker_max_spread_bps, 100.0)
        with patch.dict(os.environ, {"OUTCOME_ROTATION_TAKER_AFTER_SEC": "8"}):
            cfg = build_config({"market": "#101", "order_size_usd": 15,
                                "max_position_usd": 200}, None)
        self.assertEqual(cfg.rotation_taker_after_sec, 8.0)


if __name__ == "__main__":
    unittest.main()
