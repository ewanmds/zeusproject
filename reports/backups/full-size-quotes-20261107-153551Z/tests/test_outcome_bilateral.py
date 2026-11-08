"""Two simultaneous queues, funding/cap guards and passive rotation drains."""
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock

from zeus.bots.outcome_worker import build_config
from zeus.bots.worldcup import HyperliquidSpreadBot, MarketSpec, MarketState, SessionStats
from zeus.safety import SafetyLatch


class BilateralTests(unittest.TestCase):
    def bot(self, position=20):
        bot = HyperliquidSpreadBot.__new__(HyperliquidSpreadBot)
        bot.cfg = build_config(dict(market="#101", outcome_side="no", exec_mode="maker",
             two_sided=True, order_size_usd=10, max_position_usd=20, maker_spread_pct=None), None)
        bot.running = True
        bot.lock = threading.RLock()
        bot.wake = threading.Event()
        bot.safety = SafetyLatch(lambda: setattr(bot, "running", False))
        bot.safety.phase = "running"
        bot.safety.account_known = True
        bot.stats = SessionStats()
        bot.account_revision = 0
        bot.available_usdc = 100
        bot.funds = None
        bot.exchange = Mock()
        bot.exchange.order.side_effect = [
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 111}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 222}}]}}}]
        bot.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
        bot.sync_account = Mock(return_value=True)
        bot.can_send_action = Mock(return_value=True)
        st = MarketState(MarketSpec("#101", 0, balance_coin="+101"), position=position,
                         available_position=position)
        st.update_bbo(.4, .401)
        bot.states = {"#101": st}
        return bot, st

    def test_inventory_backed_ask_and_bid_coexist_without_cancelling_each_other(self):
        bot, st = self.bot()
        bot.place_or_requote(st)
        self.assertEqual(st.ask_oid, 111)
        st.pending_until = 0
        bot.place_or_requote(st)
        self.assertEqual((st.bid_oid, st.ask_oid), (222, 111))
        self.assertEqual([c.kwargs["is_buy"] for c in bot.exchange.order.call_args_list], [False, True])
        self.assertTrue(all(c.kwargs["order_type"] == {"limit": {"tif": "Alo"}}
                            for c in bot.exchange.order.call_args_list))
        bot.exchange.bulk_cancel.assert_not_called()
        self.assertLessEqual(st.position*st.mid+st.bid_quantity*st.quoted_bid, bot.cfg.max_position_usd)

    def test_flat_bootstrap_does_not_sell_tokens_it_does_not_own(self):
        bot, st = self.bot(position=0)
        self.assertEqual(set(bot.bilateral_requests(st)), {True})
        bot.place_or_requote(st)
        self.assertTrue(bot.exchange.order.call_args.kwargs["is_buy"])

    def test_full_ask_respects_minimum_notional_when_enough_tokens_exist(self):
        bot, st = self.bot(position=50)
        req = bot.bilateral_requests(st)[False]
        self.assertGreaterEqual(req["sz"]*req["limit_px"], bot.cfg.min_order_usd)
        self.assertLessEqual(req["sz"], st.available_position)

    def test_cap_and_funding_remove_bid_but_never_block_inventory_ask(self):
        bot, st = self.bot(position=50)
        self.assertEqual(set(bot.bilateral_requests(st)), {False})
        bot, st = self.bot()
        bot.available_usdc = 0
        self.assertEqual(set(bot.bilateral_requests(st)), {False})
        self.assertLessEqual(bot.bilateral_requests(st)[False]["sz"], st.available_position)

    def test_rotation_keeps_original_loss_budget_instead_of_resetting_risk(self):
        bot, st = self.bot()
        bot.cfg.risk_pnl_carry = -49
        bot.stats.gross_closed_pnl = -2
        self.assertIn("stop loss", bot.risk_block_reason(st))

    def test_own_holds_are_counted_once_and_partial_queues_are_preserved(self):
        bot, st = self.bot()
        st.bid_oid, st.ask_oid = 1, 2
        st.quoted_bid, st.quoted_ask = .4, .401
        st.bid_quantity, st.ask_quantity = 20, 10
        st.available_position = 10  # Other 10 tokens held by our ask.
        bot.available_usdc = 2
        plan = bot.bilateral_requests(st)
        self.assertLessEqual(plan[False]["sz"], 20)
        st.pending_until = 0
        bot.place_or_requote(st)
        bot.exchange.order.assert_not_called()
        bot.exchange.bulk_cancel.assert_not_called()

    def test_changed_fresh_balance_is_revalidated_before_signing(self):
        bot, st = self.bot(position=0)
        def sync():
            st.position = st.available_position = 50
            return True
        bot.sync_account.side_effect = sync
        bot.place_or_requote(st)
        bot.exchange.order.assert_not_called()

    def test_unknown_ack_prevents_opposite_submission_until_reconciled(self):
        bot, st = self.bot()
        bot.exchange.order.side_effect = None
        bot.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": []}}}
        bot.place_or_requote(st)
        self.assertTrue(st.reconcile_required)
        st.pending_until = 0
        bot.place_or_requote(st)
        self.assertEqual(bot.exchange.order.call_count, 1)

    def test_drain_cancels_only_bid_and_keeps_inventory_exit_queue(self):
        bot, st = self.bot()
        st.bid_oid, st.ask_oid = 1, 2
        st.quoted_bid, st.quoted_ask = .4, .401
        st.bid_quantity, st.ask_quantity = 25, 20
        st.available_position = 0
        with tempfile.TemporaryDirectory() as folder:
            bot.cfg.rotation_file = str(Path(folder)/"drain.json")
            Path(bot.cfg.rotation_file).touch()
            bot.place_or_requote(st)
        self.assertIsNone(st.bid_oid)
        self.assertEqual(st.ask_oid, 2)
        bot.exchange.order.assert_not_called()
        self.assertEqual(bot.exchange.bulk_cancel.call_args.args[0], [{"coin": "#101", "oid": 1}])

    def test_flat_drain_rechecks_exchange_and_never_liquidates(self):
        bot, st = self.bot(position=0)
        bot._cleanup_snapshot = Mock(return_value=[("#101", 0, [])])
        with tempfile.TemporaryDirectory() as folder:
            bot.cfg.rotation_file = str(Path(folder)/"drain.json")
            Path(bot.cfg.rotation_file).touch()
            bot.finish_rotation_if_flat()
            self.assertFalse(bot.running)
            bot._cleanup_snapshot.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, "automatic taker"):
                bot._cleanup_close("#101", 1)
        bot.exchange.order.assert_not_called()

    def test_drain_keeps_running_until_actual_inventory_is_zero(self):
        bot, st = self.bot(position=1)
        bot.rotation_requested = lambda: True
        bot._cleanup_snapshot = Mock()
        bot.finish_rotation_if_flat()
        self.assertTrue(bot.running)
        bot._cleanup_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
