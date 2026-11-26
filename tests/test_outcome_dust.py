"""Dust, partial-bid and rotation-inventory regressions. No exchange I/O."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.worldcup import EXCHANGE_MIN_ORDER_USD, MarketSpec, MarketState
from zeus.outcome_rotation import OutcomeRotation


class DustAndPartialBidTests(unittest.TestCase):
    def state(self, position, bid=.50, ask=.5001):
        st = MarketState(MarketSpec('#101', 0, balance_coin='+101'),
                         position=position, available_position=position)
        st.update_bbo(bid, ask)
        return st

    def test_partial_bid_down_to_the_venue_minimum_keeps_volume(self):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.available_usdc = 31.86
        st = self.state(40)
        plan = bot.bilateral_requests(st)
        self.assertIn(True, plan)
        value = plan[True]['sz'] * plan[True]['limit_px']
        self.assertGreaterEqual(value, EXCHANGE_MIN_ORDER_USD)
        self.assertLessEqual(value, 31.86 + 1e-9)
        self.assertEqual(plan[True]['order_type']['limit']['tif'], 'Alo')

    def test_draining_sale_below_the_venue_minimum_is_not_submitted(self):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.rotation_requested = lambda: True
        st = self.state(10)  # about $5
        self.assertNotIn(False, bot.bilateral_requests(st))

    def test_draining_sale_above_the_venue_minimum_sells_the_lot(self):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.rotation_requested = lambda: True
        st = self.state(40)  # about $20
        plan = bot.bilateral_requests(st)
        self.assertEqual(plan[False]['sz'], 40)
        self.assertEqual(plan[False]['limit_px'], st.ask)

    def test_residual_dust_is_bounded_by_notional_and_unknown_price(self):
        bot = make_bot()
        self.assertIsNotNone(bot.residual_dust(self.state(5)))
        self.assertIsNone(bot.residual_dust(self.state(40)))
        # 10 whole tokens at the maximum outcome price are still < $10.
        high = self.state(10, bid=.9999, ask=.99999)
        self.assertIsNotNone(bot.residual_dust(high))
        # Unknown book: only a conservative whole-lot bound is written off.
        unknown = MarketState(MarketSpec('#101', 0, balance_coin='+101'), position=5)
        self.assertIsNotNone(bot.residual_dust(unknown))
        unknown.position = 20
        self.assertIsNone(bot.residual_dust(unknown))

    def test_cleanup_suppresses_and_journals_only_unsellable_dust(self):
        bot = make_bot()
        bot.residual_dusts = {}
        bot.safety = SimpleNamespace(metadata={}, publish=Mock())
        st = self.state(5)
        bot.states = {'#101': st}
        rows = bot._suppress_dust([('#101', 5.0, [])])
        self.assertEqual(rows, [('#101', 0.0, [])])
        self.assertEqual(bot.safety.metadata['residual_dust']['#101']['qty'], 5)
        st.position = 40
        rows = bot._suppress_dust([('#101', 40.0, [])])
        self.assertEqual(rows, [('#101', 40.0, [])])


class RotationInventoryDustTests(unittest.TestCase):
    def test_only_unsellable_dust_allows_rotation(self):
        base = dict(position_known=True, position_usd=2.0, position_qty=5,
                    unsellable_qty=5, book_price_known=True)
        self.assertEqual(OutcomeRotation.rotation_inventory_reason(base), '')
        sellable = dict(base, position_usd=20.0, position_qty=40, unsellable_qty=0)
        self.assertIn('différée', OutcomeRotation.rotation_inventory_reason(sellable))

    def test_missing_mark_defers_instead_of_rotating_with_inventory(self):
        current = dict(position_known=True, position_usd=0.0,
                       position_qty=40, unsellable_qty=0, book_price_known=False)
        self.assertIn('prix', OutcomeRotation.rotation_inventory_reason(current))

    def test_inventory_reason_ages_after_five_minutes(self):
        group = {}
        now = 1000.0
        first = OutcomeRotation._inventory_message(
            group, "Rotation différée: vente de l'inventaire en cours", now)
        self.assertNotIn('min', first)
        later = OutcomeRotation._inventory_message(
            group, "Rotation différée: vente de l'inventaire en cours", now + 360)
        self.assertIn('6 min', later)


class DustRotationCompletionTests(unittest.TestCase):
    def test_dust_only_inventory_can_finish_the_rotation(self):
        bot = make_bot()
        bot.rotation_requested = lambda: True
        bot.running = True
        bot.safety = SimpleNamespace(lock=threading.RLock(), phase='running')
        bot.states = {'#101': DustAndPartialBidTests().state(5)}
        bot.sync_account = Mock(return_value=True)
        bot._cleanup_snapshot = Mock(return_value=[('#101', 0.0, [])])
        bot.request_stop = Mock()
        bot.finish_rotation_if_flat()
        bot.request_stop.assert_called_once()
        self.assertIn('dust', bot.status)


class FlatVerificationDustTests(unittest.TestCase):
    def test_only_journaled_unsellable_dust_passes_flat_verification(self):
        from zeus.server import BotManager
        manager = SimpleNamespace(account_context=None)
        record = {'account_kind': 'outcome', 'account': '0xabc', 'symbols': ['#101'],
                  'pending_cloids': [], 'residual_dust': {
                      '#101': {'qty': 5, 'notional': 2.0}}}
        with patch.dict('os.environ', {'WALLET_ADDRESS_OUTCOME': '0xabc'}), \
                patch('zeus.server.account_snapshot', return_value=[('#101', 5.0, [])]), \
                patch('hyperliquid.info.Info'):
            BotManager._verify_session_flat(manager, record)
        with patch.dict('os.environ', {'WALLET_ADDRESS_OUTCOME': '0xabc'}), \
                patch('zeus.server.account_snapshot', return_value=[('#101', 40.0, [])]), \
                patch('hyperliquid.info.Info'):
            with self.assertRaises(RuntimeError):
                BotManager._verify_session_flat(manager, record)


if __name__ == '__main__':
    unittest.main()
