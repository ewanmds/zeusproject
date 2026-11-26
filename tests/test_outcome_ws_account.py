"""WS-first account state, 429 backoff and shared snapshot regressions."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_outcome_maker_exit import make_bot
from zeus.bots.worldcup import MarketSpec, MarketState, RateLimited, read_info
from zeus.network import retry_read


SPOT = {"data": {"user": "0xabc", "spotState": {"balances": [
    {"coin": "USDC", "total": "100", "hold": "70"},
    {"coin": "+101", "total": "40", "hold": "40", "entryNtl": "20"},
]}}}
ORDERS = {"data": {"user": "0xabc", "dex": "", "orders": [
    {"coin": "#101", "side": "A", "limitPx": "0.50", "sz": "40", "oid": 7},
]}}


class WsAccountTests(unittest.TestCase):
    def bot(self):
        bot = make_bot(order_size_usd=70, max_position_usd=120)
        bot.states = {'#101': MarketState(MarketSpec('#101', 0, balance_coin='+101'))}
        bot.lock = threading.RLock()
        bot.account_settle_until = 0.0
        bot.ws_spot_state = None
        bot.ws_open_orders = None
        bot.ws_spot_at = 0.0
        bot.ws_orders_at = 0.0
        bot.ws_account_ready = False
        bot.safety = SimpleNamespace(account_known=False)
        bot.available_usdc = 0.0
        return bot

    def test_spot_and_orders_snapshots_replace_account_polling(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        self.assertFalse(bot.account_ready())
        bot.on_open_orders(ORDERS)
        state = bot.states['#101']
        self.assertEqual(state.position, 40)
        self.assertEqual(state.available_position, 0)
        self.assertEqual(bot.available_usdc, 30)
        self.assertTrue(bot.account_ready())
        self.assertEqual(state.ask_oid, 7)
        self.assertAlmostEqual(state.quoted_ask, .50)
        self.assertAlmostEqual(state.ask_quantity, 40)
        self.assertFalse(state.reconcile_required)
        self.assertTrue(bot.safety.account_known)

    def test_fill_settle_window_keeps_reconcile_until_next_snapshot(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        bot.account_settle_until = time.time() + 2
        bot.states['#101'].reconcile_required = True
        bot.on_open_orders(ORDERS)
        self.assertTrue(bot.states['#101'].reconcile_required)
        bot.account_settle_until = 0
        with patch('zeus.bots.worldcup.time.time', return_value=time.time() + 3):
            bot.on_open_orders(ORDERS)
        self.assertFalse(bot.states['#101'].reconcile_required)

    def test_invalid_snapshot_does_not_mutate_or_claim_ready(self):
        bot = self.bot()
        bad = {"data": {"spotState": {"balances": [
            {"coin": "+101", "total": "10", "hold": "20"}]}}}
        bot.on_spot_state(bad)
        self.assertFalse(bot.ws_account_ready)
        self.assertEqual(bot.states['#101'].position, 0)

    def test_ready_state_expires(self):
        bot = self.bot()
        bot.on_spot_state(SPOT)
        bot.on_open_orders(ORDERS)
        self.assertTrue(bot.account_ready(max_age=10))
        with patch('zeus.bots.worldcup.time.time', return_value=time.time() + 20):
            self.assertFalse(bot.account_ready(max_age=10))


class InfoBackoffTests(unittest.TestCase):
    def tearDown(self):
        import zeus.bots.worldcup as wc
        with wc._INFO_BACKOFF_LOCK:
            wc._INFO_BACKOFF_UNTIL = 0.0
            wc._INFO_BACKOFF_SEC = 0.0

    def test_read_info_honours_429_retry_after(self):
        response = Mock(status_code=429, headers={'Retry-After': '7'})
        session = Mock()
        session.post.return_value = response
        with patch('zeus.bots.worldcup._local', Mock(session=session)):
            with self.assertRaises(RateLimited) as raised:
                read_info('https://api.hyperliquid.xyz', {'type': 'l2Book'})
        self.assertGreaterEqual(raised.exception.retry_after, 7)
        session.post.assert_called_once()

    def test_retry_read_sleeps_for_retry_after(self):
        class Limited(Exception):
            retryable = True
            retry_after = 4.0

        calls = {'n': 0}
        sleeps = []
        def operation():
            calls['n'] += 1
            if calls['n'] < 3:
                raise Limited()
            return 'ok'
        result = retry_read('startup', operation, attempts=3, sleep=sleeps.append)
        self.assertEqual(result, 'ok')
        self.assertEqual(calls['n'], 3)
        self.assertGreaterEqual(min(sleeps), 4.0)


if __name__ == '__main__':
    unittest.main()
